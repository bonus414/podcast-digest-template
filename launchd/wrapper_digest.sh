#!/bin/bash
# Weekly digest compiler + Slack post. Invoked by launchd Sunday at 2pm.
set -eo pipefail

DIR="$HOME/podcast-digest-template"
LOG="$DIR/wrapper.log"

cd "$DIR"

set -a
[ -f "$DIR/.env" ] && source "$DIR/.env"
set +a

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') digest wrapper start ====="
  /usr/bin/python3 compile_digest.py --since 7
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') digest wrapper done ====="
} >> "$LOG" 2>&1

touch "$DIR/.last_digest_run"
