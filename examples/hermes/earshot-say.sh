#!/usr/bin/env bash
# Opt-in final-reply speaker. Reply text becomes a plaintext object in your private bucket.
set -u
# Keep this 0600 file inside the active profile, including multiplexed gateways.
settings="${HERMES_HOME:?}/earshot.env"
[ -r "$settings" ] || exit 0
. "$settings"
[ -n "${EARSHOT_API_URL:-}" ] && [ -n "${EARSHOT_API_TOKEN:-}" ] || exit 0
export EARSHOT_API_URL EARSHOT_API_TOKEN
payload=$(cat)
platform=$(printf '%s' "$payload" | jq -r '.extra.platform // ""')
[ -n "$platform" ] || exit 0
# Cron and subagent remain excluded even if the operator adds them to the allowlist.
case "$platform" in cron|subagent) exit 0 ;; esac
case " ${EARSHOT_HERMES_PLATFORMS:-telegram discord slack signal whatsapp sms email ntfy cli tui} " in
  *" $platform "*) ;;
  *) exit 0 ;;
esac
text=$(printf '%s' "$payload" | jq -r '(.extra.assistant_response // "")[:1500]')
[ -n "$text" ] || exit 0
( printf '%s' "$text" | nohup setsid earshot-relay say --normalize --tag hermes - ) >/dev/null 2>&1 &
exit 0
