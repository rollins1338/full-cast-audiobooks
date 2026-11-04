import argparse, json, re, zipfile
from pathlib import Path
from bs4 import BeautifulSoup

VOICE = "Lee Daniels"

def clean(x):
    return re.sub(r"\s+", " ", x).strip()

def make_plan(epub):
    with zipfile.ZipFile(epub) as z:
        files = [n for n in z.namelist() if n.lower().endswith((".xhtml",".html",".htm"))]
        scored = []
        for n in files:
            raw = z.read(n).decode("utf-8", errors="ignore")
            score = int("chapter" in raw.lower()) + int("userstuff" in raw.lower())
            scored.append((score, n, raw))
        scored.sort(reverse=True)
        _, name, raw = scored[0]

    soup = BeautifulSoup(raw, "lxml")
    paras = [clean(x.get_text(" ", strip=True))
             for x in soup.find_all(["p","div","li"])]
    paras = [x for x in paras if x]

    chosen, chars = [], 0
    for p in paras:
        if chars >= 1800:
            break
        chosen.append(p)
        chars += len(p)

    return {
        "source": str(epub),
        "chapter_file": name,
        "sample": True,
        "segments": [
            {"speaker": "Narrator", "voice": VOICE, "text": p, "language": "en"}
            for p in chosen
        ]
    }

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("epub")
    ap.add_argument("--output", default="sample_plan.json")
    a = ap.parse_args()
    plan = make_plan(Path(a.epub))
    Path(a.output).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Sample plan created:", a.output)
    print("Chapter source:", plan["chapter_file"])
    print("Segments:", len(plan["segments"]))
