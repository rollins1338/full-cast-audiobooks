"""WAV Forge v2: the v1 merger (wav_web.py + wav_merge_engine.py, unchanged) with a new UI
and an XTTS cleanup / voice blend stage (voice_blend.py) that runs before the merge."""

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import wav_web as v1
from wav_web import LOCK, STATE, add_log, set_status, snapshot, json_response
from wav_merge_engine import get_wav_info, secs_to_hms

import voice_blend

PORT = 8776
BLEND_KEYS = ("level_match", "declick", "dehiss", "deess", "tone_match", "steady_tone",
              "room_tone", "edge_fades", "shared_room")

STATE.update({"blend_progress": 0, "blend_label": "", "stage": "", "reroll": "", "preview": None})
RENDER = {"key": None, "info": None, "titles": {}, "book": ""}


def render_info():
    """voice_blend.find_render for the loaded files, cached until the file list changes."""
    with LOCK:
        paths = [f["path"] for f in STATE["files"]]
    key = tuple(paths)
    if RENDER["key"] != key:
        info, titles, book = None, {}, ""
        if paths:
            try:
                info = voice_blend.find_render(paths)
            except Exception as exc:
                add_log(f"Render folder check failed: {exc}", "warning")
            if info:
                try:
                    plan = json.loads(Path(info["plan"]).read_text(encoding="utf-8"))
                    titles = {i: ch.get("title", "") for i, ch in enumerate(plan.get("chapters", []), 1)}
                    book = plan.get("book") or plan.get("title", "")
                except (OSError, ValueError):
                    pass
        RENDER.update(key=key, info=info, titles=titles, book=book)
    return RENDER["info"]


def chapter_no(path):
    stem = Path(path).stem
    return int(stem) if stem.isdigit() else None


def snapshot_v2():
    snap = snapshot()
    info = render_info()
    with LOCK:
        first = STATE["files"][0] if STATE["files"] else None
        snap.update(blendProgress=STATE["blend_progress"], blendLabel=STATE["blend_label"],
                    stage=STATE["stage"], reroll=STATE["reroll"], preview=STATE["preview"])
    for f in snap["files"]:
        n = chapter_no(f["path"])
        f["title"] = RENDER["titles"].get(n, "") if n else ""
    snap["format"] = (f"{first.get('framerate', 0) / 1000:g} kHz · "
                      f"{'mono' if first.get('channels') == 1 else 'stereo'}") if first else "—"
    snap["book"] = (RENDER["book"] or Path(info["out_dir"]).name) if info else ""
    snap["render"] = None if not info else {
        "outDir": info["out_dir"], "lines": info["lines"], "voices": info["voices"],
        "missing": info["missing"], "suspects": info["suspects"]}
    snap["presets"] = voice_blend.PRESETS
    return snap


def audio_allowed(path):
    """Only stream loaded chapter files and files inside a render folder's .blend preview dir."""
    real = os.path.realpath(path)
    with LOCK:
        loaded = {os.path.realpath(f["path"]) for f in STATE["files"]}
    if real in loaded:
        return True
    info = RENDER["info"]
    return bool(info) and real.startswith(os.path.realpath(os.path.join(info["out_dir"], ".blend")) + os.sep)


class Handler(v1.Handler):
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
            json_response(self, {"ok": True, "state": snapshot_v2()})
            return
        if parsed.path == "/api/logs":
            cursor = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
            with LOCK:
                logs = STATE["logs"][cursor:]
                new_cursor = len(STATE["logs"])
            json_response(self, {"ok": True, "logs": logs, "cursor": new_cursor, "state": snapshot_v2()})
            return
        if parsed.path == "/audio":
            self.send_audio(parse_qs(parsed.query).get("path", [""])[0])
            return
        json_response(self, {"ok": False, "error": "Not found"}, 404)

    def send_audio(self, path):
        if not path or not os.path.isfile(path) or not audio_allowed(path):
            json_response(self, {"ok": False, "error": "Not found"}, 404)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = min(int(b), size - 1) if b else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/preview", "/api/reroll"):
            try:
                payload = v1.read_json(self)
                handler = self.preview if parsed.path == "/api/preview" else self.reroll
                json_response(self, handler(payload))
            except Exception as exc:
                json_response(self, {"ok": False, "error": str(exc)}, 500)
            return
        super().do_POST()

    def action(self, payload):
        result = super().action(payload)
        result["state"] = snapshot_v2()
        return result

    def blend_options(self, payload):
        return {k: bool((payload.get("blend") or {}).get(k)) for k in BLEND_KEYS}

    def preview(self, payload):
        info = render_info()
        if not info:
            return {"ok": False, "error": "Preview needs a render folder (the renderer's .chunks)."}
        chapter = int(payload.get("chapter") or 0)
        if chapter not in info["chapters"]:
            # no chapter picked: the one with the most voice changes per line shows the blend best
            chapter = max(info["chapters"], key=lambda c: sum(
                a["speaker"] != b["speaker"] for a, b in zip(info["chapters"][c], info["chapters"][c][1:]))
                / max(len(info["chapters"][c]), 1))
        start = payload.get("start")
        start = None if start is None else float(start)
        with LOCK:
            STATE["preview"] = {"state": "working", "chapter": chapter}
        options = self.blend_options(payload)

        def work():
            try:
                before, after = voice_blend.preview(info, options, chapter, start, 30)
                result = {"state": "ready", "chapter": chapter, "before": before, "after": after}
            except Exception as exc:
                result = {"state": "error", "error": str(exc)}
                add_log(f"Preview failed: {exc}", "error")
            with LOCK:
                STATE["preview"] = result
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "state": snapshot_v2()}

    def reroll(self, payload):
        info = render_info()
        chunk = str(payload.get("id") or "")
        if not info or not chunk:
            return {"ok": False, "error": "Nothing to re-roll."}
        with LOCK:
            if STATE["running"] or STATE["reroll"]:
                return {"ok": False, "error": "Busy. Wait for the current job to finish."}
            STATE["reroll"] = chunk

        def work():
            add_log("Re-rolling 1 line with the renderer…", "muted")
            try:
                code = voice_blend.reroll(info, chunk)
                add_log("Re-roll done. Listen again." if code == 0 else f"Re-roll failed (exit {code}).",
                        "success" if code == 0 else "error")
            except Exception as exc:
                add_log(f"Re-roll failed: {exc}", "error")
            RENDER["key"] = None
            with LOCK:
                STATE["reroll"] = ""
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "state": snapshot_v2()}

    def start(self, payload):
        self._blend = self.blend_options(payload)
        with LOCK:
            STATE["blend_progress"] = 0
            STATE["blend_label"] = ""
            STATE["stage"] = ""
        result = super().start(payload)
        if result.get("ok"):
            result["state"] = snapshot_v2()
        return result

    def run_merge(self, files, output, options, do_m4b, bitrate, cancel):
        blend = getattr(self, "_blend", {})
        info = render_info() if any(blend.values()) else None
        if any(blend.values()) and not info:
            add_log("No render folder found: merging the chapter files without voice blend.", "warning")
        if info:
            with LOCK:
                STATE["stage"] = "blend"
            set_status("Voice blend…")

            def progress(pct, label=""):
                with LOCK:
                    STATE["blend_progress"] = pct
                    STATE["blend_label"] = label
                if label:
                    set_status(label)
            try:
                blended = voice_blend.blend_book(info, blend, progress_cb=progress,
                                                 log_cb=lambda m, t=None: add_log(m, t), cancel_event=cancel)
            except Exception as exc:
                add_log(f"Voice blend failed: {exc}", "error")
                blended = None
            if cancel.is_set() or not blended:
                with LOCK:
                    STATE["running"] = False
                    STATE["cancel"] = None
                    STATE["exit_code"] = 1
                    STATE["status"] = "Cancelled" if cancel.is_set() else "Error"
                    STATE["stage"] = ""
                return
            by_chapter = {chapter_no(p): p for p in blended}
            swapped = []
            for f in files:
                n = chapter_no(f["path"])
                if n in by_chapter:
                    new = get_wav_info(by_chapter[n])
                    new["name"] = f["name"]
                    swapped.append(new)
                else:
                    swapped.append(f)
            files = swapped
            with LOCK:
                STATE["blend_progress"] = 100
        with LOCK:
            STATE["stage"] = "master"
        super().run_merge(files, output, options, do_m4b, bitrate, cancel)
        with LOCK:
            STATE["stage"] = ""


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WAV Forge</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2314161a'/%3E%3Cpath d='M14 32v0M23 22v20M32 12v40M41 22v20M50 32v0' stroke='%234ade80' stroke-width='6' stroke-linecap='round'/%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--bg:#0c0d10;--panel:#14161a;--panel2:#1a1d22;--line:#24272e;--line2:#2f333b;--text:#e9eaed;--muted:#8d929c;--dim:#5f646d;--acc:#4ade80;--acc-ink:#06240f;--acc-bg:rgba(74,222,128,.10);--amber:#f5b85c;--red:#ff7b72}
[hidden]{display:none!important}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12px}
button{font:inherit}
header{height:56px;display:flex;align-items:center;gap:12px;padding:0 24px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
.logo{width:26px;height:26px;border-radius:7px;background:var(--acc-bg);display:grid;place-items:center;color:var(--acc)}
.brand{font-weight:600;font-size:14px;letter-spacing:-.01em}.crumb{color:var(--muted)}.crumb b{color:var(--text);font-weight:500}
.sp{flex:1}
.btn{height:30px;padding:0 12px;border-radius:6px;border:1px solid var(--line2);background:transparent;color:var(--text);font-weight:500;font-size:12.5px;display:inline-flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--panel2)}.btn:disabled{opacity:.45;cursor:default;background:transparent}
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}.btn.pri:hover{filter:brightness(1.06)}
.btn.icon{width:30px;padding:0;justify-content:center}.btn.danger{color:var(--red)}
.btn svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;padding:20px 24px;max-width:1500px;margin:0 auto;align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--line)}
.stat{padding:14px 16px;border-right:1px solid var(--line);min-width:0}.stat:last-child{border:0}
.stat span{display:block;color:var(--muted);font-size:11.5px;margin-bottom:2px}.stat b{font-size:18px;font-weight:500;letter-spacing:-.01em;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.src{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);font-size:12.5px;flex-wrap:wrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--acc);flex:none}.dot.off{background:var(--dim)}
.warn{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--amber)}
.pill{font-size:11px;height:22px;padding:0 8px;border-radius:4px;background:var(--panel2);color:var(--muted);border:1px solid var(--line);cursor:pointer;display:inline-flex;align-items:center}
.pill:hover{color:var(--text)}.warn .pill{color:var(--amber);border-color:rgba(245,184,92,.35);background:rgba(245,184,92,.08)}
.tl{display:flex;gap:2px;padding:14px 16px;height:62px;border-bottom:1px solid var(--line)}
.tl i{display:block;border-radius:3px;background:#20242a;position:relative;overflow:hidden;cursor:pointer;min-width:3px}
.tl i::after{content:"";position:absolute;inset:30% 0;background:repeating-linear-gradient(90deg,#3a4049 0 1px,transparent 1px 3px)}
.tl i:hover{background:#262b32}.tl i.sel{background:var(--acc-bg);outline:1px solid var(--acc)}
.ch{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line)}
.ch h2{margin:0;font-size:13.5px;font-weight:600}.sub{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-weight:500;color:var(--dim);font-size:11.5px;padding:8px 16px;border-bottom:1px solid var(--line)}
td{padding:0 16px;height:40px;border-bottom:1px solid var(--line)}tbody tr:last-child td{border:0}
tbody tr{cursor:pointer}tbody tr:hover td{background:rgba(255,255,255,.015)}tr.sel td{background:rgba(74,222,128,.06)!important}
th:nth-child(4),th:nth-child(5){width:110px}.num{color:var(--dim);width:44px}.r{text-align:right}.title{color:var(--muted);max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.act{opacity:0;width:60px}tr:hover .act,tr.playing .act{opacity:1}
.drop{margin:16px;border:1px dashed var(--line2);border-radius:8px;padding:34px;text-align:center;color:var(--muted)}
.drop.over{border-color:var(--acc);background:var(--acc-bg);color:var(--text)}
.side{display:flex;flex-direction:column;gap:16px;position:sticky;top:76px}
.sec{padding:14px 16px;border-bottom:1px solid var(--line)}.sec:last-child{border:0}
.sec h3{margin:0 0 10px;font-size:11.5px;font-weight:500;color:var(--muted)}.cnt{float:right;color:var(--dim);font-weight:400}
.field{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:9px 0}
.field small{display:block;color:var(--dim);font-size:11.5px}
input.t,select.t{height:30px;border-radius:6px;border:1px solid var(--line2);background:var(--bg);color:var(--text);padding:0 10px;font:13px Inter,sans-serif;width:100%}
input.n{width:84px}
.seg{display:flex;border:1px solid var(--line2);border-radius:6px;overflow:hidden}
.seg button{border:0;border-right:1px solid var(--line2);background:transparent;color:var(--muted);padding:4px 10px;font-size:12px;cursor:pointer}
.seg button:last-child{border:0}.seg button.on{background:var(--panel2);color:var(--text)}
.seg.wide{width:100%}.seg.wide button{flex:1}
.note{margin:10px 0 0;color:var(--muted);font-size:12px}
.tog{appearance:none;width:30px;height:17px;border-radius:9px;background:#2a2e35;position:relative;flex:none;margin:0;cursor:pointer;transition:background .15s}
.tog::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#8d929c;transition:transform .15s,background .15s}
.tog:checked{background:var(--acc)}.tog:checked::after{transform:translateX(13px);background:var(--acc-ink)}
.tog:disabled{opacity:.4;cursor:default}
.kv{display:flex;justify-content:space-between;margin:7px 0;color:var(--muted)}.kv b{color:var(--text);font-weight:400}
details summary{cursor:pointer;color:var(--muted);font-size:11.5px;list-style:none}details summary::-webkit-details-marker{display:none}
details summary::before{content:"›";display:inline-block;margin-right:6px;transition:transform .15s}details[open] summary::before{transform:rotate(90deg)}
.prog{height:4px;border-radius:2px;background:var(--panel2);margin:10px 0 6px;overflow:hidden}.prog div{height:4px;width:0;border-radius:2px;background:var(--acc);transition:width .4s}
.row2{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:12px}
.big{flex:1;height:36px;justify-content:center;font-size:13px}
.ab{display:flex;gap:8px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.log{max-height:180px;overflow:auto;font-family:"JetBrains Mono",monospace;font-size:11.5px;color:var(--muted);padding:10px 16px}
.log .success{color:var(--acc)}.log .warning{color:var(--amber)}.log .error{color:var(--red)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;background:var(--panel2);border:1px solid var(--line2);padding:8px 14px;border-radius:8px;transition:.2s;pointer-events:none}
.toast.show{opacity:1;transform:translateX(-50%)}
@media (max-width:980px){main{grid-template-columns:1fr}.side{position:static}.stats{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<header>
  <div class="logo"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 7v0M4.5 4.5v5M7 2v10M9.5 4.5v5M12 7v0"/></svg></div>
  <div class="brand">WAV Forge</div><div class="crumb" id="crumb"></div>
  <div class="sp"></div>
  <button class="btn" id="openBtn" title="Open the output folder"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg>Output</button>
  <button class="btn" id="addFolderBtn"><svg viewBox="0 0 24 24"><path d="M3 7h6l2 2h10v10H3z"/></svg>Open folder</button>
  <button class="btn" id="addBtn"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Add files</button>
</header>
<main>
  <section class="card" id="mainCard">
    <div class="stats">
      <div class="stat"><span>Chapters</span><b id="mCount">0</b></div>
      <div class="stat"><span>Length</span><b class="mono" style="font-size:17px" id="mDuration">0:00</b></div>
      <div class="stat"><span>Size</span><b id="mSize">0 MB</b></div>
      <div class="stat"><span>Format</span><b id="mFormat">—</b></div>
    </div>
    <div class="src" id="src"></div>
    <div class="tl" id="tl"></div>
    <div class="ch"><h2>Tracks</h2><span class="sub">Order follows file names</span><div class="sp"></div>
      <button class="btn icon" id="upBtn" title="Move up"><svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
      <button class="btn icon" id="downBtn" title="Move down"><svg viewBox="0 0 24 24"><path d="M12 5v14M6 13l6 6 6-6"/></svg></button>
      <button class="btn" id="sortBtn">Sort by name</button>
      <button class="btn icon" id="removeBtn" title="Remove selected"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
      <button class="btn icon danger" id="clearBtn" title="Clear the list"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>
    <table><thead><tr><th>#</th><th>File</th><th>Chapter</th><th class="r">Length</th><th class="r">Size</th><th></th></tr></thead><tbody id="rows"></tbody></table>
    <div class="drop" id="drop">Drop chapter WAVs here, or use Open folder</div>
    <input type="file" id="fileInput" accept=".wav,audio/wav" multiple hidden>
  </section>
  <aside class="side">
    <section class="card">
      <div class="sec"><h3>Quality</h3>
        <div class="seg wide" id="preset"><button data-p="best">Best quality</button><button data-p="current">Current</button><button data-p="custom">Custom</button></div>
        <p class="note" id="presetNote"></p>
      </div>
      <div class="sec"><h3>XTTS cleanup <span class="cnt">per line</span></h3>
        <div class="field"><div><label for="level_match">Level-match lines</label><small>Even out loud and quiet takes</small></div><input type="checkbox" class="tog bl" id="level_match"></div>
        <div class="field"><div><label for="declick">Trim line ends</label><small>DC offset and dead air at line ends</small></div><input type="checkbox" class="tog bl" id="declick"></div>
        <div class="field"><div><label for="dehiss">De-hiss</label><small>Light cut of the metallic hiss</small></div><input type="checkbox" class="tog bl" id="dehiss"></div>
        <div class="field"><div><label for="deess">De-ess</label><small>Soften harsh s sounds</small></div><input type="checkbox" class="tog bl" id="deess"></div>
      </div>
      <div class="sec"><h3>Voice blend <span class="cnt">per line</span></h3>
        <div class="field"><div><label for="tone_match">Match voice tone</label><small>Same-mic EQ across the cast</small></div><input type="checkbox" class="tog bl" id="tone_match"></div>
        <div class="field"><div><label for="steady_tone">Steady character tone</label><small>Each voice sounds the same every line</small></div><input type="checkbox" class="tog bl" id="steady_tone"></div>
        <div class="field"><div><label for="room_tone">Room tone</label><small>Soft bed instead of dead silence</small></div><input type="checkbox" class="tog bl" id="room_tone"></div>
        <div class="field"><div><label for="edge_fades">Edge fades</label><small>15 ms in and out on every line</small></div><input type="checkbox" class="tog bl" id="edge_fades"></div>
        <div class="field"><div><label for="shared_room">Shared room</label><small>A touch of the same space</small></div><input type="checkbox" class="tog bl" id="shared_room"></div>
      </div>
      <div class="sec"><h3>Master <span class="cnt">current chain</span></h3>
        <div class="field"><div><label for="acx">ACX master</label><small>High-pass, 4:1 compressor, two-pass loudness</small></div><input type="checkbox" class="tog" id="acx" checked></div>
        <div class="kv"><span>Loudness</span><b class="mono" id="kvLoud">−20 LUFS · −3 dB peak</b></div>
        <div class="field"><label for="sampleRate">Sample rate</label><select class="t" id="sampleRate" style="width:120px"></select></div>
        <div class="field"><label>Channels</label><div class="seg" id="channels"><button data-v="1">Mono</button><button data-v="2">Stereo</button></div></div>
        <details style="margin-top:10px"><summary>More master settings</summary>
          <div class="field"><label for="bitDepth">Bit depth</label><select class="t" id="bitDepth" style="width:120px"></select></div>
          <div class="field"><label for="speed">Speed</label><input class="t n mono" id="speed" type="number" step="0.05" min="0.5" max="2" value="1"></div>
          <div class="field"><label for="gain">Gain dB</label><input class="t n mono" id="gain" type="number" step="0.5" value="0"></div>
          <div class="field"><label for="headroom">Headroom dB</label><input class="t n mono" id="headroom" type="number" step="0.5" value="-1"></div>
          <div class="field"><div><label for="normalizeMaster">Normalize master</label></div><input type="checkbox" class="tog" id="normalizeMaster"></div>
          <div class="field"><div><label for="autoConvert">Auto-convert formats</label></div><input type="checkbox" class="tog" id="autoConvert" checked></div>
          <div class="field"><div><label for="trimSilence">Trim silence</label></div><input type="checkbox" class="tog" id="trimSilence"></div>
          <div class="field"><label for="trimThreshold">Trim threshold dB</label><input class="t n mono" id="trimThreshold" type="number" value="-55"></div>
          <div class="field"><div><label for="fadeIn">Fade in ms</label></div><span style="display:flex;gap:8px;align-items:center"><input class="t n mono" id="fadeInMs" type="number" value="1000"><input type="checkbox" class="tog" id="fadeIn"></span></div>
          <div class="field"><div><label for="fadeOut">Fade out ms</label></div><span style="display:flex;gap:8px;align-items:center"><input class="t n mono" id="fadeOutMs" type="number" value="1000"><input type="checkbox" class="tog" id="fadeOut"></span></div>
          <div class="field"><div><label for="gap">Gap between files ms</label></div><span style="display:flex;gap:8px;align-items:center"><input class="t n mono" id="gapMs" type="number" value="500"><input type="checkbox" class="tog" id="gap"></span></div>
          <div class="field"><div><label for="crossfade">Crossfade ms</label></div><span style="display:flex;gap:8px;align-items:center"><input class="t n mono" id="crossfadeMs" type="number" value="500"><input type="checkbox" class="tog" id="crossfade"></span></div>
        </details>
      </div>
      <div class="sec"><h3>Output</h3>
        <input class="t" id="output" placeholder="Book name">
        <div class="field" style="margin-top:12px"><div><label for="m4b">Also make M4B</label><small>With chapter markers</small></div><input type="checkbox" class="tog" id="m4b" checked></div>
        <div class="field"><label>M4B bitrate</label><div class="seg" id="bitrate"><button data-v="64">64k</button><button data-v="96">96k</button><button data-v="128">128k</button></div></div>
      </div>
    </section>
    <section class="card"><div class="sec">
      <div class="row2"><span id="stageText">Ready</span><span class="mono" id="pct">0%</span></div>
      <div class="prog"><div id="bar"></div></div>
      <div class="row2" style="margin-bottom:14px"><span id="nextText">&nbsp;</span><span class="mono" id="detail"></span></div>
      <div style="display:flex;gap:8px"><button class="btn" id="previewBtn">Preview 30s</button><button class="btn danger" id="stopBtn" hidden>Stop</button><button class="btn pri big" id="mergeBtn">Merge</button></div>
      <div class="ab" id="ab" hidden>
        <div class="seg"><button id="abBefore">Before</button><button id="abAfter" class="on">After</button></div>
        <span class="sub" id="abNote"></span><div class="sp"></div>
        <button class="btn icon" id="abPlay" title="Play"><svg viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z"/></svg></button>
      </div>
    </div></section>
    <section class="card"><details id="logBox"><summary style="padding:12px 16px">Activity</summary><div class="log" id="log"></div></details></section>
  </aside>
</main>
<audio id="player" preload="none"></audio>
<div class="toast" id="toast"></div>
<script>
const $ = id => document.getElementById(id);
const BL = ["level_match","declick","dehiss","deess","tone_match","steady_tone","room_tone","edge_fades","shared_room"];
const NOTES = {best:"Everything on except Room tone and Shared room. Built on the current master, with XTTS cleanup and voice blend before it.",
  current:"Exactly the v1 merger: no per-line processing, same master chain.",
  custom:"Your own mix of the options below."};
let S = null, selected = new Set(), logCursor = 0, preset = "best", presets = null, playing = null, abSide = "after", lastPreview = null;
const store = {get(k,d){try{const v=localStorage.getItem("wf2."+k);return v===null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem("wf2."+k,JSON.stringify(v))}catch(e){}}};

function toast(m){$("toast").textContent=m;$("toast").classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>$("toast").classList.remove("show"),2200)}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
async function api(path, body){const r=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||"Request failed");return d}
function segVal(id){const b=$(id).querySelector("button.on");return b?b.dataset.v:null}
function setSeg(id,v){$(id).querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.v==String(v)))}

function blendOpts(){const o={};BL.forEach(k=>o[k]=$(k).checked);return o}
function applyPreset(p){
  preset=p;store.set("preset",p);
  $("preset").querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.p===p));
  $("presetNote").textContent=NOTES[p];
  if(p!=="custom"&&presets&&presets[p])BL.forEach(k=>$(k).checked=!!presets[p][k]);
  if(p==="custom")BL.forEach(k=>$(k).checked=!!store.get("custom",{})[k]);
  if(p!=="custom")$("acx").checked=true;
  updateAvail();
}
function updateAvail(){const has=!!(S&&S.render);BL.forEach(k=>$(k).disabled=!has);$("previewBtn").disabled=!has||(S&&S.running)}

function config(){return{output:$("output").value,sampleRate:Number($("sampleRate").value),channels:Number(segVal("channels")||2),
  bitDepth:Number($("bitDepth").value),speed:Number($("speed").value||1),gain:Number($("gain").value||0),autoConvert:$("autoConvert").checked,
  acx:$("acx").checked,m4b:$("m4b").checked,bitrate:Number(segVal("bitrate")||96),normalizeMaster:$("normalizeMaster").checked,
  trimSilence:$("trimSilence").checked,trimThreshold:Number($("trimThreshold").value||-55),headroom:Number($("headroom").value||-1),
  fadeInMs:$("fadeIn").checked?Number($("fadeInMs").value||1000):0,fadeOutMs:$("fadeOut").checked?Number($("fadeOutMs").value||1000):0,
  gapMs:$("gap").checked?Number($("gapMs").value||500):0,crossfadeMs:$("crossfade").checked?Number($("crossfadeMs").value||500):0,
  blend:S&&S.render?blendOpts():{}}}

function fmtT(sec){sec=Math.round(sec);const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return(h?h+":"+String(m).padStart(2,"0"):m)+":"+String(s).padStart(2,"0")}

function render(st){
  S=st;if(!presets&&st.presets){presets=st.presets;applyPreset(preset)}
  $("crumb").innerHTML=st.book?`/ <b>${esc(st.book)}</b>`:"";
  $("mCount").textContent=st.count;$("mDuration").textContent=st.duration;$("mSize").textContent=st.size;$("mFormat").textContent=st.format;
  if(st.book&&!st.running&&(!st.outputLabel||st.outputLabel==="merged_output")&&!render.named){render.named=1;action("set_output_name",{value:st.book})}
  if(document.activeElement!==$("output"))$("output").value=st.outputLabel||"";
  const r=st.render;
  if(r){const sus=r.suspects||[];
    $("src").innerHTML=`<span class="dot"></span><span>Render folder found <span class="mono" style="color:var(--muted)">${esc(r.outDir.split("/").slice(-2).join("/"))} · ${r.lines.toLocaleString()} lines · ${r.voices} voices${r.missing?` · ${r.missing} missing`:""}</span></span>`+
      (sus.length?`<span class="warn">${sus.length} line${sus.length>1?"s":""} flagged as possibly garbled${sus.map(s=>` <span class="mono">${esc(s.speaker)} · ch ${s.chapter}${s.at?" · "+esc(String(s.at).split(" ").pop()):""}</span><span class="pill" data-listen="${esc(s.at||"")}" data-ch="${s.chapter}">Listen</span><span class="pill" data-reroll="${esc(s.id)}">${st.reroll===s.id?"Re-rolling…":"Re-roll"}</span>`).join("")}</span>`:"");
  }else $("src").innerHTML=st.count?`<span class="dot off"></span><span class="sub">No render folder next to these files. They'll merge with the current master only.</span>`:`<span class="dot off"></span><span class="sub">Open a book's output folder to start.</span>`;
  const files=st.files,total=files.reduce((a,f)=>a+f.seconds,0)||1;
  $("tl").innerHTML=files.map(f=>`<i data-i="${f.index}" title="${esc(f.name)} · ${esc(f.duration)}" class="${selected.has(f.index)?"sel":""}" style="flex:${Math.max(f.seconds/total*1000,4)}"></i>`).join("");
  $("tl").hidden=!files.length;$("drop").hidden=!!files.length;
  $("rows").innerHTML=files.map(f=>`<tr data-i="${f.index}" class="${selected.has(f.index)?"sel":""}${playing===f.path?" playing":""}"><td class="num mono">${String(f.index+1).padStart(2,"0")}</td><td class="mono">${esc(f.name)}</td><td class="title" title="${esc(f.title||"")}">${esc(f.title||"")}</td><td class="r mono">${esc(f.duration)}</td><td class="r mono" style="color:var(--muted)">${esc(f.sizeText)}</td><td class="r act"><span class="pill" data-play="${esc(f.path)}">${playing===f.path?"Stop":"Play"}</span></td></tr>`).join("");
  const run=st.running,stage=st.stage;
  let pct=0,label=st.status||"Ready",next="";
  if(run&&stage==="blend"){pct=st.blendProgress;label=st.blendLabel||"Voice blend";next="Then master"+(config().m4b?", then M4B":"")}
  else if(run){const m4b=st.m4bProgress>0||/m4b/i.test(st.status||"");pct=m4b?st.m4bProgress:st.progress;next=m4b?"M4B export":(config().m4b?"Then M4B":"")}
  else if(st.exitCode===0)pct=100;
  $("stageText").textContent=label;$("pct").textContent=Math.round(pct)+"%";$("bar").style.width=pct+"%";$("nextText").innerHTML=next||"&nbsp;";
  $("detail").textContent=run?(st.m4bDetail||st.progressDetail||""):"";
  $("mergeBtn").textContent=run?"Working…":`Merge ${st.count||""} chapter${st.count===1?"":"s"}`;$("mergeBtn").disabled=run||st.count<2;
  $("stopBtn").hidden=!run;updateAvail();
  const pv=st.preview;
  if(pv&&pv.state==="working"){$("previewBtn").textContent="Rendering preview…";$("previewBtn").disabled=true}
  else{$("previewBtn").textContent="Preview 30s"}
  if(pv&&pv.state==="ready"&&lastPreview!==pv.after){lastPreview=pv.after;$("ab").hidden=false;$("abNote").textContent=`Chapter ${pv.chapter} · 30 s`;setAB("after");play(pv.after,true)}
}
function setAB(side){abSide=side;$("abBefore").classList.toggle("on",side==="before");$("abAfter").classList.toggle("on",side==="after")}
function play(path,keepTime){const p=$("player");const t=keepTime&&!p.paused?p.currentTime:0;p.src="/audio?path="+encodeURIComponent(path);p.currentTime=t;p.play().catch(()=>{});playing=path;if(S)render(S)}
function stopPlay(){$("player").pause();playing=null;if(S)render(S)}
$("player").onended=()=>{playing=null;if(S)render(S)};

async function refresh(){try{render((await api("/api/state")).state)}catch(e){console.error(e)}}
async function pollLogs(){try{const d=await api("/api/logs?cursor="+logCursor);logCursor=d.cursor;if(d.logs.length){$("log").insertAdjacentHTML("beforeend",d.logs.map(l=>`<div class="${esc(l.tag)}">${esc(l.time)}  ${esc(l.message)}</div>`).join(""));$("log").scrollTop=1e9}render(d.state)}catch(e){console.error(e)}setTimeout(pollLogs,800)}
async function action(name,extra={}){try{const d=await api("/api/action",{name,selected:[...selected],...extra});if(name==="clear"||name==="remove")selected.clear();render(d.state)}catch(e){toast(e.message)}}
async function upload(list){const files=[...list].filter(f=>/\.wav$/i.test(f.name));if(!files.length){toast("Drop one or more WAV files");return}
  toast("Uploaded copies don't keep the render folder. Use Open folder for voice blend.");
  for(const f of files){const fd=new FormData();fd.append("files",f,f.name);const r=await fetch("/api/upload",{method:"POST",body:fd});const d=await r.json();if(!r.ok||d.ok===false){toast(d.error||"Upload failed");return}render(d.state)}}

function wire(){
  $("addBtn").onclick=()=>action("add");$("addFolderBtn").onclick=()=>action("add_folder");$("openBtn").onclick=()=>action("open");
  $("upBtn").onclick=()=>action("up");$("downBtn").onclick=()=>action("down");$("sortBtn").onclick=()=>action("sort");
  $("removeBtn").onclick=()=>{if(!selected.size){toast("Select tracks to remove");return}action("remove")};
  $("clearBtn").onclick=()=>{if(confirm("Clear the track list?"))action("clear")};
  $("stopBtn").onclick=()=>action("stop");
  $("output").addEventListener("change",()=>action("set_output_name",{value:$("output").value}));
  $("mergeBtn").onclick=async()=>{try{const d=await api("/api/start",config());logCursor=d.cursor;$("log").innerHTML="";render(d.state)}catch(e){toast(e.message)}};
  $("previewBtn").onclick=async()=>{const sel=[...selected][0];const f=S&&S.files.find(x=>x.index===sel);const ch=f?parseInt(f.name,10):0;
    try{render((await api("/api/preview",{chapter:ch||0,start:null,blend:blendOpts()})).state)}catch(e){toast(e.message)}};
  $("abBefore").onclick=()=>{setAB("before");if(S&&S.preview&&S.preview.before)play(S.preview.before,true)};
  $("abAfter").onclick=()=>{setAB("after");if(S&&S.preview&&S.preview.after)play(S.preview.after,true)};
  $("abPlay").onclick=()=>{const pv=S&&S.preview;if(!pv||pv.state!=="ready")return;if(!$("player").paused){$("player").pause();return}play(abSide==="after"?pv.after:pv.before,true)};
  $("preset").onclick=e=>{const b=e.target.closest("button");if(b)applyPreset(b.dataset.p)};
  BL.forEach(k=>$(k).onchange=()=>{const c=blendOpts();store.set("custom",c);if(preset!=="custom"){preset="custom";store.set("preset","custom");$("preset").querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.p==="custom"));$("presetNote").textContent=NOTES.custom}});
  ["channels","bitrate"].forEach(id=>$(id).onclick=e=>{const b=e.target.closest("button");if(b){setSeg(id,b.dataset.v);store.set(id,b.dataset.v)}});
  $("acx").onchange=()=>$("kvLoud").textContent=$("acx").checked?"−20 LUFS · −3 dB peak":"−23 LUFS · −1.5 dB peak";
  document.addEventListener("click",e=>{
    const t=e.target.closest("[data-play],[data-reroll],[data-listen]");
    if(t&&t.dataset.play!==undefined){e.stopPropagation();playing===t.dataset.play?stopPlay():play(t.dataset.play);return}
    if(t&&t.dataset.reroll!==undefined){api("/api/reroll",{id:t.dataset.reroll}).then(d=>render(d.state)).catch(err=>toast(err.message));return}
    if(t&&t.dataset.listen!==undefined){const f=S.files.find(x=>parseInt(x.name,10)===Number(t.dataset.ch));if(!f)return;const m=(t.dataset.listen.split(" ").pop()||"0:0").split(":").map(Number);const sec=m.reduce((a,v)=>a*60+v,0);play(f.path);setTimeout(()=>{$("player").currentTime=Math.max(0,sec-2)},150);return}
    const row=e.target.closest("tbody tr,.tl i");if(row){const i=Number(row.dataset.i);if(!e.ctrlKey&&!e.metaKey){const only=selected.has(i)&&selected.size===1;selected.clear();if(only){render(S);return}}selected.has(i)?selected.delete(i):selected.add(i);render(S)}
  });
  const drop=$("mainCard");let depth=0;
  ["dragenter","dragover"].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();if(t==="dragenter")depth++;$("drop").classList.add("over")}));
  drop.addEventListener("dragleave",e=>{e.preventDefault();if(--depth<=0){depth=0;$("drop").classList.remove("over")}});
  drop.addEventListener("drop",e=>{e.preventDefault();depth=0;$("drop").classList.remove("over");if(e.dataTransfer.files.length)upload(e.dataTransfer.files)});
  $("drop").onclick=()=>$("fileInput").click();$("fileInput").onchange=()=>{if($("fileInput").files.length)upload($("fileInput").files);$("fileInput").value=""};
  document.addEventListener("dragover",e=>e.preventDefault());document.addEventListener("drop",e=>e.preventDefault());
}
function init(){
  [22050,24000,44100,48000].forEach(v=>$("sampleRate").add(new Option((v/1000)+" kHz",v)));$("sampleRate").value=44100;
  [16,24].forEach(v=>$("bitDepth").add(new Option(v+"-bit",v)));$("bitDepth").value=16;
  setSeg("channels",store.get("channels","1"));setSeg("bitrate",store.get("bitrate","96"));
  preset=store.get("preset","best");
}
init();wire();refresh();pollLogs();
</script></body></html>
"""


def main():
    v1.add_files([arg for arg in sys.argv[1:] if arg.lower().endswith(".wav")])
    port = PORT
    while True:
        try:
            server = ThreadingHTTPServer((v1.HOST, port), Handler)
            break
        except OSError:
            port += 1
            if port > PORT + 20:
                raise
    url = f"http://{v1.HOST}:{port}"
    print(f"WAV Forge v2 running at {url}")
    if v1.should_open_browser():
        threading.Timer(0.4, lambda: v1.open_url_quietly(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
