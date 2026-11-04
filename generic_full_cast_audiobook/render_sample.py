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

def split_long_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_CHARS:
        return [text]

    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if len(sentence) > MAX_CHARS:
            for word in sentence.split():
                if not current:
                    current = word
                elif len(current) + 1 + len(word) <= MAX_CHARS:
                    current += " " + word
                else:
                    chunks.append(current)
                    current = word
        elif current and len(current) + 1 + len(sentence) <= MAX_CHARS:
            current += " " + sentence
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)
    return chunks

def prepare_reference(src, dst):
    subprocess.run([
        "ffmpeg", "-y", "-i", str(src),
        "-ac", "1", "-ar", "24000",
        "-af", "highpass=f=60,lowpass=f=12000,loudnorm=I=-20:TP=-2:LRA=7",
        str(dst)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def render(plan_file, output_file):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print("LOADING XTTS-V2...")
    use_gpu = torch.cuda.is_available()
    device = torch.cuda.get_device_name(0) if use_gpu else "CPU"
    print(f"Synthesis device: {device}")
    load_started = time.monotonic()
    tts = TTS(MODEL, progress_bar=True)
    tts.to("cuda" if use_gpu else "cpu")
    model_load_seconds = time.monotonic() - load_started

    with tempfile.TemporaryDirectory(prefix="audiobook_sample_") as td:
        td = Path(td)
        refs = {}

        for name, src in VOICE_FILES.items():
            if not src.exists():
                raise FileNotFoundError(f"Voice file not found: {src}")
            ref = td / (name.replace(" ", "_") + "_ref.wav")
            prepare_reference(src, ref)
            refs[name] = ref

        queue = []
        for seg in plan["segments"]:
            for chunk in split_long_text(seg["text"]):
                queue.append((seg["speaker"], seg["voice"], chunk))

        clips = []
        synthesis_started = time.monotonic()
        total_chars = 0
        total_tokens = 0
        for i, (speaker, voice, text) in enumerate(queue, 1):
            wav = td / f"{i:05d}.wav"
            print(f"[{i}/{len(queue)}] {speaker} -> {voice}")
            total_chars += len(text)
            try:
                total_tokens += len(tts.synthesizer.tts_model.tokenizer.encode(text, lang="en"))
            except Exception:
                total_tokens += len(text.split())
            tts.tts_to_file(
                text=text,
                language="en",
                speaker_wav=str(refs[voice]),
                file_path=str(wav),
                split_sentences=False,
                temperature=0.65,
                length_penalty=1.0,
                repetition_penalty=2.0,
                top_k=50,
                top_p=0.85,
                speed=1.0
            )
            clips.append(wav)

        concat = td / "concat.txt"
        concat.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in clips),
            encoding="utf-8"
        )

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat),
            "-af", "loudnorm=I=-18:TP=-2:LRA=7",
            "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", "128k",
            str(output_file)
        ], check=True)

    synthesis_seconds = time.monotonic() - synthesis_started
    total_seconds = time.monotonic() - load_started
    print(f"\nModel load: {model_load_seconds:.1f}s")
    print(f"Synthesis: {synthesis_seconds:.1f}s")
    print(f"Total render: {total_seconds:.1f}s")
    print(f"Text: {total_chars} characters, ~{total_tokens} XTTS tokens")
    if synthesis_seconds:
        print(f"Throughput: {total_tokens / synthesis_seconds:.2f} tokens/sec ({total_tokens * 60 / synthesis_seconds:.1f} tokens/min)")

    print("\nSAMPLE COMPLETE")
    print("Output:", output_file)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("output")
    a = ap.parse_args()
    render(a.plan, a.output)
