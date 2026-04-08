from __future__ import annotations

import sys

from .cli import main
from .config import _repo_root


def _forward(config_name: str) -> int:
    config_path = _repo_root() / config_name
    return main(["--config", str(config_path), "remote", *sys.argv[1:]])


def sherlock_compute_main() -> int:
    return _forward("sherlock_presets.toml")


def marlowe_compute_main() -> int:
    return _forward("marlowe_presets.toml")
