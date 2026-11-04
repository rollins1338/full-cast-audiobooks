#!/usr/bin/env bash
# Render any plan.json into a full-cast audiobook, one file per chapter.
#
#   ./render.sh PLAN.json [options]
#   ./render.sh                     (asks you to drag the plan into the window)
#   or drop a plan.json onto the "Render Audiobook" desktop icon
#
#   --check              validate the plan, show cast/stats/time estimate, render nothing
#   --audition           render 2 lines per speaker into one file to hear the cast first
#   --chapters 1-3,7     only these chapters
#   --voice "Daniel=Corvin King"   swap a speaker's voice (repeatable); only their lines re-render
#   --out DIR            output folder (default: output/<book name>)
#   --format mp3         mp3 instead of lossless wav
#   --plain              plain log lines instead of the live dashboard (for nohup / logs)
#   -y, --yes            don't ask for confirmation
#
# Safe to stop at any time (Ctrl+C). Run the same command again to resume.
# Crashes restart automatically (up to 5 times) and continue from the last saved
# line; damaged files from a crash or power cut are detected and redone.
# Env: FULLCAST_PYTHON overrides the Python interpreter (default: the first of
#      .venv, ~/bots/dramione/.venv, python3.12, python3 that has the libraries);
#      FULLCAST_RESTARTS / FULLCAST_RESTART_DELAY tune auto-restart (default 5 / 20s).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Use the first interpreter that actually has the libraries (a cheap find_spec
# check, no imports), so an empty venv can't shadow a working one.
has_deps() {
  [[ -x "$1" ]] && "$1" -c 'import importlib.util as u,sys
sys.exit(any(u.find_spec(m) is None for m in ("TTS","torch","rich","soundfile","numpy")))' 2>/dev/null
}
if [[ -n "${FULLCAST_PYTHON:-}" ]]; then
  PY="$FULLCAST_PYTHON"
else
  PY=""
  for cand in "$HERE/.venv/bin/python" "$HOME/bots/dramione/.venv/bin/python" \
              "$(command -v python3.12 2>/dev/null)" "$(command -v python3 2>/dev/null)"; do
    if has_deps "$cand"; then PY="$cand"; break; fi
  done
  # nothing has them: fall back to the first interpreter found so preflight can say what's missing
  if [[ -z "$PY" ]]; then
    for cand in "$HERE/.venv/bin/python" "$(command -v python3.12 2>/dev/null)" "$(command -v python3 2>/dev/null)"; do
      [[ -x "$cand" ]] && { PY="$cand"; break; }
    done
  fi
fi
ENGINE="$HERE/render_engine.py"

# Started from the file manager or a desktop icon (no terminal): reopen in one.
if [[ ! -t 1 && -z "${FULLCAST_HOLD:-}" ]] && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] \
   && { [[ -n "${FULLCAST_GUI:-}" ]] || [[ "$(ps -o comm= "$PPID" 2>/dev/null)" =~ ^(nautilus|gjs|gnome-shell|ding|nemo|dolphin|thunar|caja)$ ]]; }; then
  for term in ptyxis kgx gnome-terminal konsole xfce4-terminal x-terminal-emulator xterm; do
    command -v "$term" >/dev/null || continue
    case "$term" in
      ptyxis)                  exec ptyxis --new-window -- env FULLCAST_HOLD=1 "$0" "$@" ;;
      kgx|gnome-terminal)      exec "$term" -- env FULLCAST_HOLD=1 "$0" "$@" ;;
      *)                       exec "$term" -e env FULLCAST_HOLD=1 "$0" "$@" ;;
    esac
  done
fi
# Keep a window opened by the desktop icon open so the result can be read.
[[ -n "${FULLCAST_HOLD:-}" ]] && trap 'echo; read -r -p "Press Enter to close this window… " _' EXIT

if [[ -t 1 ]]; then
  B=$'\e[1m' D=$'\e[2m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' C=$'\e[36m' N=$'\e[0m'
else
  B="" D="" R="" G="" Y="" C="" N=""
fi
ok()   { printf "  ${G}✓${N} %-14s %s\n" "$1" "$2"; }
warn() { printf "  ${Y}!${N} %-14s %s\n" "$1" "$2"; }
fail() { printf "  ${R}✗${N} %-14s %s\n" "$1" "$2"; FAILED=1; }
usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

# spin LABEL MESSAGE CMD...: run a slow step with a live spinner and timer so the
# screen never looks frozen; the step's stdout ends up in $SPIN_OUT.
spin() {
  local label="$1" msg="$2" out pid start frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
  shift 2
  out="$(mktemp)"
  "$@" >"$out" 2>/dev/null &
  pid=$!
  start=$(date +%s)
  if [[ -t 1 ]]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf "\r  ${C}%s${N} %-14s ${D}%s %ds${N}\e[K" "${frames:i++%10:1}" "$label" "$msg" $(( $(date +%s) - start ))
      sleep 0.1
    done
    printf "\r\e[2K"
  else
    echo "  … $label: $msg"
  fi
  wait "$pid"
  SPIN_CODE=$?
  SPIN_OUT="$(cat "$out")"
  rm -f "$out"
  return $SPIN_CODE
}

# ------------------------------------------------------------ arguments
PLAN="" YES=0 MODE=render ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -y|--yes) YES=1 ;;
    --check) MODE=check; ARGS+=("$1") ;;
    --audition) MODE=audition; ARGS+=("$1") ;;
    --plain) ARGS+=("$1") ;;
    --chapters|--voice|--out|--format|--max-chars|--retries)
      [[ $# -ge 2 ]] || { echo "${R}$1 needs a value${N}"; exit 2; }
      ARGS+=("$1" "$2"); shift ;;
    -*) echo "${R}Unknown option: $1${N}"; usage; exit 2 ;;
    *) [[ -z "$PLAN" ]] && PLAN="$1" || { echo "${R}Only one plan file, got '$PLAN' and '$1'${N}"; exit 2; } ;;
  esac
  shift
done
if [[ -z "$PLAN" && -t 0 ]]; then
  echo
  echo "${B}Drag your plan.json into this window, then press Enter:${N}"
  read -r PLAN
  PLAN="${PLAN#"${PLAN%%[![:space:]]*}"}"; PLAN="${PLAN%"${PLAN##*[![:space:]]}"}"   # trim
  PLAN="${PLAN#file://}"
  [[ "$PLAN" == \'*\' || "$PLAN" == \"*\" ]] && PLAN="${PLAN:1:${#PLAN}-2}"
  PLAN="$(printf '%b' "${PLAN//%/\\x}")"   # undo %20-style escapes from some file managers
fi
[[ -n "$PLAN" ]] || { usage; exit 2; }

# ------------------------------------------------------------ preflight
FAILED=0
echo
echo "${B}Full Cast Audiobook renderer${N}  ${D}$(date '+%a %d %b %H:%M')${N}"
echo "${D}────────────────────────────────────────────────────────────${N}"

if [[ -f "$PLAN" ]]; then
  ok "plan" "$(realpath "$PLAN")  ${D}($(du -h "$PLAN" | cut -f1))${N}"
else
  fail "plan" "not found: $PLAN"
fi

CUDA=""
if [[ -x "$PY" ]]; then
  ok "python" "$PY ${D}($("$PY" -c 'import sys;print(sys.version.split()[0])'))${N}"
  # one import of the heavy libraries (torch alone takes 10-30s cold) answers both
  # "is everything installed" and "can torch see the GPU"
  if spin "libraries" "loading torch + coqui-tts (first start can take ~30s)…" "$PY" -c '
import TTS, torch, rich, soundfile, numpy
print(f"coqui-tts {TTS.__version__}, torch {torch.__version__}")
print("cuda" if torch.cuda.is_available() else "cpu")'; then
    ok "libraries" "$(head -1 <<<"$SPIN_OUT")"
    CUDA="$(sed -n 2p <<<"$SPIN_OUT")"
  else
    fail "libraries" "missing TTS/torch/rich/soundfile/numpy in that environment"
  fi
else
  fail "python" "not found: $PY (set FULLCAST_PYTHON)"
fi

if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  ok "ffmpeg" "$(ffmpeg -version | head -1 | cut -d' ' -f1-3)"
else
  fail "ffmpeg" "ffmpeg/ffprobe not on PATH"
fi

if command -v nvidia-smi >/dev/null && GPU="$(nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu,driver_version --format=csv,noheader,nounits 2>/dev/null | head -1)" && [[ -n "$GPU" ]]; then
  IFS=',' read -r GNAME GUSED GTOTAL GTEMP GDRV <<<"$GPU"
  ok "gpu" "${GNAME# }  ${D}VRAM ${GUSED# }/${GTOTAL# } MB · ${GTEMP# }°C · driver ${GDRV# }${N}"
  FREE_MB=$(( ${GTOTAL// /} - ${GUSED// /} ))
  (( FREE_MB < 2500 )) && warn "vram" "only ${FREE_MB} MB free; XTTS wants ~2.5 GB. Close other GPU apps."
  [[ "$CUDA" == cpu ]] && warn "cuda" "torch can't see the GPU: rendering will run on CPU (very slow)"
else
  warn "gpu" "no NVIDIA GPU detected: rendering will run on CPU (very slow)"
fi

MODEL_DIR="$HOME/.local/share/tts/tts_models--multilingual--multi-dataset--xtts_v2"
if [[ -d "$MODEL_DIR" ]]; then
  ok "model" "XTTS-v2 cached ${D}($(du -sh "$MODEL_DIR" 2>/dev/null | cut -f1))${N}"
else
  warn "model" "XTTS-v2 not downloaded yet: first run downloads ~1.8 GB"
fi

# A laptop that is already hot throttles both CPU and GPU, and that sets the render speed
CPU_TEMP=$(for h in /sys/class/hwmon/hwmon*; do [[ "$(cat "$h/name" 2>/dev/null)" == coretemp ]] && cat "$h/temp1_input" 2>/dev/null; done | head -1)
if [[ -n "$CPU_TEMP" ]]; then
  CPU_TEMP=$(( CPU_TEMP / 1000 ))
  if (( CPU_TEMP >= 85 )); then
    warn "heat" "CPU already at ${CPU_TEMP}°C: the laptop will throttle and render slower. Clear the vents / raise it / use a cooling pad."
  else
    ok "heat" "CPU ${CPU_TEMP}°C ${D}($(powerprofilesctl get 2>/dev/null || echo '?') power mode)${N}"
  fi
fi

DISK_FREE=$(df -Pk "$HERE" | awk 'NR==2{print int($4/1024/1024)}')
(( DISK_FREE < 5 )) && fail "disk" "only ${DISK_FREE} GB free" || ok "disk" "${DISK_FREE} GB free"

if (( FAILED )); then
  echo; echo "${R}${B}Preflight failed.${N} Fix the items marked ✗ above."; exit 1
fi

# ------------------------------------------------------------ plan check
echo
CHECK_ARGS=()
for a in "${ARGS[@]}"; do [[ "$a" == --check || "$a" == --audition ]] || CHECK_ARGS+=("$a"); done
echo "  ${D}reading the plan, fingerprinting voices and checking what's already rendered…${N}"
"$PY" "$ENGINE" "$PLAN" --check "${CHECK_ARGS[@]}" || { echo "${R}${B}Plan check failed.${N}"; exit 1; }
[[ "$MODE" == check ]] && exit 0

if (( ! YES )) && [[ -t 0 ]]; then
  echo
  if [[ "$MODE" == render ]]; then
    echo "${B}What now?${N}  ${C}Enter${N} render the book   ${C}a${N} audition the cast first (2 lines each)   ${C}n${N} cancel"
    read -r -p "> " REPLY
    case "$REPLY" in
      ""|[Yy]*|[Rr]*) ;;
      [Aa]*) MODE=audition; ARGS+=(--audition) ;;
      *) echo "Cancelled."; exit 0 ;;
    esac
  else
    read -r -p "${B}Start ${MODE}? [Y/n] ${N}" REPLY
    [[ -z "$REPLY" || "$REPLY" =~ ^[Yy] ]] || { echo "Cancelled."; exit 0; }
  fi
fi

# ------------------------------------------------------------ render
export COQUI_TOS_AGREED=1 PYTHONUNBUFFERED=1
RUN=("$PY" "$ENGINE" "$PLAN" "${ARGS[@]}")
if command -v systemd-inhibit >/dev/null; then
  # keep the laptop from sleeping mid-book; released automatically on exit
  RUN=(systemd-inhibit --what=sleep:idle --who="Full Cast renderer" --why="Rendering audiobook" "${RUN[@]}")
fi

# Crash recovery: a crashed renderer (CUDA error, out of memory, killed) is
# restarted automatically and resumes from its last saved chunk. It stops
# when a crash keeps repeating without finishing any new chunk.
MAX_RESTARTS="${FULLCAST_RESTARTS:-5}"
RESTART_DELAY="${FULLCAST_RESTART_DELAY:-20}"
STATUS_FILE="$(mktemp -t fullcast-status.XXXXXX)"
export FULLCAST_STATUS_FILE="$STATUS_FILE"
status() { "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$STATUS_FILE" "$1" 2>/dev/null; }

START=$(date +%s)
RESTARTS=0 NO_PROGRESS=0 LAST_DONE=""
while :; do
  "${RUN[@]}"
  CODE=$?
  case $CODE in 0|2|130) break ;; esac          # finished / plan error / paused by you

  DONE="$(status done)" TOTAL="$(status total)" OUT="$(status out)"
  [[ -n "$DONE" && "$DONE" == "$LAST_DONE" ]] && NO_PROGRESS=$((NO_PROGRESS + 1)) || NO_PROGRESS=0
  LAST_DONE="$DONE"
  echo
  echo "${R}${B}Renderer crashed${N} (exit $CODE) with ${DONE:-?}/${TOTAL:-?} chunks saved."
  [[ -n "$OUT" && -f "$OUT/render.log" ]] && { echo "${D}Last log lines:${N}"; tail -n 5 "$OUT/render.log" | sed 's/^/  /'; }
  if (( NO_PROGRESS >= 2 )); then
    echo "${R}It crashed twice in a row without finishing a single new chunk; stopping so it doesn't loop forever.${N}"
    break
  fi
  if (( RESTARTS >= MAX_RESTARTS )); then
    echo "${R}Gave up after $RESTARTS automatic restarts.${N}"
    break
  fi
  RESTARTS=$((RESTARTS + 1))
  for (( s = RESTART_DELAY; s > 0; s-- )); do
    printf "\r${Y}Restarting automatically in %2ds${N} (restart %d of %d) · Ctrl+C to stop " "$s" "$RESTARTS" "$MAX_RESTARTS"
    sleep 1
  done
  printf "\r\e[2K${Y}Restarting (%d of %d)…${N}\n" "$RESTARTS" "$MAX_RESTARTS"
done
rm -f "$STATUS_FILE"

TOOK=$(( $(date +%s) - START ))
printf "\n${D}session time %dh%02dm%02ds%s${N}\n" $((TOOK/3600)) $((TOOK%3600/60)) $((TOOK%60)) \
  "$( (( RESTARTS )) && echo " · recovered from $RESTARTS crash(es)")"

case $CODE in
  0)   echo "${G}${B}Finished.${N}" ;;
  130) echo "${Y}${B}Paused.${N} Resume with:  ${C}$0 \"$PLAN\" ${ARGS[*]} -y${N}" ;;
  2)   echo "${R}${B}Plan problem${N}: see the message above." ;;
  *)   echo "${R}${B}Stopped after a crash (exit $CODE).${N} Everything rendered so far is saved;"
       echo "re-run the same command to resume from the last finished chunk." ;;
esac
exit $CODE
