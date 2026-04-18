#!/usr/bin/env python3
"""Discover new podcast episodes from YouTube RSS feeds.

Reads feeds.json, fetches each feed's RSS (channel or playlist), parses entries,
applies per-feed filters (keyword gate, publish-day filter), and writes the list
of new episodes to new_episodes.json for downstream consumption.

Modes:
  --dry-run      : show latest N entries per feed without updating state
  --backfill N   : treat last N days as "new" on first run (default: 0 = don't backfill)
  (default)      : fetch only entries with video_id not in seen_ids set; update state
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
FEEDS_PATH = ROOT / "feeds.json"
STATE_PATH = ROOT / "state.json"
OUT_PATH = ROOT / "new_episodes.json"
LOG_PATH = ROOT / "fetch.log"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def feed_url(feed: dict) -> str:
    if feed["source_type"] == "channel":
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={feed['channel_id']}"
    if feed["source_type"] == "playlist":
        return f"https://www.youtube.com/feeds/videos.xml?playlist_id={feed['playlist_id']}"
    raise ValueError(f"unknown source_type: {feed['source_type']}")


def fetch_feed(feed: dict, timeout: int = 30) -> list[dict]:
    """Return list of entries: {video_id, title, published, description, url}."""
    url = feed_url(feed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
        title = entry.findtext("atom:title", default="", namespaces=NS)
        published = entry.findtext("atom:published", default="", namespaces=NS)
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={video_id}"
        desc = ""
        mg = entry.find("media:group", NS)
        if mg is not None:
            desc = mg.findtext("media:description", default="", namespaces=NS)
        entries.append({
            "video_id": video_id,
            "title": title,
            "published": published,
            "url": link,
            "description": desc,
        })
    return entries


WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def passes_publish_day_filter(entry: dict, feed: dict) -> bool:
    allowed = feed.get("publish_day_filter")
    if not allowed:
        return True
    try:
        dt = datetime.fromisoformat(entry["published"].replace("Z", "+00:00"))
    except Exception:
        return True
    return WEEKDAY_ABBR[dt.weekday()] in allowed


def passes_keyword_gate(entry: dict, feed: dict) -> bool:
    gate = feed.get("keyword_gate")
    if not gate:
        return True
    haystack = (entry["title"] + " " + entry["description"]).lower()
    return any(kw.lower() in haystack for kw in gate)


def within_backfill_window(entry: dict, backfill_days: int) -> bool:
    if backfill_days <= 0:
        return False
    try:
        dt = datetime.fromisoformat(entry["published"].replace("Z", "+00:00"))
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=backfill_days)
    return dt >= cutoff


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"feeds": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def run(dry_run: bool = False, backfill_days: int = 0, sample_n: int = 5) -> None:
    cfg = json.loads(FEEDS_PATH.read_text())
    state = load_state()
    all_new: list[dict] = []

    for feed in cfg["feeds"]:
        if not feed.get("enabled", True):
            log(f"[skip] {feed['id']}: disabled")
            continue

        fid = feed["id"]
        feed_state = state["feeds"].setdefault(fid, {"seen_ids": []})
        seen = set(feed_state.get("seen_ids", []))

        try:
            entries = fetch_feed(feed)
        except Exception as e:
            log(f"[error] {fid}: fetch failed — {type(e).__name__}: {e}")
            continue

        if not entries:
            log(f"[empty] {fid}: RSS returned 0 entries")
            continue

        if dry_run:
            # Show latest N entries, indicate which WOULD be new
            log(f"[dry-run] {fid}: {len(entries)} entries, showing {min(sample_n, len(entries))}")
            for e in entries[:sample_n]:
                passes_day = passes_publish_day_filter(e, feed)
                passes_kw = passes_keyword_gate(e, feed)
                would_pick = e["video_id"] not in seen and passes_day and passes_kw
                flags = []
                if not passes_day: flags.append("day-filter")
                if not passes_kw: flags.append("keyword-gate")
                if e["video_id"] in seen: flags.append("already-seen")
                tag = "PICK" if would_pick else f"skip({','.join(flags)})"
                log(f"    [{tag}] {e['published'][:10]} {e['video_id']} {e['title'][:80]}")
            continue

        # Production mode
        picked = []
        for e in entries:
            if e["video_id"] in seen:
                continue
            if not passes_publish_day_filter(e, feed):
                continue
            if not passes_keyword_gate(e, feed):
                continue
            # On first run (seen empty), only pick episodes within backfill window
            if not seen and not within_backfill_window(e, backfill_days):
                continue
            picked.append({**e, "feed_id": fid, "feed_name": feed["name"]})

        log(f"[feed] {fid}: {len(picked)} new out of {len(entries)}")
        all_new.extend(picked)
        # Update state with every entry we observed, not just picked — prevents
        # re-processing filtered-out entries forever.
        feed_state["seen_ids"] = sorted(set(feed_state.get("seen_ids", [])) | {e["video_id"] for e in entries})
        # Cap retention to last 500 IDs per feed
        feed_state["seen_ids"] = feed_state["seen_ids"][-500:]

    if not dry_run:
        save_state(state)
        OUT_PATH.write_text(json.dumps(all_new, indent=2))
        log(f"[done] {len(all_new)} new episodes → {OUT_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show what would be fetched without updating state")
    ap.add_argument("--backfill", type=int, default=0, help="On first run, treat last N days as new")
    ap.add_argument("--sample", type=int, default=5, help="Entries per feed to show in dry-run")
    args = ap.parse_args()
    run(dry_run=args.dry_run, backfill_days=args.backfill, sample_n=args.sample)
