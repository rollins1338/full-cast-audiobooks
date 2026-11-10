#!/usr/bin/env python3
"""Download Audible narrator samples and build XTTS voice references.

For each narrator in voices_mainstream.json:
  - query Audible catalog filtered by narrator
  - prefer solo-narrated (or first-narrated) unabridged English books
  - download up to 3 five-minute samples to voices/raw/<Name>/
  - produce one 60s normalized primary ref: voices/new/<Name>.mp3
  - produce 60s alternates: voices/alt/<Name>/NN - <title>.mp3
  - write voices/voice_catalog.json
"""
import json, re, subprocess, sys, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
RAW = VOICES / "raw"
ALT = VOICES / "alt"
PRIMARY = VOICES / "new"
NAMES = json.loads((ROOT / "voices_mainstream.json").read_text(encoding="utf-8"))
CACHE = Path("/tmp/audible_search.json")
MAX_SAMPLES = 3
WORKERS = 4


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return "".join(c for c in s if c.isalnum())


def pick(name: str, products: list[dict]) -> list[dict]:
    scored = []
    for p in products:
        narr = [x if isinstance(x, str) else x.get("name", "") for x in p.get("narrators", [])]
        if not any(norm(x) == norm(name) for x in narr):
            continue
        if not (p.get("sample_url") or p.get("sample")):
            continue
        if len(narr) == 1:
            score = 0
        elif norm(narr[0]) == norm(name):
            score = 1
        else:
            score = 2
        blob = ((p.get("title") or "") + " " + (p.get("subtitle") or "")).lower()
        if "dramatized" in blob or "full cast" in blob or "full-cast" in blob:
            score += 2
        scored.append((score, p))
    scored.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _, p in scored:
        key = norm((p.get("title") or "") + (p.get("subtitle") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= MAX_SAMPLES:
            break
    return out


def search(name: str) -> list[dict]:
    q = urllib.parse.urlencode({
        "narrator": name, "num_results": "12",
        "response_groups": "product_desc,media,product_attrs,sample",
        "language": "english",
    })
    url = "https://api.audible.com/1.0/catalog/products?" + q
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(req, timeout=30)).get("products", [])
        except Exception as exc:
            last = exc
            time.sleep(3 + attempt * 2)
    print(f"  search failed for {name}: {last}", file=sys.stderr)
    return []


def download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 30_000:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        p = subprocess.run(["curl", "-L", "--fail", "--max-time", "180", "-sS",
                            "-o", str(dest), url], capture_output=True)
        if p.returncode == 0 and dest.exists() and dest.stat().st_size > 30_000:
            return True
        time.sleep(2 + attempt * 2)
    return False


def duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def silence_intervals(path: Path, dur: float) -> list[tuple[float, float]]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                        "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        out.append((s, e))
    return out


def best_start(path: Path, dur: float, length: float = 60.0) -> float:
    if dur <= length + 1:
        return 0.0
    sil = silence_intervals(path, dur)
    starts = {0.0, 2.0, 5.0, 8.0, 12.0}
    starts.update(round(e + 0.15, 2) for _, e in sil if e < dur - length)
    best, best_sil = 5.0, 10**9
    for s in sorted(starts):
        if s + length > dur - 2:
            continue
        overlap = sum(max(0.0, min(e, s + length) - max(a, s)) for a, e in sil)
        # prefer starting just after a pause, and avoid the first 2 seconds
        penalty = 0 if 2 <= s <= 30 else 1.5
        score = overlap + penalty
        if score < best_sil:
            best_sil, best = score, s
    return best


def make_clip(src: Path, dest: Path, start: float) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", "60", "-i", str(src),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac", "2", "-ar", "48000", "-b:a", "128k", str(dest),
    ], capture_output=True)
    return p.returncode == 0 and dest.exists() and dest.stat().st_size > 30_000


def main() -> int:
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    RAW.mkdir(parents=True, exist_ok=True)
    ALT.mkdir(parents=True, exist_ok=True)
    PRIMARY.mkdir(parents=True, exist_ok=True)

    jobs = []
    for name in NAMES:
        products = cache.get(name) or search(name)
        if not products:
            products = search(name)
        chosen = pick(name, products)
        if not chosen:
            print(f"!! no sample for {name}")
            continue
        for i, p in enumerate(chosen, 1):
            dest = RAW / name / f"{i:02d}_{p.get('asin','x')}.mp3"
            jobs.append((name, i, p, dest))
        print(f"{name:28} {len(chosen)} samples")

    print(f"\nDownloading {len(jobs)} samples with {WORKERS} workers...")
    ok = 0
    def run(job):
        name, i, p, dest = job
        return job, download(p.get("sample_url") or p["sample"], dest)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(run, j) for j in jobs]
        for n, f in enumerate(as_completed(futs), 1):
            (name, i, p, dest), good = f.result()
            ok += bool(good)
            if n % 10 == 0 or not good:
                print(f"  {n}/{len(jobs)} downloaded ({ok} ok){'' if good else ' FAILED '+name}", flush=True)

    catalog = {}
    title_map = {dest: (p.get("title") or "") for _, _, p, dest in jobs}
    print(f"\nBuilding clips ({ok} raw files) with {WORKERS} workers...", flush=True)

    def process_name(name: str):
        srcs = sorted((RAW / name).glob("*.mp3")) if (RAW / name).exists() else []
        good = []
        for src in srcs:
            dur = duration(src)
            if dur < 20:
                continue
            good.append((src, dur, best_start(src, dur)))
        if not good:
            return name, None, "no usable audio"
        # primary from the first (best-ranked) sample
        src, dur, start = good[0]
        primary = PRIMARY / f"{name}.mp3"
        if not make_clip(src, primary, start):
            return name, None, "clip failed"
        entries = []
        (ALT / name).mkdir(parents=True, exist_ok=True)
        for i, (s, d, st) in enumerate(good, 1):
            title = title_map.get(s, "")
            alt = ALT / name / f"{i:02d} - {re.sub(r'[^A-Za-z0-9 ]+','',title)[:60].strip() or 'sample'}.mp3"
            make_clip(s, alt, st)
            entries.append({"file": str(alt.relative_to(VOICES)), "title": title,
                            "source_duration": round(d, 1), "start": round(st, 1)})
        return name, {"primary": str(primary.relative_to(VOICES)), "samples": entries}, None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process_name, n) for n in NAMES]
        for n, f in enumerate(as_completed(futs), 1):
            name, entry, err = f.result()
            if entry:
                catalog[name] = entry
                print(f"  {n}/{len(NAMES)} {name:28} -> {entry['primary']} ({len(entry['samples'])} clips)", flush=True)
            else:
                print(f"  {n}/{len(NAMES)} !! {name}: {err}", flush=True)

    (VOICES / "voice_catalog.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nDone: {len(catalog)} narrators, primary refs in {PRIMARY}, alternates in {ALT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
