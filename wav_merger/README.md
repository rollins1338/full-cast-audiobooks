# WAV Merger

Standalone local UI for turning rendered chapter WAVs into one finished
audiobook. No Python packages, no tkinter — only FFmpeg/ffprobe.

## Run

From the repo root:

```bash
./wav_merger/run_wav_web.sh output/<book>/*.wav
# or
./wav_merger/run_wav_web.sh
```

Open <http://localhost:8775>, add the chapter WAVs/folder, set the output name,
then merge. It writes `<name>_ACX_Mastered.wav` and optionally
`<name>_ACX_Mastered.m4b` with chapter markers.

Chapter order follows the filenames — keep the renderer's `001.wav`, `002.wav`
naming.

## Files

- `wav_web.py` — HTTP server + embedded browser UI
- `wav_merge_engine.py` — FFmpeg merge, gaps/crossfade, ACX mastering, M4B export
- `run_wav_web.sh` — launcher; prefers the repo `.venv`, then `python3.12`

Use Python 3.10–3.12 (`audioop` is gone in 3.13+). FFmpeg and ffprobe must be on
`PATH`.
