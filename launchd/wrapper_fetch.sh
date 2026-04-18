#!/bin/bash
# Daily fetch → transcript → extract chain. Invoked by launchd at 6am.
# Edit DIR and the env-file source below for your install path.
set -eo pipefail

DIR="$HOME/podcast-digest-template"
LOG="$DIR/wrapper.log"

cd "$DIR"

# Load env (GOOGLE_API_KEY, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID).
# Edit this path if your .env lives elsewhere.
set -a
[ -f "$DIR/.env" ] && source "$DIR/.env"
set +a

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') fetch wrapper start ====="
  /usr/bin/python3 fetch_episodes.py --backfill 7
  /usr/bin/python3 get_transcripts.py
  /usr/bin/python3 extract_episode.py
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') fetch wrapper done ====="
} >> "$LOG" 2>&1

touch "$DIR/.last_fetch_run"
