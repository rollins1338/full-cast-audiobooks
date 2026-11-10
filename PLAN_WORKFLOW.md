# Making a full-cast plan.json for any book

Use this file with **DeepSeek Flash**. Point it at this workflow and the EPUB,
and tell it to follow every phase exactly. It must use `plan_tools.py` and read
the book line by line — no regex or alternating-turn speaker guessing.

You are turning an EPUB into a speaker-labelled plan that `render.sh` renders.
**Your job is judgment: who says each line.** All the mechanical work
(extraction, text fidelity checks, plan assembly, voice paths) is done by
`plan_tools.py`. Never write plan.json by hand and never write your own
extraction or attribution script. Regex/heuristic speaker guessing is exactly
what has failed before: **you read every line and decide.**

Run everything from the repo root (the folder containing `plan_tools.py`).
`<slug>` is a short book name like `my_book`.

The work runs in **five phases**. Each phase ends at a **gate**, where you
stop and the user decides. Never cross a gate on your own.

> **Only stop at a gate.** The four gates and a real blocker (a missing
> file, a tool that won't run, a question only the user can answer) are the
> only reasons to end your turn and wait for the user. Everything else you
> carry on with yourself: finishing a part, a batch of helpers coming back,
> a `verify` that fails, a review that finds errors. Never end your turn
> with "the delegates are running, I'll continue when they finish". Nothing
> wakes you up when they finish, so you'd sit there until the user notices.
> While helpers run, stay in the watch loop below and keep going as soon as
> they're done.

| Phase | What you do | Gate: you stop until… |
| --- | --- | --- |
| **1 · Text** | EPUB → cleaned `book.txt` with typos fixed, plus `text_fixes.md`, `tts_fixes.md` and a pronunciation test | **Gate 1:** the user has edited `book.txt`, answered the fixes and says "go" |
| **2 · Read** | Extract; scouts read the whole book (you read it yourself if it's under 40k words); you write `bible.md` | no gate: go straight on to Phase 3 |
| **3 · Cast** | `book.json`, `cast_voices/` | **Gate 2:** the user approves every voice |
| **4 · Label** | Helpers label, `verify` until clean, blind reviewers label again and `compare` finds the differences, questions for the user | **Gate 3:** the user answers `questions.md` |
| **5 · Build** | Final checks, `plan.json`, `report.md` | **Gate 4:** you stop for good; the user starts the render |

**Keep `books/<slug>/progress.md` up to date.** Create it at the start of
Phase 1 and rewrite it every time you finish a numbered part, cross a gate,
or stop. When you start or resume work on a book, read it first and carry
on from there; never start a phase over because you lost track.

```
# Progress: <slug>
Phase: 4 · Label
Done: 4.1 labelled 31/47 chapters (batches 1-8 back)
Running (4 max): batch 9 u033-u035 run_a1, batch 10 u036-u038 run_b2,
  batch 11 u039-u041 run_c3, batch 12 u042-u044 run_d4
To do: batch 13 u045-u047
Waiting on: nothing
Next: start batch 13 when a slot frees, then 4.2 verify
Notes: user chose deep POV option A at Gate 2; late cast: Proctor (af_sarah)
```

`Waiting on:` names the gate when you're stopped at one ("Gate 2: user is
reviewing cast_voices/").

> **Never start a render.** Your job ends when `plan.json` and the report are
> done. Rendering always waits for the user's go-ahead, and the user starts it
> themselves. Don't run `./render.sh` without `--check`, don't render a test
> chapter or a sample, and don't tell a helper to. This holds even when
> everything passes, even when you have time left, and even if an earlier
> message seemed to allow it. `./render.sh ... --check` is fine: it only
> prints the cast table, stats and a time estimate. The **one exception** is
> the pronunciation test at Gate 1 (1.3): it renders only the test lines in
> `pron_test/plan.json`, never the book.

### Helpers: subagents and ACP delegates

You don't have to do the reading alone. Run helpers in parallel, as often
as the book needs. **You have full permission to design the setup
yourself:** how big each batch is, which tool runs which job, and whether
to add extra passes (a reconcile pass, a third review, a spot check). You
don't need to ask the user about any of that.

> **At most 4 helpers at once, ACP delegates and subagents together.** More
> than that runs the machine out of memory and crashes the terminal. This
> holds however many chapters the book has: a 60-chapter book still runs 4
> at a time. Never start a 5th while 4 are running, and don't rely on the
> delegate queue to hold the rest back. Keep the other batches in a to-do
> list in `progress.md` and start the next one only when one finishes.

Every helper runs **DeepSeek Flash 4.1** (`opencode-go/deepseek-v4.1-flash`),
the same model as you. ACP delegates inherit your model. For subagents, use
an agent with no `model` line, or one set to that model.

Use both kinds, together:

| Kind | Tools | Best for |
| --- | --- | --- |
| **ACP delegates** | `acp_delegate`, `acp_delegate_wait`, `acp_delegate_cancel`, `acp_delegate_list` | Long background batches: scouting (2.2), labelling (4.1), review (4.3). They run while you keep working. |
| **Subagents** | the client's subagent tool (for example pi's `subagent`, with parallel `tasks`) | Short jobs whose answer you need before you go on: settle an alias question, re-check one chapter, compare two readings of a passage. |

Subagents count toward the same limit of 4: if 4 delegates are running,
don't start a subagent until one finishes. If the API starts returning
rate-limit errors, run fewer than 4.

**ACP delegate tools:**

| Tool | Use |
| --- | --- |
| `acp_delegate` | Start one. Pass `agent` (`"worker"` to scout or label, `"reviewer"` to review), `task` (the full brief), `async: true`, and optionally `timeoutMinutes`. Returns a `runId`. |
| `acp_delegate_wait` | Collect a result: `runId`, plus `timeout` in ms (e.g. `240000`). If it times out, the delegate is still running, so call it again. |
| `acp_delegate_cancel` | Stop a delegate that's stuck or was given the wrong brief. |
| `acp_delegate_list` | See what's queued, running and finished. |

**Rules for every helper, whichever tool runs it:**
- **A helper starts with no context.** It hasn't seen this conversation,
  the bible or any earlier decision. Its brief must say everything: which
  files to read (by path), which chapter IDs it owns, what to write, and
  what to report back.
- **Each job has a brief file:** `briefs/SCOUT.md` (2.2),
  `briefs/WORKER.md` (4.1) and `briefs/REVIEWER.md` (4.3). Tell the helper
  to read its brief file first, then give it only what the brief asks for:
  the slug, its slice or chapter IDs, and its `ctx/` file. Never hand a
  helper this whole workflow; it's mostly about your job, not theirs, and
  it costs memory and attention. Add a line or two of your own only for
  something this book needs (a trap in its chapters, a POV switch).
- **Run them as a rolling pool of 4.** Split the job into batches and list
  them all in `progress.md` as a to-do list. Start the first 4
  (`async: true`). Each time one finishes, start the next batch from the
  list straight away, so 4 are always running until the list is empty.
  Don't wait for all 4 to finish before starting more, and don't run
  batches one after another.
- **Watch the pool until it's empty, in the same turn.** Write each
  `runId` and its output path next to its batch in `progress.md`, then
  loop:
  1. `acp_delegate_list` to see which are still running.
  2. For each one that's still running, `acp_delegate_wait` with a
     `timeout` of `240000`. A timeout only means it's still working: loop
     again, don't stop.
  3. As each one finishes, check its output file exists and isn't empty,
     mark the batch done in `progress.md`, and start the next batch from
     the list (or a retry, or a fix pass it unblocks) in its slot.
  4. When the list is empty and none are left running, go straight on to
     the next numbered part.

  While you wait you can do other work that doesn't depend on the running
  batches (start the bible from chapter 1, prepare the next briefs), but
  come back to the loop; never end the turn with helpers still out.
- **A helper that fails or hangs is yours to fix, not the user's.** If one
  returns an error or an empty or missing file, start a fresh one with the
  same brief. If one is still running after twice its `timeoutMinutes` (or
  40 minutes when none was set), `acp_delegate_cancel` it and start a fresh
  one. After the second failure on the same batch, do the batch yourself
  or split it smaller. Only go to the user if the tools themselves stop
  working.
- **Resuming after a break:** if `progress.md` lists `runId`s, run
  `acp_delegate_list` first. Collect what finished, and restart anything
  that's missing from the list and has no output file.
- You can't message a helper while it runs. Anything it needs has to be in
  the brief; anything you learn later goes in a fix pass or a new helper.
- Helpers never write `book.json`, `bible.md` or `reviewed.json`. Those are
  yours.
- Two helpers never write the same file.
- The gates don't move: the cast approval (Gate 2) and the questions
  (Gate 3) are always yours to bring to the user.
- If neither tool is available, say so, and do the batches yourself, one
  at a time.

---

# Phase 1 · Text

- **Goal:** the book as clean, editable plain text, and a list of the spots
  XTTS will read wrong.
- **Needs:** the EPUB.
- **Done when:** `book.txt` is cleaned, `text_fixes.md` and `tts_fixes.md` are written, the
  pronunciation test is rendered, and you've
  stopped at Gate 1.

## 1.1 Make the clean text

```bash
python3 plan_tools.py totext "<book>.epub" books/<slug>
```

This writes `books/<slug>/book.txt`: the whole book as plain text, one
paragraph per block, with a `=== uNNN ===` line starting each EPUB section.
Italics are kept as `*like this*`. Lines starting with `# ` are comments.

Clean it, editing `book.txt` in place:

- **Delete non-story sections** with their `===` line: copyright page,
  table of contents, dedication, acknowledgements, "about the author",
  newsletter or social media plugs, and previews or ads for other books.
  Keep the title page only if it reads well aloud.
- **Fix extraction junk:** stray page numbers, a chapter heading printed
  twice, words broken by a hyphen at a line end, footnote markers,
  leftover markup.
- **Fix the source's typos and punctuation mistakes** (see "Source
  mistakes" below). These books are often small-press or self-published and
  have more of them than usual.
- **Don't change the story.** No rewording, no cuts, no "improving" the
  author's style. Keep every chapter heading. Pronunciation fixes are
  proposals for the user (see 1.2), not cleaning.
- Don't touch the `=== uNNN ===` lines, and don't add your own sections.

On a big book, split the cleaning across helpers by section, one helper
per range of sections, each editing only its own range.

### Source mistakes: fix them

Fix plain mistakes in `book.txt` yourself, without asking first. They
matter for more than looks: a missing or wrong quote mark can make a line of
dialogue come out in the narrator's voice, or put the narration in a
character's.

**Fix:**
- **Misspellings and typos:** "teh", "recieve", "their" for "there",
  "loose" for "lose", a missing or doubled letter, two words run together
  ("ofthe") or one split ("some thing" for "something"), a doubled word
  ("the the").
- **Quote marks:** a dialogue line that never closes its quote, or never
  opens one; a closing quote facing the wrong way; straight and curly
  quotes mixed in the same line; a quote mark left over after the speech
  ended. Look at the speech around it to decide where the quote really
  starts and ends.
- **Other punctuation:** a missing full stop or comma at the end of a
  sentence or before a closing quote, doubled marks (",," "..", "?." ),
  a space before a comma or full stop, no space after one, a missing
  apostrophe in an obvious contraction ("dont", "Im", "youre"), a stray
  mark in the middle of a word.
- **Wrong word an editor would catch:** "of" for "have" in narration
  ("should of"), "your" for "you're", "its"/"it's" mixed up.

**Leave alone:**
- **Anything said on purpose:** dialect and slang in dialogue ("gonna",
  "y'all", "ain't", "should of" spoken by a character who talks that way),
  a character's texting style, a child's or a non-native speaker's
  mistakes, stutters, made-up words, names and their spellings.
- **Style:** sentence fragments, run-on sentences, odd but deliberate
  punctuation, unusual word choices, British vs American spelling.
- **Anything you'd have to guess at.** If you can't tell what the author
  meant, or the fix would change the meaning, don't fix it: list it under
  "Unsure" for the user.

Keep every fix as small as possible: change the mistake, not the sentence
around it. Never merge or split paragraphs while fixing.

Log every fix in `books/<slug>/text_fixes.md`, grouped by kind, so the user
can see them and undo any at Gate 1:

```
## Quote marks (4)
| # | Where | Before | After |
| 1 | u007 | ...map, Mom said. | ...map,” Mom said. |

## Unsure (2)
| # | Where | Text | Question |
| 1 | u012 | ...the lead pipe... | "lead" or "led"? |
```

Keep the text columns to a few words around the fix. When the same fix
repeats ("dont" → "don't" 31 times), list it once with a count. On a big
book, the cleaning helpers log their own sections and you merge the
lists.

## 1.2 XTTS quirks: find them, propose fixes

The renderer (XTTS-v2) reads the text literally, and some text comes out
wrong. While cleaning, find every case below and **propose** a fix, but
change nothing until the user approves it. On a big book, the cleaning
helpers collect the cases for their own sections and you merge their lists.

XTTS lowercases everything and only expands a few things itself: plain
numbers, ordinals (1st, 22nd), money ($500,000), Mr., Mrs., Dr., St.,
Jr., Capt., Sgt. and the symbols `& @ % # $ £ °`. Everything else is read
as written.

| Problem | What XTTS does | Proposed fix |
| --- | --- | --- |
| Years: "1995" | "one thousand, nine hundred and ninety-five" | nineteen ninety-five |
| Times: "3:00", "10pm", "7:45" | "three:zero", "tenpm" | three o'clock, ten p.m., seven forty-five |
| Roman numerals: "Chapter IV", "Henry VIII" | "iv", "viii" as letters or a word | Chapter Four, Henry the Eighth |
| Acronyms and initialisms: "FBI", "OMG", "NASA" | lowercased, so "fbi" may come out as one word | F.B.I., O.M.G. (keep NASA if it's said as a word) |
| ALL-CAPS shouting: "STOP" | lowercased, the shout is lost | nothing to fix; just tell the user |
| Unexpanded abbreviations: Ms., Prof., vs., etc., e.g., approx., km, lbs | read letter by letter or skipped | Miz, Professor, versus, et cetera, for example, kilometers |
| Mixed letters and digits: "2B", "4x4", "24/7", "50/50" | "twob", "twenty-four/seven" | two B, four by four, twenty-four seven, fifty-fifty |
| `#` and `@`: "#tagged", "@mia" | "hash tagged", "at mia" | hashtag tagged, or drop the symbol |
| Emoticons, emoji, "<3", ":)", "lol", "brb" | "<three", noise, or a strange word | heart, or describe it, or LOL |
| URLs, emails, usernames | read character soup | spell it out or describe it |
| Stutters: "I-I", "W-what" | mangled or skipped | I... I, W... what |
| Stretched words: "Yesss", "Nooo", "Mmm", "Ugh", "Hmm", "Haha" | odd noises, sometimes garbled | yes, no, or keep it if the user likes it |
| Sound effects: "Thwack!", "Bzzz", "Shhh" | unpredictable | keep, cut, or describe |
| A name alone at the end of a 1–2 word line: "Mia.", "Damn, Mia." | "Mia dot" | nothing to fix: the renderer ends these on "..." itself |
| Very short lines, 1–3 words alone ("Yes,", "Mason,") | XTTS garbles tiny inputs; the renderer retries but some slip through | nothing to fix; just count them for the user |
| Made-up or foreign names and words | guessed, and can change from line to line | a phonetic respelling used everywhere, for example "Aris" → "AIR-iss" |
| Heteronyms where the context is unclear: read, lead, live, tear, wind, close, bow | the wrong reading | rarely worth fixing; flag only the ones that matter |
| Non-English phrases inside English text | English pronunciation | leave, translate, or make it its own segment with that `language` |
| Song lyrics, poems, lists, text messages, letters | flat, no line breaks or pauses | usually fine; flag long ones |

Write the list to `books/<slug>/tts_fixes.md`, grouped by problem:

```
## Years (3)
| # | Where | Text | Proposed fix |
| 1 | u007p0012 | ...left in 1995 and... | 1995 → nineteen ninety-five |
```

Keep the "Text" column to a few words around the problem. When the same
fix applies everywhere (every "Prof." → "Professor"), list it once with a
count.

## 1.3 Pronunciation test

The user should hear the fixes, not just read them. Write
`books/<slug>/pron_test.json` with one short line per proposed respelling
or fix, grouped like `tts_fixes.md`. For each one, give the user both
versions to compare: the line as written, then the line with the fix.

```json
{"language": "en", "groups": [
 {"title": "Respelled names", "lines": [
  {"label": "Aris, as written", "text": "I told Aris to wait outside."},
  {"label": "Aris -> AIR-iss", "text": "I told AIR-iss to wait outside."}]},
 {"title": "Years", "lines": [
  {"label": "1995 -> nineteen ninety-five", "text": "She left in nineteen ninety-five."}]}
]}
```

Make each line a short sentence of your own around the word, not a quote
from the book. Group book-wide fixes once, not once per place. Then build
and render it:

```bash
python3 plan_tools.py prontest books/<slug> --voice "<voice name>"
./render.sh books/<slug>/pron_test/plan.json --out books/<slug>/pron_test --no-blend --yes --plain
```

Use the voice you expect to give the Narrator (`ls voices/new voices/old`); once
`book.json` exists, `--voice` can be left out. You get one `NNN.wav` per
group in `pron_test/`, with `INDEX.md` listing the lines in each. Run it in
the background and watch it like a helper; it takes a minute or two. This
is the only render you ever start.

## Gate 1: the user edits the text

**STOP and tell the user:**

- the path to `book.txt`, and its section, paragraph and word counts;
- each section you deleted, and what kinds of junk you fixed;
- how many source mistakes you fixed per kind, pointing them to
  `text_fixes.md`, plus the "Unsure" ones for them to answer;
- the quirks you found, with counts per problem, pointing them to
  `tts_fixes.md`;
- the pronunciation test in `pron_test/`, with `INDEX.md` to follow along.

If the user changes a fix, update `pron_test.json` and run 1.3 again so
they can hear the new version.

The user answers per group or per line: apply, skip, or a different fix.
Apply exactly what they approve, to `book.txt`, and nothing else. Show
the new counts and confirm what you changed.

The user edits `book.txt` however they like: cuts a scene, rewrites a
passage, renames something. **Wait for their go-ahead.** Applying their
approved fixes isn't a go-ahead either: after that, wait for their "go". Don't start
Phase 2, read ahead or send out scouts until they say go, however long it takes.

# Phase 2 · Read

- **Goal:** the whole book read, and everything learned written into
  `bible.md`.
- **Needs:** the user's "go" at Gate 1.
- **Done when:** `extract/` exists, the whole book is covered by reports in
  `scout/`, and `bible.md` covers every item in 2.3. There is no gate here:
  go straight on to Phase 3.

## 2.1 Extract

Only after the user's "go" at Gate 1:

```bash
python3 plan_tools.py extract books/<slug>/book.txt books/<slug>
```

From here on `book.txt` is the book. Don't go back to the EPUB, and don't
edit `book.txt` yourself, except to fix source mistakes (below). If a helper spots another XTTS quirk later,
add it to `tts_fixes.md` and ask the user about it at Gate 3 (4.4). If they
approve, edit `book.txt`, re-extract and redo that chapter. If the user edits it again later, run this step
again. Paragraph IDs can shift in the sections they changed, so redo those
chapters from 4.1.

This writes `books/<slug>/extract/uNNN.txt`, one file per EPUB section.
Every paragraph gets an ID:

```
u007p0012 | “Hand me the map,” Mom said.
```

- Italics are kept as `*like this*`. They're thoughts, texts and emphasis.
- Scene-break lines (`* * *`) are already removed.
- `extract/index.json` lists the sections with word counts and first lines.

**Source mistakes found later.** Scouts, workers and reviewers will spot
mistakes the cleaning missed. Every helper brief says: report each source
mistake you find (paragraph ID, before, after) under "Source mistakes" in
your reply, and don't fix it in your own files. You apply them, using the
same fix / leave alone rules as 1.1:
- **Before any chapter is labelled** (Phase 2 and 3): fix them in
  `book.txt` and re-run `extract`. Fixing words inside a paragraph doesn't
  change any paragraph IDs.
- **After a chapter is labelled** (Phase 4 and 5): fix `book.txt`, re-run
  `extract`, then change the same words in that chapter's
  `segments/<id>.json` and run `verify --chapter <id>` until it's clean. If
  the fix changes who speaks (a quote mark that moves a line out of the
  narration), relabel those lines too.
- Collect them and apply them in batches (every few batches), not
  one at a time.
- Add each one to `text_fixes.md`, and put the ones you were unsure about
  in `questions.md` (4.4) instead of fixing them.

## 2.2 Scout the book

Nothing gets labelled until the whole book has been read. **Scouts do the
reading for any book over 40,000 words** (the total in
`extract/index.json`). Reading a whole novel yourself fills your context
before the labelling even starts; scouts read in parallel and finish
sooner. You still know the book first-hand: you read chapter 1 in full and
every passage the scouts flag (item 3 below).

**Under 40,000 words** (a novella or short story), skip the scouts: read
the whole book yourself, write what a scout would (see `briefs/SCOUT.md`)
into `scout/all.md` as you go, and then write the bible from it.

1. **Slice the book.** Check the word counts in `extract/index.json`. Cut
   the chapters, in reading order, into slices of roughly 20–30k words.
   Never split a chapter across slices.
2. **Send out scouts.** Give each slice to its own helper (ACP delegates
   with `agent: "worker"`, 4 at a time as a rolling pool; see Helpers).
   Each brief is `briefs/SCOUT.md`, plus the slug, the slice's extract file
   paths and the output path `books/<slug>/scout/<first unit>-<last unit>.md`.
   The brief file says what to read and what the report covers: chapters,
   characters, unnamed speakers, deep-POV evidence, traps, open questions,
   source mistakes and XTTS quirks, all with paragraph IDs.
3. **Merge them yourself.** Read `extract/index.json`, every scout report,
   the first chapter in full, and every paragraph a scout cites as a trap
   or open question. Merge the reports into `bible.md`:
   - The same person under two names in two slices becomes one character
     with aliases.
   - Two different people sharing a name stay separate, with a trap note.
   - Pick the narration mode and POV characters for the whole book.
4. **Settle conflicts and gaps.** When scouts disagree, or a question needs
   more reading than a few paragraphs, send a subagent to answer it with
   the exact paragraph ranges, or read them yourself. If a slice's report
   is thin or confused, send that slice to a fresh scout.
5. **Optional reconcile pass.** For a long series or a huge cast, send the
   draft bible back out to fresh scouts, one per slice, to check every
   character and alias against their slice. Merge their corrections.

The scouts' reading is the bible's evidence: make the Phase 3 cast and the
4.1 briefs from it, and keep `scout/` until the plan is built.

## 2.3 Write the bible

You end Phase 2 with `books/<slug>/bible.md` containing:

- **Narration mode per chapter.**
  - *Third person* (`he/she` prose): narration is spoken by `Narrator`.
  - *First person* (`I` prose): narration is spoken by the POV character.
  - Name the POV character of each chapter. Note mid-chapter POV switches
    and the paragraph ID where each one happens.
  - *Deep third person limited:* see 2.4. Decide whether
    the book is one before Phase 3.
- **Characters.** For each one:
  - canonical name, plus every alias: nicknames, titles, "Mom", "the
    Captain", surname-only uses
  - gender and rough age
  - relationships ("Mom" = Ruth, Daniel's mother)
  - which chapters they appear in
  - speech habits that identify them (pet names they use, swearing, accent,
    catchphrases)
- **Unnamed or minor speakers** (`Waiter`, `Guard 1`, `Girl at party`).
  Keep their count small and reuse names.
- **Traps.** Twins, two people with the same nickname, a character whose
  name changes, flashbacks, phone calls, text messages, letters.

## 2.4 Deep POV books (deep third person limited)

Many modern novels (romance, YA, thrillers and more) are written in
**deep third person limited**. The grammar is third person (`he`, `she`), but almost all of the prose is the POV
character's interior voice, even outside quotes:
- their opinions, slang and swearing
- their questions (`Why did he always do this?`)
- their judgments (`God, he was an idiot.`)

So a neutral narrator reading that prose sounds like she's reading the
character's inner thoughts. And there is no clean sentence-level boundary
between "thought" and "the rest".

**How to recognise it:** most narration paragraphs could be put in first
person just by changing he/she to I, and they'd read naturally. The tone,
vocabulary and opinions change with whose chapter it is.

**Rules:**
1. **Never carve free indirect thought out sentence by sentence.** Splitting
   every opinionated sentence away from the narrator produces constant
   mid-paragraph voice switches that sound broken. Rule 7 (in
   `briefs/LABELLING_RULES.md`) still applies:
   first-person thoughts and direct present-tense ones go to the thinker,
   and third-person ones stay with the narrator.
2. **Tell the user at Gate 2** that the book is deep POV, with 2–3
   short example paragraphs as evidence. Let them choose:
   - **A. POV voices** (recommended for deep POV): each chapter's narration is
     voiced by that chapter's POV character, like a first-person book. Set
     `narrated_by` to the POV character per chapter, and use
     `narrated_by_changes` at each POV switch. The neutral `Narrator` keeps
     only the announcements and any truly omniscient passages (prologues,
     epilogues, scenes with no POV character).
   - **B. Neutral narrator:** `Narrator` reads all the prose, including the
     interior voice. Only first-person and direct thoughts (rule 7) go
     to the characters.
3. **Apply the choice consistently** to the whole book. The only exceptions
   are chapters that are genuinely different, such as an omniscient prologue.
   Record the choice and the reason in `bible.md`.
4. **With option A, the POV voice follows the POV, not the paragraph.** A
   POV switch happens at a scene break or chapter start, never mid-scene,
   unless the book really does switch mid-scene. In that case, mark it with
   `narrated_by_changes` at the exact paragraph.
5. **Option A sets `"pov_mode": "deep_pov"`** in `book.json`. Option B
   leaves it `"third_person"`. This also decides who speaks the POV headers
   (see 2.5).

## 2.5 POV changes (who says the POV character's name)

Many books mark whose point of view a chapter or section is in with a
header: a line `DANIEL`, `Mara`, `Chapter 3 - Daniel`, or a name over a
scene break. These are **spoken, never skipped**, and labelled
`POV_HEADER`. Who says them follows the book's `pov_mode`:

- `deep_pov`: the POV character says their own name.
- `third_person` (including deep POV option B) and `first_person`: the
  `Narrator` says it.

How to label them, and where the `narrated_by_changes` entry goes, is in
`briefs/LABELLING_RULES.md` ("POV headers").

# Phase 3 · Cast

- **Goal:** every speaker has a voice the user has approved.
- **Needs:** `bible.md` and the scout reports.
- **Done when:** `book.json` is written, `cast_voices/` matches it, and the
  user has given the go-ahead at Gate 2.

## 3.0 Series: reuse the earlier books' cast

Before casting, check whether this book shares a series or characters with
a book already done: look through `books/*/book.json` for the same
`series`, the same author, or several of the same character names. If you
can't tell, cast it as a standalone and mention the possible link at
Gate 2.

If it does, start from the most recent earlier book in the series:
- **Returning characters keep their voice and aliases.** Copy their `cast`
  and `aliases` entries as they are, and add any new aliases this book
  uses. Mark them as returning in `bible.md`.
- **The Narrator keeps the same voice too**, unless this book's narration
  mode is different (first person this time, or a different POV
  character).
- Cast only the new characters fresh, and avoid giving them a voice a
  returning character already uses.
- Set the same `series` name in this book's `book.json`.

## 3.1 Write `books/<slug>/book.json`

```json
{
  "title": "Book Title",
  "author": "Author Name",
  "language": "en",
  "series": "Series Name",
  "pov_mode": "third_person",
  "announcement": "This is, Book Title. By Author Name. Narrated by <narrator names> with a Full Cast.",
  "outro": "You were listening to, Book Title. By Author Name.",
  "cast": {
    "Narrator": "<voice file from voices/>",
    "Daniel": "<voice file from voices/>",
    "Mara": "<voice file from voices/>",
    "Ruth": "<voice file from voices/>",
    "Waiter": "<voice file from voices/>"
  },
  "aliases": { "Ruth": ["Mom", "Mrs. Hale"], "Daniel": ["Danny"] },
  "main": ["Daniel", "Mara"],
  "chapters": [
    { "id": "001", "title": "Chapter 1", "announce": "Chapter 1.", "source": ["u007"], "narrated_by": "Narrator" },
    { "id": "002", "title": "Chapter 2 - Mara", "announce": "Chapter 2.", "source": ["u008"], "narrated_by": "Mara",
      "narrated_by_changes": [["u008p0068", "Daniel"]] }
  ]
}
```

- **`cast`** maps each speaker to a voice name. The voice name is a file in
  `voices/` without the extension (`ls voices/`). Every speaker you will
  ever use must be a key here, `Narrator` included. Several minor characters
  may share one voice. **Propose** voices by gender and age. Voice choice
  is the user's call; see Gate 2 below.
- **`series`** is the series name, if the book is part of one (see 3.0).
  Leave it out for a standalone.
- **`main`** lists the main characters: the POV characters and the other
  leads. It's usually 2–4 names. The user confirms it at Gate 2. Gate 3 only asks the user about
  lines involving these characters.
- **`aliases`** feeds the self-address check in `verify`. Put every name
  others use for that character here.
- **`chapters`** must be in reading order.
  - `source` lists the extract units that make up the chapter.
  - If one unit holds two chapters, split it with `"from": "<first para id>"`
    and `"to": "<last para id>"`.
  - Front/back matter (copyright, dedication, acknowledgements, "also by",
    TOC) gets no chapter.
- **`pov_mode`** is `first_person`, `third_person` or `deep_pov` (see 2.4
  and 2.5). Ask the user if unsure.
- **`narrated_by`** is who voices `NARRATION` in that chapter: `Narrator`
  for third person, the POV character for first person and `deep_pov`.
  `narrated_by_changes` switches it from a given paragraph on.
- **`announce`** is spoken by `Narrator` before the chapter. Use
  `"announce_by": "<speaker>"` to change who says it.
- **`announcement`** plays before chapter 1, and **`outro`** plays after
  the last line of the book, both read by `Narrator`. Always write both;
  without an outro the book just stops. If the book has a sequel or is part
  of a series, add a line about it to the outro (for example "Noah and
  Mia's story will continue in Empire of Rain!"), and ask the user when you
  aren't sure of the name. Write the author's name the way it should be
  spoken: `Silken Starling`, not the handle `Silken_Starling`. Show both
  lines to the user at Gate 2.
- **The 2-second pauses are automatic.** `build` puts 2 seconds of silence
  after the `announcement` and before the `outro` in `plan.json`. Don't add
  them yourself, and don't hand-edit `plan.json` to change them.

## 3.2 Copy the cast voices

When `book.json` is written, copy every chosen voice into the book's
folder so the user can listen to them:

```bash
python3 plan_tools.py castvoices books/<slug>
```

This fills `books/<slug>/cast_voices/` with one file per character, named
`NN <Character> - <Voice>.mp3`: the Narrator first, then the main
characters, then everyone else. A character who shares a voice gets their
own copy. If it says `MISSING`, the voice name in `cast` doesn't match a
file in `voices/`. Fix the name and run it again.

## Gate 2: the user approves the cast

**STOP and show the user:**

- the path to `cast_voices/`;
- the cast table: character, voice, a one-line description (gender, age,
  role) and roughly how many lines they have. Keep it easy to get through:
  - **Returning characters** from an earlier book in the series go in one
    short table of their own: name, voice, "same as <book>". The user only
    needs to look at them if they want a change.
  - **A big cast (over 15 characters):** list the Narrator, the `main`
    characters and anyone with 20 or more lines one per row. Group the
    rest by voice, one row per voice: "Waiter, Guard 1, Clerk: <voice>,
    minor male adults, 3–8 lines each". The user can approve a whole group
    at once or split one out;
- the `main` list;
- the `announcement` and `outro` lines;
- for a deep POV book, the A/B choice from 2.4 with 2–3 example paragraphs.

**The user has the final call on every voice.** They listen, then change
any voice they want. Apply each change to `cast` in `book.json`, run
`castvoices` again so the folder matches, and show the new table. Repeat
until they give the go-ahead. Don't start Phase 4, or hand any work to a
helper, until they do.

# Phase 4 · Label

- **Goal:** every line of every chapter has the right speaker, checked twice.
- **Needs:** the user's go-ahead at Gate 2.
- **Done when:** every chapter shows `0 errors, 0 unreviewed warnings`,
  every chapter has had a blind review, and the user has answered
  `questions.md` at Gate 3.

## 4.1 Label the chapters (parallel helpers)

Once the user has approved the cast, split the chapters into batches of
about 3–5 and hand each batch to its own **worker** delegate
(`acp_delegate`, `agent: "worker"`, `async: true`). Run them as the
rolling pool of 4 from Helpers: start 4, and start the next batch each time
one comes back. Subagents can take batches too, but they count toward the
same 4.

First write the scene-continuity files, the last 30 paragraphs of the
chapter before each one:

```bash
python3 plan_tools.py context books/<slug>
```

Each worker's brief is `briefs/WORKER.md` (it covers what to read, what to
write and what to report), plus:
- the slug
- its chapter IDs
- the `ctx/<id>-prev.txt` file for the first chapter of its batch

Don't paste this whole workflow into a brief: the worker only needs its
brief file and the files it names. Two workers never share a chapter. Each
writes only its own `segments/<id>.json`, and leaves "speaker not in cast"
errors for any new character it had to name; you add those (below).

**Late additions.** The user approved the cast at Gate 2, so every character
added after that is unapproved:
- Before adding one, check it isn't an alias of an existing character, or a
  minor speaker you could reuse (`Guard 1`). Add a new name only when
  neither fits.
- Give it a **provisional** voice, chosen by gender and age like the rest,
  and record it in `books/<slug>/late_cast.md`: name, provisional voice,
  first paragraph ID, a one-line description and roughly how many lines it
  has.
- Don't stop labelling to ask. The user reviews these at Gate 3.

The segment format, the attribution rules, POV headers and every `verify`
warning are in **`briefs/LABELLING_RULES.md`**. Read it in full before you
start Phase 4: the workers and the blind reviewers follow the same file, and
you judge their work by it.

## 4.2 Verify each chapter until clean

When a worker's batch comes back, run `verify --chapter <id>` on each of
its chapters yourself:

```bash
python3 plan_tools.py verify books/<slug> --chapter 001
```

- Copy each warning the worker kept, with its reason, into
  `books/<slug>/reviewed.json` (format in `briefs/LABELLING_RULES.md`),
  after checking the reason holds.
- Add its new characters (see Late additions above), then fix any warning
  or error that's left.
- Add its source mistakes to your batch of fixes (2.1) and its unsure lines
  to your list for 4.4.

A chapter is done with 4.2 when verify shows `0 errors, 0 unreviewed
warnings`.

## 4.3 Blind review (second check, parallel helpers)

Every chapter is labelled a second time by a **blind reviewer**: a fresh
helper that never sees the first labels. It labels the chapter from scratch
into `blind/<id>.json`, and `plan_tools.py compare` lists every line where
the two readings disagree. A reviewer that reads the first labels mostly
agrees with them; a blind one actually checks.

1. **Send out reviewers** (`acp_delegate`, `agent: "reviewer"`,
   `async: true`, `timeoutMinutes: 75`), in the rolling pool of 4. Each
   brief is `briefs/REVIEWER.md`, plus the slug, its chapter IDs and their
   `ctx/` files. Batch the chapters as in 4.1, but never give a reviewer a
   chapter it labelled.
2. **Compare** each chapter as soon as its reviewer is back:

   ```bash
   python3 plan_tools.py compare books/<slug> --chapter 007
   ```

   It writes `books/<slug>/review/<id>.json`: one row per disagreement,
   with the paragraph, the labelled text, `labelled`, `should_be` (the
   reviewer's speaker), the reviewer's split (`blind_text`) and its reason
   from `blind/<id>.notes.json`. It compares who is actually heard, so a
   different split, or `NARRATION` vs the POV character in a first-person
   chapter, isn't a disagreement. If it says the text differs in a
   paragraph, the reviewer's text is wrong there. Run
   `verify --chapter <id> --dir blind`, correct only the text in
   `blind/<id>.json` (never its speakers) until it's clean, then compare
   again. If it's badly broken, send the chapter to a fresh reviewer.
3. **Settle every disagreement yourself:**
   - Re-read the passage, with a few paragraphs before and after.
   - Decide who is right. If it's the reviewer, fix `segments/<id>.json`.
   - If you keep the original label, add a `reviewed.json` entry with the
     reason, keyed `REVIEW:<p>`.
   - When you can't decide, send the passage to a subagent for a third
     reading, with the exact paragraph range and both options. If it
     involves a main character and is still unclear, it goes in
     `questions.md` (4.4).
4. **Look for patterns.** When several disagreements in a batch are the same
   mistake (one character's thoughts always left in narration, a run always
   off by one), check the labeller's other chapters for it too.
5. Run `verify --chapter <id>` again on every chapter you changed.

Never skip this step, even when 4.1 looked clean, and never let a reviewer
see `segments/`. Most wrong-speaker bugs survive a single reading. A
chapter with zero disagreements is fine; a whole book with zero is a sign
a reviewer saw the labels.

## 4.4 Collect the questions

After both passes, collect every line where you are **not fully sure** of
the speaker. That includes:
- reviewer disagreements you settled on a judgment call
- runs where counting and content point different ways
- pronoun tags with two possible people
- any `reviewed.json` reason that contains a guess

Also ask about any new XTTS quirk a helper found after Phase 1 (see
`tts_fixes.md`), whatever character it involves.

Don't hide doubts to look finished. A line is uncertain if you would bet
less than ~95% on it.

**Only ask the user about lines involving a main character** (the `main`
list in `book.json`): the current label or any of the possible speakers
is a main character. When every option is a minor or side character
(`Waiter` vs `Guard 1`, Ben vs Leo), decide yourself. Pick the most
likely speaker, note the reason in `reviewed.json` and don't ask.

**Exception: narrator confusion is always asked, whoever is involved.** Ask
about any line where you're unsure whether quoted text is spoken by a
character or belongs to the narrator (rule 10). Also ask about any line
where you're unsure whether prose should be `NARRATION` or a character's
speech or thought. That includes italics that might be either a thought or
just emphasis (rule 7). Use the same format, with
`Options: Narrator / <character>`.

**Late cast additions are always asked**, main or minor. If
`late_cast.md` has any entries, put them at the top of `questions.md` under
`## New characters`, one row each: name, provisional voice, first paragraph
ID, description and line count. Add every late addition to `cast` with its
provisional voice and run `castvoices` first, so the user can listen to
the new voices in `cast_voices/` too. The user confirms or changes each
voice, and may merge a character into an existing one; apply that in
`book.json` and `segments/`, and run `castvoices` again, before Phase 5.

Write them all to `books/<slug>/questions.md` and show the list to the user
in chat. Number every question, and show each one like this:

```
### Q3 · Chapter 7 · u013p0088
Options: Daniel / Tom        Current label: Daniel
Why unsure: two men present, tag is only "he said"

  u013p0085  Tom set the box down. "That's the last of it."
  u013p0086  Daniel didn't answer.
  u013p0087  "You're welcome," he added.
  u013p0088 >>> "I didn't ask for your help." <<<
  u013p0089  The door slammed.
  u013p0090  "Well," Ruth said. "That went great."
```

- Show 3–5 paragraphs before and 2–3 after, copied exactly from `extract/`,
  each with its paragraph ID. Mark the line in question with `>>> <<<`.
- Give the options you are deciding between, the current label and the
  reason for the doubt in one line.
- Group the questions by chapter, in reading order.

## Gate 3: the user answers the questions

**Stop and wait.** The user answers by number (e.g. "Q3 Tom, Q4 keep,
Q7 Ruth").
- Apply each answer to `segments/<id>.json` exactly as given.
- If an answer changes who speaks in an alternating run, re-check the rest
  of that run.
- Record each one in `reviewed.json` as `USER:<p>`: "user: <answer>".
- Re-run `verify` on the chapters you touched.

If no doubts remain and there are no late cast additions, say so
explicitly and move on to Phase 5 without stopping.

# Phase 5 · Build

- **Goal:** `plan.json` and a report the user can check before rendering.
- **Needs:** Gate 3 answered (or no questions left).
- **Done when:** `build` has written `plan.json`, `report.md` is shown to
  the user, and you've stopped for good at Gate 4.

## 5.1 Final pass and build

1. Run `python3 plan_tools.py verify books/<slug>` for the whole book. That
   adds the book-wide checks (single-line speakers, unused cast, skipped
   sections).
2. Skim `Lines per speaker`. Does every count make sense for the story?
3. Build:

```bash
python3 plan_tools.py build books/<slug>
```

`build` refuses while any error or unreviewed warning remains. It writes
`books/<slug>/plan.json` with the announcement, chapter announcements,
narration resolved to the right voice per chapter, and absolute voice paths.

## 5.2 Final report to the user

When the plan is built, write `books/<slug>/report.md` and show it to the
user in chat. It has two parts.

**1. Speaker distribution**, computed, never estimated:

```bash
python3 plan_tools.py stats books/<slug>
```

Paste its tables as they are:
- each character's share of the dialogue and of the whole book
- who voices the narration
- total airtime per character

Below them, add 2–3 lines on anything surprising. For example, a main
character with less dialogue than a side character, or one voice carrying
over 80% of the book.

**2. Decision-making**, short and concrete:
- **POV and narration mode.** First, third or deep POV, the user's A/B
  choice, and which chapters use which narration voice and why.
- **Cast decisions.** Merged or renamed characters, minor speakers grouped
  under one name (`Waiter`, `Guard 1`), and voices shared between
  characters. List every late cast addition with the voice it ended up
  with, and whether the user confirmed or changed it.
- **Judgment calls by category, with counts.**
  - untagged back-and-forth runs resolved by content
  - pronoun tags resolved
  - thoughts given to characters
  - quotes kept as narration (scare quotes, titles)
  - telepathy, phone calls and letters

  Give one or two real examples each, with paragraph IDs.
- **What the review changed.** Disagreements per chapter, how many were real
  fixes, which ones went to a third reading, and any pattern you found.
- **The user's answers at Gate 3,** and what they changed.
- **Source mistakes fixed,** counts per kind from `text_fixes.md`, split
  into the ones fixed while cleaning and the ones the helpers found later.
- **Remaining risks.** Any spot you would still double-check by listening,
  with chapter and paragraph ID. If there are none, say so.

## Gate 4: the user starts the render

**Stop here for good.** Post the report, set `progress.md` to "Phase 5
done, waiting on: the user to render", and end your turn. Do not render. The user
reads the report and gives the final go-ahead, then runs the render
themselves:

```bash
./render.sh books/<slug>/plan.json --check     # cast table, stats, time estimate
./render.sh books/<slug>/plan.json             # render
```

## Efficiency notes

- In Phase 2 the scouts do the reading; you read their reports, chapter 1
  and the passages they flag. Helpers do the chapter-by-chapter reading in
  4.1 and 4.3. (Who owns which file, and what helpers read: see Helpers above.)
- Write each chapter file in one go, then run verify. Fix everything in one
  edit pass, then verify again.
- The repo-wide rules (no printing book text, no editing the tools,
  Python, git) are in `AGENTS.md`.
