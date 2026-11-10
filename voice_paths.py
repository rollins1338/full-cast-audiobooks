#!/usr/bin/env python3
"""Locate a voice reference clip by name.

Clips live in `voices/` and in its one-level buckets (`new/` for recent
downloads, `old/` for the older batch), so every lookup goes through here
instead of assuming a flat folder. The download folders (`raw/`, `alt/`)
are skipped: they hold source material, not references.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
SKIP = {"alt", "raw"}
EXTENSIONS = ("", ".mp3", ".wav", ".flac", ".m4a", ".ogg")


def voice_dirs(voices=None):
    """`voices/` itself, then each bucket folder in name order (new/ before old/)."""
    voices = Path(voices or VOICES)
    dirs = [voices]
    if voices.is_dir():
        dirs += [d for d in sorted(voices.iterdir()) if d.is_dir() and d.name not in SKIP]
    return dirs


def voice_clip(name, voices=None):
    """Path to a clip for `name` (bare, or `new/<name>`, or with an extension), else None."""
    voices = Path(voices or VOICES)
    name = str(name or "").strip()
    if not name:
        return None
    if Path(name).suffix.lower() in EXTENSIONS[1:]:  # already names a file
        p = voices / name
        return p if p.is_file() else None
    for folder in voice_dirs(voices):
        for ext in EXTENSIONS:
            p = folder / f"{name}{ext}"
            if p.is_file():
                return p
    return None
