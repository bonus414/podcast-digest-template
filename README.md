# podcast-digest-template

A weekly podcast digest pipeline for people who'd rather read than listen.

Pulls YouTube auto-subtitles from a list of podcast channels, extracts structured signal (summary, themes, quotes, tools mentioned, trends) via Gemini 2.5 Flash, and posts a weekly synthesis to Slack. Per-episode details land in threaded replies so the main channel stays skimmable.

Runs on macOS via `launchd` (cron also works). Costs pennies per week at Flash pricing.

## What it does

```
YouTube channels + playlists
        ↓
 Daily fetcher (launchd, 6am)
        ↓  (dedup via state.json + filters)
 youtube-transcript-api → auto-subs
        ↓
 Gemini 2.5 Flash → structured JSON per episode
        ↓
 Per-episode JSON cache (extracts/)
        ↓
     ... Sunday 2pm ...
        ↓
 Weekly digest compiler → synthesis pass via Flash
        ↓
 Slack post — main message + threaded per-episode replies
```

## Setup

### Prerequisites

- Python 3.9+
- A Google AI Studio API key with billing enabled ([docs](https://ai.google.dev/)). Free tier limits (20 req/day) will choke on real volume.
- A Slack bot token with `chat:write` scope and the bot invited to your target channel.
- macOS if you want launchd scheduling (otherwise cron or any scheduler).

### Install

```bash
git clone https://github.com/bonus414/podcast-digest-template.git
cd podcast-digest-template
pip install -r requirements.txt
cp feeds.example.json feeds.json
```

### Configure feeds

Edit `feeds.json`. Each entry is either a YouTube channel or playlist:

```json
{
  "id": "my-feed-id",
  "name": "Display Name",
  "source_type": "channel",
  "channel_id": "UCxxxxxxxxxxxxxxxxxxxx",
  "enabled": true,
  "last_seen_video_id": null
}
```

To resolve a YouTube handle (e.g. `@LatentSpacePod`) to a channel ID, use the included helper:

```bash
./scripts/resolve_channel_ids.sh @LatentSpacePod @AIDailyBrief @NoPriorsPodcast
```

Optional per-feed filters:

- `"keyword_gate": ["ai","llm",...]` — only pick episodes whose title+description contain at least one keyword. Useful for broad feeds where only some episodes are relevant.
- `"publish_day_filter": ["Mon","Tue","Wed","Thu","Fri"]` — only pick episodes published on these weekdays. Useful for daily feeds where you want weekdays only.

### Environment

Set these in your shell env or a `.env` file:

```bash
export GOOGLE_API_KEY="your-google-ai-studio-key"
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_CHANNEL_ID="C01234567"   # destination for the weekly digest
```

### Run manually

```bash
# Dry run — show what would be picked up, no side effects
python3 fetch_episodes.py --dry-run

# First run — backfill last 7 days
python3 fetch_episodes.py --backfill 7

# Pull transcripts + extract
python3 get_transcripts.py
python3 extract_episode.py

# Compile and post weekly digest
python3 compile_digest.py --since 7
# or preview without posting
python3 compile_digest.py --since 7 --no-slack
```

Outputs:
- `transcripts/` — raw auto-subs (gitignored)
- `extracts/` — per-episode JSON (one file per episode)
- `digests/` — weekly markdown copies of what gets posted
- `state.json` — per-feed seen-video-id state

### Schedule

See `launchd/` for example plists and wrapper scripts. Install with:

```bash
cp launchd/com.example.podcast-fetch.plist ~/Library/LaunchAgents/
cp launchd/com.example.podcast-digest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.podcast-fetch.plist
launchctl load ~/Library/LaunchAgents/com.example.podcast-digest.plist
```

Edit the plists first to point to your install path.

## Design choices worth calling out

- **YouTube auto-subs, not Whisper.** Free and fast, but this means audio-only podcasts (lots of the MIT Sloan / Changelog Network kind) are invisible to the pipeline. See "Deferred" below.
- **Short-clip filter.** Transcripts under 3KB are skipped as likely YouTube Shorts. Keeps the digest from being polluted with 60-second teasers.
- **Per-feed dedup via seen-video-ID set.** Simple and robust. State capped at 500 IDs per feed.
- **Two-pass extraction.** Per-episode pass produces raw JSON; a second Flash pass at digest time deduplicates themes across shows and flags cross-show convergence. You could skip the synthesis pass and get a raw dump instead — it's roughly one API call away.
- **`gemini-2.5-flash`, deliberately.** Flash cost ~$0.002/episode and returned clean JSON. Gemma 4 31B was 9x slower with thinking-mode leaking into output and hit capacity 503s. See [BAKEOFF-RESULTS.md](./BAKEOFF-RESULTS.md).

## Deferred / things you'll hit

- **Audio-only feeds.** A `get_transcripts_whisper.py` path would unlock podcasts that live on RSS but not YouTube. Not included because Whisper is its own setup story. PRs welcome.
- **Channel handle drift.** YouTube handles occasionally change. Re-run `resolve_channel_ids.sh` if a feed goes cold.
- **Rate limits.** Flash paid tier is fine; free tier will throttle. Enable billing on your GCP project.
- **Synthesis quality varies with episode volume.** 3-episode weeks feel thin; 15-episode weeks may need a tighter prompt. Tune the `compile_digest.py` prompt to your taste.

## Cost

At ~$0.002/episode (Flash pricing at time of writing) and ~25 episodes/week across 10-ish feeds, weekly cost is under a dollar. The Slack API and YouTube auto-subs are free.

## License

MIT. Take what's useful.
