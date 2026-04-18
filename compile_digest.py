#!/usr/bin/env python3
"""Compile weekly podcast digest from extracts/ and post to Slack #podcast-digest.

Pipeline:
  1. Load all extract JSON from the last N days
  2. Aggregate raw signals (tool mentions, build/teach/trend items)
  3. Flash synthesis pass — dedupe, rank, cluster
  4. Format Slack main post + thread replies
  5. Post via Slack API (main → get ts → threaded per-episode replies)
  6. Save a copy to digests/YYYY-WW.md

Options:
  --since DAYS    : lookback window (default 7)
  --dry-run       : compile + print, skip Slack post
  --no-slack      : write markdown only, skip post
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from json import JSONDecoder
from pathlib import Path

ROOT = Path(__file__).parent
EXTRACTS_DIR = ROOT / "extracts"
DIGESTS_DIR = ROOT / "digests"
LOG_PATH = ROOT / "digest.log"

API_KEY = os.environ.get("GOOGLE_API_KEY")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_ID", "")
MODEL = "gemini-2.5-flash"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


# --- Load + aggregate ------------------------------------------------------


def load_extracts(days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for p in EXTRACTS_DIR.glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except Exception as e:
            log(f"[skip-bad-json] {p.name}: {e}")
            continue
        pub = rec.get("published", "")
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= cutoff:
            out.append(rec)
    out.sort(key=lambda r: r.get("published", ""), reverse=True)
    return out


def aggregate(extracts: list[dict]) -> dict:
    """Build raw aggregation — tool convergence + flat signal lists with provenance."""
    tool_to_feeds: dict[str, set[str]] = defaultdict(set)
    tool_to_context: dict[str, list[dict]] = defaultdict(list)
    build_signals: list[dict] = []
    teach_signals: list[dict] = []
    industry_trends: list[dict] = []
    themes_counter: Counter = Counter()

    for rec in extracts:
        e = rec.get("extract", {})
        fname = rec.get("feed_name", "?")
        for t in e.get("tools_and_companies_mentioned", []) or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            tool_to_feeds[key].add(fname)
            tool_to_context[key].append({"feed": fname, "context": t.get("context", "")[:200]})
        for s in e.get("build_signals", []) or []:
            build_signals.append({"text": s, "feed": fname, "title": rec.get("title", "")})
        for s in e.get("teach_signals", []) or []:
            teach_signals.append({"text": s, "feed": fname, "title": rec.get("title", "")})
        for s in e.get("industry_trends", []) or []:
            industry_trends.append({"text": s, "feed": fname, "title": rec.get("title", "")})
        for t in e.get("key_themes", []) or []:
            themes_counter[t.lower()] += 1

    convergence = sorted(
        [
            {"tool": tool_to_context[k][0]["context"].split(" ")[0] if False else k, "name_raw": k, "shows": sorted(tool_to_feeds[k])}
            for k in tool_to_feeds
            if len(tool_to_feeds[k]) >= 2
        ],
        key=lambda x: (-len(x["shows"]), x["name_raw"]),
    )

    return {
        "tool_to_feeds": {k: sorted(v) for k, v in tool_to_feeds.items()},
        "tool_to_context": tool_to_context,
        "build_signals": build_signals,
        "teach_signals": teach_signals,
        "industry_trends": industry_trends,
        "convergence_raw": convergence,
    }


# --- Flash synthesis -------------------------------------------------------


SYNTHESIS_PROMPT = """You are producing a weekly podcast digest — a synthesis of structured signals extracted from this week's episodes.

I'm giving you raw aggregated signals from {n_eps} podcast episodes across {n_shows} shows this week. Your job: dedupe, cluster, and rank into a cleaner digest.

Return JSON only, this exact schema:

{{
  "tldr": "1-2 sentences capturing the week's big story across these podcasts",
  "convergence": [
    {{"topic": "short phrase", "shows": ["show A", "show B"], "what": "1 sentence what they're saying about it"}}
  ],
  "build_signals": [
    {{"signal": "concrete buildable thing", "source": "show name"}}
  ],
  "teach_signals": [
    {{"signal": "concrete teachable pattern", "source": "show name"}}
  ],
  "industry_trends": [
    {{"trend": "1 sentence", "source": "show name or 'multiple'"}}
  ]
}}

Rules:
- 3-6 items per list. Prune generic or duplicate entries. Pick the most concrete and most actionable.
- convergence = topics/tools mentioned in ≥2 shows. If nothing qualifies, return empty list.
- Tag source with the actual show name from the data, not a made-up one.
- Skip anything that reads as filler or platitudes.
- Keep each item tight — readers skim this.

Raw signals JSON:
{raw}
"""


def call_flash(prompt: str, timeout: int = 180) -> tuple[bool, str, dict]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return True, text, usage
    except urllib.error.HTTPError as e:
        return False, e.read().decode("utf-8", "replace"), {}
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}


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


def synthesize(aggregated: dict, n_eps: int, n_shows: int) -> dict:
    # Pare down to just what Flash needs — don't blow context on full contexts
    raw = {
        "tool_convergence_hints": [
            {"tool": k, "shows": v}
            for k, v in aggregated["tool_to_feeds"].items()
            if len(v) >= 2
        ],
        "build_signals": [
            {"signal": s["text"], "source": s["feed"]} for s in aggregated["build_signals"]
        ],
        "teach_signals": [
            {"signal": s["text"], "source": s["feed"]} for s in aggregated["teach_signals"]
        ],
        "industry_trends": [
            {"trend": s["text"], "source": s["feed"]} for s in aggregated["industry_trends"]
        ],
    }
    prompt = SYNTHESIS_PROMPT.format(
        n_eps=n_eps, n_shows=n_shows, raw=json.dumps(raw, ensure_ascii=False)[:30000]
    )
    ok, text, usage = call_flash(prompt)
    if not ok:
        log(f"[synthesis-fail] {text[:300]}")
        return {}
    obj = parse_json_loose(text)
    if obj is None:
        log("[synthesis-parse-fail]")
        return {}
    log(f"[synthesis] tokens in/out={usage.get('promptTokenCount','?')}/{usage.get('candidatesTokenCount','?')}")
    return obj


# --- Formatting ------------------------------------------------------------


def week_label(extracts: list[dict]) -> str:
    dates = [e.get("published", "")[:10] for e in extracts if e.get("published")]
    if not dates:
        return datetime.now().strftime("%Y-%m-%d")
    return f"{min(dates)} → {max(dates)}"


def format_main_post(synthesis: dict, n_eps: int, n_shows: int, label: str, n_missing: int) -> str:
    lines = []
    lines.append(f":radio: *Weekly Podcast Digest — {label}*")
    lines.append(f"{n_eps} episodes from {n_shows} shows" + (f" · {n_missing} missing transcripts" if n_missing else ""))

    if synthesis.get("tldr"):
        lines.append("")
        lines.append(f"*TL;DR*  {synthesis['tldr']}")

    if synthesis.get("convergence"):
        lines.append("")
        lines.append(":fire: *Convergence (≥2 shows):*")
        for c in synthesis["convergence"][:6]:
            shows = ", ".join(c.get("shows", []))
            lines.append(f"• *{c.get('topic','?')}* ({shows}) — {c.get('what','')}")

    if synthesis.get("build_signals"):
        lines.append("")
        lines.append(":building_construction: *Build signals:*")
        for b in synthesis["build_signals"][:6]:
            lines.append(f"• {b.get('signal','?')}  _({b.get('source','')})_")

    if synthesis.get("teach_signals"):
        lines.append("")
        lines.append(":mortar_board: *Teach signals:*")
        for t in synthesis["teach_signals"][:6]:
            lines.append(f"• {t.get('signal','?')}  _({t.get('source','')})_")

    if synthesis.get("industry_trends"):
        lines.append("")
        lines.append(":chart_with_upwards_trend: *Industry trends:*")
        for t in synthesis["industry_trends"][:6]:
            lines.append(f"• {t.get('trend','?')}  _({t.get('source','')})_")

    lines.append("")
    lines.append(":newspaper: Per-episode details in thread ↓")
    return "\n".join(lines)


def format_episode_reply(rec: dict) -> str:
    e = rec.get("extract", {})
    summary = e.get("summary", "").strip()
    quote = ""
    if e.get("notable_quotes"):
        q = e["notable_quotes"][0]
        quote = f'\n> "{q.get("quote","").strip()}" — {q.get("speaker","")}'
    lines = [
        f"*{rec.get('feed_name','?')}* — {rec.get('title','')}",
        summary,
    ]
    if quote:
        lines.append(quote)
    lines.append(f":link: {rec.get('url','')}")
    return "\n".join(lines)


# --- Slack post ------------------------------------------------------------


def slack_post(text: str, channel: str = SLACK_CHANNEL, thread_ts: str | None = None) -> str | None:
    if not SLACK_TOKEN:
        log("[slack] SLACK_BOT_TOKEN missing — set env var or use --no-slack")
        return None
    if not channel:
        log("[slack] SLACK_CHANNEL_ID missing — set env var or use --no-slack")
        return None
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SLACK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not data.get("ok"):
            log(f"[slack-error] {data.get('error')}")
            return None
        return data.get("ts")
    except Exception as e:
        log(f"[slack-exception] {type(e).__name__}: {e}")
        return None


# --- Main ------------------------------------------------------------------


def run(since_days: int, dry_run: bool, no_slack: bool) -> None:
    DIGESTS_DIR.mkdir(exist_ok=True)
    extracts = load_extracts(since_days)
    if not extracts:
        log(f"[empty] no extracts in last {since_days} days")
        return

    feeds = {e.get("feed_name", "?") for e in extracts}
    n_eps = len(extracts)
    n_shows = len(feeds)
    label = week_label(extracts)
    log(f"[load] {n_eps} episodes across {n_shows} shows — {label}")

    aggregated = aggregate(extracts)
    log(
        f"[aggregate] {len(aggregated['build_signals'])} build, "
        f"{len(aggregated['teach_signals'])} teach, "
        f"{len(aggregated['industry_trends'])} trends, "
        f"{len(aggregated['convergence_raw'])} convergence candidates"
    )

    synthesis = {}
    if API_KEY:
        synthesis = synthesize(aggregated, n_eps, n_shows)
    else:
        log("[warn] GOOGLE_API_KEY missing — skipping synthesis pass")

    # Count missing transcripts for this window if the log exists
    missing_count = 0
    miss_log = ROOT / "missing_transcripts.log"
    if miss_log.exists():
        # rough: count lines, not window-filtered — good enough for now
        missing_count = sum(1 for _ in miss_log.open())

    main_post = format_main_post(synthesis, n_eps, n_shows, label, missing_count)

    # Save markdown copy
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    digest_path = DIGESTS_DIR / f"{year}-W{week:02d}.md"
    md_lines = [f"# Podcast Digest — {label}", "", main_post, "", "## Episodes", ""]
    for rec in extracts:
        md_lines.append("---")
        md_lines.append(format_episode_reply(rec))
        md_lines.append("")
    digest_path.write_text("\n".join(md_lines))
    log(f"[saved] {digest_path}")

    if dry_run:
        print("\n========== MAIN POST ==========\n")
        print(main_post)
        print("\n========== EPISODES ==========\n")
        for rec in extracts:
            print("---")
            print(format_episode_reply(rec))
            print()
        return

    if no_slack:
        log("[skip-slack] --no-slack requested")
        return

    ts = slack_post(main_post)
    if not ts:
        log("[fail] main Slack post failed; skipping thread replies")
        return
    log(f"[slack-main] posted ts={ts}")

    posted = 0
    for rec in extracts:
        reply_ts = slack_post(format_episode_reply(rec), thread_ts=ts)
        if reply_ts:
            posted += 1
        time.sleep(0.3)  # gentle pacing
    log(f"[done] posted main + {posted}/{n_eps} thread replies")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=7, help="Days of lookback (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="Print to stdout, skip markdown + Slack")
    ap.add_argument("--no-slack", action="store_true", help="Save markdown but skip Slack post")
    args = ap.parse_args()
    run(since_days=args.since, dry_run=args.dry_run, no_slack=args.no_slack)
