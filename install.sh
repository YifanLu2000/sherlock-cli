#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${1:-sherlock}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$CLUSTER" in
    sherlock) CONFIG_PATH="$HOME/.config/sherlock-cli/sherlock.toml" ;;
    marlowe) CONFIG_PATH="$HOME/.config/sherlock-cli/marlowe.toml" ;;
    *)
        echo "Usage: ./install.sh [sherlock|marlowe]"
        exit 1
        ;;
esac

python3 -m pip install -e "$SCRIPT_DIR"
sherlock-cli setup "$CLUSTER"
sherlock-cli --config "$CONFIG_PATH" remote setup --full
