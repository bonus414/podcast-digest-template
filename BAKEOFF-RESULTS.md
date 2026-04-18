# Model Bakeoff: `gemini-2.5-flash` vs `gemma-4-31b-it`

Before committing to a model, I ran a head-to-head on the actual task — structured JSON extraction (summary, themes, verbatim quotes, tools, build/teach signals, trends) from real podcast transcripts. Findings below; the full test script is at [`bakeoff.py`](./bakeoff.py) if you want to reproduce against your own content.

## Verdict

Use `gemini-2.5-flash`. It was faster, cleaner, more reliable, and orders of magnitude cheaper than the alternative — not by a small margin, and not under only one scenario.

## Results

Tested on two podcast transcripts, ~15–17KB each.

| Transcript | Size | Model | Latency | JSON valid | Outcome |
|---|---|---|---|---|---|
| Transcript A | 16.9KB | `gemini-2.5-flash` | **12.4s** | **YES** | Clean structured output, verbatim quotes, specific signals |
| Transcript A | 16.9KB | `gemma-4-31b-it` | 115.2s | NO | Thinking/scratchpad leaked before JSON — output opened by restating the schema and drafting content as bullets |
| Transcript B | 14.5KB | `gemini-2.5-flash` | 0.9s → 19.5s on retry | **YES** | First attempt hit transient 503; one retry succeeded |
| Transcript B | 14.5KB | `gemma-4-31b-it` | — | — | 3 consecutive 503 UNAVAILABLE responses; couldn't complete |

## What this tells you

**Speed.** Flash ~12-20 seconds, Gemma 31B ~115 seconds where it worked. About 9x slower. Real cost isn't just latency — it's how many episodes you can process in a launchd window before timeouts start cascading.

**JSON compliance.** Flash honors `responseMimeType: application/json` at the API level. Gemma doesn't support that parameter on Google AI Studio and emits freeform text, so your parser has to dig the JSON out of a preamble that often contains the model's scratchpad. This is a known pattern with thinking-mode models — the chain-of-thought writes into the response body instead of being cleanly separated.

**Reliability.** The Gemma 31B deployment on Google AI Studio was capacity-constrained during my testing window. Three consecutive 503s on one transcript. Not a model-quality problem — a serving problem — but if you're building a pipeline that has to complete overnight without a human babysitting it, it's still a blocker.

**Cost.** Flash ran about $0.001–0.002 per episode on the 4.5K input / 1K-2K output tokens typical of this task. Weekly pipeline cost across ~25 episodes per week: pennies.

## The bigger lesson

Benchmarks measure reasoning, coding, math. Your task — "extract structured JSON with these specific fields from a 17KB conversation transcript" — is not on any benchmark. Labs optimize what they measure. Flash wins here because Google specifically tunes it to be the boring, obedient schema-follower for production use. The glamorous new models are tuned for reasoning quality or agentic tool use, not JSON compliance.

Newer is also not uniformly better. On a parallel long-context test, Gemma 3 27B (dense, older) held at 108% recall on Person extraction while Gemma 4 26B MoE (newer, bigger-sounding) partial-collapsed to 62%. Architecture matters more than generation number.

**Practical rule:** test on your task, not on the benchmark. For production pipelines that need obedience, reach for the boring production-tuned model first, not the one with the biggest number. You can run the included [`bakeoff.py`](./bakeoff.py) against any pair of models on transcripts you care about — 5 minutes of API calls will tell you more than any announcement blog post.

## Running your own bakeoff

```bash
export GOOGLE_API_KEY="your-key"
mkdir -p transcripts
# Drop any two .txt transcripts into transcripts/
python3 bakeoff.py
# Results land in bakeoff-results.md
```

Change the `MODELS` list at the top of `bakeoff.py` to compare other models (any model served by `generativelanguage.googleapis.com` works with this script).
