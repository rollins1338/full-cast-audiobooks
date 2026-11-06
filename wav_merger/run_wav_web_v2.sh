#!/usr/bin/env bash
# WAV Forge v2: the same merger plus XTTS cleanup and voice blend from the render folder.
# Opens a local web UI at http://localhost:8776
#
#   ./wav_merger/run_wav_web_v2.sh
#   ./wav_merger/run_wav_web_v2.sh output/my_book/*.wav
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${FULLCAST_PYTHON:-}" && -x "${FULLCAST_PYTHON}" ]]; then
  PYTHON="${FULLCAST_PYTHON}"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON="$(command -v python3.12)"
else
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" -u "$SCRIPT_DIR/wav_web_v2.py" "$@"
