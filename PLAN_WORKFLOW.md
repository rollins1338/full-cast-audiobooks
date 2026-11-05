# Making a full-cast plan.json for any book

Use this file with **DeepSeek Flash**. Point it at this workflow and the EPUB,
and tell it to follow every step exactly. It must use `plan_tools.py` and read
the book line by line — no regex or alternating-turn speaker guessing.

You are turning an EPUB into a speaker-labelled plan that `render.sh` renders.
**Your job is judgment: who says each line.** All the mechanical work
(extraction, text fidelity checks, plan assembly, voice paths) is done by
`plan_tools.py`. Never write plan.json by hand and never write your own
extraction or attribution script. Regex/heuristic speaker guessing is exactly
what has failed before: **you read every line and decide.**

Run everything from the repo root (the folder containing `plan_tools.py`).
`<slug>` is a short book name like `my_book`.

> **Two stops are always the user's.** After Step 0 you stop until the user
> has edited `book.txt` and says go. After Step 9 you stop for good: the
> user starts the render.
>
> **Never start a render.** Your job ends when `plan.json` and the report are
> done. Rendering always waits for the user's go-ahead, and the user starts it
> themselves. Don't run `./render.sh` without `--check`, don't render a test
> chapter or a sample, and don't tell a helper to. This holds even when
> everything passes, even when you have time left, and even if an earlier
> message seemed to allow it. `./render.sh ... --check` is fine: it only
> prints the cast table, stats and a time estimate.

### Helpers: subagents and ACP delegates

You don't have to do the reading alone. Run helpers in parallel, as many and
as often as the book needs. **You have full permission to design the setup
yourself:** how many helpers, how big each batch is, which tool runs which
job, and whether to add extra passes (a reconcile pass, a third review, a
spot check). You don't need to ask the user about any of that.

Every helper runs **DeepSeek Flash 4.1** (`opencode-go/deepseek-v4.1-flash`),
the same model as you. ACP delegates inherit your model. For subagents, use
an agent with no `model` line, or one set to that model.

Use both kinds, together:

| Kind | Tools | Best for |
| --- | --- | --- |
| **ACP delegates** | `acp_delegate`, `acp_delegate_wait`, `acp_delegate_cancel`, `acp_delegate_list` | Long background batches: scouting (step 2), labelling (step 4), review (step 6). They run while you keep working; up to 6 at a time, the rest queue. |
| **Subagents** | the client's subagent tool (for example pi's `subagent`, with parallel `tasks`) | Short jobs whose answer you need before you go on: settle an alias question, re-check one chapter, compare two readings of a passage. |

When the delegate queue is full, subagents add more parallel capacity. If
the API starts returning rate-limit errors, run fewer at once.

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
- Start a whole wave of delegates first (`async: true`), then collect them
  one by one. Don't run batches one after another.
- You can't message a helper while it runs. Anything it needs has to be in
  the brief; anything you learn later goes in a fix pass or a new helper.
- Helpers never write `book.json`, `bible.md` or `reviewed.json`. Those are
  yours.
- Two helpers never write the same file.
- The user's checkpoints don't move: the cast approval in step 3 and the
  questions in step 7 are always yours to bring to the user.
- If neither tool is available, say so, and do the batches yourself, one
  at a time.

---

## Step 0: Make the clean text, then STOP

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
- **Don't change the story.** No rewording, no cuts, no fixing the
  author's grammar or spelling. Keep every chapter heading. Pronunciation
  fixes are proposals for the user (see below), not cleaning.
- Don't touch the `=== uNNN ===` lines, and don't add your own sections.

On a big book, split the cleaning across helpers by section, one helper
per range of sections, each editing only its own range.

### XTTS quirks: find them, propose fixes, ask the user

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

Then **STOP and tell the user:**

- the path to `book.txt`, and its section, paragraph and word counts;
- each section you deleted, and what kinds of junk you fixed;
- the quirks you found, with counts per problem, pointing them to
  `tts_fixes.md`.

The user answers per group or per line: apply, skip, or a different fix.
Apply exactly what they approve, to `book.txt`, and nothing else. Show
the new counts and confirm what you changed.

The user edits `book.txt` however they like: cuts a scene, rewrites a
passage, renames something. **Wait for their go-ahead.** Applying their
approved fixes isn't a go-ahead either: after that, wait for their "go". Don't run Step 1,
read ahead or start the bible until they say go, however long it takes.

## Step 1: Extract

Only after the user's go-ahead on Step 0:

```bash
python3 plan_tools.py extract books/<slug>/book.txt books/<slug>
```

From here on `book.txt` is the book. Don't go back to the EPUB, and don't
edit `book.txt` yourself. If a helper spots another XTTS quirk later,
add it to `tts_fixes.md` and ask the user about it in Step 7. If they
approve, edit `book.txt`, re-extract and redo that chapter. If the user edits it again later, run this step
again. Paragraph IDs can shift in the sections they changed, so redo those
chapters from Step 4.

This writes `books/<slug>/extract/uNNN.txt`, one file per EPUB section.
Every paragraph gets an ID:

```
u007p0012 | “Hand me the map,” Mom said.
```

- Italics are kept as `*like this*`. They're thoughts, texts and emphasis.
- Scene-break lines (`* * *`) are already removed.
- `extract/index.json` lists the sections with word counts and first lines.

## Step 2: Read the whole book, write the bible

Nothing gets labelled until the whole book has been read. How it gets read
depends on its size. Check the word counts in `extract/index.json`.

- **Small book** (it fits comfortably in your context with room to work,
  roughly under 100k words): read **every** `extract/u*.txt` yourself, from
  start to finish.
- **Big book** (anything larger, or a series omnibus): split the reading
  across helpers. See "Big books: the scout pass" below.

Either way, you end with `books/<slug>/bible.md` containing:

- **Narration mode per chapter.**
  - *Third person* (`he/she` prose): narration is spoken by `Narrator`.
  - *First person* (`I` prose): narration is spoken by the POV character.
  - Name the POV character of each chapter. Note mid-chapter POV switches
    and the paragraph ID where each one happens.
  - *Deep third person limited:* see "Deep POV books" below. Decide whether
    the book is one before step 3.
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

### Big books: the scout pass

1. **Slice the book.** Cut the chapters, in reading order, into slices of
   roughly 20–30k words. Never split a chapter across slices.
2. **Send out scouts.** Give each slice to its own helper (ACP delegates
   with `agent: "worker"`; add subagents if you want more running at once).
   Each brief includes this file's Step 2, the slice's extract file paths,
   and the output path `books/<slug>/scout/<first unit>-<last unit>.md`.
   Each scout reads its slice in full and writes, citing paragraph IDs
   throughout:
   - **Per chapter:** narration mode (first person, third person, possibly
     deep POV), the POV character, any mid-chapter POV switch, and any POV
     header line.
   - **Characters met:** every name and alias exactly as the text uses it,
     gender and age clues, relationships, speech habits, the first
     paragraph where they speak, and roughly how many lines they have.
   - **Unnamed speakers** (`the bartender`, `a girl in the hall`).
   - **Deep-POV evidence:** a few paragraphs where narration slides into a
     character's own thoughts, and a few where it doesn't.
   - **Traps** in the list above, and **open questions** ("is 'the boss'
     in u031p0040 Logan?").
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

The scouts' reading is the bible's evidence: make the step 3 cast and the
step 4 briefs from it, and keep `scout/` until the plan is built.

### Deep POV books (deep third person limited)

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
   mid-paragraph voice switches that sound broken. Rule 7 still applies only
   to *explicit* direct thoughts: italic ones, tagged ones, or first-person
   ones.
2. **Stop and tell the user in step 3** that the book is deep POV, with 2–3
   short example paragraphs as evidence. Let them choose:
   - **A. POV voices** (recommended for deep POV): each chapter's narration is
     voiced by that chapter's POV character, like a first-person book. Set
     `narrated_by` to the POV character per chapter, and use
     `narrated_by_changes` at each POV switch. The neutral `Narrator` keeps
     only the announcements and any truly omniscient passages (prologues,
     epilogues, scenes with no POV character).
   - **B. Neutral narrator:** `Narrator` reads all the prose, including the
     interior voice. Only explicit direct thoughts (rule 7) go to the
     characters.
3. **Apply the choice consistently** to the whole book. The only exceptions
   are chapters that are genuinely different, such as an omniscient prologue.
   Record the choice and the reason in `bible.md`.
4. **With option A, the POV voice follows the POV, not the paragraph.** A
   POV switch happens at a scene break or chapter start, never mid-scene,
   unless the book really does switch mid-scene. In that case, mark it with
   `narrated_by_changes` at the exact paragraph.
5. **Option A sets `"pov_mode": "deep_pov"`** in `book.json`. Option B
   leaves it `"third_person"`. This also decides who speaks the POV headers
   (see "POV changes" below).

### POV changes (who says the POV character's name)

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

## Step 3: Write `books/<slug>/book.json`, then STOP and ask the user

```json
{
  "title": "Book Title",
  "author": "Author Name",
  "language": "en",
  "pov_mode": "third_person",
  "announcement": "This is, Book Title. By Author Name. Narrated by <narrator names> with a Full Cast.",
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
  may share one voice. **Propose** voices by gender and age, then **stop
  and show the user the cast table**. Voice choice is the user's call.
  Continue only after they confirm.
- **`main`** lists the main characters: the POV characters and the other
  leads. It's usually 2–4 names. Show it to the user together
  with the cast table; they confirm it. Step 7 only asks the user about
  lines involving these characters.
- **`aliases`** feeds the self-address check in `verify`. Put every name
  others use for that character here.
- **`chapters`** must be in reading order.
  - `source` lists the extract units that make up the chapter.
  - If one unit holds two chapters, split it with `"from": "<first para id>"`
    and `"to": "<last para id>"`.
  - Front/back matter (copyright, dedication, acknowledgements, "also by",
    TOC) gets no chapter.
- **`pov_mode`** is `first_person`, `third_person` or `deep_pov` (see "Deep
  POV books" and "POV changes"). Ask the user if unsure.
- **`narrated_by`** is who voices `NARRATION` in that chapter: `Narrator`
  for third person, the POV character for first person and `deep_pov`.
  `narrated_by_changes` switches it from a given paragraph on.
- **`announce`** is spoken by `Narrator` before the chapter. Use
  `"announce_by": "<speaker>"` to change who says it. The book
  `announcement` plays before chapter 1.

## Step 4: Label the chapters (parallel helpers)

Once the user has approved the cast, split the chapters into batches of
about 3–5 and hand each batch to its own **worker** delegate
(`acp_delegate`, `agent: "worker"`, `async: true`). Start all of them, then
collect each one with `acp_delegate_wait`. Subagents can take batches too
when you want more running at once; they follow the same rules.

Each brief includes (files by path, since the helper starts with no context):
- this file
- `bible.md` and `book.json`
- its chapter IDs
- the last ~30 paragraphs of the chapter before its batch, for scene
  continuity

Each worker reads its chapters in full and writes **only** its own
`segments/<id>.json` files. Two workers never share a chapter. It runs
`verify --chapter <id>` on each chapter until there are 0 errors. Its final
reply reports:
- every warning, with its decision
- any new character it needed

Workers never edit `book.json` or `bible.md`. A worker that meets a
speaker who isn't in the cast still labels them with a clear name, and lists
them in its report. The resulting "speaker not in cast" errors are the only
ones it may leave. When the results come back, you, the main agent, add the
new characters to the cast, then re-run `verify --chapter <id>` on those
chapters yourself.

**Late additions.** The user approved the cast in step 3, so every character
added after that is unapproved:
- Before adding one, check it isn't an alias of an existing character, or a
  minor speaker you could reuse (`Guard 1`). Add a new name only when
  neither fits.
- Give it a **provisional** voice, chosen by gender and age like the rest,
  and record it in `books/<slug>/late_cast.md`: name, provisional voice,
  first paragraph ID, a one-line description and roughly how many lines it
  has.
- Don't stop labelling to ask. The user reviews these in step 7.

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
  or drop the quote marks and `*`. Nothing else may change. Don't fix typos
  and don't add words.
- **speaker** is one of:
  - a cast name
  - `NARRATION`: prose, voiced by `narrated_by`
  - `POV_HEADER`: a POV marker, spoken aloud (see "POV changes" below)
  - `SKIP`: not spoken. Chapter headings that `announce` already covers,
    epigraph credits.
- **Split each paragraph** at every switch between quoted speech and prose.
  The dialogue tag (`he asked.`, `Mom said.`) is always `NARRATION`,
  including in first-person chapters, where `NARRATION` becomes the POV voice
  automatically.

### Attribution rules (this is the whole job)

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
7. **Inner thoughts are spoken by the thinker, never by the narrator.** This
   matters most in **third-person** books, where the narrator must not voice
   a character's thoughts.
   - **Direct thoughts** go to the character who thinks them. Split them out
     of the narration exactly like dialogue, and leave the tag (`she
     thought.`) as `NARRATION`. Direct thoughts are:
     - italic inner voice (`*I can't do this.*`, `*Why is he here?*`)
     - thoughts written in their own words, usually first person or present
       tense, with or without quotes (`This is a mistake, Mara thought.`)
     - silent words addressed to someone (`*Please don't look at me.*`)
   - **Example:**
     `Daniel stared at the door. *She's not coming back.* He turned away.`
     becomes three segments:
     - `NARRATION`: `Daniel stared at the door.`
     - `Daniel`: `*She's not coming back.*`
     - `NARRATION`: `He turned away.`
   - **What stays `NARRATION`:**
     - thoughts *reported* in the narrator's own third-person past-tense
       words (`She wondered whether he'd noticed.`, `He knew it was over.`)
     - italics that aren't thought: emphasis on one word inside a sentence
       (`she was *not* going`), titles, foreign words, sounds, sign text
   - **Telepathy and mind-speech** (common in paranormal books) is dialogue.
     It goes to the sender.
   - **First person:** the POV character's own thoughts can stay in
     `NARRATION`, because it's the same voice. Another character's thoughts
     (shared through telepathy, read in a letter) still go to that
     character.
   - **Many books don't use italics for thoughts at all.** Then judge by the
     words, not the formatting. In a third-person chapter, any `I` / `me` /
     `my` outside quotes, or present-tense words in a character's own voice
     before a tag like `she thought` or `he wondered`, is a direct thought
     and goes to that character:
     `This was a mistake. I should never have come, Mara thought.` becomes
     - `Mara`: `This was a mistake. I should never have come,`
     - `NARRATION`: `Mara thought.`

     Rhetorical questions in third person past tense (`Why did he always
     do this?` in a Mara scene) are reported thought. They stay
     `NARRATION`, unless the book clearly treats them as the character's own
     voice. Unsure? Ask in step 7.
   - **Unsure whether something is a thought or just emphasis or narration,
     or whose thought it is?** Ask the user in step 7.
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
      to the user in step 7.
11. **Never guess.** If two readings stay possible after rules 1–10, read the
    surrounding page again. Choose the one the following lines confirm (who
    answers what, who reacts). Write the reason into `reviewed.json` (see
    step 5).

Keep `bible.md` open while labelling. If a new character appears, add them
to the bible, `cast` and `aliases` first.

## Step 5: Verify each chapter until clean

```bash
python3 plan_tools.py verify books/<slug> --chapter 001
```

- **ERROR** means the text doesn't match the source, a paragraph is missing,
  a speaker isn't in the cast, and so on. Fix it. Errors block the build.
- **WARN** means a suspicious label. Each one needs a decision:
  - `QUOTE_IN_NARRATION`: dialogue left inside prose.
  - `THOUGHT_IN_NARRATION`: italic text inside the narrator's prose in a
    third-person chapter. Usually a character's thought (rule 7). Fine only
    if it's emphasis, a title, a foreign word or a sound.
  - `FIRST_PERSON_IN_NARRATION`: the narrator says I/me/my in a
    third-person chapter. Almost always a thought or a letter left in
    narration.
  - `THOUGHT_TAG_IN_NARRATION`: `she thought` / `he wondered` is still
    joined to the thought before it. Split the thought out for the thinker.
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

Don't move to the next chapter until its verify shows `0 errors, 0
unreviewed warnings`.

## Step 6: Independent review (second check, parallel helpers)

Every chapter is checked a second time by a **fresh reviewer**: an ACP
delegate (`acp_delegate`, `agent: "reviewer"`, `async: true`) or a subagent,
never the worker that labelled it. It doesn't see the labeller's reasoning, only the result.
Reviewers read whole chapters slowly, so give them a long limit (e.g.
`timeoutMinutes: 75`).

1. Each reviewer's brief names this file, `bible.md`, `book.json`, its chapter
   IDs, and their `segments/<id>.json`. Batch the chapters the same way as
   step 4, with different pairings.
2. The reviewer reads the source chapter in full from `extract/` and
   re-decides the speaker of **every** dialogue line and thought on its
   own, using the attribution rules. It must not trust the existing labels.
   Pay most attention to:
   - untagged back-and-forth runs
   - pronoun-only tags (`she said` with two women present)
   - phone calls, texts and flashbacks
   - first-person `I said` in chapters where the POV switches
   - quoted dialogue left inside `NARRATION`, especially in third-person
     chapters (rule 10). Check every narration segment that contains a quote
     mark.
   - inner thoughts left with the narrator in third-person chapters (rule 7).
     Check every narration segment that contains `*italics*`, `I`/`me`/`my`,
     or a tag like `she thought` / `he wondered`. Also read for un-tagged,
     un-italic thoughts that only the meaning gives away.
3. The reviewer **does not edit** segments. It writes
   `books/<slug>/review/<id>.json`, listing only the lines where it
   disagrees:

```json
[
  {"p": "u007p0044", "text": "“You can't be serious.”", "labelled": "Daniel", "should_be": "Ruth",
   "why": "Daniel asked the question in p0043; Ruth answers, and p0045 has Daniel react to her"}
]
```

4. You, the main agent, go through every disagreement:
   - Re-read the passage yourself.
   - Decide who is right, and fix `segments/<id>.json` if the reviewer is.
   - If you keep the original label, add a `reviewed.json` entry with the
     reason, keyed `REVIEW:<p>`.
5. **Third pass, only where needed.** If a chapter had more than ~3 real
   fixes, the labeller missed something systematic. Send that chapter to
   another fresh reviewer and repeat until a review comes back with no
   valid disagreements.
6. Run `verify --chapter <id>` again on every chapter you changed.

Never skip this step, even when step 4 looked clean. Most wrong-speaker
bugs survive a single pass.

## Step 7: Ask the user about anything still uncertain

After both passes, collect every line where you are **not fully sure** of
the speaker. That includes:
- reviewer disagreements you settled on a judgment call
- runs where counting and content point different ways
- pronoun tags with two possible people
- any `reviewed.json` reason that contains a guess

Also ask about any new XTTS quirk a helper found after Step 0 (see
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
ID, description and line count. The user confirms or changes each voice, and
may merge a character into an existing one; apply that in `book.json` and
`segments/` before step 8.

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

Then **stop and wait.** The user answers by number (e.g. "Q3 Tom, Q4 keep,
Q7 Ruth").
- Apply each answer to `segments/<id>.json` exactly as given.
- If an answer changes who speaks in an alternating run, re-check the rest
  of that run.
- Record each one in `reviewed.json` as `USER:<p>`: "user: <answer>".
- Re-run `verify` on the chapters you touched.

If no doubts remain, say so explicitly and move on.

## Step 8: Final pass and build

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

## Step 9: Final report to the user

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
  fixes, and which chapters needed a third pass.
- **The user's answers from step 7,** and what they changed.
- **Remaining risks.** Any spot you would still double-check by listening,
  with chapter and paragraph ID. If there are none, say so.

**Stop here.** Post the report and end your turn. Do not render. The user
reads the report and gives the final go-ahead, then runs the render
themselves:

```bash
./render.sh books/<slug>/plan.json --check     # cast table, stats, time estimate
./render.sh books/<slug>/plan.json             # render
```

## Efficiency notes

- The main agent stays in charge: it owns `book.json`, `bible.md`,
  `reviewed.json` and every final decision. It reads a small book fully in
  step 2; on a big book the scouts read it and the main agent reads their
  reports plus the passages they flag. Helpers do the chapter-by-chapter
  reading in steps 4 and 6.
- Workers and reviewers each re-read only their own chapters,
  plus the end of the previous chapter for scene continuity.
- Never let two helpers write the same file.
- Write each chapter file in one go, then run verify. Fix everything in one
  edit pass, then verify again.
- Don't print whole extract files back into the chat. Read them with your
  file tools.
- Don't edit `plan_tools.py`, `render.sh` or `render_engine.py`. If a tool
  is wrong, stop and report it.
