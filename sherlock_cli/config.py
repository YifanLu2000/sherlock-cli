from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from .models import AppConfig, ConnectionConfig, Preset


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def load_config(config_path: str | None = None) -> AppConfig:
    repo_root = _repo_root()
    selected_path = Path(
        config_path or os.environ.get("SHERLOCK_CONFIG") or repo_root / "sherlock_presets.toml"
    ).expanduser()
    with selected_path.open("rb") as handle:
        raw = tomllib.load(handle)

    connection_raw = raw["connection"]
    connection = ConnectionConfig(
        name=connection_raw.get("name", connection_raw["resource"]),
        resource=connection_raw["resource"],
        domain_name=connection_raw["domain_name"],
        forward_username=connection_raw["forward_username"],
        email=connection_raw["email"],
        machine_prefix=connection_raw["machine_prefix"],
        default_notebook_dir=connection_raw["default_notebook_dir"],
        remote_util_dir=connection_raw["remote_util_dir"],
        use_kerberos=connection_raw.get("use_kerberos", True),
    )

    presets: dict[str, Preset] = {}
    for item in raw.get("presets", []):
        preset = Preset(
            id=item["id"],
            label=item["label"],
            job_name=item["job_name"],
            sbatch_script=item["sbatch_script"],
            partition=item["partition"],
            cpus=int(item["cpus"]),
            mem=item["mem"],
            time=item["time"],
            port=int(item["port"]),
            account=item.get("account"),
            gpus=int(item.get("gpus", 0)),
            nodelist=item.get("nodelist"),
            constraint=item.get("constraint"),
            gres_flags=item.get("gres_flags"),
            gpu_cmode=item.get("gpu_cmode"),
            isolated_compute_node=bool(item.get("isolated_compute_node", True)),
            notebook_dir=item.get("notebook_dir"),
        )
        presets[preset.id] = preset

    state_path = _expand_path(raw.get("state", {}).get("path", "~/.local/state/sherlock-cli/state.json"))

    return AppConfig(
        repo_root=repo_root,
        config_path=selected_path.resolve(),
        connection=connection,
        presets=presets,
        state_path=state_path,
        poll_interval_seconds=int(raw.get("watch", {}).get("poll_interval_seconds", 5)),
        startup_timeout_seconds=int(raw.get("watch", {}).get("startup_timeout_seconds", 300)),
    )
