from __future__ import annotations

import atexit
import os
import queue
import shlex
import signal
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from .models import AppConfig, JobDetail, JobInfo, JobLogs, JobMetadata, JupyterInfo, SubmissionRequest
from .parsing import (
    parse_job_detail,
    parse_jupyter_info,
    parse_sacct_job,
    parse_sbatch_submission,
    parse_squeue_jobs,
    rewrite_local_jupyter_url,
)
from .state import StateStore, utc_now


class SherlockError(RuntimeError):
    pass


class SessionDisconnectedError(SherlockError):
    pass


class PersistentSSHSession:
    def __init__(
        self,
        resource: str | None = None,
        *,
        command: list[str] | None = None,
        popen_factory=None,
    ):
        if command is None and resource is None:
            raise ValueError("resource or command is required")
        self.command = command or ["ssh", resource, "bash", "-l"]
        self.popen_factory = popen_factory or subprocess.Popen
        self._process = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        atexit.register(self.close)

    def run(self, command: str, *, check: bool = True) -> str:
        for attempt in range(2):
            try:
                return self._run_once(command, check=check)
            except SessionDisconnectedError:
                self.close()
                if attempt == 1:
                    raise
        raise SessionDisconnectedError("Persistent SSH session disconnected.")

    def write_text(self, remote_path: str, content: str) -> None:
        delimiter = f"__SHERLOCK_FILE_{uuid.uuid4().hex}__"
        while delimiter in content:
            delimiter = f"__SHERLOCK_FILE_{uuid.uuid4().hex}__"
        payload = f"cat > {shlex.quote(remote_path)} <<'{delimiter}'\n{content}"
        if not content.endswith("\n"):
            payload += "\n"
        payload += f"{delimiter}"
        self.run(payload)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.write("exit\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except Exception:
            pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _run_once(self, command: str, *, check: bool) -> str:
        with self._lock:
            process = self._ensure_started()
            self._drain_queue(self._stdout_queue)
            self._drain_queue(self._stderr_queue)

            token = uuid.uuid4().hex
            stdout_end = f"__SHERLOCK_STDOUT_END_{token}__"
            stderr_end = f"__SHERLOCK_STDERR_END_{token}__"
            exit_prefix = f"__SHERLOCK_EXIT_{token}__:"
            payload = (
                f"{command}\n"
                "__sherlock_status=$?\n"
                f"printf '%s%s\\n' {shlex.quote(exit_prefix)} \"$__sherlock_status\"\n"
                f"printf '%s\\n' {shlex.quote(stdout_end)}\n"
                f"printf '%s\\n' {shlex.quote(stderr_end)} >&2\n"
            )

            try:
                assert process.stdin is not None
                process.stdin.write(payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise SessionDisconnectedError("Could not write to persistent SSH session.") from exc

            stdout, returncode = self._collect_stdout(stdout_end, exit_prefix)
            stderr = self._collect_stderr(stderr_end)
            stdout_text = stdout.strip()
            stderr_text = stderr.strip()
            if check and returncode != 0:
                raise SherlockError(stderr_text or stdout_text or f"Remote command failed: {command}")
            return stdout_text

    def _ensure_started(self):
        if self._process is not None and self._process.poll() is None:
            return self._process
        self.close()
        process = self.popen_factory(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise SessionDisconnectedError("Persistent SSH session did not expose stdio pipes.")
        self._process = process
        self._stdout_queue = queue.Queue()
        self._stderr_queue = queue.Queue()
        threading.Thread(
            target=self._pump_stream,
            args=(process.stdout, self._stdout_queue),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stream,
            args=(process.stderr, self._stderr_queue),
            daemon=True,
        ).start()
        return process

    @staticmethod
    def _pump_stream(stream, output_queue) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put(line)
        finally:
            output_queue.put(None)

    @staticmethod
    def _drain_queue(output_queue) -> None:
        while True:
            try:
                output_queue.get_nowait()
            except queue.Empty:
                return

    def _collect_stdout(self, end_marker: str, exit_prefix: str) -> tuple[str, int]:
        chunks: list[str] = []
        returncode: int | None = None
        while True:
            item = self._stdout_queue.get()
            if item is None:
                raise SessionDisconnectedError("Persistent SSH session stdout closed unexpectedly.")
            if exit_prefix in item:
                prefix, _marker, suffix = item.partition(exit_prefix)
                if prefix:
                    chunks.append(prefix)
                returncode = int(suffix.strip())
                continue
            if item.rstrip("\n") == end_marker:
                if returncode is None:
                    raise SessionDisconnectedError("Persistent SSH session did not report a return code.")
                return "".join(chunks), returncode
            chunks.append(item)

    def _collect_stderr(self, end_marker: str) -> str:
        chunks: list[str] = []
        while True:
            item = self._stderr_queue.get()
            if item is None:
                raise SessionDisconnectedError("Persistent SSH session stderr closed unexpectedly.")
            if item.rstrip("\n") == end_marker:
                return "".join(chunks)
            chunks.append(item)


class SherlockService:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        *,
        runner=None,
        popen_factory=None,
        ssh_session_factory=None,
        sleeper=None,
        browser_opener=None,
    ):
        self.config = config
        self.state = state
        self.runner = runner or subprocess.run
        self.popen_factory = popen_factory or subprocess.Popen
        self.ssh_session_factory = ssh_session_factory or self._build_ssh_session
        self.sleeper = sleeper or time.sleep
        self.browser_opener = browser_opener or webbrowser.open
        self._remote_home: str | None = None
        self._remote_util_dir: str | None = None
        self._ssh_session = None

    def close(self) -> None:
        if self._ssh_session is None:
            return
        self._ssh_session.close()
        self._ssh_session = None

    def list_jobs(self) -> list[JobInfo]:
        connected_job_ids = self._connected_job_ids()
        output = self._ssh_output(
            self._remote_bash(
                "squeue -u {user} --states=PENDING,RUNNING -o '%i|%j|%T|%P|%R|%M' -h".format(
                    user=shlex.quote(self.config.connection.forward_username)
                )
            )
        )
        jobs = parse_squeue_jobs(output, self.state.managed_job_ids())
        for job in jobs:
            job.connected = job.job_id in connected_job_ids
        jobs.sort(key=lambda job: (job.state != "RUNNING", job.job_id))
        return jobs

    def resolve_job(self, target: str) -> JobInfo:
        jobs = self.list_jobs()
        exact_id = [job for job in jobs if job.job_id == target]
        if exact_id:
            return exact_id[0]
        exact_name = [job for job in jobs if job.name == target]
        if len(exact_name) == 1:
            return exact_name[0]
        if len(exact_name) > 1:
            raise SherlockError(f"Multiple active jobs match name '{target}'. Use a job id.")
        raise SherlockError(f"Active job '{target}' not found.")

    def get_job_detail(self, job_id: str) -> JobDetail:
        output = self._ssh_output(self._remote_bash(f"scontrol show job -o {shlex.quote(job_id)}"))
        detail = parse_job_detail(output)
        if not detail.job_id:
            raise SherlockError(f"Could not fetch job detail for {job_id}.")
        return detail

    def get_job_status(self, job_id: str) -> JobInfo | None:
        connected_job_ids = self._connected_job_ids()
        queue_output = self._ssh_output(
            self._remote_bash(f"squeue -j {shlex.quote(job_id)} -o '%i|%j|%T|%P|%R|%M' -h")
        )
        if queue_output.strip():
            status = parse_squeue_jobs(queue_output, self.state.managed_job_ids())[0]
            status.connected = status.job_id in connected_job_ids
            return status
        history_output = self._ssh_output(
            self._remote_bash(
                "sacct -j {job_id} --format=JobIDRaw,JobName,State,Partition,NodeList,Elapsed -P -n".format(
                    job_id=shlex.quote(job_id)
                )
            )
        )
        status = parse_sacct_job(history_output, self.state.managed_job_ids())
        if status:
            status.connected = status.job_id in connected_job_ids
        return status

    def submit_job(self, request: SubmissionRequest) -> str:
        remote_util_dir = self.remote_util_dir
        template_path = self.config.repo_root / request.preset.sbatch_script
        if not template_path.exists():
            raise SherlockError(f"sbatch template not found: {template_path}")

        remote_template = f"{remote_util_dir}/{template_path.name}"
        self._upload_text_file(template_path, remote_template)

        stdout_pattern = f"{remote_util_dir}/{request.job_name}-%j.out"
        stderr_pattern = f"{remote_util_dir}/{request.job_name}-%j.err"

        sbatch_args = [
            "sbatch",
            "--parsable",
            "-p",
            request.partition,
            "--job-name",
            request.job_name,
            "--output",
            stdout_pattern,
            "--error",
            stderr_pattern,
            "--mem",
            request.mem,
            "--time",
            request.time,
            "-c",
            str(request.cpus),
            "--mail-type=BEGIN",
            "--mail-user",
            self.config.connection.email,
        ]
        if request.nodelist:
            sbatch_args.extend(["--nodelist", request.nodelist])
        if request.constraint:
            sbatch_args.extend(["--constraint", request.constraint])
        if request.gpus:
            sbatch_args.extend(["--gres", f"gpu:{request.gpus}"])
            if request.gres_flags:
                sbatch_args.extend(["--gres-flags", request.gres_flags])
            if request.gpu_cmode:
                sbatch_args.extend(["--gpu_cmode", request.gpu_cmode])
        sbatch_args.extend([remote_template, str(request.port), request.notebook_dir])

        submit_output = self._ssh_output(self._remote_bash(self._shell_join(sbatch_args)))
        job_id = parse_sbatch_submission(submit_output)
        self.state.record_submission(
            job_id=job_id,
            job_name=request.job_name,
            preset_id=request.preset.id,
            notebook_dir=request.notebook_dir,
            remote_port=request.port,
            remote_stdout=stdout_pattern.replace("%j", job_id),
            remote_stderr=stderr_pattern.replace("%j", job_id),
            remote_template=remote_template,
        )
        return job_id

    def watch_job(self, target: str, *, connect_on_run: bool = False, console=None) -> JobInfo | None:
        job = self.resolve_job(target) if not target.isdigit() else None
        job_id = target if target.isdigit() else job.job_id
        last_state: str | None = None
        while True:
            status = self.get_job_status(job_id)
            if status is None:
                raise SherlockError(f"Could not determine status for job {job_id}.")
            if status.state != last_state and console is not None:
                console.print(
                    f"[cyan]{status.job_id}[/cyan] {status.name} "
                    f"[bold]{status.state}[/bold] {status.reason_or_node}"
                )
            last_state = status.state
            if status.state == "RUNNING":
                if connect_on_run:
                    self.connect_job(job_id, open_browser=True)
                return status
            if self._is_terminal_state(status.state):
                return status
            self.sleeper(self.config.poll_interval_seconds)

    def connect_job(self, target: str, *, open_browser: bool = True) -> JupyterInfo:
        job = self.resolve_job(target) if not target.isdigit() else None
        job_id = target if target.isdigit() else job.job_id
        detail = self.get_job_detail(job_id)
        if detail.state != "RUNNING":
            raise SherlockError(f"Job {job_id} is not RUNNING. Current state: {detail.state}")

        metadata = self.state.get(job_id)
        remote_port = metadata.remote_port if metadata else None
        jupyter = self.wait_for_jupyter(job_id, fallback_port=remote_port)
        existing = metadata.tunnel_pid if metadata else None
        if existing and self._pid_alive(existing):
            jupyter.local_port = metadata.local_port
            jupyter.local_url = metadata.jupyter_url
            return jupyter
        local_port = self._choose_local_port(jupyter.port)

        if not metadata:
            metadata = self._adopt_external_job(job_id, detail, jupyter.port)

        tunnel = self._start_tunnel(
            node=detail.node_list,
            remote_port=jupyter.port,
            local_port=local_port,
            isolated_compute_node=self._job_is_isolated(metadata, detail),
        )
        local_url = rewrite_local_jupyter_url(jupyter.url, local_port)
        self.state.update_tunnel(job_id, tunnel.pid, local_port, local_url)
        jupyter.local_port = local_port
        jupyter.local_url = local_url
        if open_browser:
            self.browser_opener(local_url)
        return jupyter

    def wait_for_jupyter(self, job_id: str, *, fallback_port: int | None = None) -> JupyterInfo:
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            logs = self.get_job_logs(job_id, lines=200)
            combined = "\n".join([logs.stdout, logs.stderr])
            info = parse_jupyter_info(combined)
            if info:
                if not info.port and fallback_port:
                    info.port = fallback_port
                return info
            status = self.get_job_status(job_id)
            if status and self._is_terminal_state(status.state):
                raise SherlockError(f"Job {job_id} reached terminal state {status.state} before Jupyter was ready.")
            self.sleeper(self.config.poll_interval_seconds)
        if fallback_port:
            return JupyterInfo(url=f"http://localhost:{fallback_port}/", port=fallback_port)
        raise SherlockError(f"Timed out waiting for Jupyter startup for job {job_id}.")

    def get_job_logs(self, target: str, *, lines: int = 60) -> JobLogs:
        job = self.resolve_job(target) if not target.isdigit() else None
        job_id = target if target.isdigit() else job.job_id
        detail = self.get_job_detail(job_id)
        metadata = self.state.get(job_id)
        stdout_path = detail.stdout_path or (metadata.remote_stdout if metadata else None)
        stderr_path = detail.stderr_path or (metadata.remote_stderr if metadata else None)
        stdout = self._read_remote_file(stdout_path, lines) if stdout_path else ""
        stderr = self._read_remote_file(stderr_path, lines) if stderr_path else ""
        return JobLogs(stdout=stdout, stderr=stderr)

    def kill_job(self, target: str) -> str:
        job = self.resolve_job(target) if not target.isdigit() else None
        job_id = target if target.isdigit() else job.job_id
        self._ssh_output(self._remote_bash(f"scancel {shlex.quote(job_id)}"))
        metadata = self.state.get(job_id)
        if metadata and metadata.tunnel_pid:
            self._terminate_pid(metadata.tunnel_pid)
            self.state.clear_tunnel(job_id)
        return job_id

    def remote_util_listing(self) -> str:
        return self._ssh_output(self._remote_bash(f"ls -1 {shlex.quote(self.remote_util_dir)}"))

    @property
    def remote_util_dir(self) -> str:
        if self._remote_util_dir is None:
            remote_home = self.remote_home
            configured = self.config.connection.remote_util_dir
            if configured.startswith("~/"):
                self._remote_util_dir = f"{remote_home}/{configured[2:]}"
            elif configured == "~":
                self._remote_util_dir = remote_home
            else:
                self._remote_util_dir = configured
            self._ssh_output(self._remote_bash(f"mkdir -p {shlex.quote(self._remote_util_dir)}"))
        return self._remote_util_dir

    @property
    def remote_home(self) -> str:
        if self._remote_home is None:
            self._remote_home = self._ssh_output(self._remote_bash("printf '%s' \"$HOME\"")).strip()
        return self._remote_home

    def _adopt_external_job(self, job_id: str, detail: JobDetail, remote_port: int) -> JobMetadata:
        metadata = JobMetadata(
            job_id=job_id,
            job_name=detail.name,
            preset_id="",
            notebook_dir="",
            remote_port=remote_port,
            remote_stdout=detail.stdout_path or "",
            remote_stderr=detail.stderr_path or "",
            remote_template="",
            created_at=utc_now(),
        )
        return self.state.upsert(metadata)

    def _job_is_isolated(self, metadata, detail: JobDetail | None = None) -> bool:
        if metadata is None:
            if detail:
                for preset in self.config.presets.values():
                    if preset.job_name == detail.name or preset.partition == detail.partition:
                        return preset.isolated_compute_node
            return True
        preset = self.config.presets.get(metadata.preset_id)
        return preset.isolated_compute_node if preset else True

    def _read_remote_file(self, path: str, lines: int) -> str:
        return self._ssh_output(self._remote_bash(f"tail -n {int(lines)} {shlex.quote(path)}"), check=False)

    def _connected_job_ids(self) -> set[str]:
        connected: set[str] = set()
        for job_id, metadata in self.state.jobs.items():
            if metadata.tunnel_pid and self._pid_alive(metadata.tunnel_pid):
                connected.add(job_id)
        return connected

    def _upload_text_file(self, local_path: Path, remote_path: str) -> None:
        self._ssh_session_or_create().write_text(remote_path, local_path.read_text())

    def _start_tunnel(self, *, node: str, remote_port: int, local_port: int, isolated_compute_node: bool):
        if not node:
            raise SherlockError("Job does not have an assigned node yet.")
        if isolated_compute_node:
            args = [
                "ssh",
                "-L",
                f"{local_port}:localhost:{remote_port}",
                self.config.connection.resource,
                "ssh",
                "-L",
                f"{remote_port}:localhost:{remote_port}",
                "-N",
                node,
            ]
        else:
            args = [
                "ssh",
                self.config.connection.domain_name,
                "-l",
                self.config.connection.forward_username,
            ]
            if self.config.connection.use_kerberos:
                args.append("-K")
            args.extend(["-L", f"{local_port}:{node}:{remote_port}", "-N"])
        return self.popen_factory(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    def _choose_local_port(self, preferred: int) -> int:
        if self._port_available(preferred):
            return preferred
        raise SherlockError(f"Local port {preferred} is already in use.")

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return sock.connect_ex(("127.0.0.1", port)) != 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return

    @staticmethod
    def _is_terminal_state(state: str) -> bool:
        terminals = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL")
        return any(state.startswith(item) for item in terminals)

    @staticmethod
    def _shell_join(args: list[str]) -> str:
        return " ".join(shlex.quote(arg) for arg in args)

    def _build_ssh_session(self) -> PersistentSSHSession:
        return PersistentSSHSession(self.config.connection.resource)

    def _ssh_session_or_create(self):
        if self._ssh_session is None:
            self._ssh_session = self.ssh_session_factory()
        return self._ssh_session

    @staticmethod
    def _remote_bash(command: str) -> str:
        return command

    def _ssh_output(self, command: str, *, check: bool = True) -> str:
        try:
            return self._ssh_session_or_create().run(command, check=check)
        except SessionDisconnectedError:
            self.close()
            raise SherlockError("Persistent SSH session disconnected.")
