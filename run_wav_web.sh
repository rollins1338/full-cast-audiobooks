#!/usr/bin/env bash
# Merge rendered chapter WAVs into one audiobook (WAV + optional M4B).
# Opens a local web UI at http://localhost:8775
#
#   ./run_wav_web.sh
#   ./run_wav_web.sh output/my_book/*.wav
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${FULLCAST_PYTHON:-}" && -x "${FULLCAST_PYTHON}" ]]; then
  PYTHON="${FULLCAST_PYTHON}"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON="$(command -v python3.12)"
else
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" -u wav_web.py "$@"
