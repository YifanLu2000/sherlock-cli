#!/bin/bash

set -euo pipefail

PORT=$1
NOTEBOOK_DIR=${2:-${HOME}}

source /projects/m000134/LuYifan/miniconda3/etc/profile.d/conda.sh
conda activate /projects/m000134/LuYifan/miniconda3

cd "$NOTEBOOK_DIR"

exec python -m jupyterlab --no-browser --ip=0.0.0.0 --port="$PORT" --notebook-dir="$NOTEBOOK_DIR"
