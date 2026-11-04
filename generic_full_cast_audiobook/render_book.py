# Full-book renderer entry point.
# It expects a reviewed analysis JSON containing:
# {
#   "chapters": [
#     {"title": "...",
#      "segments": [
#        {"speaker":"Narrator","voice":"Rebecca Soler","text":"..."},
#        ...
#      ]}
#   ]
# }
#
# Keep semantic analysis separate from synthesis so the expensive TTS step
# starts only after the chapter/speaker plan has been reviewed.

import argparse, json, os, re, subprocess, tempfile, time
from pathlib import Path

# Keep import-time caches in writable temporary locations.
os.environ.setdefault("NUMBA_CACHE_DIR", str(Path(tempfile.gettempdir()) / "full_cast_numba_cache"))
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "full_cast_mpl_cache"))
Path(os.environ["NUMBA_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

from TTS.api import TTS
import torch

MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
MAX_CHARS = 180

ROOT = Path(__file__).resolve().parent.parent
VOICE_FILES = {
    "Savannah Peachwood": ROOT / "voices/Savannah Peachwood.mp3",
    "Lee Daniels": ROOT / "voices/Lee Daniels.mp3",
    "Jacob Morgan": ROOT / "voices/Jacob Morgan.mp3",
    "Teddy Hamilton": ROOT / "voices/Teddy Hamilton.mp3",
}

def split_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_CHARS:
        return [text]
    out, cur = [], ""
    for s in re.split(r"(?<=[.!?…])\s+", text):
        if len(s) > MAX_CHARS:
            for w in s.split():
                if not cur:
                    cur = w
                elif len(cur) + 1 + len(w) <= MAX_CHARS:
                    cur += " " + w
                else:
                    out.append(cur); cur = w
        elif cur and len(cur) + 1 + len(s) <= MAX_CHARS:
            cur += " " + s
        else:
            if cur: out.append(cur)
            cur = s
    if cur: out.append(cur)
    return out

def prep(src, dst):
    subprocess.run([
        "ffmpeg","-y","-i",str(src),"-ac","1","-ar","24000",
        "-af","highpass=f=60,lowpass=f=12000,loudnorm=I=-20:TP=-2:LRA=7",
        str(dst)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def resolve_reference(value):
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if candidate.exists():
        return candidate
    # Scene plans may refer to the prepared reference name even when the
    # source MP3 is all that is present in the repository.
    if candidate.name.endswith("_ref.wav"):
        source = candidate.with_name(candidate.name[:-8].replace("_", " ") + ".mp3")
        if source.exists():
            return source
    return candidate

def output_is_valid(path):
    """Return true only when an existing chapter file is probeable audio."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    formats = result.stdout.strip().lower()
    return bool(formats)

def progress_line(done, total, chapter, chapter_total, speaker, started, force=False):
    """Print a compact progress bar without flooding redirected logs."""
    elapsed = max(time.monotonic() - started, 0.001)
    rate = done / elapsed
    remaining = max(total - done, 0)
    eta = remaining / rate if rate else 0
    width = 30
    filled = int(width * done / total) if total else width
    bar = "#" * filled + "." * (width - filled)
    pct = (100.0 * done / total) if total else 100.0
    msg = (f"Synthesis [{bar}] {pct:6.2f}% | {done}/{total} chunks | "
           f"Chapter {chapter}/{chapter_total} | {speaker} | "
           f"{rate:.2f}/s | ETA {eta/60:.1f}m")
    # A terminal can redraw one line. Logs and captured output use periodic
    # newline updates so progress messages never run together.
    if os.isatty(1):
        print("\r\033[2K" + msg, end="", flush=True)
    elif force or done == 1 or done == total or done % max(1, total // 100) == 0:
        print(msg, flush=True)

def render(plan_file, output_dir, output_format="wav", sample_rate=24000, channels=1):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    # Accept both the reviewed multi-chapter format and the existing single
    # chapter scene plans used by the command line workflow.
    if "chapters" not in plan:
        if "segments" not in plan:
            raise ValueError("Plan must contain either 'chapters' or 'segments'")
        plan = {**plan, "chapters": [{
            "title": plan.get("title") or plan.get("chapter") or "Chapter 1",
            "segments": plan["segments"],
        }]}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    extension = "wav" if output_format == "wav" else "mp3"
    total_chapters = len(plan["chapters"])
    completed = {}
    pending = []
    for ci, chapter in enumerate(plan["chapters"], 1):
        out = output_dir / f"{ci:03d}.{extension}"
        if output_is_valid(out):
            completed[ci] = out
        else:
            pending.append((ci, chapter, out))
    print(
        f"Resume check: {len(completed)}/{total_chapters} valid chapter files found; "
        f"rendering {len(pending)} remaining chapters.", flush=True
    )
    manifest_entries = {}
    for ci, chapter in enumerate(plan["chapters"], 1):
        if ci in completed:
            out = completed[ci]
            print(f"Skipping completed chapter {ci}/{total_chapters}: {out.name}", flush=True)
        else:
            out = output_dir / f"{ci:03d}.{extension}"
        manifest_entries[ci] = (
            f"[Chapter {ci:02d}] {out.name}"
            + (f"\t{chapter.get('title', '').strip()}" if chapter.get("title") else "")
        )

    if not pending:
        manifest = output_dir / "Chapter_manifest.txt"
        manifest.write_text("\n".join(manifest_entries[i] for i in range(1, total_chapters + 1)) + "\n", encoding="utf-8")
        print(f"All {total_chapters} chapters are already complete. Manifest: {manifest}", flush=True)
        return

    use_gpu = torch.cuda.is_available()
    device = torch.cuda.get_device_name(0) if use_gpu else "CPU"
    print(f"Synthesis device: {device}")
    load_started = time.monotonic()
    tts = TTS(MODEL, progress_bar=True)
    tts.to("cuda" if use_gpu else "cpu")
    model_load_seconds = time.monotonic() - load_started

    with tempfile.TemporaryDirectory(prefix="book_render_") as td:
        td = Path(td)
        refs = {}
        for name, src in VOICE_FILES.items():
            if src.exists():
                ref = td/(name.replace(" ","_")+"_ref.wav")
                prep(src, ref)
                refs[name] = ref

        synthesis_started = time.monotonic()
        total_chars = 0
        total_tokens = 0
        total_jobs = sum(
            len(split_text(seg["text"]))
            for _, chapter, _ in pending
            for seg in chapter["segments"]
        )
        completed_jobs = 0
        print(f"Starting synthesis: {total_jobs} text chunks across {total_chapters} chapters", flush=True)
        for ci, chapter, out in pending:
            clips = []
            for si, seg in enumerate(chapter["segments"], 1):
                for part, text in enumerate(split_text(seg["text"]), 1):
                    wav = td/f"{ci:03d}_{si:05d}_{part:03d}.wav"
                    total_chars += len(text)
                    try:
                        total_tokens += len(tts.synthesizer.tts_model.tokenizer.encode(text, lang=seg.get("language", "en")))
                    except Exception:
                        total_tokens += len(text.split())
                    voice = seg.get("voice") or seg.get("speaker")
                    explicit_ref = seg.get("voice_ref")
                    if explicit_ref:
                        candidate = resolve_reference(explicit_ref)
                        if candidate.exists():
                            # Reviewed full-cast plans carry an explicit
                            # reference on every segment.  Prepare each
                            # source file once and reuse it; running ffmpeg
                            # for every short paragraph makes a full book
                            # needlessly slow.
                            cache_key = str(candidate.resolve())
                            if cache_key not in refs:
                                refs[cache_key] = td / (candidate.stem + "_prepared.wav")
                                prep(candidate, refs[cache_key])
                            ref = refs[cache_key]
                        else:
                            raise FileNotFoundError(f"Voice reference not found: {candidate}")
                    elif voice in refs:
                        ref = refs[voice]
                    else:
                        # Full-cast plans may contain any reference from the
                        # local voices directory. Resolve it by cast name so
                        # adding a narrator or supporting actor never requires
                        # editing a hard-coded list in the renderer.
                        candidate = ROOT / "voices" / f"{voice}.mp3"
                        if not candidate.is_file():
                            candidate = ROOT / "voices" / f"{voice}.wav"
                        if not candidate.is_file():
                            raise ValueError(f"No voice reference configured for segment speaker/voice: {voice!r}")
                        ref = td / (candidate.stem + "_ref.wav")
                        prep(candidate, ref)
                        refs[voice] = ref
                    tts.tts_to_file(
                        text=text, language=seg.get("language","en"),
                        speaker_wav=str(ref),
                        file_path=str(wav), split_sentences=False,
                        temperature=0.65, length_penalty=1.0,
                        repetition_penalty=2.0, top_k=50, top_p=0.85,
                        speed=seg.get("speed",1.0)
                    )
                    clips.append(wav)
                    completed_jobs += 1
                    progress_line(
                        completed_jobs, total_jobs, ci, total_chapters,
                        seg.get("speaker", "Unknown"), synthesis_started,
                        force=(completed_jobs == total_jobs)
                    )

            concat = td/f"chapter_{ci}.txt"
            concat.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in clips),
                encoding="utf-8"
            )
            ffmpeg_args = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                "-ar", str(sample_rate), "-ac", str(channels),
            ]
            if output_format == "wav":
                # Lossless chapter masters: native XTTS rate, PCM16, RF64 when needed.
                ffmpeg_args += ["-c:a", "pcm_s16le", "-rf64", "auto"]
            else:
                ffmpeg_args += ["-c:a", "libmp3lame", "-b:a", "128k"]
            # FFmpeg's own animated status line collides with the synthesis
            # progress bar, so keep its detailed output out of the terminal.
            subprocess.run(
                [*ffmpeg_args, "-loglevel", "error", str(out)],
                check=True,
            )
            if os.isatty(1):
                print()
            print(f"Chapter {ci}/{total_chapters} complete: {out}", flush=True)
            manifest_entries[ci] = (
                f"[Chapter {ci:02d}] {out.name}"
                + (f"\t{chapter.get('title', '').strip()}" if chapter.get("title") else "")
            )

        manifest = output_dir / "Chapter_manifest.txt"
        manifest.write_text(
            "\n".join(manifest_entries[i] for i in range(1, total_chapters + 1)) + "\n",
            encoding="utf-8",
        )
        print("Manifest:", manifest)

    synthesis_seconds = time.monotonic() - synthesis_started
    total_seconds = time.monotonic() - load_started
    print(f"\nModel load: {model_load_seconds:.1f}s")
    print(f"Synthesis: {synthesis_seconds:.1f}s")
    print(f"Total render: {total_seconds:.1f}s")
    print(f"Text: {total_chars} characters, ~{total_tokens} XTTS tokens")
    if synthesis_seconds:
        print(f"Throughput: {total_tokens / synthesis_seconds:.2f} tokens/sec ({total_tokens * 60 / synthesis_seconds:.1f} tokens/min)")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("output_dir")
    ap.add_argument("--format", choices=("wav", "mp3"), default="wav",
                    help="Chapter output format (default: lossless WAV).")
    ap.add_argument("--sample-rate", type=int, default=24000,
                    help="Output sample rate in Hz (default: XTTS native 24000).")
    ap.add_argument("--channels", type=int, choices=(1, 2), default=1,
                    help="Output channels (default: mono).")
    a = ap.parse_args()
    render(a.plan, a.output_dir, a.format, a.sample_rate, a.channels)
