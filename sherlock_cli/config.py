from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from .models import AppConfig, ConnectionConfig, Preset, RemoteConfig, RemoteProfile


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_raw_config(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def user_config_dir() -> Path:
    return _expand_path("~/.config/sherlock-cli")


def default_user_config_path(resource: str) -> Path:
    return user_config_dir() / f"{resource}.toml"


def bundled_config_path(resource: str) -> Path:
    return _repo_root() / f"{resource}_presets.toml"


def is_bundled_config_path(path: Path) -> bool:
    return path.resolve().parent == _repo_root() and path.name.endswith("_presets.toml")


def resolve_resource_config_path(resource: str) -> Path:
    user_path = default_user_config_path(resource)
    if user_path.exists():
        return user_path
    return bundled_config_path(resource)


def find_config_path(cluster: str) -> Path | None:
    wanted = cluster.strip().lower()
    for name, path in discover_configs():
        raw = _load_raw_config(path)
        resource = raw.get("connection", {}).get("resource", "")
        aliases = {
            name.lower(),
            resource.lower(),
            path.stem.lower(),
            path.stem.removesuffix("_presets").lower(),
        }
        if wanted in aliases:
            return path
    return None


def discover_configs() -> list[tuple[str, Path]]:
    """Return available configs, preferring user overrides over bundled presets."""
    configs_by_resource: dict[str, tuple[str, Path]] = {}

    for path in sorted(_repo_root().glob("*_presets.toml")):
        raw = _load_raw_config(path)
        connection = raw.get("connection", {})
        resource = connection.get("resource", path.stem)
        name = connection.get("name", path.stem)
        configs_by_resource[resource] = (name, path)

    config_dir = user_config_dir()
    if config_dir.exists():
        for path in sorted(config_dir.glob("*.toml")):
            raw = _load_raw_config(path)
            connection = raw.get("connection", {})
            resource = connection.get("resource", path.stem)
            name = connection.get("name", path.stem)
            configs_by_resource[resource] = (name, path)

    return sorted(configs_by_resource.values(), key=lambda item: item[0].lower())


def resolve_config_path(config_path: str | None = None) -> Path:
    if config_path:
        return Path(config_path).expanduser()

    env_config = os.environ.get("SHERLOCK_CONFIG")
    if env_config:
        return Path(env_config).expanduser()

    return resolve_resource_config_path("sherlock")


def _toml_quote(value: str) -> str:
    return json.dumps(value)


def write_config(
    source_path: Path,
    output_path: Path,
    *,
    forward_username: str,
    email: str,
    default_notebook_dir: str,
) -> Path:
    content = source_path.read_text()
    replacements = {
        "forward_username": forward_username,
        "email": email,
        "default_notebook_dir": default_notebook_dir,
    }
    for key, value in replacements.items():
        pattern = rf"(^\s*{re.escape(key)}\s*=\s*)\".*?\""
        content, count = re.subn(
            pattern,
            lambda match: f"{match.group(1)}{_toml_quote(value)}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f"Could not update {key} in {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    return output_path.resolve()


def load_config(config_path: str | None = None) -> AppConfig:
    repo_root = _repo_root()
    selected_path = resolve_config_path(config_path)
    raw = _load_raw_config(selected_path)

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
        ssh_host=connection_raw.get("ssh_host"),
        shell_init=connection_raw.get("shell_init"),
    )
    remote_raw = raw.get("remote", {})
    remote = RemoteConfig(
        login_alias=remote_raw.get("login_alias", connection.ssh_host or connection.resource),
        login_host=remote_raw.get("login_host", connection.domain_name),
        compute_host_alias=remote_raw.get("compute_host_alias", f"{connection.resource}-compute"),
        workspace_dir=remote_raw.get("workspace_dir", f"~/.cursor-remote/{connection.resource}"),
        shell_init=remote_raw.get("shell_init", connection.shell_init),
        home_template=remote_raw.get("home_template", "/home/users/{user}"),
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

    remote_profiles: dict[str, RemoteProfile] = {}
    remote_default_profile_id: str | None = None
    for item in raw.get("remote_profiles", []):
        profile = RemoteProfile(
            id=item["id"],
            label=item.get("label"),
            job_name=item["job_name"],
            partition=item["partition"],
            cpus=int(item["cpus"]),
            mem=item["mem"],
            time=item["time"],
            account=item.get("account"),
            gpus=int(item.get("gpus", 0)),
            nodelist=item.get("nodelist"),
            constraint=item.get("constraint"),
            gres_flags=item.get("gres_flags"),
            gpu_cmode=item.get("gpu_cmode"),
            default=bool(item.get("default", False)),
        )
        remote_profiles[profile.id] = profile
        if profile.default and remote_default_profile_id is None:
            remote_default_profile_id = profile.id

    state_path = _expand_path(raw.get("state", {}).get("path", "~/.local/state/sherlock-cli/state.json"))

    return AppConfig(
        repo_root=repo_root,
        config_path=selected_path.resolve(),
        connection=connection,
        presets=presets,
        state_path=state_path,
        remote=remote,
        remote_profiles=remote_profiles,
        remote_default_profile_id=remote_default_profile_id,
        poll_interval_seconds=int(raw.get("watch", {}).get("poll_interval_seconds", 5)),
        startup_timeout_seconds=int(raw.get("watch", {}).get("startup_timeout_seconds", 300)),
    )
