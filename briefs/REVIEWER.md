# Blind reviewer brief

You are a blind reviewer. Another helper has already labelled these
chapters, but you never see their labels. You label each chapter again,
from scratch, following `briefs/LABELLING_RULES.md`. A tool then compares
your labels with theirs, and the main agent settles every line where you
disagree. Your independence is the whole point: two careful readings that
agree are worth far more than one reading checked against itself.

Your task message gives you:
- `<slug>`: the book's folder, `books/<slug>/`
- your chapter IDs
- for each chapter that starts your batch, its context file
  `books/<slug>/ctx/<id>-prev.txt`. Read it, but don't label it.

## Never open

- `books/<slug>/segments/`, `books/<slug>/review/` and `reviewed.json`.
  These hold the other labeller's answers. Reading them, even to "check",
  ruins the review.

## Read first

1. `briefs/LABELLING_RULES.md`, in full.
2. `books/<slug>/bible.md` and `books/<slug>/book.json`.

## For each chapter

1. Read the chapter in `books/<slug>/extract/` in full, slowly, in order.
   Decide the speaker of every line yourself. Take most care with:
   - untagged back-and-forth runs: count, then check every line on content
   - pronoun-only tags (`she said` with two women present)
   - phone calls, texts, letters and flashbacks
   - first-person `I said` where the POV switches
   - quoted dialogue that must not stay in `NARRATION` (rule 10)
   - thoughts on the right side of rule 7, including thoughts with no
     italics and no tag
2. Write `books/<slug>/blind/<id>.json`, in the same format as a segments
   file.
3. Run `python3 plan_tools.py verify books/<slug> --chapter <id> --dir blind`
   and fix every error until it shows 0 errors. Warnings are for your own
   check; you don't need to resolve them.
4. Write `books/<slug>/blind/<id>.notes.json`: for every line you found
   hard, the paragraph ID and a one-line reason for your choice.

   ```json
   {"u007p0044": "Daniel asked in p0043; Ruth answers, and p0045 has Daniel react to her"}
   ```

## Rules

- Write only your own `blind/<id>.json` and `blind/<id>.notes.json` files.
- Copy the text exactly. Report source typos; don't fix them.
- Label new characters with a clear name and report them; don't edit
  `book.json`.

## Your final reply

Use these headings:
- **Chapters:** each ID with its verify result.
- **New characters:** name, first paragraph ID, one-line description.
- **Unsure:** paragraph ID, the options, your label and why.
- **Source mistakes:** paragraph ID, before, after.
