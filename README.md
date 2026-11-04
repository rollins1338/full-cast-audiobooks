# Full Cast Audiobooks

Turn an EPUB into a multi-voice audiobook locally: extract the text, label every
line with a speaker, render each voice with Coqui XTTS-v2, then merge the chapter
WAVs into a mastered audiobook.

Everything runs on your machine. No cloud TTS, no API keys, no per-word costs.

## Requirements

- Git
- **Python 3.10–3.12** (PyTorch/Coqui do not support 3.13+ yet)
- `ffmpeg` and `ffprobe` on `PATH`
- A GPU is strongly recommended, but CPU works slowly

## Quick start

### 1. Clone

This is a private repo, so use an account that has access:

```bash
git clone https://github.com/rollins1338/full-cast-audiobooks.git
cd full-cast-audiobooks
```

### 2. Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Accept the Coqui XTTS license once:

```bash
export COQUI_TOS_AGREED=1
```

The XTTS model weights download automatically on the first render.

### 3. Add voice references

Voice MP3s are **not** committed (the full library is ~1.3 GB). Put a clean
30–60 s speech clip per character in `voices/<Name>.mp3`, or fetch the 87
mainstream/Podium narrator references:

```bash
python3 download_mainstream_voices.py
```

`voices/voice_catalog.json` and `voices/VOICE_LIBRARY.md` list what is included.
For a one-off clip, `prepare_reference.py` cleans and normalizes it.

### 4. Add a book and make a plan

Put the EPUB in `sources/` (ignored by git), then follow
[`PLAN_WORKFLOW.md`](PLAN_WORKFLOW.md). The planner uses `plan_tools.py` to
extract chapters, assign speakers and validate the result:

```bash
.venv/bin/python plan_tools.py --help
```

The output is `books/<slug>/plan.json` with one segment per line:

```json
{"speaker": "Jesse", "voice_ref": "/absolute/path/voices/Jesse.mp3", "text": "..."}
```

Speaker labelling is the part that needs real reading — do not trust simple
regex/alternating-turn automation.

### 5. Render the chapters

```bash
./render.sh books/<slug>/plan.json
```

Useful flags:

```bash
./render.sh books/<slug>/plan.json --check       # validate only
./render.sh books/<slug>/plan.json --audition    # short sample
./render.sh books/<slug>/plan.json --chapters 1-3
./render.sh books/<slug>/plan.json --voice "Jesse=voices/Other.mp3"
```

Rendering resumes after Ctrl+C, and changing one voice only re-renders that
speaker's lines. Output goes to `output/<book>/` as one lossless WAV per chapter
(`001.wav`, `002.wav`, …) plus `Chapter_manifest.txt`. Add `--format mp3` only
for an MP3 deliverable.

### 6. Merge and master

The renderer produces chapter WAVs. Combine them, apply ACX loudness/peak
mastering, and export an M4B with chapter markers:

```bash
./wav_merger/run_wav_web.sh output/<book>/*.wav
```

This opens <http://localhost:8775> with the WAVs preloaded. Or start empty:

```bash
./wav_merger/run_wav_web.sh
```

Output is `<name>_ACX_Mastered.wav` plus `<name>_ACX_Mastered.m4b`. Chapter order
follows the filenames, so keep the zero-padded `001.wav` naming.

## Repo layout

- `sources/` — input EPUBs (ignored by git)
- `books/<slug>/` — per-book extraction, labels and `plan.json`
- `voices/` — XTTS reference clips + `voice_catalog.json`
- `wav_merger/` — standalone chapter-WAV merger/mastering UI
- `output/<book>/` — finished renders (ignored by git)

## Voice references

MP3 reference clips are intentionally **not tracked** (size and licensing).
`voices/voice_catalog.json` and `voices/VOICE_LIBRARY.md` list every narrator and
sample. To fetch the 87 mainstream/Podium narrators, run:

```bash
python3 download_mainstream_voices.py
```

For other cast members, use `prepare_reference.py` on a clean 30–60 s speech clip.

## Low-level CLI

The renderer is also available directly:

```bash
.venv/bin/python audiobook.py validate books/<slug>/plan.json
.venv/bin/python audiobook.py render-book books/<slug>/plan.json output/<book>
.venv/bin/python audiobook.py sample-plan book.epub -o sample_plan.json
```

`render-book` (also available as `render`) writes one lossless mono 24 kHz WAV
per chapter plus `Chapter_manifest.txt`. If XTTS or a voice reference cannot be
loaded, it fails with an error; it never writes a tone and labels it as speech.

## Notes

- The renderer is CLI-only. `wav_merger/` is the only browser UI, and it only
  joins and masters finished chapter files.
- Speaker attribution is manual/assisted because the whole point is getting the
  right voice on every line. Expect to review the plan before rendering.
