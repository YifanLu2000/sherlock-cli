#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${1:-sherlock}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$CLUSTER" in
    sherlock) CONFIG_PATH="$SCRIPT_DIR/sherlock_presets.toml" ;;
    marlowe) CONFIG_PATH="$SCRIPT_DIR/marlowe_presets.toml" ;;
    *)
        echo "Usage: ./install.sh [sherlock|marlowe]"
        exit 1
        ;;
esac

python3 -m pip install -e "$SCRIPT_DIR"
sherlock-cli --config "$CONFIG_PATH" remote setup --full
