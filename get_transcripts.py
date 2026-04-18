#!/usr/bin/env python3
"""Fetch YouTube auto-subtitles for each episode in new_episodes.json.

Saves one text file per episode in transcripts/. Emits ready_for_extract.json
listing episodes that got transcripts (downstream extract step reads this).

Skips (logs to missing_transcripts.log) if no transcript available.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

ROOT = Path(__file__).parent
NEW_PATH = ROOT / "new_episodes.json"
READY_PATH = ROOT / "ready_for_extract.json"
TRANSCRIPTS_DIR = ROOT / "transcripts"
MISSING_LOG = ROOT / "missing_transcripts.log"
LOG_PATH = ROOT / "transcripts.log"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def log_missing(ep: dict, reason: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with MISSING_LOG.open("a") as f:
        f.write(f"[{ts}] {ep['feed_id']} {ep['video_id']} {reason} :: {ep.get('title','')[:80]}\n")


def snippet_filename(ep: dict) -> Path:
    date = ep["published"][:10] if ep.get("published") else "unknown"
    return TRANSCRIPTS_DIR / f"{date}_{ep['feed_id']}_{ep['video_id']}.txt"


def fetch_transcript(video_id: str, langs=("en", "en-US")) -> str | None:
    """Return joined transcript text, or None if unavailable."""
    api = YouTubeTranscriptApi()
    try:
        # prefer manual over auto; fall back to any English variant
        fetched = api.fetch(video_id, languages=list(langs))
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None
    except Exception as e:
        # Library sometimes raises other exceptions; treat as missing for v1
        log(f"  [unexpected] {video_id}: {type(e).__name__}: {e}")
        return None
    # fetched is iterable of FetchedTranscriptSnippet with .text, .start, .duration
    lines = [s.text.strip() for s in fetched if s.text and s.text.strip()]
    return "\n".join(lines) if lines else None


def run(limit: int | None = None) -> None:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    if not NEW_PATH.exists():
        log(f"[error] {NEW_PATH} not found — run fetch_episodes.py first")
        sys.exit(1)

    episodes = json.loads(NEW_PATH.read_text())
    if limit:
        episodes = episodes[:limit]
    log(f"[start] {len(episodes)} episodes to transcribe")

    ready: list[dict] = []
    skipped = 0
    for i, ep in enumerate(episodes, 1):
        out = snippet_filename(ep)
        if out.exists() and out.stat().st_size > 100:
            log(f"[cached] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']}")
            ready.append({**ep, "transcript_path": str(out)})
            continue

        text = fetch_transcript(ep["video_id"])
        if not text:
            log_missing(ep, "no-transcript")
            log(f"[missing] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} — {ep['title'][:60]}")
            skipped += 1
            continue

        header = f"Feed: {ep['feed_name']} ({ep['feed_id']})\nTitle: {ep['title']}\nPublished: {ep['published']}\nURL: {ep['url']}\nVideo ID: {ep['video_id']}\n\n---\n\n"
        out.write_text(header + text)
        size_kb = out.stat().st_size / 1024
        log(f"[ok] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} {size_kb:.1f}KB")
        ready.append({**ep, "transcript_path": str(out)})

        # gentle pacing — YouTube can rate-limit
        time.sleep(0.5)

    READY_PATH.write_text(json.dumps(ready, indent=2))
    log(f"[done] {len(ready)} transcripts saved, {skipped} missing → {READY_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only process first N episodes (for testing)")
    args = ap.parse_args()
    run(limit=args.limit)
