from __future__ import annotations

import hashlib
import os
import shlex
import signal
import socket
import subprocess
import time
import webbrowser
from pathlib import Path

from .models import AppConfig, JobDetail, JobInfo, JobLogs, JupyterInfo, SubmissionRequest
from .parsing import (
    parse_job_detail,
    parse_jupyter_info,
    parse_sacct_job,
    parse_sbatch_submission,
    parse_squeue_jobs,
    rewrite_local_jupyter_url,
)
from .state import StateStore


class SherlockError(RuntimeError):
    pass


class SherlockService:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        *,
        runner=None,
        popen_factory=None,
        sleeper=None,
        browser_opener=None,
    ):
        self.config = config
        self.state = state
        self.runner = runner or subprocess.run
        self.popen_factory = popen_factory or subprocess.Popen
        self.sleeper = sleeper or time.sleep
        self.browser_opener = browser_opener or webbrowser.open
        self._remote_home: str | None = None
        self._remote_util_dir: str | None = None
        socket_key = "|".join(
            [
                self.config.connection.resource,
                self.config.connection.domain_name,
                self.config.connection.forward_username,
            ]
        )
        socket_hash = hashlib.sha1(socket_key.encode("utf-8")).hexdigest()[:12]
        self._ssh_control_path = self.config.state_path.parent / f"ssh-{socket_hash}.sock"
        self._ssh_control_persist_seconds = 600

    def list_jobs(self) -> list[JobInfo]:
        output = self._ssh_output(
            self._remote_bash(
                "squeue -u {user} --states=PENDING,RUNNING -o '%i|%j|%T|%P|%R|%M' -h".format(
                    user=shlex.quote(self.config.connection.forward_username)
                )
            )
        )
        jobs = parse_squeue_jobs(output, self.state.managed_job_ids())
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
        queue_output = self._ssh_output(
            self._remote_bash(f"squeue -j {shlex.quote(job_id)} -o '%i|%j|%T|%P|%R|%M' -h")
        )
        if queue_output.strip():
            return parse_squeue_jobs(queue_output, self.state.managed_job_ids())[0]
        history_output = self._ssh_output(
            self._remote_bash(
                "sacct -j {job_id} --format=JobIDRaw,JobName,State,Partition,NodeList,Elapsed -P -n".format(
                    job_id=shlex.quote(job_id)
                )
            )
        )
        return parse_sacct_job(history_output, self.state.managed_job_ids())

    def submit_job(self, request: SubmissionRequest) -> str:
        remote_util_dir = self.remote_util_dir
        template_path = self.config.repo_root / request.preset.sbatch_script
        if not template_path.exists():
            raise SherlockError(f"sbatch template not found: {template_path}")

        remote_template = f"{remote_util_dir}/{template_path.name}"
        self._run(self._scp_to_remote(template_path, remote_template))

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
        local_port = self._choose_local_port(jupyter.port)
        existing = metadata.tunnel_pid if metadata else None
        if existing and self._pid_alive(existing):
            jupyter.local_port = metadata.local_port
            jupyter.local_url = metadata.jupyter_url
            return jupyter

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
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

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

    def _ssh_multiplex_options(self) -> list[str]:
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPersist={self._ssh_control_persist_seconds}",
            "-o",
            f"ControlPath={self._ssh_control_path}",
        ]

    def _remote_bash(self, command: str) -> list[str]:
        return [
            "ssh",
            *self._ssh_multiplex_options(),
            self.config.connection.resource,
            f"bash -lc {shlex.quote(command)}",
        ]

    def _scp_to_remote(self, local_path: Path, remote_path: str) -> list[str]:
        return [
            "scp",
            *self._ssh_multiplex_options(),
            str(local_path),
            f"{self.config.connection.resource}:{remote_path}",
        ]

    def _remove_stale_control_socket(self) -> None:
        try:
            self._ssh_control_path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _is_stale_control_socket_error(message: str) -> bool:
        indicators = (
            "Control socket connect(",
            "mux_client_request_session:",
            "master is dead",
        )
        failures = (
            "Connection refused",
            "Broken pipe",
            "No such file or directory",
        )
        return any(item in message for item in indicators) and any(item in message for item in failures)

    def _run(self, args: list[str], *, check: bool = True):
        result = self.runner(args, capture_output=True, text=True)
        if result.returncode != 0 and args and args[0] in {"ssh", "scp"}:
            message = "\n".join(part for part in [result.stderr, result.stdout] if part)
            if self._is_stale_control_socket_error(message):
                self._remove_stale_control_socket()
                result = self.runner(args, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise SherlockError(result.stderr.strip() or result.stdout.strip() or f"Command failed: {args}")
        return result

    def _ssh_output(self, args: list[str], *, check: bool = True) -> str:
        result = self._run(args, check=check)
        return (result.stdout or "").strip()
