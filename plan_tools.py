#!/usr/bin/env python3
"""Book -> plan.json tooling. Standard library only, so any python3 runs it.

    python3 plan_tools.py totext  book.epub books/<slug>     # -> books/<slug>/book.txt to edit
    python3 plan_tools.py extract books/<slug>/book.txt books/<slug>   # numbered paragraphs
                                                             # (a .epub works here too)
    python3 plan_tools.py verify  books/<slug> [--chapter 003]
    python3 plan_tools.py build   books/<slug>               # -> books/<slug>/plan.json
    python3 plan_tools.py stats   books/<slug> | plan.json   # who speaks how much

The AI only ever writes book.json (cast + chapter map) and segments/<id>.json
(speaker labels + the text copied verbatim). `verify` proves no text was
dropped, duplicated or altered and flags every suspicious speaker label;
`build` turns the result into the renderer's plan.json. See PLAN_WORKFLOW.md.
"""

import argparse, json, posixpath, re, sys, unicodedata, zipfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
NARRATION, SKIP, POV_HEADER = "NARRATION", "SKIP", "POV_HEADER"
SPECIAL = (NARRATION, SKIP, POV_HEADER)
POV_MODES = ("first_person", "third_person", "deep_pov")

# ---------------------------------------------------------------- extract

BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
         "section", "article", "td", "th", "dt", "dd", "pre", "figcaption", "tr"}
ITALIC = {"i", "em", "cite"}
IGNORE = {"script", "style", "head", "title", "svg", "math"}


class Paragraphs(HTMLParser):
    """Flatten XHTML into paragraphs; italics become *...* (thoughts, texts, emphasis)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.ignore = [], [], 0

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        text = re.sub(r"\*\s*\*", "", text).strip()  # empty italic runs
        if re.search(r"\w", text):  # drops scene breaks like "* * *"
            self.out.append(text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in IGNORE:
            self.ignore += 1
        elif tag in BLOCK:
            self.flush()
        elif tag == "br":
            self.buf.append(" ")
        elif tag in ITALIC:
            self.buf.append("*")

    def handle_endtag(self, tag):
        if tag in IGNORE:
            self.ignore = max(0, self.ignore - 1)
        elif tag in BLOCK:
            self.flush()
        elif tag in ITALIC:
            self.buf.append("*")

    def handle_data(self, data):
        if not self.ignore:
            self.buf.append(data)


def spine_documents(epub: Path):
    with zipfile.ZipFile(epub) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("}item")}
        meta = {}
        for e in opf.iter():
            for key in ("title", "creator"):
                if e.tag.endswith("}" + key) and e.text and key not in meta:
                    meta[key] = e.text.strip()
        for e in opf.iter():
            if e.tag.endswith("itemref") and e.get("idref") in manifest:
                href = posixpath.normpath(posixpath.join(base, manifest[e.get("idref")]))
                from urllib.parse import unquote
                yield meta, href, z.read(unquote(href)).decode("utf-8", "replace")


def epub_units(epub: Path):
    """[(unit id, href, paragraphs)] for every non-empty spine document, plus the book metadata."""
    units, meta = [], {}
    for n, (meta, href, html) in enumerate(spine_documents(epub)):
        parser = Paragraphs()
        parser.feed(html)
        parser.flush()
        if parser.out:
            units.append((f"u{n:03d}", href, parser.out))
    return units, meta


TXT_HELP = """\
# Edit freely. Lines starting with "# " are comments. Paragraphs are separated by blank lines;
# a paragraph may wrap over several lines. Each "=== uNNN ===" line starts a section: keep these
# lines as they are. Delete a whole section (marker and text) to drop it from the book.
"""
UNIT_LINE = re.compile(r"^===\s*(u\d{3})\s*===")


def txt_units(path: Path):
    """Parse a book.txt written by totext (and edited by the user) back into units."""
    units, meta, block, seen = [], {}, [], set()

    def end_paragraph():
        text = re.sub(r"\s+", " ", " ".join(block)).strip()
        if re.search(r"\w", text):
            units[-1][2].append(text)
        block.clear()

    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = UNIT_LINE.match(line)
        if m:
            if units:
                end_paragraph()
            if m.group(1) in seen:
                sys.exit(f"{path}:{no}: section {m.group(1)} appears twice")
            seen.add(m.group(1))
            href = line.partition("#")[2].strip() if "#" in line[m.end():] else None
            units.append((m.group(1), href, []))
        elif line.startswith("# ") or line.strip() == "#":
            key, _, value = line[1:].partition(":")
            if not units and key.strip().lower() in ("title", "author") and value.strip():
                meta["title" if key.strip().lower() == "title" else "creator"] = value.strip()
        elif not line.strip():
            if units:
                end_paragraph()
        elif not units:
            sys.exit(f"{path}:{no}: text before the first '=== uNNN ===' section line")
        else:
            block.append(line)
    if units:
        end_paragraph()
    return [u for u in units if u[2]], meta


def cmd_totext(args):
    dest = Path(args.book_dir) / "book.txt"
    if dest.exists() and not args.force:
        print(f"{dest} already exists (it may hold your edits). Use --force to overwrite it.")
        return 1
    units, meta = epub_units(Path(args.epub))
    out = [f"# Title: {meta.get('title') or ''}", f"# Author: {meta.get('creator') or ''}", TXT_HELP]
    for uid, href, paras in units:
        out.append(f"=== {uid} ===  # {href}\n")
        out += [p + "\n" for p in paras]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out), encoding="utf-8")
    print(f"{len(units)} units, {sum(len(u[2]) for u in units)} paragraphs, "
          f"{sum(len(p.split()) for u in units for p in u[2])} words -> {dest}")
    print(f"Title: {meta.get('title')!r}  Author: {meta.get('creator')!r}")
    return 0


def cmd_extract(args):
    book_dir = Path(args.book_dir)
    out = book_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.epub)
    from_txt = src.suffix.lower() == ".txt"
    units, meta = txt_units(src) if from_txt else epub_units(src)
    for stale in out.glob("u*.txt"):
        stale.unlink()
    index = []
    for uid, href, paras in units:
        lines = [f"{uid}p{i:04d} | {p}" for i, p in enumerate(paras)]
        (out / f"{uid}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        index.append({"unit": uid, "href": href, "paragraphs": len(paras),
                      "words": sum(len(p.split()) for p in paras),
                      "starts_with": paras[0][:80]})
    (out / "index.json").write_text(json.dumps(
        {"epub": None if from_txt else str(src.resolve()), **({"source": str(src.resolve())} if from_txt else {}),
         "title": meta.get("title"), "author": meta.get("creator"), "units": index},
        indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(index)} units, {sum(u['paragraphs'] for u in index)} paragraphs, "
          f"{sum(u['words'] for u in index)} words -> {out}")
    print(f"Title: {meta.get('title')!r}  Author: {meta.get('creator')!r}")
    for u in index:
        print(f"  {u['unit']}  {u['paragraphs']:5d} paras {u['words']:7d} words  {u['starts_with'][:60]}")


# ---------------------------------------------------------------- shared

def load_units(book_dir: Path):
    paras = {}
    for f in sorted((book_dir / "extract").glob("u*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            pid, _, text = line.partition(" | ")
            paras[pid] = text
    return paras


def chapter_paragraphs(chapter, paras):
    """Ordered paragraph ids for one chapter of book.json."""
    ids = []
    for unit in chapter["source"]:
        ids += [p for p in paras if p.startswith(unit + "p")]
    if chapter.get("from"):
        ids = ids[ids.index(chapter["from"]):]
    if chapter.get("to"):
        ids = ids[:ids.index(chapter["to"]) + 1]
    return ids


QUOTES = "\"“”„«»‘’'‚‹›`"


def norm(text):
    text = unicodedata.normalize("NFKC", text).replace("*", "")
    return re.sub(r"[\s" + re.escape(QUOTES) + "]+", "", text)


def clean_spoken(text):
    text = re.sub(r"\s+", " ", text.replace("*", "")).strip()
    text = text.strip("\"“”„«»").strip()
    if text.startswith("‘") and text.endswith("’"):
        text = text[1:-1].strip()
    return text


def voice_file(name):
    for ext in (".mp3", ".wav", ".flac", ".m4a"):
        if (VOICES / f"{name}{ext}").is_file():
            return VOICES / f"{name}{ext}"
    return None


def load_book(book_dir):
    return json.loads((book_dir / "book.json").read_text(encoding="utf-8"))


def load_segments(book_dir, chapter_id):
    f = book_dir / "segments" / f"{chapter_id}.json"
    if not f.is_file():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    return data["segments"] if isinstance(data, dict) else data


def pov_mode(book, chapter):
    return chapter.get("pov_mode") or book.get("pov_mode") or "third_person"


def pov_header_voice(book, chapter, pid, order):
    """Deep POV: the POV character announces their own section. Otherwise the Narrator does."""
    return narrated_by(chapter, pid, order) if pov_mode(book, chapter) == "deep_pov" else "Narrator"


def narrated_by(chapter, pid, order):
    who = chapter.get("narrated_by", "Narrator")
    for start, name in chapter.get("narrated_by_changes", []):
        if order[pid] >= order.get(start, 10**9):
            who = name
    return who


# ---------------------------------------------------------------- verify

TAG_VERBS = (r"said|says|asked|asks|replied|answered|added|murmured|whispered|shouted|yelled|"
             r"cried|snapped|growled|muttered|called|told|demanded|hissed|breathed|laughed|sighed|"
             r"continued|admitted|insisted|repeated|pleaded|begged|teased|warned|agreed|explained")
THOUGHT_TAG_RE = re.compile(r"[,.?!…—-]\s*(?:he|she|they|[A-Z][a-z]+)\s+(?:thought|wondered|"
                            r"told (?:himself|herself|themself|themselves)|reminded (?:himself|herself)|"
                            r"prayed|mused|decided)\b")
TAG_RE = re.compile(r"\b(?:he|she|they|I|we|[A-Z][a-z]+)\s+(?:" + TAG_VERBS + r")\b")


def cmd_verify(args):
    book_dir = Path(args.book_dir)
    book, paras = load_book(book_dir), load_units(book_dir)
    cast = book.get("cast", {})
    aliases = {k: [k] + v for k, v in book.get("aliases", {}).items()}
    reviewed = {}
    if (book_dir / "reviewed.json").is_file():
        reviewed = json.loads((book_dir / "reviewed.json").read_text(encoding="utf-8"))
    errors, warnings = [], []
    speaker_count = Counter()

    def err(cid, pid, msg):
        errors.append(f"ERROR  [{cid}] {pid}: {msg}")

    def warn(code, cid, pid, msg, text=""):
        if f"{code}:{pid}" not in reviewed:
            snippet = f"\n         \"{text[:140]}\"" if text else ""
            warnings.append(f"WARN   [{cid}] {code}:{pid}  {msg}{snippet}")

    # cast sanity
    for speaker in ["Narrator"] + [s for s in cast]:
        if speaker not in cast:
            errors.append(f"ERROR  cast: '{speaker}' missing from book.json cast")
        elif not voice_file(cast[speaker]):
            errors.append(f"ERROR  cast: voice '{cast[speaker]}' for '{speaker}' not found in voices/")

    claimed = Counter()
    for ch in book["chapters"]:
        cid = ch["id"]
        if args.chapter and cid != args.chapter:
            continue
        for unit in ch["source"]:
            if not any(p.startswith(unit + "p") for p in paras):
                err(cid, unit, "source unit not found in extract/")
        if pov_mode(book, ch) not in POV_MODES:
            err(cid, "pov_mode", f"pov_mode must be one of {POV_MODES}")
        pids = chapter_paragraphs(ch, paras)
        claimed.update(pids)
        order = {p: i for i, p in enumerate(pids)}
        for name in [ch.get("narrated_by", "Narrator")] + [n for _, n in ch.get("narrated_by_changes", [])]:
            if name not in cast:
                err(cid, "narrated_by", f"'{name}' is not in cast")
        segs = load_segments(book_dir, cid)
        if segs is None:
            err(cid, "-", f"segments/{cid}.json missing")
            continue
        by_para = defaultdict(list)
        last_order = -1
        for n, seg in enumerate(segs):
            if not (isinstance(seg, list) and len(seg) == 3):
                err(cid, f"#{n}", f"segment must be [paragraph_id, speaker, text], got {seg!r}"[:200])
                continue
            pid, speaker, text = seg
            if pid not in order:
                err(cid, pid, "paragraph id is not part of this chapter")
                continue
            if order[pid] < last_order:
                err(cid, pid, "segments are out of paragraph order")
            last_order = order[pid]
            if speaker not in cast and speaker not in SPECIAL:
                err(cid, pid, f"speaker '{speaker}' not in cast (add it, or use an existing name)")
            by_para[pid].append((speaker, text))
            if speaker not in SPECIAL:
                speaker_count[speaker] += 1
            if speaker == POV_HEADER:
                names = {n: c for c in cast for n in aliases.get(c, [c])}
                named = [c for n, c in names.items() if re.search(r"\b" + re.escape(n) + r"\b", text, re.I)]
                if not named:
                    warn("POV_HEADER_NAME", cid, pid, "POV header names nobody in cast/aliases", text)
                elif pov_mode(book, ch) == "deep_pov" and narrated_by(ch, pid, order) not in named:
                    err(cid, pid, f"POV header names {named} but narration here is voiced by "
                                  f"'{narrated_by(ch, pid, order)}' - put the narrated_by change on this header paragraph")

        prev_dialogue_only = None
        for pid in pids:
            got = by_para.get(pid)
            if not got:
                err(cid, pid, "paragraph not covered by any segment")
                prev_dialogue_only = None
                continue
            want, have = norm(paras[pid]), norm("".join(t for _, t in got))
            if want != have:
                i = next((k for k, (a, b) in enumerate(zip(want, have)) if a != b), min(len(want), len(have)))
                err(cid, pid, f"text does not match source at char {i}: "
                              f"source …{want[max(0, i-25):i+25]}… vs yours …{have[max(0, i-25):i+25]}…")
            speakers = [s for s, _ in got if s not in SPECIAL]
            for speaker, text in got:
                if speaker == NARRATION and re.search(r"[“”\"]", text):
                    warn("QUOTE_IN_NARRATION", cid, pid, "narration contains a quote mark - missed dialogue?", text)
                if speaker == NARRATION and narrated_by(ch, pid, order) == "Narrator":
                    # a whole italic sentence or 3+ italic words reads as a thought, not emphasis
                    for span in re.findall(r"\*([^*]+)\*", text):
                        if len(span.split()) >= 3 or re.match(r"^[A-Z].*[.?!…]$", span.strip()):
                            warn("THOUGHT_IN_NARRATION", cid, pid, "italics inside the narrator's prose - "
                                 "a character's inner thought? (rule 7)", text)
                            break
                    # without italics: a third-person narrator never says "I", and a thought tag
                    # left together with the words before it means the thought itself was not split out
                    plain = re.sub(r"[“\"][^”\"]*[”\"]", "", text)
                    if re.search(r"\b(I|I'm|I’m|I'll|I’ll|I'd|I’d|I've|I’ve|me|my|mine|myself)\b", plain):
                        warn("FIRST_PERSON_IN_NARRATION", cid, pid, "third-person narrator says I/me/my - "
                             "a character's thought left in narration? (rule 7)", text)
                    elif THOUGHT_TAG_RE.search(plain) and len(THOUGHT_TAG_RE.split(plain)[0].split()) >= 2:
                        warn("THOUGHT_TAG_IN_NARRATION", cid, pid, "thought tag still attached to the words "
                             "before it - split the thought out for the thinker? (rule 7)", text)
                if speaker not in SPECIAL:
                    inner = clean_spoken(text)
                    if re.search(r"[“”\"]", inner):
                        warn("QUOTE_IN_DIALOGUE", cid, pid, f"{speaker}'s line still contains a quote mark - split it", text)
                    if TAG_RE.search(inner):
                        warn("TAG_IN_DIALOGUE", cid, pid, f"{speaker}'s line contains a dialogue tag - is the tag narration?", text)
                    for alias in aliases.get(speaker, [speaker]):
                        if re.search(r"(^|[,.!?…]\s*)" + re.escape(alias) + r"\s*[,.!?…]", inner):
                            warn("SELF_ADDRESS", cid, pid, f"{speaker} addresses '{alias}' by name - wrong speaker?", text)
            if len(set(speakers)) > 1:
                warn("MULTI_SPEAKER_PARA", cid, pid, f"one paragraph has several speakers {sorted(set(speakers))}")
            dialogue_only = bool(speakers) and all(s not in SPECIAL for s, _ in got)
            if dialogue_only and prev_dialogue_only and prev_dialogue_only == speakers[0] and len(set(speakers)) == 1:
                warn("SAME_SPEAKER_RUN", cid, pid, f"untagged dialogue paragraph follows another by the same speaker ({speakers[0]}) - "
                     "new paragraph usually means new speaker", got[0][1])
            prev_dialogue_only = speakers[-1] if dialogue_only else None

    if not args.chapter:
        left_out = Counter()
        for pid in paras:
            if claimed[pid] == 0:
                left_out[pid.split("p")[0]] += 1
            elif claimed[pid] > 1:
                errors.append(f"ERROR  {pid} belongs to {claimed[pid]} chapters")
        for unit, n in left_out.items():
            if f"NOT_IN_ANY_CHAPTER:{unit}" not in reviewed:
                warnings.append(f"WARN   NOT_IN_ANY_CHAPTER:{unit}  {n} paragraphs not voiced "
                                "(front/back matter? add to reviewed.json if intended)")
        for speaker, n in speaker_count.items():
            if n == 1:
                warn("SINGLE_LINE_SPEAKER", "book", speaker, f"'{speaker}' speaks only once - typo or misattribution?")
        unused = [s for s in cast if s != "Narrator" and speaker_count[s] == 0
                  and not any(s == ch.get("narrated_by") for ch in book["chapters"])]
        if unused:
            warnings.append(f"WARN   UNUSED_CAST {unused}")

    warnings = [w for w in warnings if w]
    for line in errors + warnings:
        print(line)
    print(f"\n{len(errors)} errors, {len(warnings)} unreviewed warnings")
    if speaker_count:
        print("Lines per speaker: " + ", ".join(f"{s} {n}" for s, n in speaker_count.most_common()))
    return 1 if errors else 2 if warnings else 0


# ---------------------------------------------------------------- build

def cmd_build(args):
    book_dir = Path(args.book_dir)
    book, paras = load_book(book_dir), load_units(book_dir)
    cast, lang = book["cast"], book.get("language", "en")

    def seg(speaker, text, kind):
        return {"speaker": speaker, "voice_ref": str(voice_file(cast[speaker])), "text": text,
                "language": lang, "kind": kind}

    chapters = []
    for n, ch in enumerate(book["chapters"]):
        pids = chapter_paragraphs(ch, paras)
        order = {p: i for i, p in enumerate(pids)}
        out = []
        if n == 0 and book.get("announcement"):
            out.append(seg("Narrator", book["announcement"], "announce"))
        if ch.get("announce"):
            out.append(seg(ch.get("announce_by", "Narrator"), ch["announce"], "announce"))
        for pid, speaker, text in load_segments(book_dir, ch["id"]):
            if speaker == SKIP:
                continue
            if speaker == POV_HEADER:
                text = clean_spoken(text)
                if text.isupper():  # TTS can spell ALL-CAPS names out letter by letter
                    text = text.title()
                if re.search(r"\w", text):
                    out.append(seg(pov_header_voice(book, ch, pid, order), text, "pov_header"))
                continue
            text = clean_spoken(text)
            if not re.search(r"\w", text):
                continue
            who = narrated_by(ch, pid, order) if speaker == NARRATION else speaker
            out.append(seg(who, text, "narration" if speaker == NARRATION else "dialogue"))
        chapters.append({"title": ch.get("title", ch["id"]), "segments": out})
    plan = {"book": book.get("title"), "author": book.get("author"),
            "cast": {k: str(voice_file(v)) for k, v in cast.items()},
            "chapters": chapters}
    dest = book_dir / "plan.json"
    dest.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(chapters)} chapters, {sum(len(c['segments']) for c in chapters)} segments -> {dest}")


# ---------------------------------------------------------------- stats

def cmd_stats(args):
    path = Path(args.target)
    plan_path = path / "plan.json" if path.is_dir() else path
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    chapters = plan.get("chapters") or [{"segments": plan.get("segments", [])}]
    words = lambda t: len(re.findall(r"[\w'’-]+", t))
    narr, dlg, voice_total, lines = Counter(), Counter(), Counter(), Counter()
    voice_of, guessed = {}, False
    for ch in chapters:
        for sg in ch.get("segments", []):
            sp, n = sg.get("speaker", "?"), words(sg.get("text", ""))
            kind = sg.get("kind")
            if kind is None:  # plans not made by `build`: only the Narrator narrates
                guessed = True
                kind = "narration" if sp == "Narrator" else "dialogue"
            voice_total[sp] += n
            voice_of[sp] = Path(sg.get("voice_ref") or sg.get("voice") or "?").stem
            if kind == "dialogue":
                dlg[sp] += n
                lines[sp] += 1
            elif kind == "narration":
                narr[sp] += n
    total = sum(voice_total.values()) or 1
    d_total, n_total = sum(dlg.values()) or 1, sum(narr.values())
    pct = lambda a, b: f"{100 * a / b:.1f}%"
    out = [f"## Speaker distribution: {plan.get('book') or plan_path.parent.name}", "",
           f"{total:,} spoken words. Narration {pct(n_total, total)}, dialogue & thoughts "
           f"{pct(sum(dlg.values()), total)}, announcements {pct(total - n_total - sum(dlg.values()), total)}.", ""]
    if guessed:
        out += ["*(plan has no `kind` tags, so narration voiced by a POV character is counted as dialogue)*", ""]
    out += ["### Dialogue & thoughts", "", "| Character | Voice | Lines | Words | % of dialogue | % of book |",
            "|---|---|---:|---:|---:|---:|"]
    for sp, n in dlg.most_common():
        out.append(f"| {sp} | {voice_of[sp]} | {lines[sp]:,} | {n:,} | {pct(n, d_total)} | {pct(n, total)} |")
    out += ["", "### Narration (who voices the prose)", "", "| Voice of | Words | % of narration | % of book |",
            "|---|---:|---:|---:|"]
    for sp, n in narr.most_common():
        out.append(f"| {sp} | {n:,} | {pct(n, n_total or 1)} | {pct(n, total)} |")
    out += ["", "### Total airtime per character (narration + dialogue)", "", "| Character | Voice | % of book |",
            "|---|---|---:|"]
    for sp, n in voice_total.most_common():
        out.append(f"| {sp} | {voice_of[sp]} | {pct(n, total)} |")
    print("\n".join(out))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("totext", help="EPUB -> books/<slug>/book.txt for the user to edit")
    p.add_argument("epub"); p.add_argument("book_dir")
    p.add_argument("--force", action="store_true", help="overwrite an existing book.txt")
    p = sub.add_parser("extract", help="book.txt (or .epub) -> numbered paragraphs")
    p.add_argument("epub", metavar="book.txt|book.epub"); p.add_argument("book_dir")
    p = sub.add_parser("verify"); p.add_argument("book_dir"); p.add_argument("--chapter")
    p = sub.add_parser("stats"); p.add_argument("target", help="books/<slug> or any plan.json")
    p = sub.add_parser("build"); p.add_argument("book_dir")
    p.add_argument("--allow-warnings", action="store_true", help="build even with unreviewed warnings")
    args = ap.parse_args()
    if args.cmd == "totext":
        return cmd_totext(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "build":
        status = cmd_verify(argparse.Namespace(book_dir=args.book_dir, chapter=None))
        if status == 1 or (status == 2 and not args.allow_warnings):
            print("\nbuild refused: fix the errors and resolve every warning "
                  "(fix it, or add it to reviewed.json with a reason)")
            return 1
        cmd_build(args)
        return 0
    cmd_extract(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
