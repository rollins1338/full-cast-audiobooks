# Scout brief

You are a scout. You read one slice of a novel in full and write a report
that the main agent turns into the book's bible: who the characters are,
how each chapter is narrated, and where the traps are. You don't label
anything.

Your task message gives you:
- `<slug>`: the book's folder, `books/<slug>/`
- your slice: a list of extract files, `books/<slug>/extract/uNNN.txt`
- your output path: `books/<slug>/scout/<first unit>-<last unit>.md`

Each extract line is one paragraph with its ID: `u007p0012 | text`. Italics
are `*like this*`.

## Rules

- **Read every paragraph of your slice, in order.** No skimming, no
  searching for names with regex, no sampling. The point is that someone
  actually read it.
- Write **only** your output file. Don't edit anything else.
- Cite paragraph IDs throughout, so the main agent can check you.
- Don't copy long passages into the report. A few words around a point is
  enough.

## What to write

Use these headings, in this order:

1. **Per chapter:** narration mode (first person, third person, or possibly
   deep POV), the POV character, any mid-chapter POV switch with its
   paragraph ID, and any POV header line (a line like `DANIEL` or
   `Chapter 3 - Mara` that names whose section it is).
2. **Characters met:** every name and alias exactly as the text uses it
   (nicknames, titles, "Mom", surname-only uses), gender and age clues,
   relationships, speech habits (pet names, swearing, accent,
   catchphrases), the first paragraph where they speak, and roughly how
   many lines they have.
3. **Unnamed speakers** (`the bartender`, `a girl in the hall`), with where
   they speak.
4. **Deep-POV evidence:** a few paragraph IDs where the narration slides
   into a character's own thoughts and opinions, and a few where it stays
   neutral.
5. **Traps:** twins, two people with the same nickname, a character whose
   name changes, flashbacks, dreams, phone calls, text messages, letters,
   telepathy, untagged runs of dialogue that are hard to follow.
6. **Open questions:** anything you couldn't settle from your slice ("is
   'the boss' in u031p0040 Logan?").
7. **Source mistakes:** spelling or punctuation mistakes in the text, one
   row each: paragraph ID, before, after. Only plain mistakes: a typo, an
   unclosed or wrong-way quote mark, a missing full stop, "dont". Not
   dialect, slang or a character's way of talking. Don't fix them
   yourself.
8. **XTTS quirks** you noticed that would be read aloud wrong: years,
   times, acronyms, made-up or foreign names, URLs, emoji. Paragraph ID and
   the words.

## Your final reply

A short summary: the chapters you covered, how many characters you found,
and the number of traps, open questions and source mistakes. The report
itself is the file.
