# Labelling rules

Every labeller follows this file: the main agent, the workers (4.1) and the
blind reviewers (4.3). It covers the segment format, who says each line, POV
headers and the `verify` warnings. Your own brief (`briefs/WORKER.md` or
`briefs/REVIEWER.md`) says which files you read and write.

## Segment format

For each chapter write `books/<slug>/segments/<id>.json`, one segment per
line:

```json
{"segments": [
["u007p0000", "SKIP", "Chapter 1"],
["u007p0001", "Ruth", "“Hand me the map,”"],
["u007p0001", "NARRATION", "Mom said."],
["u007p0002", "NARRATION", "Daniel stared out the window."],
["u007p0002", "Daniel", "*Not this again.*"],
["u007p0003", "Daniel", "“Where are we going?”"],
["u007p0003", "NARRATION", "he asked."]
]}
```

Each segment is `[paragraph_id, speaker, text]`.

- **Text is copied exactly from the extract**, in order, covering every
  paragraph completely: every word and every punctuation mark. You may keep
  or drop the quote marks and `*`. Nothing else may change, and don't add
  words. If you spot a typo or punctuation mistake, copy it as it is and
  report it under "Source mistakes" in your reply; the main agent fixes
  `book.txt`.
- **speaker** is one of:
  - a cast name
  - `NARRATION`: prose, voiced by `narrated_by`
  - `POV_HEADER`: a POV marker, spoken aloud (see "POV headers" below)
  - `SKIP`: not spoken. Chapter headings that `announce` already covers,
    epigraph credits.
- **Split each paragraph** at every switch between quoted speech and prose.
  The dialogue tag (`he asked.`, `Mom said.`) is always `NARRATION`,
  including in first-person chapters, where `NARRATION` becomes the POV voice
  automatically.


## Attribution rules (this is the whole job)

1. **An explicit tag wins**: `"…," Mom said` belongs to Mom (Ruth, via
   aliases). Resolve pronoun tags (`she said`) from who is in the scene
   and who acted last.
2. **An action beat in the same paragraph** names the speaker:
   `Ruth smiled. "Why not?"` belongs to Ruth. The character who acts in a
   paragraph normally speaks in that paragraph.
3. **Untagged back-and-forth**: go back to the last tagged line and
   alternate A/B/A/B, counting every paragraph. Then check every line on
   content: does this person know this, want this, talk like this? If
   content contradicts the count, the content wins. Re-check the run.
4. **Vocatives**: `"Daniel, stop."` is never said by Daniel. A name inside the
   quote is the person being *addressed*.
5. **First person**: `"Fine," I said` is the POV character. In first-person
   chapters "I" in a tag is always the `narrated_by` character for that
   paragraph.
6. **New paragraph means new speaker**, unless the previous quote did not
   close (`"…` with no closing mark): then the same speaker continues across
   paragraphs.
7. **Inner thoughts: the grammatical person decides who says them.** This
   matters in **third-person** chapters. Ask one question: **how does the
   thinker refer to themself inside the thought?**
   - **First person (`I`, `me`, `my`, `we`) → the thinker says it.** Split
     it out of the narration like dialogue, and leave the tag (`she
     thought.`) as `NARRATION`.
   - **Third person (`she`, `he`, their own name) → the narrator says it.**
     It's written in the narrator's grammar, so it stays `NARRATION`, even
     when it's in italics and even with a `she thought` tag.
   - **No self-reference at all:** judge by the voice.
     - Present tense, a direct question or exclamation, or the thinker
       talking to someone or to themself as `you` → the thinker says it.
     - Past tense in the narrator's style → `NARRATION`.
   - Italics don't decide it. They only tell you it might be a thought.
   - **Examples** (Mia is the POV character of a third-person chapter):

     | Text | Segments |
     | --- | --- |
     | `*I can't do this.*` | `Mia`: the whole line |
     | `*She couldn't do this.*` | `NARRATION`: the whole line |
     | `I should never have come, Mia thought.` | `Mia`: `I should never have come,` / `NARRATION`: `Mia thought.` |
     | `She should never have come, Mia thought.` | `NARRATION`: one segment |
     | `Mia stared at the door. *He's not coming back.* She turned away.` | `NARRATION`: `Mia stared at the door.` / `Mia`: `*He's not coming back.*` / `NARRATION`: `She turned away.` |
     | `*Why is he here?*` | `Mia`: present tense, a direct question |
     | `Why had he come back?` | `NARRATION`: past tense, the narrator's voice |
     | `*You idiot, Mia. You absolute idiot.*` | `Mia`: talking to herself |
     | `*Not again.*` | `Mia`: an exclamation in her own voice |
     | `*Mia was going to kill him.*` | `NARRATION`: third person, even in italics |
     | `*God, I hate him. She'd never admit it, though.*` | `Mia`: `*God, I hate him.` / `NARRATION`: `She'd never admit it, though.*` |
     | `She wondered whether he'd noticed.` | `NARRATION`: reported thought |

     A thought that switches person partway gets split at the switch, as
     in the last-but-one example. Asterisks don't count in `verify`, so a
     split can fall inside the italics.
   - **What else stays `NARRATION`:** italics that aren't thought, like
     emphasis on one word inside a sentence (`she was *not* going`),
     titles, foreign words, sounds and sign text.
   - **Telepathy and mind-speech** (common in paranormal books) is dialogue.
     It goes to the sender.
   - **First-person chapters, and `deep_pov` chapters with POV voices:** the
     POV character's own thoughts can stay in `NARRATION` whatever their
     person, because the narration is already in their voice. Another
     character's first-person thoughts (shared through telepathy, read in
     a letter) still go to that character.
   - **Many books don't use italics for thoughts at all.** The same test
     applies to plain text: in a third-person chapter, `I` / `me` / `my`
     outside quotes is almost always a first-person thought, so it goes to
     the thinker.
   - **Unsure whether something is a thought, whose it is, or which side of
     the line it falls on?** Ask the user at Gate 3.
8. **Texts, letters, notes, signs** are voiced by whoever wrote them. Read
   aloud by someone else? Then by the reader.
9. **Phone calls, flashbacks, dreams**: the actual speaker, even when absent
   from the scene.
10. **The narrator never speaks quoted dialogue.** This is a common mistake
    in third-person books. `NARRATION` is only the prose *outside* the
    quotes: description, action and tags.
    - Anything a character says out loud inside quote marks goes to that
      character, **even** when the paragraph is mostly narration and the
      quote is short (`He grunted a "Fine" and left.` gives `"Fine"` to him).
    - Split it out; never leave it inside the narration segment.
    - The only quoted text that stays `NARRATION` is text nobody speaks:
      - scare quotes (`her "boyfriend"`)
      - titles (`"Romeo and Juliet"`)
      - a word mentioned as a word (`the word "home"`)
      - signs and labels that nobody reads aloud
    - The reverse mistake: description that mentions speech without quoting
      it (`She told him to leave.`) is `NARRATION`, not her line.
    - When you can't tell whether a quote is spoken or narration, it goes
      to the user at Gate 3.
11. **Never guess.** If two readings stay possible after rules 1–10, read the
    surrounding page again. Choose the one the following lines confirm (who
    answers what, who reacts). Write down the reason (see "Who writes
    what" below).


**New characters.** If a speaker isn't in `book.json`'s cast, label them
with a clear name anyway (a minor speaker you can reuse, like `Guard 1`,
if one fits) and list them in your reply. Never edit `book.json` or
`bible.md` yourself; the main agent adds them.

**Deep POV books** (`"pov_mode": "deep_pov"`, or a third-person chapter
whose prose is the POV character's inner voice): never carve the free
indirect thought out sentence by sentence. Only the thoughts rule 7 gives to
the thinker get split out; the rest stays `NARRATION`.

## POV headers

Many books mark whose point of view a chapter or section is in with a
header. Examples: a line `DANIEL`, `Mara`, `Chapter 3 - Daniel`, or a name
over a scene break. **These are spoken, never skipped.** Label the name part
`POV_HEADER`. If the name shares a paragraph with the chapter number, split
the paragraph: `["u012p0000", "SKIP", "Chapter 3 -"], ["u012p0000",
"POV_HEADER", "Daniel"]`. The chapter number is already spoken by `announce`.

Who says it depends on the book's `pov_mode`, and `build` does this
automatically:

| `pov_mode` | Kind of book | Who says the POV header |
|---|---|---|
| `deep_pov` | third person, deep POV, option A | **the POV character themself** ("Daniel" in Daniel's voice) |
| `third_person` | normal third person, or deep POV option B | `Narrator` |
| `first_person` | first person | `Narrator` |

- **In `deep_pov` books, put the `narrated_by_changes` entry on the header
  paragraph itself,** so the header is already in the new POV character's
  voice. `verify` errors if a header names Mara but the voice there is
  still Daniel's.
- Set `pov_mode` once for the book. A chapter can override it with its own
  `"pov_mode"`, e.g. an omniscient prologue in a deep-POV book.
- `verify` warns (`POV_HEADER_NAME`) if a POV header names nobody in the
  cast or aliases.


## Verify until clean

```bash
python3 plan_tools.py verify books/<slug> --chapter 001
```

- **ERROR** means the text doesn't match the source, a paragraph is missing,
  a speaker isn't in the cast, and so on. Fix it. Errors block the build.
- **WARN** means a suspicious label. Each one needs a decision:
  - `QUOTE_IN_NARRATION`: dialogue left inside prose.
  - `THOUGHT_IN_NARRATION`: italic text inside the narrator's prose in a
    third-person chapter. Apply rule 7: a first-person or direct thought
    goes to the thinker. A third-person thought, emphasis, a title, a
    foreign word or a sound stays, with that reason in `reviewed.json`.
  - `FIRST_PERSON_IN_NARRATION`: the narrator says I/me/my in a
    third-person chapter. Almost always a thought or a letter left in
    narration.
  - `THOUGHT_TAG_IN_NARRATION`: `she thought` / `he wondered` is still
    joined to the thought before it. Split the thought out for the thinker,
    unless it's a third-person thought (rule 7). Then it stays, with that
    reason in `reviewed.json`.
  - `QUOTE_IN_DIALOGUE`: two quotes merged into one segment.
  - `TAG_IN_DIALOGUE`: a tag swallowed into speech.
  - `SELF_ADDRESS`: a speaker calls themselves by name (rule 4).
  - `SAME_SPEAKER_RUN`: same speaker in consecutive untagged paragraphs
    (rule 6).
  - `MULTI_SPEAKER_PARA`, `SINGLE_LINE_SPEAKER`, `NOT_IN_ANY_CHAPTER`,
    `UNUSED_CAST`.

  Re-read the passage. Either fix the label, or, if it is correct, add the
  code and ID to `books/<slug>/reviewed.json` with a one-line reason:

```json
{
  "SAME_SPEAKER_RUN:u007p0044": "Daniel keeps talking; Ruth only nods in p0043",
  "NOT_IN_ANY_CHAPTER:u000": "copyright page"
}
```

Don't move to the next chapter until its verify shows `0 errors` and you
have a decision for every warning.

## Who writes what

- **The main agent** writes the decisions into `reviewed.json` and owns
  `book.json` and `bible.md`.
- **A worker or reviewer never writes `reviewed.json`, `book.json` or
  `bible.md`.** Where this file says "add it to `reviewed.json`" or "write
  the reason", put it in your final reply instead: the code, the paragraph
  ID and the one-line reason. The main agent copies them in.
- Anything this file sends "to the user at Gate 3" goes under **Unsure** in
  your reply, with the paragraph ID, the options and why you're unsure.
  Still label it with your best reading.
