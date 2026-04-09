from __future__ import annotations

import sys

from .cli import main
from .config import resolve_resource_config_path


def _forward(resource: str) -> int:
    config_path = resolve_resource_config_path(resource)
    return main(["--config", str(config_path), "remote", *sys.argv[1:]])


def sherlock_compute_main() -> int:
    return _forward("sherlock")


def marlowe_compute_main() -> int:
    return _forward("marlowe")
