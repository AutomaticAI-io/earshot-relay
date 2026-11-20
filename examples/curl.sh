#!/usr/bin/env bash
# Send one message and wait for the phone's receipt. Needs curl and jq.
#   EARSHOT_API_URL=http://127.0.0.1:8080 EARSHOT_API_TOKEN=... ./curl.sh "Build finished"
set -euo pipefail
: "${EARSHOT_API_TOKEN:?set EARSHOT_API_TOKEN to the EARSHOT_RELAY_TOKEN value}"
URL="${EARSHOT_API_URL:-http://127.0.0.1:8080}"
TEXT="${1:-Hello from earshot relay}"

# Text -> the phone reads it out loud (202 Accepted once the object is in the bucket).
payload=$(jq -n --arg text "$TEXT" '{text: $text}')
resp=$(curl -fsS -X POST "$URL/v1/messages" \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$payload")
echo "$resp" | jq .
id=$(echo "$resp" | jq -r .id)

# An audio file works too (ogg, opus, mp3, m4a, aac, wav, flac, webm):
#   curl -fsS -X POST "$URL/v1/messages" -H "Authorization: Bearer $EARSHOT_API_TOKEN" -F file=@ding.mp3

# Receipt: long-poll up to EARSHOT_WAIT seconds (default 30, max 60) for a final status. "finished" means the phone finished playback.
curl -fsS "$URL/v1/messages/$id?wait=${EARSHOT_WAIT:-30}" -H "Authorization: Bearer $EARSHOT_API_TOKEN" | jq '{status, hops, hint}'
