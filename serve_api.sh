#!/usr/bin/env bash
set -euo pipefail

# Non-interactive shells do not have `conda` on PATH until this is sourced.
if [ -f /root/miniconda3/etc/profile.d/conda.sh ]; then
  # shellcheck source=/dev/null
  source /root/miniconda3/etc/profile.d/conda.sh
else
  echo "Miniconda not found at /root/miniconda3" >&2
  exit 1
fi

conda activate biomni_e1
# shellcheck source=/dev/null
source /root/work/Biomni/biomni_env/setup_path.sh
hash -r

cd /root/work/Biomni

HOST="${BIOMNI_API_HOST:-0.0.0.0}"
PORT="${BIOMNI_API_PORT:-7861}"

exec python -m uvicorn biomni.server.api:app --host "$HOST" --port "$PORT"
