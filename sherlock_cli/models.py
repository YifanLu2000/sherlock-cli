from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConnectionConfig:
    name: str
    resource: str
    domain_name: str
    forward_username: str
    email: str
    machine_prefix: str
    default_notebook_dir: str
    remote_util_dir: str
    use_kerberos: bool = True


@dataclass
class Preset:
    id: str
    label: str
    job_name: str
    sbatch_script: str
    partition: str
    cpus: int
    mem: str
    time: str
    port: int
    account: str | None = None
    gpus: int = 0
    nodelist: str | None = None
    constraint: str | None = None
    gres_flags: str | None = None
    gpu_cmode: str | None = None
    isolated_compute_node: bool = True
    notebook_dir: str | None = None

    @property
    def is_gpu(self) -> bool:
        return self.gpus > 0


@dataclass
class AppConfig:
    repo_root: Path
    config_path: Path
    connection: ConnectionConfig
    presets: dict[str, Preset]
    state_path: Path
    poll_interval_seconds: int = 5
    startup_timeout_seconds: int = 300


@dataclass
class JobInfo:
    job_id: str
    name: str
    state: str
    partition: str
    reason_or_node: str
    elapsed: str
    origin: str
    connected: bool = False


@dataclass
class JobMetadata:
    job_id: str
    job_name: str
    preset_id: str
    notebook_dir: str
    remote_port: int
    remote_stdout: str
    remote_stderr: str
    remote_template: str
    created_at: str
    tunnel_pid: int | None = None
    local_port: int | None = None
    jupyter_url: str | None = None


@dataclass
class JupyterInfo:
    url: str
    port: int
    token: str | None = None
    local_url: str | None = None
    local_port: int | None = None


@dataclass
class SubmissionRequest:
    preset: Preset
    job_name: str
    notebook_dir: str
    port: int
    partition: str
    mem: str
    time: str
    cpus: int
    account: str | None
    gpus: int
    nodelist: str | None
    constraint: str | None
    gres_flags: str | None
    gpu_cmode: str | None
    isolated_compute_node: bool


@dataclass
class JobLogs:
    stdout: str
    stderr: str


@dataclass
class JobDetail:
    job_id: str
    name: str
    state: str
    partition: str
    node_list: str
    reason: str
    elapsed: str
    stdout_path: str | None = None
    stderr_path: str | None = None


@dataclass
class StateData:
    jobs: dict[str, JobMetadata] = field(default_factory=dict)
