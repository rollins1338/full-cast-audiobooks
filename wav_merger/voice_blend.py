"""XTTS cleanup and voice blend, run by render.sh after it writes the chapters.

Each chapter is rebuilt from the render folder's per-line clips (output/<book>/.chunks), cleaning
every line before it is joined: trimmed ends, steady tone per character, cast-wide tone match above
300 Hz, level match and edge fades; then de-hiss and de-ess on the chapter. The blended chapter
replaces output/<book>/NNN.wav; .blend/NNN.json remembers what was done so unchanged chapters are
skipped next time. Nothing is re-rendered with XTTS except by reroll().
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import fft as sfft
from scipy.signal import oaconvolve, welch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import render_engine  # noqa: E402

SR = render_engine.SR
VERSION = 3
KEYS = ("declick", "steady_tone", "tone_match", "level_match", "edge_fades",
        "room_tone", "shared_room", "dehiss", "deess")
PRESETS = {"best": {k: k not in ("shared_room", "room_tone") for k in KEYS}, "current": {k: False for k in KEYS}}

BANDS = 80 * 2 ** (np.arange(0, 22) / 3)          # 1/3-octave centres, 80 Hz .. ~10 kHz
EDGES = np.sqrt(BANDS[:-1] * BANDS[1:])
EDGES = np.concatenate([[BANDS[0] / 2 ** (1 / 6)], EDGES, [BANDS[-1] * 2 ** (1 / 6)]])
MID = (BANDS >= 300) & (BANDS <= 3000)
BROAD = [(BANDS < 500), (BANDS >= 500) & (BANDS < 3000), (BANDS >= 3000)]
ROOM_DB = -80.0          # bed level before mastering: felt in the gaps, not heard as hiss
FFMPEG = {"dehiss": "afftdn=nr=8:nf=-60:tn=1", "deess": "deesser=i=0.4:m=0.5:f=0.5:s=o"}

_INFO_CACHE = {}
_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ render folder

def info_from_jobs(out_dir, plan, jobs, chapters=None):
    """Render info from the renderer's jobs: chapters to work on (default all) and every job for
    measuring the cast."""
    out = Path(out_dir)
    jobs = [{"id": j["id"], "speaker": j["speaker"], "gap": j["gap"], "chapter": j["chapter"]} for j in jobs]
    wanted = set(chapters) if chapters is not None else {j["chapter"] for j in jobs}
    by_ch = {}
    for j in jobs:
        if j["chapter"] in wanted:
            by_ch.setdefault(j["chapter"], []).append(j)
    try:
        report = json.loads((out / "render_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    picked = [j for c in by_ch.values() for j in c]
    suspects = [{"id": cid, "chapter": s.get("chapter"), "speaker": s.get("speaker", ""), "at": s.get("at", "")}
                for cid, s in report.get("suspect_chunks", {}).items() if s.get("chapter") in by_ch]
    return {"out_dir": str(out), "plan": str(plan), "chapters": by_ch, "all": jobs, "lines": len(picked),
            "voices": len({j["speaker"] for j in picked}),
            "missing": sum(not (out / ".chunks" / f"{j['id']}.wav").is_file() for j in picked),
            "suspects": suspects}


def find_render(paths):
    """Render info for chapter WAVs loaded in the merger, or None when they don't sit in a render folder."""
    dirs = {Path(p).resolve().parent for p in paths}
    if len(dirs) != 1:
        return None
    out = dirs.pop()
    plan = REPO / "books" / out.name / "plan.json"
    if not (out / ".chunks").is_dir() or not plan.is_file():
        return None
    key = (str(plan), plan.stat().st_mtime_ns)
    with _LOCK:
        jobs = _INFO_CACHE.get(key)
        if jobs is None:
            _, jobs, *_ = render_engine.load_plan(plan, {}, 200, None)
            render_engine.assign_ids(jobs)
            _INFO_CACHE[key] = jobs
    loaded = {int(Path(p).stem) for p in paths if Path(p).stem.isdigit()}
    info = info_from_jobs(out, plan, jobs, loaded)
    info["blended"] = sum(is_blended(info, ch) for ch in info["chapters"])
    return info


def is_blended(info, ch):
    """True when NNN.wav is still the file blend_book wrote (render.sh rewrites it raw when a line changes)."""
    out = Path(info["out_dir"])
    try:
        meta = json.loads((out / ".blend" / f"{ch:03d}.json").read_text())
        return meta.get("mtime") == (out / f"{ch:03d}.wav").stat().st_mtime_ns
    except (OSError, ValueError):
        return False


def chunk_path(info, cid):
    return Path(info["out_dir"]) / ".chunks" / f"{cid}.wav"


# ------------------------------------------------------------------ measuring

def gated_db(x):
    """RMS level in dBFS of the 20 ms frames within 40 dB of the loudest frame."""
    hop = SR // 50
    n = len(x) // hop
    if n == 0:
        return -96.0
    e = (x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)
    keep = e[e > e.max() * 1e-4]
    return float(10 * np.log10(keep.mean() + 1e-12))


def band_db(x):
    f, p = welch(x, SR, nperseg=2048)
    idx = np.digitize(f, EDGES) - 1
    out = np.array([p[idx == b].mean() if np.any(idx == b) else 1e-12 for b in range(len(BANDS))])
    return 10 * np.log10(out + 1e-12)


def normalized(spec):
    return spec - spec[MID].mean()


def analyse_voices(info, progress_cb=None):
    """Per-speaker level and long-term spectrum from up to 120 lines each, cached in .blend/voices.json."""
    by_spk = {}
    for j in info["all"]:
        if chunk_path(info, j["id"]).is_file():
            by_spk.setdefault(j["speaker"], []).append(j["id"])
    picks = {}
    for spk, ids in by_spk.items():
        ids = list(dict.fromkeys(ids))
        picks[spk] = list(dict.fromkeys(ids[k] for k in np.linspace(0, len(ids) - 1, min(120, len(ids))).astype(int)))
    key = hashlib.sha256(json.dumps([VERSION, sorted(picks.items())]).encode()).hexdigest()
    blend = Path(info["out_dir"]) / ".blend"
    blend.mkdir(exist_ok=True)
    cache = blend / "voices.json"
    try:
        data = json.loads(cache.read_text())
        if data.get("key") == key:
            return data
    except (OSError, ValueError):
        pass
    speakers = {}
    for i, (spk, pick) in enumerate(sorted(picks.items())):
        levels, specs, weights = [], [], []
        for cid in pick:
            p = chunk_path(info, cid)
            if not p.is_file():
                continue
            x = render_engine.read_wav(p)
            if len(x) < SR // 4:
                continue
            levels.append(gated_db(x))
            specs.append(10 ** (normalized(band_db(x)) / 10))
            weights.append(len(x))
        if levels:
            spec = 10 * np.log10(np.average(specs, axis=0, weights=weights))
            speakers[spk] = {"level": float(np.median(levels)), "spec": normalized(spec).tolist(), "n": len(levels)}
        if progress_cb:
            progress_cb((i + 1) / len(by_spk))
    lines = {s: len(v) for s, v in by_spk.items()}
    narr = "Narrator" if "Narrator" in speakers else max(speakers, key=lambda s: lines[s])
    target_spec = 0.5 * np.array(speakers[narr]["spec"]) + 0.5 * np.median([s["spec"] for s in speakers.values()], axis=0)
    data = {"key": key, "speakers": speakers, "narrator": narr, "target_level": speakers[narr]["level"],
            "target_spec": target_spec.tolist()}
    cache.write_text(json.dumps(data))
    return data


def tone_curve(stats, spk):
    """Gentle cast EQ for one speaker in dB per band: half-strength, octave-smoothed, +-4 dB."""
    s = stats["speakers"].get(spk)
    if not s or s["n"] < 8:
        return np.zeros(len(BANDS))
    d = np.array(stats["target_spec"]) - np.array(s["spec"])
    d = np.convolve(np.pad(d, 2, mode="edge"), np.ones(5) / 5, mode="valid")
    # below ~300 Hz the difference is the voice itself (pitch, chest), not the mic: leave it alone
    keep = np.clip(np.log2(BANDS / 160), 0, 1)
    return np.clip(0.5 * d * keep, -4, 4)


# ------------------------------------------------------------------ per line

def declick(x):
    """DC offset off and dead air trimmed from the line ends. (No sample-level click removal: on
    speech it eats real s and t sounds, and XTTS doesn't make that kind of click.)"""
    x = x - x.mean()
    hop = SR // 100
    n = len(x) // hop
    if n > 4:
        e = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
        loud = np.nonzero(e > e.max() * 10 ** (-45 / 20))[0]
        if len(loud):
            cap = int(0.3 * SR)
            a = min(max(0, loud[0] * hop - int(0.04 * SR)), cap)
            b = max(min(len(x), (loud[-1] + 1) * hop + int(0.12 * SR)), len(x) - cap)
            x = x[a:b]
    return x


def eq(x, gains_db):
    """Zero-phase EQ from a per-band dB curve, interpolated on log frequency."""
    if not np.any(np.abs(gains_db) > 0.05):
        return x
    n = sfft.next_fast_len(len(x) + 4096)
    f = np.fft.rfftfreq(n, 1 / SR)
    g = np.interp(np.log2(np.maximum(f, 1)), np.log2(BANDS), gains_db)
    y = sfft.irfft(sfft.rfft(x, n) * 10 ** (g / 20), n)[: len(x)]
    return y.astype(np.float32)


def steady_gains(x, stats, spk):
    s = stats["speakers"].get(spk)
    if not s or len(x) < 0.6 * SR:
        return np.zeros(len(BANDS))
    diff = np.array(s["spec"]) - normalized(band_db(x))
    lo, mid, hi = (diff[m].mean() for m in BROAD)
    lo, hi = (float(np.clip(0.5 * (v - mid), -3, 3)) for v in (lo, hi))
    return np.interp(np.log2(BANDS), np.log2([250, 500, 3000, 6000]), [lo, 0, 0, hi])


def process_line(x, spk, opts, stats, curves):
    if len(x) == 0:
        return x
    if opts.get("declick"):
        x = declick(x)
    gains = np.zeros(len(BANDS))
    if opts.get("steady_tone"):
        gains = gains + steady_gains(x, stats, spk)
    if opts.get("tone_match"):
        if spk not in curves:
            curves[spk] = tone_curve(stats, spk)
        gains = gains + curves[spk]
    x = eq(x, gains)
    if opts.get("level_match") and spk in stats["speakers"]:
        own = stats["speakers"][spk]["level"]
        g = (stats["target_level"] - own) + 0.6 * (own - gated_db(x))
        lim = 4 if len(x) < SR // 2 else 9
        g = float(np.clip(g, -lim, lim))
        peak = float(np.abs(x).max()) or 1.0
        g = min(g, 20 * np.log10(0.95 / peak))
        x = (x * 10 ** (g / 20)).astype(np.float32)
    if opts.get("edge_fades"):
        k = min(int(0.015 * SR), len(x) // 4)
        if k:
            ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k))).astype(np.float32)
            x = x.copy()
            x[:k] *= ramp
            x[-k:] *= ramp[::-1]
    return x


# ------------------------------------------------------------------ per chapter

def pink_bed(n, seed=7):
    """Seamless dark noise, 60 Hz - 1.2 kHz, at ROOM_DB RMS: a 37 s loop tiled to n samples."""
    m = 37 * SR
    rng = np.random.default_rng(seed)
    spec = rng.standard_normal(m // 2 + 1) + 1j * rng.standard_normal(m // 2 + 1)
    f = np.fft.rfftfreq(m, 1 / SR)
    spec *= np.where((f >= 60) & (f <= 1200), 1 / np.maximum(f, 1), 0)
    loop = np.fft.irfft(spec, m)
    loop *= 10 ** (ROOM_DB / 20) / np.sqrt((loop ** 2).mean())
    return np.resize(loop.astype(np.float32), n)


def room_ir(seed=11):
    rng = np.random.default_rng(seed)
    t = np.arange(int(0.35 * SR)) / SR
    ir = rng.standard_normal(len(t)) * np.exp(-6.91 * t / 0.25)
    ir = np.convolve(ir, np.ones(4) / 4, mode="same")
    ir[: int(0.004 * SR)] = 0                       # a short pre-delay keeps the dry line in front
    return (ir / np.sqrt((ir ** 2).sum())).astype(np.float32)


def ffmpeg_clean(audio, opts, workdir):
    chain = ",".join(FFMPEG[k] for k in ("dehiss", "deess") if opts.get(k))
    if not chain:
        return audio
    fd, src = tempfile.mkstemp(suffix=".wav", dir=workdir)
    os.close(fd)
    dst = src[:-4] + "_out.wav"
    try:
        sf.write(src, audio, SR, subtype="FLOAT")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", chain, "-c:a", "pcm_f32le", dst],
                       check=True)
        out, _ = sf.read(dst, dtype="float32")
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass
    out = out[: len(audio)]
    return np.pad(out, (0, len(audio) - len(out))) if len(out) < len(audio) else out


def assemble_lines(info, jobs, opts, stats, cancel=None, lead=True):
    curves = {}
    parts = [np.zeros(int(render_engine.CHAPTER_LEAD * SR), np.float32)] if lead else []
    for j in jobs:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        x = process_line(render_engine.read_wav(chunk_path(info, j["id"])), j["speaker"], opts, stats, curves)
        gap = j["gap"] if j is not jobs[-1] else render_engine.CHAPTER_TAIL
        parts += [x, np.zeros(int(gap * SR), np.float32)]
    return np.concatenate(parts)


def finish_chapter(audio, opts, workdir):
    if opts.get("shared_room"):
        audio = audio + 0.08 * oaconvolve(audio, room_ir())[: len(audio)].astype(np.float32)
    audio = ffmpeg_clean(audio, opts, workdir)
    if opts.get("room_tone"):
        audio = audio + pink_bed(len(audio))
    peak = float(np.abs(audio).max()) if len(audio) else 0
    lim = 10 ** (-1 / 20)
    if peak > lim:
        audio = audio * (lim / peak)
    return audio.astype(np.float32)


def signature(info, ch, opts, stats):
    chunks = Path(info["out_dir"]) / ".chunks"
    rows = []
    for j in info["chapters"][ch]:
        try:
            m = (chunks / f"{j['id']}.wav").stat().st_mtime_ns
        except OSError:
            m = 0
        rows.append([j["id"], j["gap"], m])
    blob = json.dumps([VERSION, {k: bool(opts.get(k)) for k in KEYS}, stats["key"], rows])
    return hashlib.sha256(blob.encode()).hexdigest()


def blend_book(info, options=None, progress_cb=None, log_cb=None, cancel_event=None):
    """Blend each chapter in place: output/<book>/NNN.wav is replaced by its blended version.
    Chapters already blended with the same lines and options are skipped. Returns how many were blended."""
    options = options or PRESETS["best"]
    log = log_cb or (lambda m, t=None: None)
    prog = progress_cb or (lambda p, l="": None)
    out_dir = Path(info["out_dir"])
    meta_dir = out_dir / ".blend"
    meta_dir.mkdir(exist_ok=True)
    prog(0, "measuring the cast")
    stats = analyse_voices(info, lambda f: prog(5 * f, "measuring the cast"))
    chapters = sorted(info["chapters"])
    done, made = [0], [0]
    lock = threading.Lock()

    def one(ch):
        out = out_dir / f"{ch:03d}.wav"
        meta = meta_dir / f"{ch:03d}.json"
        jobs = info["chapters"][ch]
        if not out.is_file():
            return False
        if any(not chunk_path(info, j["id"]).is_file() for j in jobs):
            log(f"Chapter {ch}: some line clips are missing, left as rendered.", "warning")
            return False
        sig = signature(info, ch, options, stats)
        try:
            old = json.loads(meta.read_text())
            if old.get("sig") == sig and old.get("mtime") == out.stat().st_mtime_ns:
                return False
        except (OSError, ValueError):
            pass
        audio = finish_chapter(assemble_lines(info, jobs, options, stats, cancel_event), options, meta_dir)
        render_engine.write_wav_atomic(out, audio)
        meta.write_text(json.dumps({"sig": sig, "mtime": out.stat().st_mtime_ns, "seconds": round(len(audio) / SR, 2)}))
        return True

    def run(ch):
        blended = one(ch)
        with lock:
            done[0] += 1
            made[0] += blended
            prog(5 + 95 * done[0] / len(chapters), f"chapter {ch} {'blended' if blended else 'already blended'}"
                 f" ({done[0]} of {len(chapters)})")

    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(run, ch) for ch in chapters]
        try:
            for f in futures:
                f.result()
        except Cancelled:
            for f in futures:
                f.cancel()
            raise
    return made[0]


# ------------------------------------------------------------------ re-roll

def reroll(info, chunk_id):
    """Delete one line's clip and let render.sh render it again (it rebuilds and re-blends the chapter)."""
    ch = next((c for c, jobs in info["chapters"].items() if any(j["id"] == chunk_id for j in jobs)), None)
    if ch is None:
        return 1
    try:
        chunk_path(info, chunk_id).unlink()
    except FileNotFoundError:
        pass
    with open(Path(info["out_dir"]) / "reroll.log", "a", encoding="utf-8") as log:
        return subprocess.run([str(REPO / "render.sh"), info["plan"], "--chapters", str(ch), "--yes", "--plain"],
                              cwd=REPO, stdout=log, stderr=subprocess.STDOUT).returncode
