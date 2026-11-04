# Generic Full-Cast Audiobook Generator

FIRST RUN = SAMPLE ONLY.

The generator uses the shared virtual environment at
`/home/rxxfxx/bots/dramione/.venv` and local XTTS-v2 synthesis (no cloud API).

1. Put your four voice samples in `voices/`:
   Savannah Peachwood.mp3
   Lee Daniels.mp3
   Jacob Morgan.mp3
   Teddy Hamilton.mp3

2. Put an EPUB anywhere convenient.

3. Make a short sample plan:
   /home/rxxfxx/bots/dramione/.venv/bin/python \
     generic_full_cast_audiobook/make_sample_plan.py "Solaris(1).epub"

4. Render ONLY the sample:
   /home/rxxfxx/bots/dramione/.venv/bin/python \
     generic_full_cast_audiobook/render_sample.py sample_plan.json output/sample.mp3

On the first render, Coqui asks you to accept either its commercial license or
the non-commercial CPML for XTTS-v2. Answer `y` once; subsequent runs reuse the
downloaded model. If running non-interactively after you have accepted the
terms, set `COQUI_TOS_AGREED=1`.

The full-book renderer is separate and should only be used after the sample sounds good.
It writes one lossless WAV master per chapter by default (24 kHz mono PCM16,
XTTS's native rate). This matches the chaptered-WAV workflow used by the
Dramione project and avoids an MP3 encode during synthesis.

The full-book renderer expects a reviewed chapter plan in the format described
in `render_book.py`:

```bash
/home/rxxfxx/bots/dramione/.venv/bin/python \
  generic_full_cast_audiobook/render_book.py reviewed_plan.json output/chapters
```

Optional MP3 output is still available with `--format mp3`. For a stereo WAV
master, add `--channels 2`; use `--sample-rate 44100` only when a downstream
tool requires 44.1 kHz (it does not add source detail).

The scripts resolve `voices/` relative to this repository, so they can be run
from any working directory. FFmpeg must be available on `PATH`.
