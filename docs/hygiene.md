# Hygiene: normalizer, coalescing and the delivery watchdog

Three things every sender gets, whatever it speaks: HTTP, MCP, the CLI and the [compat routes](compat.md). All are
optional and off or neutral by default, so a 1.0 setup behaves exactly as before.

## Text normalizer

Senders put things in a notification that sound terrible out loud: emoji, markdown, a 200-character URL, a stack
trace. The normalizer fixes the **text** before it is stored. It never touches audio and never calls a network.

| Setting | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_NORMALIZE` | `off` | `off` or `basic`. Off means the text is stored as sent (trimmed). |
| `EARSHOT_RELAY_MAX_WORDS` | unset | 1 to 1000. Applies only when the normalizer runs. |
| `normalize` (request) | unset | `true` or `false` overrides the setting for one message. CLI: `--normalize` / `--no-normalize`. |

What `basic` does:

- Removes emoji, pictographs, flags, skin tones and the zero-width joiners inside emoji sequences (a joiner between
  letters, as in Malayalam or Devanagari, stays). Plain symbols that read fine aloud
  (the copyright and trade-mark signs, arrows, math) stay.
- Removes markdown: bold, italic, strikethrough, headings, block quotes, rules, list markers, table pipes, inline
  code (the code text stays) and links (the link text stays). A fenced code block (a line that starts with three or
  more backticks or tildes, up to a closing line) becomes the word "code". Inline triple backticks mid-line are inline
  code, and a fence that never closes is left alone: the rest of the message is never swallowed. Python-style
  names such as `__init__` and `__post_init__` keep their underscores.
  Headings, list items and table rows each end in a period, so the voice pauses between them.
- Replaces every bare URL with the word "link" (a sentence-ending period stays).
- Collapses all whitespace, including line breaks, to single spaces.
- With `EARSHOT_RELAY_MAX_WORDS`, cuts at the last full sentence inside the limit, else at the limit with a trailing
  period (`truncated`).

A text with nothing left (only emoji, say) is refused with `422 empty_text`. Hostile input cannot make it slow: every
pattern is line-bound or linear and input over 64 KiB is refused before any work.

The send response says what happened: `normalized` (the text differs from what was sent) and `truncated`. Both are kept
in the message index and on the receipt. The text itself is still never kept.

```sh
curl -fsS -X POST http://127.0.0.1:8080/v1/messages -H "Authorization: Bearer $EARSHOT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "**Deploy done** :tada: see https://ci.example.com/run/8812/very/long/path", "normalize": true}'
# spoken: "Deploy done :tada: see link"   response: "normalized": true
```

Emoji shortcodes such as `:tada:` are plain text, not emoji characters, so they are not removed.

## Coalesce and cooldown by tag

A flapping monitor, a chatty build or an agent that narrates every step can turn the phone into a machine gun. Give
the messages a `tag` and say what to do with repeats.

| Field | Meaning |
| --- | --- |
| `tag` | 1 to 64 characters, `A-Za-z0-9._:-`, starting with a letter or digit. Scopes to the channel: the same tag on two channels is two tags. |
| `coalesce_window_s` | 0 to 3600. Hold the write. Every message with the tag inside the window replaces the text; one object, carrying the latest text, is written when the window closes. |
| `cooldown_s` | 0 to 86400. A message with the tag within this many seconds of the last written (or held) one is dropped. |

`coalesce_window_s` or `cooldown_s` without a `tag` is `422 tag_required`. `0` or `null` turns either off.

**Coalescing answers `202` with `"status": "coalescing"`**, the message `id` that will exist when the window closes, and
`write_at`. `key` is `null` until then; the key is `<channel>/<hex of id>.<ext>`. Every message that merges into the
window gets the same `id` back. The message is **not stored yet**, so a `202` here means "held", not "in the bucket",
and certainly not "played". Poll `GET /v1/messages/{id}`: it reads `coalescing` until the write, then the usual receipt.

**Cooldown answers `200` with `"status": "suppressed"`**, `cooldown_until`, and the `id` and `key` of the message that
started the cooldown. Nothing is written and the send does not count toward the rate limit. (A held message counts as
"the last one": a cooldown also drops repeats while a coalescing window is open.)

**Coalescing is pre-write, by design.** The relay holds `PutObject` only ([permissions](permissions.md)); it cannot
overwrite or delete an object that already exists. So it cannot "update the message the phone has not played yet": it
holds the text before writing and writes once. The consequences are all deliberate:

- The window is fixed from the first message. A steady stream does not postpone the write forever; the latest text at
  the end of the window is what gets written.
- Held text sits in the relay's SQLite file until it is written, then is deleted (and the write-ahead log is
  checkpointed so it does not linger). This is the one exception to "the relay keeps no message text on disk".
- Held messages survive a restart. At startup the relay writes every message whose window has already closed; windows
  still open keep waiting out the remainder.
- The window delays delivery by up to `coalesce_window_s`. Do not coalesce anything urgent (an `alarm` message you
  tagged would wait out its window too).
- One send is counted against the rate limit per window, when it opens. Updates merged into an open window are free.
- A write that fails is retried after 10, 30 and 60 seconds, then given up on (log line `coalesce_dropped`). The
  sender was told `202`, so the loss is made visible: the text is deleted, but the id's receipt reads
  `write_failed` (nothing is stored; send it again), `GET /v1/status` counts it in `hygiene.coalescing_failed`, and a
  configured watchdog sends one notice (`earshot.watchdog.write_failed`). The record is pruned with the other index rows
  after `EARSHOT_RELAY_RETENTION_DAYS`. Nothing is retried forever.
- The write re-checks the policy that admitted the message. If the relay restarted with its channel removed from
  `EARSHOT_RELAY_CHANNELS`, with compat alarm switched off (for a compat sender's top priority promoted to `alarm`), or
  without server speech, nothing is written: the receipt reads `write_failed` at once (log line `coalesce_refused`),
  with the same status count and watchdog notice.
- The window is fixed: it ends `coalesce_window_s` after the first message, whether or not the relay has written it
  yet. A send that arrives after the end writes the closed window first (with its own text) and then opens a new
  window; it never replaces the text of a window that has closed. If that closed window's write is waiting for a
  retry, a new `coalesce_window_s` send under the tag is `503 coalesce_busy` (with `Retry-After`) until it is written.
- `Idempotency-Key` works across merges: a retried request replays the same `202`.
- Coalescing is for text. The native audio upload has no hygiene fields; compat attachments are unsupported
  (ignored and named when separate message text is present, rejected otherwise).

ntfy's sequence id and Gotify's extras are **not** mapped: they promise to replace a delivered notification, which
this relay cannot do, and mapping them would claim more than it does. Producers on those routes use the
`X-Earshot-Tag`, `X-Earshot-Coalesce-Window-S`, `X-Earshot-Cooldown-S` and `X-Earshot-Normalize` headers (or the query
parameters `earshot_tag`, `earshot_coalesce_window_s`, `earshot_cooldown_s`, `earshot_normalize`). A success then carries
`X-Earshot-Status: coalescing|suppressed`; the protocol's own body still says it accepted the request.

```sh
# every "disk full" within 60 s is one spoken message, the latest
curl -fsS -X POST http://127.0.0.1:8080/v1/messages -H "Authorization: Bearer $EARSHOT_API_TOKEN" \
  -H "Content-Type: application/json" -d '{"text": "Disk 91 percent full", "tag": "disk", "coalesce_window_s": 60}'
earshot-relay say --tag disk --cooldown-s 600 "Disk is still full"
```

## Delivery watchdog

If the phone is off or the app is not listening, a send still returns `202`, and you find out later or never. The
watchdog tells you, **outside the phone path**.

```sh
EARSHOT_RELAY_WATCHDOG_S=120                       # off unless set; 120 is a sensible start
EARSHOT_RELAY_WATCHDOG_URL=https://hooks.example.com/earshot   # or an ntfy topic URL; also ..._URL_FILE
EARSHOT_RELAY_WATCHDOG_COOLDOWN_S=1800             # minimum seconds between notices (default 1800)
EARSHOT_RELAY_WATCHDOG_FORMAT=auto                 # auto, json or text
```

It needs [receipts](receipts.md) set up (`EARSHOT_RELAY_BEACON_TOKEN`). Without a beacon token the relay starts, but
disables the watchdog; status reports it disabled. With a watchdog URL and no threshold, the threshold defaults to 120 s.

**What it watches is sends, not phones.** A stored key with no matching `detected` beacon after `WATCHDOG_S` seconds
opens an incident. A beacon for another key, or a `started`/`failed` beacon, cannot clear that missing detection.
The app sends no heartbeat, so **an idle dead phone raises nothing**.

- **A held message that was never written** (see coalescing above) is its own incident: one notice, then closed. A
  playback beacon from another message does not clear it, because that message will not play.
- **Per message, never a global cursor.** Only a beacon for a message's own key reports on it. A receipt for a later
  message does not cover an earlier one that got none, and a beacon for a key the relay never stored covers nothing.
- **One notice per incident.** Until the notice goes out (it may wait for the cooldown or a webhook retry), the
  incident follows the oldest overdue message that still has no report, and closes silently if they all report late.
  Once announced, it covers every message that falls overdue while it is open (more undetected sends do not notify
  again), and it closes when a beacon received after the notice reports on its message or on any later one: the phone
  is reporting again. A message with no report is announced at most once.
- **Cooldown delays, it does not mute.** A new incident inside `WATCHDOG_COOLDOWN_S` of the last notice waits it out
  and is announced then, if it is still open. One that clears in the meantime sends nothing.
- **Body.** `json` (the default) is `{"event": "earshot.watchdog.no_playback_report", "text", "message_id", "channel",
  "incident_since", "after_s", "relay_version"}`, which any webhook receiver (n8n, Home Assistant, Slack-style
  relays) can take. `text` is a plain-text POST with `Title`, `Tags` and `Priority` headers, which is a publish to
  ntfy. `auto` picks `text` for `ntfy.sh` and for a host with an `ntfy` label (`ntfy.example.com`); a self-hosted ntfy
  under another name needs `EARSHOT_RELAY_WATCHDOG_FORMAT=text`.
- **Failure.** A failing webhook is retried at 30 s and 90 s, then given up on for that incident.
- **State survives restarts**, so a restart does not repeat a notice; a state older than ten minutes (the relay was
  down) starts over. Messages stored before the watchdog was switched on are not incidents.
- **The URL is a secret.** An ntfy topic or a token lives in it. It is never logged and never shown; `status` shows
  only the host.

`GET /v1/status` has a `watchdog` object: `enabled`, `after_s`, `cooldown_s`, `format`, `target_host`, `state`
(`idle`, `incident` or `notified`), `incident_since`, `incident_message`, `last_notice_at`, `last_notice_ok`,
`last_error`, `notices_sent`, `incident_kind` (`silent` or `write_failed`) and a `note` repeating what it does not claim. `earshot-relay doctor` also checks it.
