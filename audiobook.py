#!/usr/bin/env python3
"""Command-line XTTS audiobook renderer.

This is the only application entry point. It performs local XTTS-v2
synthesis and never substitutes tones or placeholder audio for speech.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from voice_paths import voice_clip


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audiobook", description="Render full-cast audiobook speech with local XTTS-v2.")
    sub = p.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample-plan", help="Extract a short sample plan from an EPUB")
    sample.add_argument("epub", type=Path)
    sample.add_argument("-o", "--output", type=Path, default=Path("sample_plan.json"))
    for name in ("render", "render-book"):
        render = sub.add_parser(name, help="Render one WAV/MP3 file per chapter")
        render.add_argument("plan", type=Path)
        render.add_argument("output_dir", type=Path)
        render.add_argument("--format", choices=("wav", "mp3"), default="wav")
        render.add_argument("--sample-rate", type=int, default=24000)
        render.add_argument("--channels", type=int, choices=(1, 2), default=1)
    scene = sub.add_parser("scene", help="Render the legacy scene plan format")
    scene.add_argument("plan", type=Path)
    scene.add_argument("output", type=Path)
    validate = sub.add_parser("validate", help="Check a plan and all referenced voice files")
    validate.add_argument("plan", type=Path)
    return p


def validate(plan_file: Path) -> None:
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    segments = plan.get("segments", [])
    for chapter in plan.get("chapters", []):
        segments.extend(chapter.get("segments", []))
    if not segments:
        raise ValueError("Plan contains no segments")
    missing = []
    for segment in segments:
        ref = segment.get("voice_ref")
        if ref:
            path = Path(ref)
            if not path.is_absolute():
                path = ROOT / path
            if not path.is_file() and path.name.endswith("_ref.wav"):
                path = path.with_name(path.name[:-8].replace("_", " ") + ".mp3")
        else:
            voice = segment.get("voice")
            path = voice_clip(voice) if voice else None
            path = path or Path("<missing voice>")
        if not path.is_file():
            missing.append(str(path))
    if missing:
        raise FileNotFoundError("Missing voice references:\n" + "\n".join(missing))
    print(f"OK: {len(segments)} segments, all voice references present")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sample-plan":
            from generic_full_cast_audiobook.make_sample_plan import make_plan
            args.output.write_text(json.dumps(make_plan(args.epub), indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Wrote {args.output}")
        elif args.command == "validate":
            validate(args.plan)
        elif args.command in {"render", "render-book"}:
            from generic_full_cast_audiobook.render_book import render
            validate(args.plan)
            render(args.plan, args.output_dir, args.format, args.sample_rate, args.channels)
        elif args.command == "scene":
            from render_scene import render_scene
            validate(args.plan)
            render_scene(args.plan, args.output)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
