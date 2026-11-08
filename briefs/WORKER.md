# Worker brief

You are a labeller. For each chapter you own, you read it in full and write
who says every line, following `briefs/LABELLING_RULES.md`.

Your task message gives you:
- `<slug>`: the book's folder, `books/<slug>/`
- your chapter IDs (from `book.json`'s `chapters`)
- for each chapter that starts your batch, its context file
  `books/<slug>/ctx/<id>-prev.txt`: the end of the chapter before, for scene
  continuity. Read it, but don't label it.

## Read first

1. `briefs/LABELLING_RULES.md`, in full. It is the whole job.
2. `books/<slug>/bible.md`: the characters, aliases, narration mode and
   traps.
3. `books/<slug>/book.json`: the cast, `aliases`, `pov_mode` and each
   chapter's `source`, `from`/`to`, `narrated_by` and
   `narrated_by_changes`.

## For each chapter

1. Read the chapter's paragraphs in `books/<slug>/extract/` in full, in
   order. Read every line and decide. Never write a script or regex to
   guess speakers, and never alternate speakers blindly.
2. Write `books/<slug>/segments/<id>.json` in one go.
3. Run `python3 plan_tools.py verify books/<slug> --chapter <id>`. Fix every
   error. Decide every warning: fix the label, or keep it and note the
   reason. Run it again until it shows 0 errors.

## Rules

- Write **only** your own `segments/<id>.json` files. Never edit
  `book.json`, `bible.md`, `reviewed.json` or another worker's chapter.
- The only errors you may leave are "speaker not in cast" for a new
  character you had to name.
- Copy the text exactly. If you spot a typo or punctuation mistake in the
  source, copy it as it is and report it.

## Your final reply

Use these headings:
- **Chapters:** each ID with its final verify result.
- **Warnings kept:** one per line: `CODE:paragraph_id` and the one-line
  reason.
- **New characters:** name, first paragraph ID, gender and age, one-line
  description, roughly how many lines.
- **Unsure:** paragraph ID, the options, your label and why you're unsure.
- **Source mistakes:** paragraph ID, before, after.
- **XTTS quirks:** anything that would be read aloud wrong, with its
  paragraph ID.
