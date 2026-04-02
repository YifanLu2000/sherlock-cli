from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .models import RemoteProfile, RemoteSessionMetadata
from .parsing import parse_sbatch_submission
from .service import SherlockError, SherlockService
from .state import utc_now


LOGIN_CONTROL_SETTINGS = """\
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 12h
    ServerAliveInterval 60
    ServerAliveCountMax 3
    TCPKeepAlive yes
"""


class SshConfigManager:
    def __init__(self, path: Path):
        self.path = path

    def ensure_login_alias(self, *, alias: str, host: str, user: str) -> None:
        self._prepare()
        content = self._read()
        if self._host_alias_exists(content, alias):
            return
        block = (
            f"\nHost {alias} {host}\n"
            f"    HostName {host}\n"
            f"    User {user}\n"
            f"{LOGIN_CONTROL_SETTINGS}"
        )
        self._write(content + block)

    def update_compute_alias(self, *, alias: str, node: str, port: int, user: str, login_alias: str) -> None:
        self._prepare()
        content = self._read()
        marker_begin = self._marker_begin(alias)
        marker_end = self._marker_end(alias)
        block = (
            f"{marker_begin}\n"
            f"Host {alias}\n"
            f"    HostName {node}\n"
            f"    Port {port}\n"
            f"    User {user}\n"
            f"    ProxyCommand ssh -W %h:%p {login_alias}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    ServerAliveInterval 60\n"
            f"    ServerAliveCountMax 3\n"
            f"{marker_end}\n"
        )
        if marker_begin in content and marker_end in content:
            pattern = re.compile(
                rf"{re.escape(marker_begin)}\n.*?{re.escape(marker_end)}\n?",
                re.DOTALL,
            )
            content = re.sub(pattern, block, content)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += "\n" + block
        self._write(content)

    def remove_compute_alias(self, alias: str) -> None:
        if not self.path.exists():
            return
        marker_begin = self._marker_begin(alias)
        marker_end = self._marker_end(alias)
        content = self._read()
        pattern = re.compile(
            rf"\n?{re.escape(marker_begin)}\n.*?{re.escape(marker_end)}\n?",
            re.DOTALL,
        )
        updated = re.sub(pattern, "\n", content)
        self._write(updated.lstrip("\n"))

    @staticmethod
    def _marker_begin(alias: str) -> str:
        return f"# --- {alias} BEGIN ---"

    @staticmethod
    def _marker_end(alias: str) -> str:
        return f"# --- {alias} END ---"

    @staticmethod
    def _host_alias_exists(content: str, alias: str) -> bool:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("Host "):
                continue
            aliases = stripped.split()[1:]
            if alias in aliases:
                return True
        return False

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        os.chmod(self.path, 0o600)

    def _read(self) -> str:
        return self.path.read_text()

    def _write(self, content: str) -> None:
        self.path.write_text(content)
        os.chmod(self.path, 0o600)


class RemoteService(SherlockService):
    def __init__(self, *args, ssh_config_manager: SshConfigManager | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        default_manager = SshConfigManager(Path.home() / ".ssh" / "config")
        self.ssh_config_manager = ssh_config_manager or default_manager

    @property
    def login_alias(self) -> str:
        return self.config.remote.login_alias

    @property
    def login_host(self) -> str:
        return self.config.remote.login_host

    @property
    def compute_host_alias(self) -> str:
        return self.config.remote.compute_host_alias

    @property
    def remote_shell_init(self) -> str | None:
        return self.config.remote.shell_init

    @property
    def remote_workspace_dir(self) -> str:
        configured = self.config.remote.workspace_dir
        if configured.startswith("~/"):
            return f"{self.remote_home}/{configured[2:]}"
        if configured == "~":
            return self.remote_home
        return configured

    def remote_path(self, filename: str) -> str:
        return f"{self.remote_workspace_dir}/{filename}"

    def ensure_local_public_key(self) -> Path:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise SherlockError("No SSH public key found at ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.")

    def setup_local(self) -> Path:
        key_path = self.ensure_local_public_key()
        self.ssh_config_manager.ensure_login_alias(
            alias=self.login_alias,
            host=self.login_host,
            user=self.config.connection.forward_username,
        )
        return key_path

    def setup_remote(self) -> None:
        key_path = self.setup_local()
        self.ensure_connected()
        pubkey = key_path.read_text().strip()
        key_data = pubkey.split()[1] if len(pubkey.split()) >= 2 else pubkey
        self._ssh_output(self._remote_bash(f"mkdir -p {shlex.quote(self.remote_workspace_dir)}"))
        self._ssh_output(
            self._remote_bash(
                "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                f"grep -qF {shlex.quote(key_data)} ~/.ssh/authorized_keys || "
                f"printf '%s\\n' {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys"
            )
        )
        self._install_start_script()
        self._ensure_host_key()
        self._write_sshd_config()
        self._write_remote_profiles()

    def setup_full(self) -> None:
        self.setup_local()
        self.setup_remote()

    def ensure_setup(self) -> None:
        self.setup_local()
        exists = self._ssh_output(
            self._remote_bash(
                f"test -x {shlex.quote(self.remote_path('start_sshd.sh'))} && printf '%s' yes"
            ),
            check=False,
        ).strip()
        if exists != "yes":
            self.setup_remote()

    def resolve_remote_profile(self, profile_id: str | None) -> RemoteProfile:
        if not self.config.remote_profiles:
            raise SherlockError(f"No remote profiles configured for {self.config.connection.name}.")
        resolved_id = profile_id or self.config.remote_default_profile_id
        if resolved_id:
            profile = self.config.remote_profiles.get(resolved_id)
            if profile is None:
                raise SherlockError(f"Remote profile '{resolved_id}' not found.")
            return profile
        if len(self.config.remote_profiles) == 1:
            return next(iter(self.config.remote_profiles.values()))
        available = ", ".join(sorted(self.config.remote_profiles))
        raise SherlockError(f"Remote profile is required. Available profiles: {available}")

    def list_running_jobs(self):
        return [job for job in self.list_jobs() if job.state == "RUNNING"]

    def attach(self, job_id: str) -> RemoteSessionMetadata:
        self.ensure_setup()
        detail = self.get_job_detail(job_id)
        if detail.state != "RUNNING":
            raise SherlockError(f"Job {job_id} is not RUNNING. Current state: {detail.state}")
        command = (
            f"srun --jobid={shlex.quote(job_id)} --overlap "
            f"env CURSOR_REMOTE_DIR={shlex.quote(self.remote_workspace_dir)} "
            f"bash {shlex.quote(self.remote_path('start_sshd.sh'))} "
            f"</dev/null >{shlex.quote(self.remote_path('attach.log'))} 2>&1"
        )
        self._run_login_command(command, background=True)
        session = self._wait_for_remote_session(expected_node=detail.node_list, expected_job_id=job_id, timeout=30)
        return self._activate_remote_session(session, session_type="attached")

    def start(self, profile_id: str | None = None) -> RemoteSessionMetadata:
        self.ensure_setup()
        profile = self.resolve_remote_profile(profile_id)
        remote_profile_path = self.remote_path(f"cursor_{profile.id}.slurm")
        job_id = parse_sbatch_submission(
            self._ssh_output(self._remote_bash(f"sbatch --parsable {shlex.quote(remote_profile_path)}"))
        )
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if status is None:
                raise SherlockError(f"Could not determine status for job {job_id}.")
            if self._is_terminal_state(status.state):
                raise SherlockError(f"Remote job {job_id} reached terminal state {status.state}.")
            if status.state == "RUNNING":
                session = self.read_remote_session()
                if session and session.job_id == job_id:
                    return self._activate_remote_session(session, session_type="dedicated")
            self.sleeper(self.config.poll_interval_seconds)
        raise SherlockError(f"Timed out waiting for remote session for job {job_id}.")

    def connect_remote(self) -> RemoteSessionMetadata:
        session = self.read_remote_session()
        if session is None:
            raise SherlockError("No remote session info found.")
        status = self.get_job_status(session.job_id)
        if status is None or status.state != "RUNNING":
            state = status.state if status else "gone"
            raise SherlockError(f"Session job {session.job_id} is no longer running (state: {state}).")
        return self._activate_remote_session(session, session_type=session.session_type)

    def stop(self) -> RemoteSessionMetadata:
        session = self.read_remote_session()
        if session is None:
            raise SherlockError("No active remote session found.")
        self.ssh_config_manager.update_compute_alias(
            alias=self.compute_host_alias,
            node=session.node,
            port=session.port,
            user=self.config.connection.forward_username,
            login_alias=self.login_alias,
        )
        if session.session_type == "attached":
            self._run_local_command(
                [
                    "ssh",
                    self.compute_host_alias,
                    f"kill $(cat {shlex.quote(self.remote_path('sshd.pid'))}) 2>/dev/null || true",
                ],
                check=False,
            )
        else:
            self._ssh_output(self._remote_bash(f"scancel {shlex.quote(session.job_id)}"), check=False)
        self._ssh_output(
            self._remote_bash(
                f"rm -f {shlex.quote(self.remote_path('session'))} "
                f"{shlex.quote(self.remote_path('session_type'))}"
            ),
            check=False,
        )
        self._run_local_command(["ssh", "-O", "exit", self.compute_host_alias], check=False)
        self.ssh_config_manager.remove_compute_alias(self.compute_host_alias)
        self.state.clear_remote_session()
        return session

    def clean(self) -> None:
        self.ssh_config_manager.remove_compute_alias(self.compute_host_alias)
        self.state.clear_remote_session()

    def read_remote_session(self) -> RemoteSessionMetadata | None:
        output = self._ssh_output(
            self._remote_bash(f"cat {shlex.quote(self.remote_path('session'))} 2>/dev/null"),
            check=False,
        ).strip()
        if not output:
            return None
        parts = output.split()
        if len(parts) < 3:
            raise SherlockError(f"Malformed remote session metadata: {output}")
        node, port, job_id = parts[0], int(parts[1]), parts[2]
        session_type = (
            self._ssh_output(
                self._remote_bash(f"cat {shlex.quote(self.remote_path('session_type'))} 2>/dev/null"),
                check=False,
            ).strip()
            or "dedicated"
        )
        return RemoteSessionMetadata(
            job_id=job_id,
            node=node,
            port=port,
            session_type=session_type,
            last_verified_at=utc_now(),
        )

    def _activate_remote_session(self, session: RemoteSessionMetadata, *, session_type: str) -> RemoteSessionMetadata:
        self._ssh_output(
            self._remote_bash(
                f"printf '%s\\n' {shlex.quote(session_type)} > {shlex.quote(self.remote_path('session_type'))}"
            )
        )
        updated = RemoteSessionMetadata(
            job_id=session.job_id,
            node=session.node,
            port=session.port,
            session_type=session_type,
            last_verified_at=utc_now(),
        )
        self.ssh_config_manager.update_compute_alias(
            alias=self.compute_host_alias,
            node=updated.node,
            port=updated.port,
            user=self.config.connection.forward_username,
            login_alias=self.login_alias,
        )
        self._verify_compute_connection()
        self.state.set_remote_session(updated)
        return updated

    def _verify_compute_connection(self) -> None:
        self._run_local_command(["ssh", "-o", "ConnectTimeout=10", self.compute_host_alias, "hostname"])

    def _run_local_command(self, args: list[str], *, check: bool = True) -> None:
        self.runner(args, check=check)

    def _run_login_command(self, command: str, *, background: bool = False) -> None:
        args = self._login_ssh_args()
        if background:
            args.insert(1, "-f")
        args.append(self._wrap_remote_shell(command))
        self.runner(args, check=True)

    def _wrap_remote_shell(self, command: str) -> str:
        if not self.remote_shell_init:
            return command
        return f"bash -lc {shlex.quote(f'{self.remote_shell_init}; {command}')}"

    def _wait_for_remote_session(
        self,
        *,
        expected_node: str | None,
        expected_job_id: str | None,
        timeout: int,
    ) -> RemoteSessionMetadata:
        deadline = time.time() + timeout
        while time.time() < deadline:
            session = self.read_remote_session()
            if session is None:
                self.sleeper(3)
                continue
            if expected_node and session.node != expected_node:
                self.sleeper(3)
                continue
            if expected_job_id and session.job_id != expected_job_id:
                self.sleeper(3)
                continue
            return session
        raise SherlockError("Timed out waiting for remote sshd startup.")

    def _install_start_script(self) -> None:
        local_path = self.config.repo_root / "remote" / "start_sshd.sh"
        if not local_path.exists():
            raise SherlockError(f"Remote start script not found: {local_path}")
        self._upload_text_file(local_path, self.remote_path("start_sshd.sh"))
        self._ssh_output(self._remote_bash(f"chmod +x {shlex.quote(self.remote_path('start_sshd.sh'))}"))

    def _ensure_host_key(self) -> None:
        self._ssh_output(
            self._remote_bash(
                f"test -f {shlex.quote(self.remote_path('host_key'))} || "
                f"ssh-keygen -t ed25519 -f {shlex.quote(self.remote_path('host_key'))} -N '' -q"
            )
        )

    def _write_sshd_config(self) -> None:
        sftp_path = self._ssh_output(
            self._remote_bash(
                "for p in /usr/libexec/openssh/sftp-server "
                "/usr/lib/openssh/sftp-server /usr/libexec/sftp-server; do "
                'if [ -x "$p" ]; then printf "%s" "$p"; break; fi; '
                "done"
            ),
            check=False,
        ).strip()
        lines = [
            f"HostKey {self.remote_path('host_key')}",
            "PubkeyAuthentication yes",
            "AuthorizedKeysFile ~/.ssh/authorized_keys",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "GSSAPIAuthentication no",
            "UsePAM no",
            "X11Forwarding yes",
            "AllowTcpForwarding yes",
            "PrintMotd no",
            f"PidFile {self.remote_path('sshd.pid')}",
        ]
        if sftp_path:
            lines.append(f"Subsystem sftp {sftp_path}")
        content = "\n".join(lines) + "\n"
        self._ssh_session_or_create().write_text(self.remote_path("sshd_config"), content)

    def _write_remote_profiles(self) -> None:
        for profile in self.config.remote_profiles.values():
            remote_path = self.remote_path(f"cursor_{profile.id}.slurm")
            self._ssh_session_or_create().write_text(remote_path, self._render_remote_profile(profile))

    def _render_remote_profile(self, profile: RemoteProfile) -> str:
        lines = [
            "#!/bin/bash",
            f"#SBATCH -J {profile.job_name}",
            f"#SBATCH -p {profile.partition}",
            f"#SBATCH --cpus-per-task={profile.cpus}",
            "#SBATCH -N 1",
            f"#SBATCH -t {profile.time}",
            f"#SBATCH --mem={profile.mem}",
            f"#SBATCH -o {self.remote_path('cursor.%j.log')}",
            f"#SBATCH -e {self.remote_path('cursor.%j.err')}",
        ]
        if profile.account:
            lines.append(f"#SBATCH -A {profile.account}")
        if profile.gpus:
            lines.append(f"#SBATCH --gres=gpu:{profile.gpus}")
        if profile.gpu_cmode:
            lines.append(f"#SBATCH --gpu_cmode={profile.gpu_cmode}")
        if profile.gres_flags:
            lines.append(f"#SBATCH --gres-flags={profile.gres_flags}")
        if profile.nodelist:
            lines.append(f"#SBATCH --nodelist={profile.nodelist}")
        if profile.constraint:
            lines.append(f"#SBATCH --constraint={profile.constraint}")
        lines.extend(
            [
                "",
                f'export CURSOR_REMOTE_DIR="{self.remote_workspace_dir}"',
                f"{self.remote_path('start_sshd.sh')}",
                "",
            ]
        )
        return "\n".join(lines)
