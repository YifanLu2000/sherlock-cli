#!/bin/bash

set -euo pipefail

PORT=$1
NOTEBOOK_DIR=${2:-${HOME}}
cd "$NOTEBOOK_DIR"

if ! command -v jupyter >/dev/null 2>&1; then
    echo "jupyter is not on PATH. Activate your Marlowe environment before submitting, or edit this template." >&2
    exit 1
fi

jupyter lab --no-browser --ip=0.0.0.0 --port="$PORT" --notebook-dir="$NOTEBOOK_DIR"
