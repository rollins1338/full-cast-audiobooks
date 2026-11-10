# AGENTS.md

A full-cast audiobook pipeline: an EPUB becomes a speaker-labelled
`books/<slug>/plan.json`, and `render.sh` renders it with XTTS-v2, one cloned
voice per character.

Every agent working in this repo loads this file, helpers included, so it is
kept short. The detail lives in the files below; read only the one for your job.

## Find your job

| You were asked to… | Read | Don't read |
| --- | --- | --- |
| Turn a book into `plan.json`, or resume one | `PLAN_WORKFLOW.md`, then `books/<slug>/progress.md` if it exists | — |
| Scout, label or review as a helper (your task names a brief) | your brief in `briefs/` (`SCOUT.md`, `WORKER.md`, `REVIEWER.md`), and only what it lists | `PLAN_WORKFLOW.md`, `README.md` |
| Change code, the renderer or the docs | `README.md`, then the code | — |

## Rules for everyone

- **Never start a render.** The user starts renders. `./render.sh <plan> --check`
  is fine (it only prints stats). The one exception is the pronunciation test
  in `PLAN_WORKFLOW.md` 1.3.
- **At most 4 helpers at once**, ACP delegates and subagents together. More
  runs the machine out of memory and crashes the terminal.
- **Python:** `plan_tools.py` runs on system `python3`. Anything that imports
  torch/TTS (`audiobook.py`, `render_engine.py`, `wav_merger/`) needs
  `~/bots/dramione/.venv/bin/python`. `render.sh` finds it on its own.
- **Don't print book text** into the chat. Read book files with your file tools.
- **Git:** commit and push only when the user asks. Never commit `books/`,
  `sources/`, `output/`, voice clips, or any `.epub`.
- **While planning a book, don't edit `plan_tools.py`, `render.sh` or
  `render_engine.py`.** If a tool is wrong, stop and report it. Changing them
  is a code job of its own.

## Repo map

| Path | What |
| --- | --- |
| `PLAN_WORKFLOW.md` | the 5-phase, 4-gate workflow for planning a book |
| `briefs/` | helper briefs plus `LABELLING_RULES.md` |
| `plan_tools.py` | totext · extract · castvoices · verify · context · compare · prontest · build · stats |
| `render.sh`, `render_engine.py` | launcher and XTTS-v2 renderer; voice-blends each chapter after rendering (`--no-blend` to skip) |
| `books/<slug>/` | one folder per book: text, labels, `book.json`, `plan.json`, `progress.md` |
| `voices/`, `voices/VOICE_LIBRARY.md` | voice catalogue: clips in `voices/new/` (recent) and `voices/old/` (older), both searched by name (clips are git-ignored) |
| `wav_merger/` | chapter merging, ACX mastering, M4B export |
| `output/<book>/` | finished renders (git-ignored) |
