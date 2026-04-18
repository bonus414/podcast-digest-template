#!/usr/bin/env python3
"""Call Gemini 2.5 Flash on each transcript in ready_for_extract.json.

Saves one JSON file per episode in extracts/. Retry once on 503. Persistent
failures go to failures.log and stay out of extracts/ (weekly digest skips them).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from json import JSONDecoder
from pathlib import Path

ROOT = Path(__file__).parent
READY_PATH = ROOT / "ready_for_extract.json"
EXTRACTS_DIR = ROOT / "extracts"
LOG_PATH = ROOT / "extract.log"
FAILURES_LOG = ROOT / "failures.log"

API_KEY = os.environ.get("GOOGLE_API_KEY")
MODEL = "gemini-2.5-flash"

PROMPT = """You are analyzing a podcast transcript to surface structured signal for a weekly reading digest.

Return JSON only, no preamble, with this exact schema:

{
  "episode_title": "string",
  "summary": "3-sentence summary of the episode",
  "key_themes": ["3 to 5 themes, short phrases"],
  "notable_quotes": [
    {"speaker": "Name or role", "quote": "verbatim or near-verbatim line"}
  ],
  "tools_and_companies_mentioned": [
    {"name": "X", "context": "what was said about it"}
  ],
  "build_signals": [
    "specific things being built that a reader could replicate or adapt"
  ],
  "teach_signals": [
    "patterns, workflows, or skills that are teachable as 'how I use AI' content"
  ],
  "industry_trends": [
    "broader trends or shifts surfaced in the episode"
  ]
}

Rules:
- Quotes should be the speaker's actual words, not paraphrased summaries.
- build_signals and teach_signals are distinct: build = what to make; teach = what to teach others.
- Keep each list item concrete and specific — no generic filler like "AI is changing work".
- If a field has nothing real to put, return an empty list. Do not invent.
"""


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def log_failure(ep: dict, reason: str, raw: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with FAILURES_LOG.open("a") as f:
        f.write(f"[{ts}] {ep['feed_id']} {ep['video_id']} {reason}\n")
        if raw:
            f.write("  " + raw[:500].replace("\n", " ") + "\n")


def call_flash(transcript: str, timeout: int = 180) -> tuple[bool, str, dict, float]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
    body = {
        "contents": [{"parts": [{"text": PROMPT + "\n\n---\nTRANSCRIPT:\n" + transcript}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        elapsed = time.time() - t0
        data = json.loads(raw)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return True, text, usage, elapsed
    except urllib.error.HTTPError as e:
        return False, e.read().decode("utf-8", "replace"), {}, time.time() - t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}, time.time() - t0


def parse_json_loose(text: str):
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        obj, _ = JSONDecoder().raw_decode(s)
        return obj
    except Exception:
        return None


def extract_filename(ep: dict) -> Path:
    date = ep["published"][:10] if ep.get("published") else "unknown"
    return EXTRACTS_DIR / f"{date}_{ep['feed_id']}_{ep['video_id']}.json"


def run(limit: int | None = None) -> None:
    if not API_KEY:
        log("[error] GOOGLE_API_KEY missing in env — export it or source your .env first")
        sys.exit(1)
    EXTRACTS_DIR.mkdir(exist_ok=True)

    if not READY_PATH.exists():
        log(f"[error] {READY_PATH} not found — run get_transcripts.py first")
        sys.exit(1)

    episodes = json.loads(READY_PATH.read_text())
    if limit:
        episodes = episodes[:limit]
    log(f"[start] {len(episodes)} episodes to extract")

    ok = fail = cached = 0
    for i, ep in enumerate(episodes, 1):
        out = extract_filename(ep)
        if out.exists() and out.stat().st_size > 100:
            log(f"[cached] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']}")
            cached += 1
            continue

        transcript = Path(ep["transcript_path"]).read_text()
        kb = len(transcript) / 1024

        # Skip likely-shorts / clips — full episode transcripts are almost
        # always ≥3KB (≈3 min of speech). Saves API calls and keeps digest clean.
        if kb < 3.0:
            log(f"[short] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} — {kb:.1f}KB, skipping as likely-short")
            log_failure(ep, "likely-short", f"transcript_kb={kb:.1f}")
            continue

        attempts = 0
        while attempts < 2:
            attempts += 1
            success, text, usage, elapsed = call_flash(transcript)
            if success:
                break
            if "503" in text or "UNAVAILABLE" in text:
                log(f"  [503] {ep['video_id']} attempt {attempts}, sleeping 20s")
                time.sleep(20)
                continue
            break

        if not success:
            log(f"[http-fail] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} {elapsed:.1f}s")
            log_failure(ep, "http-fail", text)
            fail += 1
            continue

        parsed = parse_json_loose(text)
        if parsed is None:
            log(f"[json-fail] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} {elapsed:.1f}s")
            log_failure(ep, "json-parse-fail", text)
            fail += 1
            continue

        record = {
            "feed_id": ep["feed_id"],
            "feed_name": ep["feed_name"],
            "video_id": ep["video_id"],
            "title": ep["title"],
            "published": ep["published"],
            "url": ep["url"],
            "transcript_kb": round(kb, 1),
            "extract": parsed,
            "usage": usage,
            "model": MODEL,
            "extracted_at": datetime.now().isoformat(),
        }
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        log(f"[ok] ({i}/{len(episodes)}) {ep['feed_id']} {ep['video_id']} {elapsed:.1f}s in/out={usage.get('promptTokenCount','?')}/{usage.get('candidatesTokenCount','?')}")
        ok += 1

    log(f"[done] {ok} ok, {cached} cached, {fail} fail → {EXTRACTS_DIR}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(limit=args.limit)
