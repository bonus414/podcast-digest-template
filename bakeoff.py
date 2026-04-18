#!/usr/bin/env python3
"""Bakeoff: gemini-2.5-flash vs gemma-4-31b-it on podcast transcript extraction.

Calls Google Generative Language API directly (urllib). Writes side-by-side
results to bakeoff-results.md.
"""
import json, os, sys, time, urllib.request, urllib.error
from pathlib import Path
from json import JSONDecoder

ROOT = Path(__file__).parent
TRANSCRIPTS = ROOT / "transcripts"
OUT = ROOT / "bakeoff-results.md"

API_KEY = os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    print("GOOGLE_API_KEY missing", file=sys.stderr); sys.exit(1)

MODELS = ["gemini-2.5-flash", "gemma-4-31b-it"]

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

def call_model(model: str, transcript: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}"
    body = {
        "contents": [{
            "parts": [{"text": PROMPT + "\n\n---\nTRANSCRIPT:\n" + transcript}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json" if "gemini" in model else "text/plain",
        }
    }
    # Gemma doesn't support responseMimeType=application/json; strip for gemma
    if "gemma" in model:
        body["generationConfig"].pop("responseMimeType", None)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
        elapsed = time.time() - t0
        data = json.loads(raw)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return {"ok": True, "text": text, "elapsed": elapsed, "usage": usage}
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": e.read().decode("utf-8", "replace"), "elapsed": time.time()-t0, "usage": {}}
    except Exception as e:
        return {"ok": False, "text": f"{type(e).__name__}: {e}", "elapsed": time.time()-t0, "usage": {}}

def parse_json_loose(text: str):
    """Use raw_decode per feedback_llm-json-raw-decode.md — LLMs append stray chars."""
    # Strip code fences if present
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
    try:
        obj, _ = JSONDecoder().raw_decode(stripped)
        return obj, None
    except Exception as e:
        return None, str(e)

def run():
    transcripts = sorted(TRANSCRIPTS.glob("*.txt"))
    results = []
    for tpath in transcripts:
        text = tpath.read_text()
        size_kb = len(text) / 1024
        print(f"[{tpath.name}] {size_kb:.1f}KB", flush=True)
        for model in MODELS:
            print(f"  -> {model} ...", end=" ", flush=True)
            r = call_model(model, text)
            obj, parse_err = (None, "not-ok")
            if r["ok"]:
                obj, parse_err = parse_json_loose(r["text"])
            print(f"{r['elapsed']:.1f}s {'OK' if obj else 'PARSE-FAIL' if r['ok'] else 'HTTP-FAIL'}", flush=True)
            results.append({
                "transcript": tpath.name,
                "size_kb": size_kb,
                "model": model,
                "elapsed": r["elapsed"],
                "usage": r["usage"],
                "ok": r["ok"],
                "parsed": obj is not None,
                "parse_error": parse_err,
                "raw_text": r["text"],
                "parsed_obj": obj,
            })
    write_report(results)

def write_report(results):
    lines = ["# Podcast Digest Bakeoff — gemini-2.5-flash vs gemma-4-31b-it\n"]
    lines.append(f"_Run: {time.strftime('%Y-%m-%d %H:%M %Z')}_\n")
    lines.append("## Summary table\n")
    lines.append("| Transcript | Size | Model | Latency | HTTP | JSON valid | Input tokens | Output tokens |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        u = r["usage"] or {}
        lines.append(
            f"| {r['transcript']} | {r['size_kb']:.1f}KB | `{r['model']}` | {r['elapsed']:.1f}s | "
            f"{'OK' if r['ok'] else 'FAIL'} | {'YES' if r['parsed'] else 'NO'} | "
            f"{u.get('promptTokenCount', '?')} | {u.get('candidatesTokenCount', '?')} |"
        )
    lines.append("")

    # Full outputs
    for r in results:
        lines.append(f"\n---\n\n## {r['transcript']} — `{r['model']}`\n")
        lines.append(f"- Latency: {r['elapsed']:.2f}s")
        lines.append(f"- HTTP OK: {r['ok']}")
        lines.append(f"- JSON parseable: {r['parsed']}")
        if not r['parsed'] and r['parse_error']:
            lines.append(f"- Parse error: `{r['parse_error']}`")
        u = r["usage"] or {}
        if u:
            lines.append(f"- Tokens in/out: {u.get('promptTokenCount','?')} / {u.get('candidatesTokenCount','?')}")
        lines.append("")
        if r["parsed"]:
            lines.append("```json")
            lines.append(json.dumps(r["parsed_obj"], indent=2, ensure_ascii=False))
            lines.append("```")
        else:
            lines.append("**Raw response (truncated to 3000 chars):**\n")
            lines.append("```")
            lines.append(r["raw_text"][:3000])
            lines.append("```")
    OUT.write_text("\n".join(lines))
    print(f"\nWrote {OUT}")

if __name__ == "__main__":
    run()
