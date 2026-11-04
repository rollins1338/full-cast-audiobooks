# Full Cast Audiobooks

## Current workflow (any book, 1st or 3rd person)

1. **Plan**: point DeepSeek (pi) at `PLAN_WORKFLOW.md` and the EPUB. It uses
   `plan_tools.py` to extract, label, verify and build `books/<slug>/plan.json`.
2. **Render**: `./render.sh books/<slug>/plan.json`. Run with `--help` for
   options (`--check`, `--audition`, `--chapters`, `--voice "Name=Voice"`).
   It resumes after Ctrl+C, and a voice swap re-renders only that speaker's lines.

## Setup

Use **Python 3.10–3.12** (PyTorch/Coqui wheels do not support 3.14 yet).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

XTTS also needs `ffmpeg`/`ffprobe` on `PATH`, and the Coqui license accepted once
(`export COQUI_TOS_AGREED=1`). The model weights download on first render.

## Repo layout

- `sources/` — input EPUBs (one per book)
- `books/<slug>/` — per-book extraction, labels and `plan.json`
- `voices/` — XTTS reference clips + `voice_catalog.json`
- `output/<book>/` — finished renders; never deleted by cleanup

## Voice references

MP3 reference clips are intentionally **not tracked** (the full library is ~1.3 GB).
`voices/voice_catalog.json` and `voices/VOICE_LIBRARY.md` list every narrator and sample.
To fetch the 87 mainstream/Podium narrators, run `python3 download_mainstream_voices.py`.
For other cast members, use `prepare_reference.py` on a clean 30–60 s speech clip.

The notes below describe the older `audiobook.py` commands.

This repository is command line only. It renders speech locally with Coqui
XTTS-v2 and the voice references in `voices/`. There is no web server,
frontend, API, or diagnostic-tone fallback in the normal renderer.

Use the shared environment:

```bash
/home/rxxfxx/bots/dramione/.venv/bin/python audiobook.py validate books/<slug>/plan.json
/home/rxxfxx/bots/dramione/.venv/bin/python audiobook.py render-book full_book_plan.json output/full_book
```

`render-book` (also available as `render`) writes one lossless mono 24 kHz
WAV file per chapter (`001.wav`, `002.wav`, and so on) plus a
`Chapter_manifest.txt`. Use `--format mp3` only when an MP3 deliverable is
required. If XTTS or a voice reference cannot be loaded, the command fails with
an error; it never writes a tone and labels it as speech.

Create a short plan from an EPUB:

```bash
/home/rxxfxx/bots/dramione/.venv/bin/python audiobook.py sample-plan book.epub -o sample_plan.json
/home/rxxfxx/bots/dramione/.venv/bin/python audiobook.py render sample_plan.json output/sample
```

Set `COQUI_TOS_AGREED=1` after accepting the XTTS license once. FFmpeg must be
available on `PATH`.
