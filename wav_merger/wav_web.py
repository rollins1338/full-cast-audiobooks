import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wav_merge_engine import do_merge, export_m4b, get_wav_info, secs_to_hms


BASE_DIR = Path(__file__).resolve().parent
HOST = "localhost"
PORT = 8775
UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="wav-forge-uploads-"))

LOCK = threading.Lock()
STATE = {
    "files": [],
    "logs": [],
    "progress": 0,
    "m4b_progress": 0,
    "progress_detail": "",
    "m4b_detail": "",
    "status": "Ready",
    "running": False,
    "exit_code": None,
    "output": "",
    "last_wav": "",
    "wav_ready": False,
    "m4b_output": "",
    "cancel": None,
}


def add_log(message, tag=None):
    with LOCK:
        STATE["logs"].append({"time": time.strftime("%H:%M:%S"), "message": str(message), "tag": tag or ""})
        if len(STATE["logs"]) > 2500:
            STATE["logs"] = STATE["logs"][-2000:]


def set_status(status):
    with LOCK:
        STATE["status"] = status


def format_progress_detail(elapsed=None, total=None, speed=None, size_mb=None):
    parts = []
    if elapsed is not None and total:
        parts.append(f"{secs_to_hms(elapsed)} / {secs_to_hms(total)}")
    if speed:
        parts.append(f"{speed:.1f}x")
    if size_mb is not None:
        parts.append(f"{size_mb:,.0f} MB")
    if elapsed and speed and speed > 0 and total and elapsed < total:
        parts.append(f"ETA {secs_to_hms((total - elapsed) / speed)}")
    return " · ".join(parts)


def file_payload(info, index):
    return {
        "index": index,
        "name": info["name"],
        "path": info["path"],
        "duration": info.get("dur_str") or secs_to_hms(info.get("duration", 0)),
        "seconds": info.get("duration", 0),
        "sampleRate": info.get("framerate", 0),
        "channels": "Stereo" if info.get("channels") == 2 else ("Mono" if info.get("channels") == 1 else f"{info.get('channels', 0)}ch"),
        "bits": info.get("sampwidth", 0) * 8,
        "size": info.get("size", 0),
        "sizeText": f"{info.get('size', 0) / (1024 * 1024):.1f} MB",
    }


def output_label(path):
    name = os.path.basename(str(path or ""))
    if name.lower().endswith(".wav"):
        return name[:-4]
    return name


def linux_open_path(path):
    opener = shutil.which("xdg-open")
    if not opener:
        raise RuntimeError("xdg-open is not available on this system.")
    subprocess.Popen(
        [opener, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=quiet_gui_env(),
    )


def should_open_browser():
    if "--no-browser" in sys.argv:
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and bool(shutil.which("xdg-open"))
    return True


def quiet_gui_env():
    env = os.environ.copy()
    if sys.platform.startswith("linux"):
        env.setdefault("NO_AT_BRIDGE", "1")
        env.setdefault("GTK_A11Y", "none")
        gtk_modules = env.get("GTK_MODULES", "")
        if gtk_modules:
            kept = [item for item in gtk_modules.split(":") if item and item != "atk-bridge"]
            if kept:
                env["GTK_MODULES"] = ":".join(kept)
            else:
                env.pop("GTK_MODULES", None)
    return env


def open_url_quietly(url):
    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        try:
            subprocess.Popen(
                [opener, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=quiet_gui_env(),
            )
            return True
        except Exception:
            return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def snapshot():
    with LOCK:
        files = list(STATE["files"])
        total_seconds = sum(item.get("duration", 0) for item in files)
        total_size = sum(item.get("size", 0) for item in files)
        output = STATE["output"]
        return {
            "files": [file_payload(info, i) for i, info in enumerate(files)],
            "count": len(files),
            "duration": secs_to_hms(total_seconds),
            "size": f"{total_size / (1024 * 1024):.1f} MB",
            "progress": STATE["progress"],
            "m4bProgress": STATE["m4b_progress"],
            "progressDetail": STATE["progress_detail"],
            "m4bDetail": STATE["m4b_detail"],
            "status": STATE["status"],
            "running": STATE["running"],
            "exitCode": STATE["exit_code"],
            "output": output,
            "outputName": os.path.basename(output) if output else "",
            "outputLabel": output_label(output),
            "lastWav": STATE["last_wav"],
            "wavReady": STATE["wav_ready"],
            "m4bOutput": STATE["m4b_output"],
        }


def resolve_output_path(value, files, current_output=""):
    value = str(value or "").strip().strip('"')
    if not value:
        value = current_output

    if value and (os.path.isabs(value) or os.path.dirname(value)):
        path = Path(value)
    else:
        if current_output:
            folder = Path(current_output).parent
        elif files:
            folder = Path(files[0]["path"]).parent
        else:
            folder = BASE_DIR
        path = folder / (value or "merged_output.wav")

    if not path.suffix:
        path = path.with_suffix(".wav")
    return str(path)


def add_files(paths):
    added = 0
    skipped = []
    with LOCK:
        existing = {info["path"] for info in STATE["files"]}
    for raw in paths:
        path = os.path.abspath(raw)
        if not path.lower().endswith(".wav"):
            skipped.append((path, "not a WAV"))
            continue
        if path in existing:
            skipped.append((path, "already added"))
            continue
        info = get_wav_info(path)
        if info.get("error"):
            skipped.append((path, info["error"]))
            continue
        with LOCK:
            STATE["files"].append(info)
        existing.add(path)
        added += 1
    if added:
        add_log(f"Added {added} file(s).", "success")
        with LOCK:
            if not STATE["output"] and STATE["files"]:
                first = STATE["files"][0]["path"]
                STATE["output"] = os.path.join(os.path.dirname(first), "merged_output.wav")
    for path, reason in skipped:
        add_log(f"Skipped {os.path.basename(path)}: {reason}", "warning")


def json_response(handler, payload, status=200):
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def read_multipart_files(handler):
    """Stream browser-uploaded files to disk without buffering the whole request."""
    content_type = handler.headers.get("Content-Type", "")
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type, re.IGNORECASE)
    if not match:
        raise ValueError("Upload is missing its multipart boundary.")
    boundary = (match.group(1) or match.group(2)).encode("utf-8")
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return []
    marker = b"\r\n--" + boundary
    pending = bytearray()
    remaining = length

    def read_some(size=1024 * 1024):
        nonlocal remaining
        if remaining <= 0:
            return b""
        chunk = handler.rfile.read(min(size, remaining))
        remaining -= len(chunk)
        return chunk

    def read_line():
        while b"\n" not in pending:
            chunk = read_some(64 * 1024)
            if not chunk:
                raise ValueError("Incomplete file upload.")
            pending.extend(chunk)
        end = pending.index(b"\n") + 1
        line = bytes(pending[:end])
        del pending[:end]
        return line

    def consume_part(target):
        """Copy a part body until its multipart boundary; return whether it was final."""
        while True:
            boundary_at = pending.find(marker)
            if boundary_at >= 0:
                if target:
                    target.write(pending[:boundary_at])
                del pending[:boundary_at + len(marker)]
                while len(pending) < 2:
                    chunk = read_some(64 * 1024)
                    if not chunk:
                        raise ValueError("Incomplete file upload.")
                    pending.extend(chunk)
                suffix = bytes(pending[:2])
                del pending[:2]
                if suffix == b"--":
                    if pending.startswith(b"\r\n"):
                        del pending[:2]
                    return True
                if suffix != b"\r\n":
                    raise ValueError("Malformed multipart upload boundary.")
                return False
            keep = len(marker)
            if len(pending) > keep:
                if target:
                    target.write(pending[:-keep])
                del pending[:-keep]
            chunk = read_some()
            if not chunk:
                raise ValueError("Incomplete file upload.")
            pending.extend(chunk)

    opening = b"--" + boundary
    while True:
        line = read_line()
        if line.rstrip(b"\r\n") == opening:
            break
        if line.rstrip(b"\r\n") == opening + b"--":
            return []

    paths = []
    batch_dir = UPLOAD_DIR / uuid.uuid4().hex
    while True:
        header_lines = []
        while True:
            line = read_line()
            if line in (b"\r\n", b"\n"):
                break
            header_lines.append(line)
        header_bytes = b"".join(header_lines)
        disposition = re.search(rb"(?:^|;)\s*filename\*=UTF-8''([^;\r\n]+)", header_bytes, re.IGNORECASE)
        if not disposition:
            disposition = re.search(rb"(?:^|;)\s*filename=\"([^\"]*)\"", header_bytes, re.IGNORECASE)
        if not disposition:
            disposition = re.search(rb"(?:^|;)\s*filename=([^;\r\n]+)", header_bytes, re.IGNORECASE)
        destination = None
        if disposition:
            raw_name = disposition.group(1).decode("utf-8", errors="replace")
            name = Path(raw_name).name
            if name in {"", ".", ".."}:
                name = "upload.wav"
            if name.lower().endswith(".wav"):
                batch_dir.mkdir(parents=True, exist_ok=True)
                destination = batch_dir / name
                if destination.exists():
                    destination = batch_dir / f"{destination.stem}-{uuid.uuid4().hex[:8]}{destination.suffix}"
        try:
            if destination:
                with destination.open("wb") as target:
                    final = consume_part(target)
                paths.append(str(destination))
            else:
                final = consume_part(None)
        except Exception:
            if destination:
                destination.unlink(missing_ok=True)
            raise
        if final:
            break
    return paths


def run_file_dialog(kind, initial=""):
    picker_code = r"""
import json
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

kind = sys.argv[1]
initial = sys.argv[2] if len(sys.argv) > 2 else ""
initial_path = Path(initial) if initial else Path.cwd()
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
try:
    if kind == "wav_files":
        result = list(filedialog.askopenfilenames(
            title="Select WAV files",
            initialdir=str(initial_path if initial_path.is_dir() else initial_path.parent),
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")]
        ))
    elif kind == "wav_folder":
        result = filedialog.askdirectory(
            title="Select folder containing WAV files",
            initialdir=str(initial_path if initial_path.is_dir() else initial_path.parent),
            mustexist=True,
        )
    elif kind == "save_wav":
        result = filedialog.asksaveasfilename(
            title="Save merged output as",
            initialdir=str(initial_path.parent if initial_path.suffix else initial_path),
            initialfile=initial_path.name if initial_path.suffix else "merged_output.wav",
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav")]
        )
    else:
        result = ""
    print(json.dumps(result))
finally:
    root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", picker_code, kind, str(initial or BASE_DIR)],
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Could not open file picker.")
    output = proc.stdout.strip()
    return json.loads(output) if output else ([] if kind == "wav_files" else "")


def wav_files_in_folder(folder):
    """Return WAV files directly inside *folder*, in natural filename order."""
    path = Path(folder)
    if not path.is_dir():
        raise ValueError(f"Not a folder: {folder}")

    def natural_key(item):
        return [int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", item.name)]

    return [str(item) for item in sorted(path.iterdir(), key=natural_key)
            if item.is_file() and item.suffix.casefold() == ".wav"]


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WAV Forge</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23171916'/%3E%3Cpath d='M12 36h7l4-15 8 30 7-24 5 9h9' fill='none' stroke='%2383e6ad' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpath d='M14 22h36M14 48h36' stroke='%238fb8ff' stroke-width='5' stroke-linecap='round' opacity='.9'/%3E%3C/svg%3E">
<style>
:root {
  color-scheme: dark;
  --bg: #0e0f0d;
  --panel: #171916;
  --panel2: #1f221e;
  --line: #30352f;
  --text: #f3f1e9;
  --muted: #a8aa9e;
  --green: #83e6ad;
  --blue: #8fb8ff;
  --amber: #f0ca72;
  --red: #ff8378;
  --input: #0a0b0a;
  --shadow: 0 18px 58px rgba(0,0,0,.34);
}
* { box-sizing: border-box; }
html, body { min-height: 100%; overflow-x: hidden; overflow-y: auto; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 13px/1.4 "Noto Sans", "DejaVu Sans", sans-serif;
}
button, input, select { font: inherit; }
button {
  border: 0;
  min-height: 34px;
  padding: 0 12px;
  border-radius: 7px;
  background: var(--panel2);
  color: var(--text);
  cursor: pointer;
}
button:hover { background: #2a2e28; }
button.primary { background: linear-gradient(135deg, var(--green), var(--blue)); color: #07100b; font-weight: 800; }
button.primary:hover { background: linear-gradient(135deg, #98efbd, #a5c8ff); }
button.danger { background: var(--red); color: #150605; font-weight: 800; }
button.success { background: var(--blue); color: #06101f; font-weight: 800; }
button:disabled { opacity: .45; cursor: not-allowed; }
input, select {
  width: 100%;
  min-height: 34px;
  padding: 0 10px;
  border-radius: 7px;
  border: 1px solid var(--line);
  background: var(--input);
  color: var(--text);
  outline: 0;
}
input:focus, select:focus { border-color: var(--green); }
.app {
  width: min(1460px, calc(100vw - 24px));
  min-height: 100vh;
  margin: 0 auto;
  padding: 10px 0;
  display: grid;
  grid-template-rows: auto auto auto 1fr;
  gap: 10px;
  min-height: 0;
}
.top {
  display: flex;
  justify-content: space-between;
  align-items: end;
  gap: 16px;
}
h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
.sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.pill {
  border: 1px solid var(--line);
  border-radius: 999px;
  min-height: 30px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  color: var(--muted);
}
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 10px; }
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  box-shadow: var(--shadow);
}
.metric span { display: block; color: var(--muted); font-size: 12px; }
.metric strong { display: block; font-size: 21px; margin-top: 1px; }
.progress-panel { padding: 10px 12px; }
.progress-top {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
}
.progress-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 5px;
}
.progress-title span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 800;
}
.progress-title strong { color: var(--green); font-size: 18px; }
.progress-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  color: var(--muted);
  font-size: 12px;
}
.progress-stats b { color: var(--text); font-weight: 800; }
.progress-stats em {
  color: var(--muted);
  font-style: normal;
  margin-left: 4px;
}
.run-actions {
  display: grid;
  grid-template-columns: 180px 120px 140px;
  gap: 8px;
}
.run-actions .primary { min-height: 42px; font-size: 15px; }
.main {
  display: grid;
  grid-template-columns: 390px minmax(0, 1fr) 390px;
  gap: 12px;
  min-height: 0;
  overflow: hidden;
}
.center-stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  max-width: 100%;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}
.side { padding: 12px; overflow: hidden; }
.section + .section { margin-top: 12px; }
.section-title {
  color: var(--muted);
  font-size: 11px;
  letter-spacing: .08em;
  text-transform: uppercase;
  margin-bottom: 7px;
}
.drop-zone {
  min-height: 82px;
  margin-top: 9px;
  padding: 12px;
  border: 1px dashed #4d5b4d;
  border-radius: 8px;
  background: rgba(131,230,173,.04);
  color: var(--muted);
  text-align: center;
  cursor: pointer;
  transition: .15s ease;
}
.drop-zone strong { display: block; color: var(--text); font-size: 12px; }
.drop-zone span { display: block; margin-top: 3px; font-size: 11px; }
.drop-zone.drag-over {
  border-color: var(--green);
  background: rgba(131,230,173,.14);
  color: var(--text);
  box-shadow: 0 0 0 2px rgba(131,230,173,.12);
}
.field { margin-bottom: 8px; }
.field label { display: block; color: var(--muted); font-size: 11px; margin-bottom: 3px; }
.path-row { display: grid; grid-template-columns: 1fr 42px; gap: 8px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.checks { display: grid; gap: 7px; }
.check {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 28px;
  color: var(--text);
}
.check input { width: auto; min-height: auto; accent-color: var(--green); }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
.progress-wrap {
  height: 12px;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 999px;
  overflow: hidden;
  margin-top: 8px;
}
.progress { height: 100%; width: 0%; background: linear-gradient(90deg, var(--green), var(--blue)); transition: width .2s ease; }
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  background: var(--panel2);
  border-bottom: 1px solid var(--line);
}
.card-head h2 { margin: 0; font-size: 14px; }
.card-head p { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
.table-panel, .log-panel { display: flex; flex-direction: column; align-self: start; }
.table-panel { height: 360px; }
.log-panel { height: 290px; }
.advanced-panel { padding-bottom: 4px; }
.advanced-panel summary.card-head {
  cursor: pointer;
  list-style: none;
}
.advanced-panel summary.card-head::-webkit-details-marker { display: none; }
.advanced-panel summary.card-head::after {
  content: "Expand";
  color: var(--green);
  font-size: 12px;
  font-weight: 800;
}
.advanced-panel[open] summary.card-head::after { content: "Collapse"; }
.advanced-panel .advanced-body { padding: 10px 12px 8px; margin-top: 0; }
.table-wrap { flex: 1; min-height: 0; min-width: 0; overflow: auto; }
table { width: 100%; max-width: 100%; table-layout: fixed; border-collapse: collapse; }
th, td {
  padding: 8px 12px;
  border-bottom: 1px solid rgba(255,255,255,.05);
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
th { position: sticky; top: 0; background: #131511; color: var(--muted); font-size: 12px; z-index: 1; }
td:nth-child(1), th:nth-child(1) { width: 48px; }
td:nth-child(3), th:nth-child(3) { width: 74px; }
td:nth-child(4), th:nth-child(4) { width: 68px; }
td:nth-child(5), th:nth-child(5) { width: 58px; }
td:nth-child(6), th:nth-child(6) { width: 50px; }
td:nth-child(7), th:nth-child(7) { width: 64px; }
td:last-child, th:last-child { text-align: right; }
.file-name { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.selected { background: rgba(143,184,255,.16); }
.log {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 10px;
  background: #090a09;
  color: #d7dbd2;
  font: 12px/1.55 "JetBrains Mono", "DejaVu Sans Mono", monospace;
  white-space: pre-wrap;
}
.log .success { color: var(--green); }
.log .warning { color: var(--amber); }
.log .error { color: var(--red); }
.log .muted { color: var(--muted); }
.advanced-body {
  max-height: none;
  overflow-y: visible;
  overflow-x: hidden;
  margin-top: 8px;
}
.advanced-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 8px;
}
.toast {
  position: fixed;
  left: 50%;
  bottom: 20px;
  transform: translateX(-50%) translateY(18px);
  opacity: 0;
  transition: .2s ease;
  pointer-events: none;
  background: var(--text);
  color: #111;
  border-radius: 7px;
  padding: 10px 14px;
  font-weight: 700;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
@media (max-width: 1160px) {
  .app { height: auto; }
  .main { grid-template-columns: 1fr; }
  .center-stack { min-width: 0; }
  .progress-top { grid-template-columns: 1fr; }
  .run-actions { grid-template-columns: 1fr 1fr; }
  .run-actions .primary { grid-column: 1 / -1; }
  .table-panel { height: 320px; }
  .log-panel { height: 240px; }
}
</style>
</head>
<body>
<main class="app">
  <header class="top">
    <div>
      <h1>WAV Forge</h1>
      <div class="sub">Merge, master, reorder, and export chaptered audio without the clutter.</div>
    </div>
    <div class="pill" id="status">Ready</div>
  </header>

  <section class="metrics">
    <div class="metric"><span>Files</span><strong id="mCount">0</strong></div>
    <div class="metric"><span>Duration</span><strong id="mDuration">0:00</strong></div>
    <div class="metric"><span>Total Size</span><strong id="mSize">0 MB</strong></div>
    <div class="metric"><span>Output</span><strong id="mOutput">WAV</strong></div>
  </section>

  <section class="panel progress-panel">
    <div class="progress-top">
      <div>
        <div class="section-title">Progress</div>
        <div class="progress-title"><span id="progressStage">Ready</span><strong id="progressPct">0%</strong></div>
        <div class="progress-stats">
          <span>Merge <b id="mergePct">0%</b><em id="mergeDetail"></em></span>
          <span>M4B <b id="m4bPct">0%</b><em id="m4bDetail"></em></span>
        </div>
      </div>
      <div class="run-actions">
        <button class="primary" id="mergeBtn">Merge Audio</button>
        <button class="danger" id="stopBtn" disabled>Stop</button>
        <button class="success" id="openBtn">Open Output</button>
      </div>
    </div>
    <div class="progress-wrap"><div class="progress" id="progress"></div></div>
  </section>

  <section class="main">
    <aside class="panel side">
      <div class="section">
        <div class="section-title">Files</div>
        <div class="drop-zone" id="dropZone" role="button" tabindex="0" aria-label="Drop WAV files here or browse">
          <strong>Drop WAV files here</strong>
          <span>or click to browse from your computer</span>
        </div>
        <input id="fileInput" type="file" accept=".wav,audio/wav,audio/x-wav" multiple hidden>
        <div class="actions">
          <button class="primary" id="addBtn">Add WAVs</button>
          <button id="addFolderBtn">Add Folder</button>
          <button class="danger" id="removeBtn">Remove</button>
        </div>
        <div class="actions">
          <button id="upBtn">Up</button>
          <button id="downBtn">Down</button>
        </div>
        <div class="actions">
          <button id="sortBtn">Sort</button>
          <button id="shuffleBtn">Shuffle</button>
        </div>
        <button id="clearBtn" style="width:100%; margin-top:8px">Clear List</button>
      </div>

      <div class="section">
        <div class="section-title">Output</div>
        <div class="field"><label>Save as</label><div class="path-row"><input id="output"><button id="outBtn">...</button></div></div>
        <div class="grid2">
          <div class="field"><label>Sample rate</label><select id="sampleRate"></select></div>
          <div class="field"><label>Bit depth</label><select id="bitDepth"></select></div>
        </div>
        <div class="field"><label>Channels</label><select id="channels"><option value="2">Stereo</option><option value="1">Mono</option></select></div>
      </div>

      <div class="section">
        <div class="section-title">Engine</div>
        <div class="field"><label>Merge engine</label><input value="FFmpeg Stream" disabled></div>
        <div class="grid2">
          <div class="field"><label>Speed</label><input id="speed" type="number" min="0.5" max="2" step="0.05"></div>
          <div class="field"><label>Gain dB</label><input id="gain" type="number" min="-40" max="40" step="0.5"></div>
        </div>
        <div class="checks">
          <label class="check"><input id="autoConvert" type="checkbox"> Auto-convert formats</label>
          <label class="check"><input id="acx" type="checkbox"> ACX mastering</label>
          <label class="check"><input id="m4b" type="checkbox"> Export chaptered M4B</label>
        </div>
        <div class="field" style="margin-top:8px"><label>M4B bitrate</label><select id="bitrate"><option>32</option><option>48</option><option selected>64</option><option>96</option><option>128</option><option>192</option></select></div>
      </div>

    </aside>

    <section class="center-stack">
      <section class="panel table-panel">
        <div class="card-head">
          <div><h2>Track List</h2><p id="summary">No files loaded.</p></div>
          <button id="refreshBtn">Refresh</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Name</th><th>Duration</th><th>Rate</th><th>Ch</th><th>Bits</th><th>Size</th></tr></thead>
            <tbody id="rows"></tbody>
          </table>
        </div>
      </section>

      <details class="panel advanced-panel">
        <summary class="card-head">
          <div><h2>Advanced Processing</h2><p>Optional finishing controls</p></div>
        </summary>
        <div class="advanced-body">
          <div class="checks">
            <label class="check"><input id="normalizeMaster" type="checkbox"> Normalize final master</label>
            <label class="check"><input id="trimSilence" type="checkbox"> Trim leading/trailing silence</label>
            <label class="check"><input id="fadeIn" type="checkbox"> Fade in</label>
            <label class="check"><input id="fadeOut" type="checkbox"> Fade out</label>
            <label class="check"><input id="gap" type="checkbox"> Silence gap</label>
            <label class="check"><input id="crossfade" type="checkbox"> Crossfade</label>
          </div>
          <div class="advanced-grid">
            <div class="field"><label>Headroom dB</label><input id="headroom" type="number" step="0.5"></div>
            <div class="field"><label>Trim dB</label><input id="trimThreshold" type="number" step="1"></div>
            <div class="field"><label>Fade in ms</label><input id="fadeInMs" type="number" step="100"></div>
            <div class="field"><label>Fade out ms</label><input id="fadeOutMs" type="number" step="100"></div>
            <div class="field"><label>Gap ms</label><input id="gapMs" type="number" step="100"></div>
            <div class="field"><label>Crossfade ms</label><input id="crossfadeMs" type="number" step="100"></div>
          </div>
        </div>
      </details>
    </section>

    <section class="panel log-panel">
      <div class="card-head">
        <div><h2>Activity</h2><p id="stage">Idle.</p></div>
        <button id="clearLogBtn">Clear</button>
      </div>
      <div class="log" id="log"></div>
    </section>
  </section>

</main>
<div class="toast" id="toast"></div>
<script>
let selected = new Set();
let logCursor = 0;
let announcedWavReady = false;
const $ = id => document.getElementById(id);

function toast(msg) {
  $("toast").textContent = msg;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 1800);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || "Request failed");
  return data;
}

function config() {
  return {
    output: $("output").value,
    sampleRate: Number($("sampleRate").value),
    channels: Number($("channels").value),
    bitDepth: Number($("bitDepth").value),
    speed: Number($("speed").value || 1),
    gain: Number($("gain").value || 0),
    autoConvert: $("autoConvert").checked,
    acx: $("acx").checked,
    m4b: $("m4b").checked,
    bitrate: Number($("bitrate").value || 64),
    normalizeMaster: $("normalizeMaster").checked,
    trimSilence: $("trimSilence").checked,
    trimThreshold: Number($("trimThreshold").value || -55),
    headroom: Number($("headroom").value || -1),
    fadeInMs: $("fadeIn").checked ? Number($("fadeInMs").value || 1000) : 0,
    fadeOutMs: $("fadeOut").checked ? Number($("fadeOutMs").value || 1000) : 0,
    gapMs: $("gap").checked ? Number($("gapMs").value || 500) : 0,
    crossfadeMs: $("crossfade").checked ? Number($("crossfadeMs").value || 500) : 0
  };
}

function setState(state) {
  const mergePct = Math.max(0, Math.min(100, state.progress || 0));
  const m4bPct = Math.max(0, Math.min(100, state.m4bProgress || 0));
  const showingM4b = state.running && (m4bPct > 0 || /m4b/i.test(state.status || ""));
  const outputName = state.outputName || (state.output ? state.output.split(/[\\/]/).pop() : "");
  const outputLabel = state.outputLabel || outputName.replace(/\.wav$/i, "");
  const terminal = !state.running && state.exitCode !== null;
  const stageText = state.wavReady && state.running && /m4b/i.test(state.status || "")
    ? "WAV complete — M4B export continues"
    : (state.status || "Ready");
  if (state.wavReady && !announcedWavReady) {
    announcedWavReady = true;
    toast(state.m4bOutput ? "WAV ready" : (state.running ? "WAV ready — M4B export continues" : "WAV ready"));
  }
  if (!state.running && state.exitCode === null) announcedWavReady = false;
  const activePct = terminal ? (state.exitCode === 0 ? 100 : 0) : (showingM4b ? m4bPct : mergePct);
  $("mCount").textContent = state.count;
  $("mDuration").textContent = state.duration;
  $("mSize").textContent = state.size;
  $("mOutput").textContent = outputName || "WAV";
  $("status").textContent = state.status;
  $("stage").textContent = stageText;
  $("progressStage").textContent = stageText;
  $("progressPct").textContent = `${activePct.toFixed(0)}%`;
  $("mergePct").textContent = `${mergePct.toFixed(0)}%`;
  $("m4bPct").textContent = `${m4bPct.toFixed(0)}%`;
  $("mergeDetail").textContent = state.running && state.progressDetail ? ` ${state.progressDetail}` : "";
  $("m4bDetail").textContent = state.running && state.m4bDetail ? ` ${state.m4bDetail}` : "";
  if (document.activeElement !== $("output")) $("output").value = outputLabel || $("output").value;
  $("output").title = state.output || "";
  $("progress").style.width = `${activePct}%`;
  $("mergeBtn").disabled = state.running;
  $("stopBtn").disabled = !state.running;
  $("summary").textContent = state.count ? `${state.count} files · ${state.duration} · ${state.size}` : "No files loaded.";
  renderRows(state.files);
}

function renderRows(files) {
  $("rows").innerHTML = files.map(file => `
    <tr data-index="${file.index}" class="${selected.has(file.index) ? "selected" : ""}">
      <td>${file.index + 1}</td>
      <td><div class="file-name" title="${file.path}">${file.name}</div></td>
      <td>${file.duration}</td>
      <td>${Number(file.sampleRate).toLocaleString()}</td>
      <td>${file.channels}</td>
      <td>${file.bits}</td>
      <td>${file.sizeText}</td>
    </tr>
  `).join("");
  document.querySelectorAll("tbody tr").forEach(row => {
    row.onclick = (event) => {
      const idx = Number(row.dataset.index);
      if (!event.ctrlKey && !event.metaKey) selected.clear();
      selected.has(idx) ? selected.delete(idx) : selected.add(idx);
      refresh();
    };
  });
}

async function refresh() {
  const data = await api("/api/state");
  setState(data.state);
}

async function pollLogs() {
  try {
    const data = await api("/api/logs?cursor=" + logCursor);
    logCursor = data.cursor;
    if (data.logs.length) {
      $("log").innerHTML += data.logs.map(item => `<div class="${item.tag}">[${item.time}] ${escapeHtml(item.message)}</div>`).join("");
      $("log").scrollTop = $("log").scrollHeight;
    }
    setState(data.state);
  } catch (err) {
    console.error(err);
  }
  setTimeout(pollLogs, 800);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]));
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  const wavFiles = files.filter(file => /\.wav$/i.test(file.name));
  if (!wavFiles.length) {
    toast("Drop one or more WAV files.");
    return;
  }
  if (wavFiles.length !== files.length) {
    toast(`Skipped ${files.length - wavFiles.length} non-WAV file(s).`);
  }
  try {
    // Send files one at a time so dropping a large folder does not create a
    // second in-memory copy of every WAV in the browser.
    for (const file of wavFiles) {
      const form = new FormData();
      form.append("files", file, file.name);
      const res = await fetch("/api/upload", {method: "POST", body: form});
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || "Upload failed");
      setState(data.state);
    }
  } catch (err) {
    toast(err.message);
  }
}

async function action(name, extra={}) {
  try {
    const data = await api("/api/action", {name, selected: [...selected], ...extra});
    if (name === "clear") selected.clear();
    setState(data.state);
  } catch (err) {
    toast(err.message);
  }
}

async function startMerge() {
  try {
    const data = await api("/api/start", config());
    logCursor = data.cursor;
    $("log").innerHTML = "";
    setState(data.state);
  } catch (err) {
    toast(err.message);
  }
}

function initControls() {
  [8000,11025,16000,22050,44100,48000,88200,96000,192000].forEach(v => $("sampleRate").add(new Option(v.toLocaleString(), v)));
  [8,16,24,32].forEach(v => $("bitDepth").add(new Option(v, v)));
  $("sampleRate").value = 44100;
  $("bitDepth").value = 16;
  $("speed").value = 1;
  $("gain").value = 0;
  $("autoConvert").checked = true;
  $("acx").checked = true;
  $("headroom").value = -1;
  $("trimThreshold").value = -55;
  $("fadeInMs").value = 1000;
  $("fadeOutMs").value = 1000;
  $("gapMs").value = 500;
  $("crossfadeMs").value = 500;
}

function wire() {
  $("addBtn").onclick = () => action("add");
  $("addFolderBtn").onclick = () => action("add_folder");
  $("removeBtn").onclick = () => action("remove");
  $("upBtn").onclick = () => action("up");
  $("downBtn").onclick = () => action("down");
  $("sortBtn").onclick = () => action("sort");
  $("shuffleBtn").onclick = () => action("shuffle");
  $("clearBtn").onclick = () => action("clear");
  $("outBtn").onclick = () => action("output");
  $("refreshBtn").onclick = refresh;
  $("clearLogBtn").onclick = () => $("log").innerHTML = "";
  $("mergeBtn").onclick = startMerge;
  $("stopBtn").onclick = () => action("stop");
  $("openBtn").onclick = () => action("open");
  $("output").addEventListener("change", () => action("set_output_name", {value: $("output").value}));

  const dropZone = $("dropZone");
  const fileInput = $("fileInput");
  dropZone.onclick = () => fileInput.click();
  dropZone.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  };
  fileInput.onchange = () => {
    if (fileInput.files.length) uploadFiles(fileInput.files);
    fileInput.value = "";
  };
  let dragDepth = 0;
  ["dragenter", "dragover"].forEach(type => dropZone.addEventListener(type, event => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth += 1;
    dropZone.classList.add("drag-over");
  }));
  dropZone.addEventListener("dragleave", event => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropZone.classList.remove("drag-over");
  });
  dropZone.addEventListener("drop", event => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth = 0;
    dropZone.classList.remove("drag-over");
    if (event.dataTransfer && event.dataTransfer.files.length) uploadFiles(event.dataTransfer.files);
  });
  document.addEventListener("dragover", event => event.preventDefault());
  document.addEventListener("drop", event => {
    if (!dropZone.contains(event.target)) event.preventDefault();
  });
}

initControls();
wire();
refresh();
pollLogs();
setInterval(() => refresh().catch(console.error), 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            data = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/state":
            json_response(self, {"ok": True, "state": snapshot()})
            return
        if parsed.path == "/api/logs":
            cursor = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
            with LOCK:
                logs = STATE["logs"][cursor:]
                new_cursor = len(STATE["logs"])
            json_response(self, {"ok": True, "logs": logs, "cursor": new_cursor, "state": snapshot()})
            return
        json_response(self, {"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                paths = read_multipart_files(self)
                if not paths:
                    json_response(self, {"ok": False, "error": "No files were included in the upload."}, 400)
                    return
                add_files(paths)
                json_response(self, {"ok": True, "state": snapshot()})
                return
            payload = read_json(self)
            if parsed.path == "/api/action":
                json_response(self, self.action(payload))
                return
            if parsed.path == "/api/start":
                json_response(self, self.start(payload))
                return
            json_response(self, {"ok": False, "error": "Not found"}, 404)
        except Exception as exc:
            json_response(self, {"ok": False, "error": str(exc)}, 500)

    def action(self, payload):
        name = payload.get("name")
        selected = sorted({int(i) for i in payload.get("selected", [])}, reverse=True)
        if name == "add":
            self.pick_files()
        elif name == "add_folder":
            self.pick_folder()
        elif name == "remove":
            with LOCK:
                for idx in selected:
                    if 0 <= idx < len(STATE["files"]):
                        STATE["files"].pop(idx)
            add_log("Removed selected file(s).", "muted")
        elif name == "up":
            self.move_selected(selected, -1)
        elif name == "down":
            self.move_selected(selected, 1)
        elif name == "sort":
            with LOCK:
                STATE["files"].sort(key=lambda item: item["name"].lower())
            add_log("Sorted files.", "muted")
        elif name == "shuffle":
            with LOCK:
                random.shuffle(STATE["files"])
            add_log("Shuffled files.", "muted")
        elif name == "clear":
            with LOCK:
                STATE["files"] = []
            add_log("Cleared file list.", "muted")
        elif name == "output":
            self.pick_output()
        elif name == "set_output_name":
            self.set_output_name(payload.get("value"))
        elif name == "stop":
            with LOCK:
                event = STATE["cancel"]
            if event:
                event.set()
            set_status("Stopping...")
        elif name == "open":
            self.open_output()
        return {"ok": True, "state": snapshot()}

    def pick_files(self):
        with LOCK:
            initial = Path(STATE["files"][0]["path"]).parent if STATE["files"] else BASE_DIR
        paths = run_file_dialog("wav_files", initial)
        if paths:
            add_files(paths)

    def pick_folder(self):
        with LOCK:
            initial = Path(STATE["files"][0]["path"]).parent if STATE["files"] else BASE_DIR
        folder = run_file_dialog("wav_folder", initial)
        if folder:
            paths = wav_files_in_folder(folder)
            if paths:
                add_files(paths)
                add_log(f"Added WAV files from folder: {Path(folder).name}", "success")
            else:
                add_log(f"No WAV files found in folder: {folder}", "warning")

    def pick_output(self):
        with LOCK:
            initial = STATE["output"] or str(BASE_DIR / "merged_output.wav")
        path = run_file_dialog("save_wav", initial)
        if path:
            with LOCK:
                STATE["output"] = path

    def set_output_name(self, value):
        with LOCK:
            files = list(STATE["files"])
            current_output = STATE["output"]
        resolved = resolve_output_path(value, files, current_output)
        with LOCK:
            STATE["output"] = resolved

    def move_selected(self, selected_desc, direction):
        if not selected_desc:
            return
        with LOCK:
            files = STATE["files"]
            indices = sorted(selected_desc, reverse=(direction > 0))
            for idx in indices:
                new_idx = idx + direction
                if 0 <= idx < len(files) and 0 <= new_idx < len(files):
                    files[idx], files[new_idx] = files[new_idx], files[idx]

    def open_output(self):
        with LOCK:
            target = STATE["last_wav"] or STATE["output"]
        if not target:
            return
        folder = Path(target).parent
        linux_open_path(folder)

    def start(self, payload):
        with LOCK:
            if STATE["running"]:
                return {"ok": False, "error": "Merge already running."}
            files = list(STATE["files"])
            current_output = STATE["output"]
        if len(files) < 2:
            return {"ok": False, "error": "Add at least 2 WAV files."}
        output = resolve_output_path(payload.get("output"), files, current_output)
        options = {
            "tgt_framerate": int(payload.get("sampleRate") or 44100),
            "tgt_channels": int(payload.get("channels") or 2),
            "tgt_sampwidth": int(payload.get("bitDepth") or 16) // 8,
            "auto_convert": bool(payload.get("autoConvert", True)),
            "normalize_master": bool(payload.get("normalizeMaster", False)),
            "trim_silence": bool(payload.get("trimSilence", False)),
            "trim_threshold_db": int(payload.get("trimThreshold") or -55),
            "headroom_db": float(payload.get("headroom") or -1.0),
            "gain_db": float(payload.get("gain") or 0),
            "fade_in_ms": int(payload.get("fadeInMs") or 0),
            "fade_out_ms": int(payload.get("fadeOutMs") or 0),
            "silence_ms": int(payload.get("gapMs") or 0),
            "crossfade_ms": int(payload.get("crossfadeMs") or 0),
            "acx_master": bool(payload.get("acx", True)),
            "speed": float(payload.get("speed") or 1),
        }
        cancel = threading.Event()
        with LOCK:
            STATE["running"] = True
            STATE["exit_code"] = None
            STATE["progress"] = 0
            STATE["m4b_progress"] = 0
            STATE["progress_detail"] = ""
            STATE["m4b_detail"] = ""
            STATE["wav_ready"] = False
            STATE["m4b_output"] = ""
            STATE["status"] = "Preparing..."
            STATE["output"] = output
            STATE["cancel"] = cancel
        add_log("Starting merge...", "success")
        thread = threading.Thread(target=self.run_merge, args=(files, output, options, bool(payload.get("m4b")), int(payload.get("bitrate") or 64), cancel), daemon=True)
        thread.start()
        return {"ok": True, "cursor": 0, "state": snapshot()}

    def run_merge(self, files, output, options, do_m4b, bitrate, cancel):
        def progress(pct, elapsed=None, total=None, speed=None, size_mb=None):
            with LOCK:
                STATE["progress"] = pct
                STATE["progress_detail"] = format_progress_detail(elapsed, total, speed, size_mb)
            if elapsed is not None and total:
                set_status(f"Merging {secs_to_hms(elapsed)} / {secs_to_hms(total)}")

        def m4b_progress(pct, elapsed=None, total=None, speed=None, size_mb=None):
            with LOCK:
                STATE["m4b_progress"] = pct
                STATE["m4b_detail"] = format_progress_detail(elapsed, total, speed, size_mb)
            if elapsed is not None and total:
                set_status(f"Exporting M4B {secs_to_hms(elapsed)} / {secs_to_hms(total)}")

        def log_cb(message, tag=None):
            add_log(message, tag)

        def status_cb(message):
            set_status(message)

        ok, result, _stats = do_merge(files, output, options, progress, log_cb, status_cb, cancel)
        wav_result = result if ok else ""
        m4b_path = None

        # Publish the WAV immediately.  M4B encoding is intentionally a
        # second stage so the user can copy/listen to the WAV while it runs.
        if ok and wav_result and not cancel.is_set():
            with LOCK:
                STATE["last_wav"] = wav_result
                STATE["wav_ready"] = True
                STATE["status"] = "WAV complete — M4B export continuing" if do_m4b else "WAV complete"
            add_log("✓ WAV complete. You can copy/listen now; M4B export continues.", "success")

        if ok and do_m4b and not cancel.is_set():
            set_status("Exporting M4B...")
            m4b_ok, m4b_path = export_m4b(wav_result, files, bitrate, log_cb, m4b_progress, status_cb, cancel, merge_options=options)
            ok = m4b_ok

        with LOCK:
            STATE["running"] = False
            STATE["cancel"] = None
            STATE["exit_code"] = 0 if ok and not cancel.is_set() else 1
            if ok and not cancel.is_set():
                STATE["status"] = "Complete"
                if m4b_path:
                    STATE["m4b_output"] = m4b_path
            elif cancel.is_set():
                STATE["status"] = "Cancelled"
            elif STATE["wav_ready"] and do_m4b:
                STATE["status"] = "WAV complete — M4B export failed"
            else:
                STATE["status"] = "Error"
            if ok and not cancel.is_set():
                STATE["progress"] = 100
                if do_m4b:
                    STATE["m4b_progress"] = 100
            STATE["progress_detail"] = ""
            STATE["m4b_detail"] = ""
        add_log("Done." if ok and not cancel.is_set() else "Stopped.", "success" if ok and not cancel.is_set() else "warning")

    def log_message(self, fmt, *args):
        return


def main():
    add_files([arg for arg in sys.argv[1:] if arg.lower().endswith(".wav")])
    server = None
    port = PORT
    while True:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            port += 1
            if port > PORT + 20:
                raise
    url = f"http://{HOST}:{port}"
    print(f"WAV Forge running at {url}")
    if should_open_browser():
        threading.Timer(0.4, lambda: open_url_quietly(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
