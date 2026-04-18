#!/bin/bash
# Resolve YouTube @handles to channel IDs (UCxxxxxxxxxxxxxxxxxxxx).
#
# Usage:
#   ./scripts/resolve_channel_ids.sh @LatentSpacePod @AIDailyBrief
#   ./scripts/resolve_channel_ids.sh LatentSpacePod AIDailyBrief   # @ is optional
#
# Pipe handles into feeds.json manually after running.
# No API key required — scrapes the public channel page.

set -eo pipefail

if [ $# -eq 0 ]; then
  echo "Usage: $0 <@handle> [<@handle> ...]"
  exit 1
fi

for raw in "$@"; do
  h="${raw#@}"   # strip leading @ if present
  cid=$(curl -sSL -A "Mozilla/5.0" "https://www.youtube.com/@${h}" 2>/dev/null \
        | grep -oE '"externalId":"UC[A-Za-z0-9_-]{22}"' \
        | head -1 \
        | grep -oE 'UC[A-Za-z0-9_-]{22}')
  if [ -z "$cid" ]; then
    echo "@${h} -> NOT_FOUND"
    continue
  fi
  # Also grab the channel title for verification
  title=$(curl -sSL "https://www.youtube.com/feeds/videos.xml?channel_id=${cid}" 2>/dev/null \
          | grep -oE '<title>[^<]+</title>' | head -1 \
          | sed -e 's/<title>//' -e 's/<\/title>//')
  echo "@${h} -> ${cid} (${title})"
done
