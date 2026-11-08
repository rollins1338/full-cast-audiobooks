#!/usr/bin/env python3
"""Universal full-cast renderer: any plan.json -> one audio file per chapter.

Run it through ./render.sh (preflight checks, sleep inhibit, confirmation).

Plan format (what plan_tools.py build writes; older plans work too):
    {"book": "...", "cast": {"Speaker": "/path/voice.mp3"},        # cast optional
     "chapters": [{"title": "...", "segments": [
         {"speaker": "Narrator", "voice_ref": "/path.mp3", "text": "...",
          "language": "en", "speed": 1.0, "pause_after": 1.0}]}]}  # last three optional
Voice per segment: --voice override > segment voice_ref > plan cast > voices/<voice|speaker>.*

Every chunk is cached under <out>/.chunks by a hash of (text, voice audio,
language, speed, model settings). Re-running resumes; editing the plan or
swapping a voice re-renders only the chunks that actually changed.
"""

import argparse, contextlib, hashlib, io, json, os, re, signal, statistics, subprocess
import sys, tempfile, threading, time, warnings
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
SR = 24000
ENGINE = "xtts_v2/r1"
SAMPLING = dict(temperature=0.65, length_penalty=1.0, repetition_penalty=2.0, top_k=50, top_p=0.85)
REF_FILTER = "highpass=f=60,lowpass=f=12000,loudnorm=I=-20:TP=-2:LRA=7"
GAP_SAME, GAP_SPEAKER, GAP_ANNOUNCE = 0.15, 0.35, 1.0
CHAPTER_LEAD, CHAPTER_TAIL = 0.5, 1.5
SPEECH_CPS = 14.5  # typical XTTS speaking rate, for estimates
ANNOUNCE_RE = re.compile(
    r"^(this is,.*|(chapter|prologue|epilogue|interlude)\b(\W+\w+){0,6}\W*"
    r"|(part|book)\s+([0-9]+|[ivxlc]+|one|two|three|four|five|six|seven|eight|nine|ten)\W*)$", re.I)
STATS_FILE = Path.home() / ".cache" / "full_cast_audiobooks" / "speed.json"

for var, sub in (("NUMBA_CACHE_DIR", "numba"), ("MPLCONFIGDIR", "mpl")):
    os.environ.setdefault(var, str(Path(tempfile.gettempdir()) / f"full_cast_{sub}_cache"))
    Path(os.environ[var]).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("COQUI_TOS_AGREED", "1")
warnings.filterwarnings("ignore")

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


# ------------------------------------------------------------------ helpers

def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def fmt_ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "book"


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_voice(name):
    if not name:
        return None
    p = Path(name)
    if p.is_file():
        return p.resolve()
    if not p.is_absolute() and (ROOT / p).is_file():
        return (ROOT / p).resolve()
    stem = p.name
    for ext in ("", ".mp3", ".wav", ".flac", ".m4a", ".ogg"):
        if (VOICES / f"{stem}{ext}").is_file():
            return (VOICES / f"{stem}{ext}").resolve()
    if stem.endswith("_ref.wav"):  # legacy prepared-reference names
        return find_voice(stem[:-8].replace("_", " "))
    return None


def parse_ranges(spec, n):
    if not spec:
        return set(range(1, n + 1))
    picked = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        picked.update(range(int(a), int(b or a) + 1))
    return {c for c in picked if 1 <= c <= n}


def tts_text(text):
    """Plain spoken text: no italics markers, quote marks or odd whitespace."""
    t = text.replace("*", "").replace("…", "...")
    t = t.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    return re.sub(r"\s+", " ", t).strip().strip('"').strip()


def finish(t):
    """End every chunk on a full stop: XTTS rambles after a trailing comma or dash."""
    t = re.sub(r"[,;:\-–—\s]+$", "", t)
    if t and t[-1] not in ".!?'\"":
        t += "."
    return t


def book_names(plan):
    """Words the book only ever writes capitalized, even mid-sentence: names like Mia or Collymore."""
    caps, lower = set(), set()
    for ch in plan["chapters"]:
        for seg in ch.get("segments", []):
            t = tts_text(seg.get("text", ""))
            caps.update(re.findall(r"(?<=[a-z,;] )([A-Z][a-z'’]+)\b", t))
            lower.update(re.findall(r"\b([a-z][a-z'’]+)\b", t))
    return {w for w in caps if w.lower() not in lower}


def name_stop(piece, names):
    """A 1-2 word line ending in a name and a full stop ("Mia.") makes XTTS say "Mia dot".
    Ending it on an ellipsis instead keeps the name and drops the "dot"."""
    m = re.search(r"\b([A-Z][\w'’]*)\.$", piece)
    if m and m.group(1) in names and len(piece.split()) <= 2 and not piece.endswith(".."):
        return piece + ".."
    return piece


def split_text(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    pieces, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = sentence.split() if len(sentence) > max_chars else [sentence]
        for w in words:
            if cur and len(cur) + 1 + len(w) > max_chars:
                pieces.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
    if cur:
        pieces.append(cur)
    # fold 1-3 word scraps into a neighbour: XTTS garbles tiny inputs
    i = 0
    while i < len(pieces):
        if len(pieces[i].split()) <= 3 and len(pieces) > 1:
            j = i - 1 if i else i + 1
            if len(pieces[j]) + 1 + len(pieces[i]) <= max_chars + 40:
                pieces[min(i, j)] = f"{pieces[min(i, j)]} {pieces[max(i, j)]}"
                del pieces[max(i, j)]
                continue
        i += 1
    return pieces


# ------------------------------------------------------------------ plan

class PlanError(Exception):
    pass


def load_plan(path, overrides, max_chars, chapter_spec):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if "chapters" not in plan:
        if "segments" not in plan:
            raise PlanError("plan has neither 'chapters' nor 'segments'")
        plan = {**plan, "chapters": [{"title": plan.get("title", "Chapter 1"), "segments": plan["segments"]}]}
    cast = plan.get("cast", {})
    problems, voice_cache = [], {}

    def resolve(seg):
        speaker = seg.get("speaker") or "Unknown"
        for candidate in (overrides.get(speaker), seg.get("voice_ref"), cast.get(speaker),
                          seg.get("voice"), speaker):
            if candidate:
                key = str(candidate)
                if key not in voice_cache:
                    voice_cache[key] = find_voice(key)
                if voice_cache[key]:
                    return voice_cache[key]
                if candidate in (overrides.get(speaker), seg.get("voice_ref")):
                    break  # an explicit reference that is missing is an error, not a fallback
        problems.append(f"no voice file for speaker {speaker!r} (voice_ref={seg.get('voice_ref')!r})")
        return None

    chapters = plan["chapters"]
    wanted = parse_ranges(chapter_spec, len(chapters))
    jobs, segments_total, speaker_voices = [], 0, defaultdict(set)
    names = book_names(plan)
    for ci, ch in enumerate(chapters, 1):
        run = None
        runs = []
        for si, seg in enumerate(ch.get("segments", [])):
            raw = seg.get("text", "")
            if not re.search(r"\w", raw):
                continue
            segments_total += 1
            voice = resolve(seg)
            if voice is None:
                continue
            speaker = seg.get("speaker") or "Unknown"
            speaker_voices[speaker].add(voice.stem)
            text = tts_text(raw)
            standalone = bool(ANNOUNCE_RE.match(finish(text))) or "pause_after" in seg
            sig = (speaker, str(voice), seg.get("language", "en"), float(seg.get("speed", 1.0)))
            if run and run["sig"] == sig and not run["standalone"] and not standalone:
                run["texts"].append(text)
            else:
                run = {"sig": sig, "texts": [text], "standalone": standalone, "seg": si,
                       "pause_after": seg.get("pause_after")}
                runs.append(run)
        if ci not in wanted:
            continue
        for ri, r in enumerate(runs):
            speaker, voice, lang, speed = r["sig"]
            pieces = [name_stop(finish(p), names) for p in split_text(" ".join(r["texts"]), max_chars)]
            for pi, piece in enumerate(pieces):
                last_of_run = pi == len(pieces) - 1
                next_speaker = runs[ri + 1]["sig"][0] if ri + 1 < len(runs) else None
                if not last_of_run:
                    gap = GAP_SAME
                elif r["pause_after"] is not None:
                    gap = float(r["pause_after"])
                elif r["standalone"]:
                    gap = GAP_ANNOUNCE
                else:
                    gap = GAP_SPEAKER if next_speaker != speaker else GAP_SAME
                jobs.append({"chapter": ci, "speaker": speaker, "voice": Path(voice), "lang": lang,
                             "speed": speed, "text": piece, "gap": gap})
    if problems:
        counts = Counter(problems)
        raise PlanError("\n".join(f"{p}  (x{n})" for p, n in counts.items()))
    return plan, jobs, segments_total, speaker_voices, wanted


def assign_ids(jobs):
    shas = {}
    for job in jobs:
        v = job["voice"]
        if v not in shas:
            shas[v] = file_sha(v)
        job["voice_sha"] = shas[v]
        key = json.dumps({"e": ENGINE, "s": SAMPLING, "f": REF_FILTER, "v": shas[v], "t": job["text"],
                          "l": job["lang"], "sp": job["speed"]}, sort_keys=True, ensure_ascii=False)
        job["id"] = hashlib.sha256(key.encode()).hexdigest()[:24]


# ------------------------------------------------------------------ audio

def read_wav(path):
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data


def fsync_replace(tmp, path):
    """Rename into place only after the bytes are on disk, so a power cut can't leave a torn file."""
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_wav_atomic(path, data):
    import soundfile as sf
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}.wav")
    sf.write(str(tmp), data, SR, subtype="PCM_16")
    fsync_replace(tmp, path)


def write_text_atomic(path, text):
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    fsync_replace(tmp, path)


def chunk_ok(path, deep=False):
    """Cheap size check; deep=True also proves it is a readable WAV with the size its header claims."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= 1000:
        return False
    if not deep:
        return True
    # The WAV header records how many bytes of audio follow; a file cut short by
    # a crash or power cut is shorter than that, even though it still "plays".
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        pos = head.find(b"data", 12)
        if pos < 0:
            return False
        declared = int.from_bytes(head[pos + 4:pos + 8], "little")
        return 0 < declared and pos + 8 + declared <= size
    except OSError:
        return False


def scan_chunks(jobs, chunks_dir):
    """Resume check: keep valid chunks, delete damaged ones (re-rendered), sweep crash leftovers."""
    for stale in list(chunks_dir.glob(".*.tmp*")) + list(chunks_dir.parent.glob(".*.tmp*")):
        stale.unlink(missing_ok=True)
    good, bad = set(), 0
    for j in jobs:
        p = chunks_dir / f"{j['id']}.wav"
        if j["id"] in good or not p.exists():
            continue
        if chunk_ok(p, deep=True):
            good.add(j["id"])
        else:
            p.unlink(missing_ok=True)
            bad += 1
    return good, bad


def trim(wav):
    import numpy as np
    if wav.size == 0:
        return wav
    hop = SR // 100
    frames = wav[: len(wav) // hop * hop].reshape(-1, hop)
    if not len(frames):
        return wav
    rms = np.sqrt((frames ** 2).mean(axis=1))
    thresh = max(rms.max() * 10 ** (-40 / 20), 1e-4)
    loud = np.nonzero(rms > thresh)[0]
    if not len(loud):
        return wav[:0]
    start = max(0, loud[0] * hop - int(0.06 * SR))
    end = min(len(wav), (loud[-1] + 1) * hop + int(0.12 * SR))
    out = wav[start:end].copy()
    fade = min(len(out) // 4, int(0.01 * SR))
    if fade:
        out[:fade] *= np.linspace(0, 1, fade, dtype=out.dtype)
        out[-fade:] *= np.linspace(1, 0, fade, dtype=out.dtype)
    return out


def duration_window(chars):
    """(min, expected, max) plausible seconds of speech for a text length."""
    return chars * 0.025, 0.3 + chars / SPEECH_CPS, 1.2 + chars * 0.13


# ------------------------------------------------------------------ gpu stats

def cpu_temp():
    """'  CPU 88°C' (red when throttling territory), or '' when the sensor isn't there."""
    for h in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (h / "name").read_text().strip() == "coretemp":
                t = int((h / "temp1_input").read_text()) // 1000
                return f"  CPU [red]{t}°C[/]" if t >= 90 else f"  CPU {t}°C"
        except OSError:
            continue
    return ""


class GpuMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.info = None

    def run(self):
        while True:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
                util, used, total, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
                self.info = (f"GPU {util}%  VRAM {int(float(used))}/{int(float(total))} MB  "
                             f"{temp}°C  {power} W{cpu_temp()}")
            except Exception:
                self.info = None
            time.sleep(3)


# ------------------------------------------------------------------ check

def estimate_cps():
    try:
        return json.loads(STATS_FILE.read_text())["chars_per_sec"]
    except Exception:
        return None


def save_cps(cps):
    try:
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps({"chars_per_sec": cps, "updated": datetime.now().isoformat()}))
    except OSError:
        pass


def print_check(plan, jobs, segments_total, speaker_voices, wanted, out_dir, plan_path):
    chunks_dir = out_dir / ".chunks"
    cached = {j["id"] for j in jobs if chunk_ok(chunks_dir / f"{j['id']}.wav")}
    chars = sum(len(j["text"]) for j in jobs)
    todo_chars = sum(len(j["text"]) for j in jobs if j["id"] not in cached)
    by_speaker = defaultdict(lambda: [0, 0, 0, None])
    for j in jobs:
        s = by_speaker[j["speaker"]]
        s[0] += 1
        s[1] += len(j["text"])
        s[2] += j["id"] in cached
        s[3] = j["voice"].stem

    console.rule(f"[bold]{plan.get('book') or Path(plan_path).stem}")
    info = Table.grid(padding=(0, 2))
    info.add_row("Plan", str(plan_path))
    info.add_row("Output", str(out_dir))
    info.add_row("Chapters", f"{len(wanted)} selected of {len(plan['chapters'])}")
    info.add_row("Segments", f"{segments_total:,} -> {len(jobs):,} synthesis chunks")
    info.add_row("Text", f"{chars:,} chars, ~{sum(len(j['text'].split()) for j in jobs):,} words")
    info.add_row("Audio length", f"~{fmt_dur(chars / SPEECH_CPS)} (estimate)")
    info.add_row("Already cached", f"{len(cached):,}/{len(jobs):,} chunks "
                 f"({100 * (chars - todo_chars) / max(chars, 1):.1f}% of text)")
    cps = estimate_cps()
    if cps:
        eta = todo_chars / cps
        info.add_row("Render time", f"~{fmt_dur(eta)} at your last speed ({cps:.1f} chars/s), "
                     f"done ~{(datetime.now() + timedelta(seconds=eta)):%a %H:%M}")
    else:
        info.add_row("Render time", "unknown until the first run measures your GPU speed")
    info.add_row("Disk needed", f"~{chars / SPEECH_CPS * SR * 2 * 2 / 1e9:.1f} GB (chunks + chapters)")
    console.print(info)

    t = Table(title="Cast", title_justify="left", header_style="bold")
    for col, kw in (("Speaker", {}), ("Voice", {}), ("Chunks", {"justify": "right"}),
                    ("Chars", {"justify": "right"}), ("Share", {"justify": "right"}),
                    ("Cached", {"justify": "right"})):
        t.add_column(col, **kw)
    for speaker, (n, c, done, voice) in sorted(by_speaker.items(), key=lambda kv: -kv[1][1]):
        t.add_row(speaker, voice, f"{n:,}", f"{c:,}", f"{100 * c / max(chars, 1):.1f}%", f"{done}/{n}")
    console.print(t)

    warns = []
    for speaker, voices in speaker_voices.items():
        if len(voices) > 1:
            warns.append(f"{speaker!r} uses {len(voices)} different voices: {sorted(voices)}")
    by_voice = defaultdict(set)
    for speaker, voices in speaker_voices.items():
        for v in voices:
            by_voice[v].add(speaker)
    for v, speakers in by_voice.items():
        if len(speakers) > 1:
            warns.append(f"voice {v!r} is shared by {sorted(speakers)}")
    for voice in sorted({j["voice"] for j in jobs}):
        try:
            d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                      "-of", "csv=p=0", str(voice)], capture_output=True, text=True).stdout)
            if d < 6:
                warns.append(f"voice sample {voice.name} is only {d:.1f}s (6-30s clones best)")
        except ValueError:
            warns.append(f"voice sample {voice.name} could not be probed")
    for w in warns:
        console.print(f"[yellow]note[/]  {w}")
    return todo_chars


# ------------------------------------------------------------------ render

class Dashboard:
    def __init__(self, plan, jobs, cached, out_dir, device, gpu, log_path):
        self.plan, self.jobs, self.out_dir, self.device, self.gpu = plan, jobs, out_dir, device, gpu
        self.log_path = log_path
        self.total_chars = sum(len(j["text"]) for j in jobs)
        self.done_chars = sum(len(j["text"]) for j in jobs if j["id"] in cached)
        self.done_chunks = len(cached)
        self.cached_at_start = len(cached)
        self.started = time.monotonic()
        self.compute_s = self.audio_s = self.new_chars = 0.0
        self.new_chunks = 0
        self.recent = deque(maxlen=8)
        self.speed_window = deque(maxlen=25)
        self.current = None
        self.status = "starting"
        self.retries = self.suspects = 0
        self.chapters_done = []
        self.chapter_total = Counter(j["chapter"] for j in jobs)
        self.chapter_done = Counter(j["chapter"] for j in jobs if j["id"] in cached)
        self.speaker_total = Counter(j["speaker"] for j in jobs)
        self.speaker_done = Counter(j["speaker"] for j in jobs if j["id"] in cached)
        self.fallback_cps = estimate_cps()
        self.voice_of = {j["speaker"]: j["voice"].stem for j in jobs}

    def cps(self):
        if self.speed_window:
            c = sum(x[0] for x in self.speed_window)
            s = sum(x[1] for x in self.speed_window)
            return c / s if s else None
        return self.fallback_cps

    def __rich__(self):
        chapters = self.plan["chapters"]
        elapsed = time.monotonic() - self.started
        cps = self.cps()
        remaining = self.total_chars - self.done_chars
        eta = remaining / cps if cps else None
        pct = 100 * self.done_chars / max(self.total_chars, 1)

        head = Table.grid(expand=True)
        head.add_column(); head.add_column(justify="right")
        head.add_row(f"[bold]{self.plan.get('book') or 'Audiobook'}[/]  [dim]→ {self.out_dir}[/]",
                     f"[dim]{self.device}[/]")
        head.add_row(f"[dim]{self.status}[/]", f"[dim]{self.gpu.info or ''}[/]")

        overall = Table.grid(padding=(0, 1), expand=True)
        overall.add_column(width=10); overall.add_column(ratio=1); overall.add_column(justify="right", width=30)
        overall.add_row("[bold]Book", ProgressBar(total=self.total_chars, completed=self.done_chars),
                        f"[bold]{pct:5.1f}%[/]  {self.done_chunks:,}/{len(self.jobs):,} chunks")
        cur_ch = self.current["chapter"] if self.current else None
        if cur_ch:
            overall.add_row(f"Chapter {cur_ch}", ProgressBar(total=self.chapter_total[cur_ch],
                                                              completed=self.chapter_done[cur_ch]),
                            f"{self.chapter_done[cur_ch]}/{self.chapter_total[cur_ch]} chunks of "
                            f"{len(self.chapter_total)} ch")

        stats = Table.grid(padding=(0, 3))
        for _ in range(4):
            stats.add_column()
        rtf = self.audio_s / self.compute_s if self.compute_s else 0
        stats.add_row(
            f"[dim]elapsed[/] {fmt_dur(elapsed)}",
            f"[dim]ETA[/] [bold]{fmt_dur(eta) if eta is not None else '…'}[/]"
            + (f" [dim](~{(datetime.now() + timedelta(seconds=eta)):%a %H:%M})[/]" if eta else ""),
            f"[dim]speed[/] {cps or 0:.1f} chars/s",
            f"[dim]realtime[/] {rtf:.2f}x",
        )
        stats.add_row(
            f"[dim]text[/] {self.done_chars:,}/{self.total_chars:,} chars",
            f"[dim]audio made[/] {fmt_dur(self.audio_s)}",
            f"[dim]book length[/] ~{fmt_dur(self.total_chars / SPEECH_CPS)}",
            f"[dim]rate[/] {60 * self.new_chunks / max(self.compute_s, 1):.1f} chunks/min",
        )
        stats.add_row(
            f"[dim]cached at start[/] {self.cached_at_start:,}",
            f"[dim]new this run[/] {self.new_chunks:,}",
            f"[dim]retries[/] {'[yellow]' if self.retries else ''}{self.retries}",
            f"[dim]suspect[/] {'[red]' if self.suspects else ''}{self.suspects}",
        )

        now = Text()
        if self.current:
            j = self.current
            title = chapters[j["chapter"] - 1].get("title", "")
            now.append(f"{j['speaker']}", style="bold cyan")
            now.append(f"  ({j['voice'].stem})  ch {j['chapter']} {title}  ·  {len(j['text'])} chars"
                       f"  ·  attempt {j.get('attempt', 1)}\n", style="dim")
            now.append(j["text"][:300])
        recent = Table(expand=True, box=None, header_style="dim", pad_edge=False)
        for col, kw in (("time", {"width": 8}), ("ch", {"width": 4, "justify": "right"}),
                        ("speaker", {"width": 14}), ("text", {"ratio": 1, "no_wrap": True}),
                        ("chars", {"width": 5, "justify": "right"}), ("audio", {"width": 6, "justify": "right"}),
                        ("took", {"width": 6, "justify": "right"}), ("rtf", {"width": 5, "justify": "right"}),
                        ("", {"width": 10})):
            recent.add_column(col, **kw)
        for r in self.recent:
            recent.add_row(*r)

        cast = Table(expand=True, box=None, header_style="dim", pad_edge=False)
        cast.add_column("speaker"); cast.add_column("voice"); cast.add_column("done", justify="right")
        cast.add_column("", ratio=1)
        for speaker, total in self.speaker_total.most_common(10):
            done = self.speaker_done[speaker]
            voice = self.voice_of[speaker]
            cast.add_row(speaker, f"[dim]{voice}[/]", f"{done}/{total}", ProgressBar(total=total, completed=done))
        if len(self.speaker_total) > 10:
            cast.add_row(f"[dim]+{len(self.speaker_total) - 10} more[/]", "", "", "")

        done_line = Text("Chapters written: ", style="dim")
        done_line.append(", ".join(str(c) for c in self.chapters_done[-30:]) or "none yet")
        foot = Text(f"Ctrl+C pauses safely; run the same command again to resume  ·  log {self.log_path}",
                    style="dim")
        return Group(
            Panel(Group(head, Text(), overall, Text(), stats), border_style="blue"),
            Panel(now, title="now rendering", title_align="left", border_style="cyan"),
            Panel(recent, title="last chunks", title_align="left", border_style="dim"),
            Panel(cast, title="cast progress", title_align="left", border_style="dim"),
            done_line, foot,
        )


def load_model():
    import torch
    from TTS.api import TTS
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
        use_gpu = torch.cuda.is_available()
        tts.to("cuda" if use_gpu else "cpu")
    device = torch.cuda.get_device_name(0) if use_gpu else "CPU (very slow)"
    return tts.synthesizer.tts_model, device


def conditioning(model, voice, sha, cache_dir):
    import torch
    cache_dir.mkdir(parents=True, exist_ok=True)
    pt = cache_dir / f"{sha[:24]}.pt"
    if pt.is_file():
        d = torch.load(pt, map_location=next(model.parameters()).device)
        return d["gpt"], d["spk"]
    ref = cache_dir / f"{sha[:24]}.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(voice), "-ac", "1", "-ar", str(SR),
                    "-af", REF_FILTER, str(ref)], check=True)
    cfg = model.config
    gpt, spk = model.get_conditioning_latents(
        audio_path=[str(ref)], gpt_cond_len=getattr(cfg, "gpt_cond_len", 30),
        gpt_cond_chunk_len=getattr(cfg, "gpt_cond_chunk_len", 4),
        max_ref_length=getattr(cfg, "max_ref_len", 30), sound_norm_refs=getattr(cfg, "sound_norm_refs", False))
    torch.save({"gpt": gpt.cpu(), "spk": spk.cpu()}, pt)
    return gpt, spk


def assemble(chapter_jobs, chunks_dir, dest, fmt):
    import numpy as np
    parts = [np.zeros(int(CHAPTER_LEAD * SR), dtype="float32")]
    starts = []
    pos = CHAPTER_LEAD
    for j in chapter_jobs:
        wav = read_wav(chunks_dir / f"{j['id']}.wav")
        starts.append(pos)
        parts.append(wav)
        gap = j["gap"] if j is not chapter_jobs[-1] else CHAPTER_TAIL
        parts.append(np.zeros(int(gap * SR), dtype="float32"))
        pos += len(wav) / SR + gap
    audio = np.concatenate(parts)
    if fmt == "wav":
        write_wav_atomic(dest, audio)
    else:
        tmp_wav = dest.with_name(f".{dest.stem}.tmp{os.getpid()}.wav")
        write_wav_atomic(tmp_wav, audio)
        tmp = dest.with_name(f".{dest.stem}.tmp{os.getpid()}.mp3")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_wav), "-c:a", "libmp3lame",
                        "-b:a", "128k", str(tmp)], check=True)
        fsync_replace(tmp, dest)
        tmp_wav.unlink()
    return len(audio) / SR, starts


def render(args, plan, jobs, out_dir, wanted, cache_root=None):
    cache_root = cache_root or out_dir
    chunks_dir = cache_root / ".chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    state_dir = out_dir / ".state"
    state_dir.mkdir(exist_ok=True)
    log_path = out_dir / "render.log"
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} start: {args.plan} ({len(jobs)} chunks)\n")
    ext = args.format
    tty = console.is_terminal and not args.plain

    cached, damaged = scan_chunks(jobs, chunks_dir)
    if damaged:
        console.print(f"[yellow]Found {damaged} damaged chunk(s) from an earlier crash; they will be re-rendered.")
    by_chapter = defaultdict(list)
    for j in jobs:
        by_chapter[j["chapter"]].append(j)
    report_path = out_dir / "render_report.json"
    try:
        report = json.loads(report_path.read_text())
    except Exception:
        report = {}
    report.setdefault("suspect_chunks", {})
    report.setdefault("chapters", {})

    gpu = GpuMonitor()
    gpu.start()
    dash = Dashboard(plan, jobs, cached, out_dir, "loading model…", gpu, log_path)
    status_file = os.environ.get("FULLCAST_STATUS_FILE")

    def note_progress():
        # render.sh reads this to tell a crash that made progress from one stuck on the same chunk
        if status_file:
            try:
                Path(status_file).write_text(json.dumps(
                    {"out": str(out_dir), "done": dash.done_chunks, "total": len(jobs)}))
            except OSError:
                pass
    note_progress()
    stop = {"flag": False}

    def on_signal(signum, frame):
        stop["flag"] = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)

    def chapter_state(ci):
        return json.dumps([j["id"] for j in by_chapter[ci]] + [j["gap"] for j in by_chapter[ci]])

    rerendered = set()

    def write_chapter(ci):
        dest = out_dir / f"{ci:03d}.{ext}"
        st = state_dir / f"{ci:03d}.json"
        sig = hashlib.sha256(chapter_state(ci).encode()).hexdigest()
        try:
            fresh = ci not in rerendered and json.loads(st.read_text()).get("sig") == sig and (
                chunk_ok(dest, deep=True) if ext == "wav" else dest.stat().st_size > 1000)
        except (OSError, ValueError):
            fresh = False
        if fresh:
            return False
        dash.status = f"assembling chapter {ci}…"
        seconds, starts = assemble(by_chapter[ci], chunks_dir, dest, ext)
        write_text_atomic(st, json.dumps({"sig": sig, "seconds": seconds}))
        report["chapters"][str(ci)] = {"file": dest.name, "seconds": round(seconds, 1),
                                       "title": plan["chapters"][ci - 1].get("title", "")}
        for j, s in zip(by_chapter[ci], starts):
            if j["id"] in report["suspect_chunks"]:
                report["suspect_chunks"][j["id"]]["at"] = f"{dest.name} {fmt_ts(s)}"
        dash.chapters_done.append(ci)
        log.write(f"chapter {ci:03d} written: {dest.name} {fmt_dur(seconds)}\n")
        log.flush()
        return True

    model = None
    latents = {}
    live = Live(dash, console=console, refresh_per_second=2, transient=False) if tty else contextlib.nullcontext()
    interrupted = False
    if not tty:
        print("Starting up (loading torch)…", flush=True)
    try:
        with live:
            dash.status = "starting up: loading torch…"
            import numpy as np
            import torch
            dash.status = "checking which chapters need work…"
            for ci in sorted(by_chapter):
                for j in by_chapter[ci]:
                    if j["id"] in cached:
                        continue
                    if model is None:
                        dash.status = "loading XTTS-v2 model onto the GPU (takes about a minute)…"
                        if not tty:
                            print("Loading XTTS-v2 model onto the GPU (takes about a minute)…", flush=True)
                        model, device = load_model()
                        dash.device = device
                        if not tty:
                            print(f"Device: {device}", flush=True)
                    if j["voice_sha"] not in latents:
                        dash.status = f"cloning voice {j['voice'].stem}…"
                        latents[j["voice_sha"]] = conditioning(model, j["voice"], j["voice_sha"], cache_root / ".voices")
                    gpt, spk = latents[j["voice_sha"]]
                    dash.current = j
                    dash.status = "rendering"
                    lo, expect, hi = duration_window(len(j["text"]))
                    best, best_score, t0, attempts = None, None, time.monotonic(), 0
                    for attempt in range(1, args.retries + 2):
                        attempts = attempt
                        j["attempt"] = attempt
                        with torch.inference_mode():
                            out = model.inference(j["text"], j["lang"], gpt, spk, speed=j["speed"],
                                                  enable_text_splitting=False, **SAMPLING)
                        wav = trim(np.asarray(out["wav"], dtype="float32").reshape(-1))
                        dur = len(wav) / SR
                        if lo <= dur <= hi:
                            best = wav  # a take of plausible length always wins over a rejected one
                            break
                        score = abs(dur - expect) / expect
                        if best is None or score < best_score:
                            best, best_score = wav, score
                        dash.retries += 1
                        log.write(f"retry {j['id']} ch{ci} {j['speaker']}: {dur:.1f}s for {len(j['text'])} chars\n")
                    took = time.monotonic() - t0
                    dur = len(best) / SR
                    suspect = not (lo <= dur <= hi)
                    write_wav_atomic(chunks_dir / f"{j['id']}.wav", best)
                    if suspect:
                        dash.suspects += 1
                        report["suspect_chunks"][j["id"]] = {"chapter": ci, "speaker": j["speaker"],
                                                             "text": j["text"], "seconds": round(dur, 2)}
                    else:
                        report["suspect_chunks"].pop(j["id"], None)
                    cached.add(j["id"])
                    n = len(j["text"])
                    dash.done_chars += n
                    dash.done_chunks += 1
                    dash.new_chunks += 1
                    dash.compute_s += took
                    dash.audio_s += dur
                    dash.new_chars += n
                    dash.speed_window.append((n, took))
                    dash.chapter_done[ci] += 1
                    rerendered.add(ci)
                    note_progress()
                    dash.speaker_done[j["speaker"]] += 1
                    flag = ("[red]suspect" if suspect
                            else f"[green]ok[/] [dim]try {attempts}[/]" if attempts > 1 else "[green]ok")
                    dash.recent.appendleft((f"{datetime.now():%H:%M:%S}", str(ci), j["speaker"][:14], j["text"],
                                            str(n), f"{dur:.1f}s", f"{took:.1f}s", f"{dur / took:.2f}", flag))
                    line = (f"{dash.done_chunks}/{len(jobs)} ch{ci} {j['speaker']} {n}ch {dur:.1f}s audio "
                            f"in {took:.1f}s{' SUSPECT' if suspect else ''}{' x' + str(attempts) if attempts > 1 else ''}")
                    log.write(line + "\n")
                    log.flush()
                    if not tty:
                        cps = dash.cps() or 0
                        eta = (dash.total_chars - dash.done_chars) / cps if cps else 0
                        print(f"[{100 * dash.done_chars / dash.total_chars:5.1f}%] {line} | "
                              f"{cps:.1f} chars/s | ETA {fmt_dur(eta)}", flush=True)
                if write_chapter(ci) and not tty:
                    print(f"Chapter {ci} written: {ci:03d}.{ext}", flush=True)
                write_text_atomic(report_path, json.dumps(report, indent=1, ensure_ascii=False))
            dash.status = "finished"
            dash.current = None
    except KeyboardInterrupt:
        interrupted = True
    finally:
        log.write(f"=== {datetime.now():%H:%M:%S} {'paused' if interrupted else 'done'}\n")
        log.close()
        write_text_atomic(report_path, json.dumps(report, indent=1, ensure_ascii=False))
        if dash.new_chars and dash.compute_s > 30:
            save_cps(dash.new_chars / dash.compute_s)

    elapsed = time.monotonic() - dash.started
    console.print()
    if interrupted:
        console.print(f"[yellow bold]Paused.[/] {dash.done_chunks:,}/{len(jobs):,} chunks saved "
                      f"({100 * dash.done_chars / dash.total_chars:.1f}%). Run the same command to resume.")
        return 130
    write_manifest(out_dir, plan, report, ext)
    if ext == "wav" and not args.no_blend:
        try:
            blend_chapters(args, out_dir)
        except KeyboardInterrupt:
            console.print("[yellow]Voice blend stopped. Run the same command to finish it.")
            return 130
    total_audio = sum(c["seconds"] for c in report["chapters"].values())
    console.rule("[bold green]Done")
    console.print(f"Rendered {dash.new_chunks:,} new chunks in {fmt_dur(elapsed)} "
                  f"({dash.audio_s / max(dash.compute_s, 1e-9):.2f}x realtime). "
                  f"Book audio: {fmt_dur(total_audio)} across {len(report['chapters'])} chapter files in {out_dir}")
    sus = report["suspect_chunks"]
    if sus:
        console.print(f"[yellow]{len(sus)} chunk(s) had implausible length (possible garble). Listen at:")
        for cid, s in list(sus.items())[:40]:
            console.print(f"  {s.get('at', 'ch' + str(s['chapter']))}  {s['speaker']}: {s['text'][:90]}")
        console.print(f"[dim]  full list: {report_path}. To re-roll one, delete .chunks/<id>.wav and re-run.")
    return 0


def blend_chapters(args, out_dir):
    """XTTS cleanup and voice blend (wav_merger/voice_blend.py) on every chapter of the book.
    Measured across the whole cast, so it covers all chapters, not only --chapters; unchanged
    ones are skipped."""
    sys.path.insert(0, str(ROOT / "wav_merger"))
    import voice_blend
    _, all_jobs, *_ = load_plan(args.plan, args.overrides, args.max_chars, None)
    assign_ids(all_jobs)
    have = [ci for ci in sorted({j["chapter"] for j in all_jobs}) if (out_dir / f"{ci:03d}.wav").is_file()]
    info = voice_blend.info_from_jobs(out_dir, Path(args.plan).resolve(), all_jobs, have)
    console.rule("[bold]Voice blend")
    t0 = time.monotonic()

    def progress(pct, label):
        if "already" not in label and label.startswith("chapter"):
            console.print(f"[dim][{pct:5.1f}%][/] {label}")
    made = voice_blend.blend_book(info, progress_cb=progress,
                                  log_cb=lambda m, t=None: console.print(f"[yellow]{m}" if t == "warning" else m))
    console.print(f"Voice blend: {made} chapter(s) blended, {len(have) - made} already done "
                  f"({fmt_dur(time.monotonic() - t0)}). Skip it with --no-blend.")


def write_manifest(out_dir, plan, report, ext):
    lines = []
    for ci in sorted(int(k) for k in report["chapters"]):
        c = report["chapters"][str(ci)]
        lines.append(f"[Chapter {ci:02d}] {c['file']}\t{fmt_ts(c['seconds'])}\t{c.get('title', '')}")
    (out_dir / "Chapter_manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def audition(args, plan, jobs, out_dir):
    """Two lines per speaker in one file: hear the whole cast before committing hours."""
    picks, seen = [], Counter()
    for j in sorted(jobs, key=lambda j: abs(len(j["text"]) - 120)):
        if seen[j["speaker"]] < 2 and 40 <= len(j["text"]) <= 200:
            seen[j["speaker"]] += 1
            picks.append(j)
    order = {s: i for i, (s, _) in enumerate(Counter(j["speaker"] for j in jobs).most_common())}
    picks.sort(key=lambda j: order[j["speaker"]])
    for j in picks:
        j["chapter"] = 1
        j["gap"] = 1.2
    sub = out_dir / "audition"
    sub.mkdir(parents=True, exist_ok=True)
    args.format = "wav"
    code = render(args, {**plan, "chapters": [{"title": "Audition"}]}, picks, sub, {1}, cache_root=out_dir)
    if code == 0:
        console.print(f"[bold]Audition file:[/] {sub / '001.wav'}")
        pos = CHAPTER_LEAD
        for j in picks:
            console.print(f"  {fmt_ts(pos)}  {j['speaker']} ({j['voice'].stem})")
            pos += len(read_wav(out_dir / ".chunks" / f"{j['id']}.wav")) / SR + j["gap"]
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--out", help="output folder (default output/<book name>)")
    ap.add_argument("--check", action="store_true", help="only validate and print stats")
    ap.add_argument("--audition", action="store_true", help="render 2 lines per speaker into one file")
    ap.add_argument("--chapters", help="e.g. 1-3,7 (default all)")
    ap.add_argument("--voice", action="append", default=[], metavar="SPEAKER=VOICE",
                    help="override a speaker's voice, e.g. 'Daniel=Corvin King' (repeatable)")
    ap.add_argument("--format", choices=("wav", "mp3"), default="wav")
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--retries", type=int, default=2, help="re-rolls for implausible-length chunks")
    ap.add_argument("--plain", action="store_true", help="line-by-line output instead of the dashboard")
    ap.add_argument("--no-blend", action="store_true", help="leave chapters raw: no XTTS cleanup / voice blend")
    args = ap.parse_args()

    overrides = {}
    for item in args.voice:
        speaker, _, voice = item.partition("=")
        if not find_voice(voice.strip()):
            console.print(f"[red]--voice: no file for {voice!r} in voices/")
            return 2
        overrides[speaker.strip()] = voice.strip()
    args.overrides = overrides
    try:
        plan, jobs, segs, speaker_voices, wanted = load_plan(args.plan, overrides, args.max_chars, args.chapters)
    except (PlanError, json.JSONDecodeError, OSError) as e:
        console.print(f"[red bold]Plan problem:[/]\n{e}")
        return 2
    if not jobs:
        console.print("[red]Nothing to render (no text in the selected chapters).")
        return 2
    assign_ids(jobs)
    plan_path = Path(args.plan).resolve()
    if args.out:
        out_dir = Path(args.out)
    elif plan.get("book"):
        out_dir = ROOT / "output" / slug(plan["book"])
    else:
        out_dir = ROOT / "output" / slug(plan_path.parent.name if plan_path.stem == "plan" else plan_path.stem)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if overrides:
        console.print("Voice overrides: " + ", ".join(f"{s} -> {v}" for s, v in overrides.items()))
    if args.check:
        print_check(plan, jobs, segs, speaker_voices, wanted, out_dir, plan_path)
        return 0
    if args.audition:
        return audition(args, plan, jobs, out_dir)
    return render(args, plan, jobs, out_dir, wanted)


if __name__ == "__main__":
    sys.exit(main())
