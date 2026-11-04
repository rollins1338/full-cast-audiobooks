#!/usr/bin/env bash
# Merge rendered chapter WAVs into one audiobook (WAV + optional M4B).
# Opens a local web UI at http://localhost:8775
#
#   ./wav_merger/run_wav_web.sh
#   ./wav_merger/run_wav_web.sh output/my_book/*.wav
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

exec "$PYTHON" -u "$SCRIPT_DIR/wav_web.py" "$@"
