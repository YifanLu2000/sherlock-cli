#!/bin/bash
set -e

DIR="${CURSOR_REMOTE_DIR:-$HOME/.cursor-remote/default}"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()")
NODE=$(hostname -s)

echo "$NODE $PORT ${SLURM_JOB_ID:-0}" > "$DIR/session"

SSHD=$(command -v sshd 2>/dev/null || echo "/usr/sbin/sshd")
echo "=== Remote sshd starting on $NODE:$PORT (job ${SLURM_JOB_ID:-?}) ==="

exec "$SSHD" -D -f "$DIR/sshd_config" -p "$PORT" -e
