# Changelog

All notable changes. Versions follow semver. A `202` always means stored (or, with `coalesce_window_s`, held), never played;
receipts describe device playback, never that a person heard anything.

## 1.1.1 (2026-11-21)

Reconciles 1.1.0 with the spec-gap corrections that had been left on the `ears25-spec-gaps` branch. Both sets are kept.

### Fixed

- **The watchdog correlates per message.** A beacon only ever accounts for its own message: a receipt for B no longer
  clears or hides an earlier A that got none, and a beacon for a key the relay never stored covers nothing. A message
  never reported on is announced once and no longer blocks later notices (a `covered_until` watermark in the saved
  state; a state saved by 1.1.0 loads unchanged). The notice says how many later messages it also covers.
- **Held writes re-check policy when they flush**, including after a restart: a message held for a channel that was
  since removed from `EARSHOT_RELAY_CHANNELS`, a compat alarm after `EARSHOT_RELAY_COMPAT_ALARM` was switched off, or
  server speech that is no longer configured, is not written. It reads `write_failed` in its receipt, is counted in
  `hygiene.coalescing_failed` and raises one watchdog notice that names the reason.
- **A coalescing window is fixed.** It ends `coalesce_window_s` after its first message. A send under a tag whose
  window has closed but is not yet flushed writes the old text first, then starts a new window; if that write is
  waiting for a retry the send is `503 coalesce_busy` with `Retry-After`. Replacement text can no longer slip into a
  window that has expired.
- **A suppressed request keeps its `Idempotency-Key` outcome** (schema v5, `suppressed_idem`), so a retry after the
  cooldown replays `suppressed` instead of being written, also across a restart.
- The flush loop and the watchdog run on events (a send, a beacon, the next deadline), not on a polling timer.

### Added

- **Directory listings.** `server.json` for the official MCP Registry (`io.github.AutomaticAI-io/earshot-relay`, an OCI
  package plus a remote template), the image label `io.modelcontextprotocol.server.name` the registry checks, a manual
  `publish-mcp` workflow (GitHub OIDC), `glama.json` for claiming the Glama listing, and a Claude Code plugin
  (`plugins/earshot`) with a `.claude-plugin/marketplace.json` so `claude plugin marketplace add AutomaticAI-io/earshot-relay`
  works. See [docs/mcp.md](docs/mcp.md#listings-and-the-claude-code-plugin). Nothing is submitted for you.
- Per-channel text hygiene: `EARSHOT_RELAY_CHANNEL_HYGIENE`, a JSON object `{channel: {normalize, max_words}}`.
- **Gemini TTS adapter** (`EARSHOT_RELAY_TTS_PROVIDER=gemini`, `GEMINI_API_KEY`). Tested against a stub server only; no
  live Gemini call has been made.
- `EARSHOT_RELAY_WATCHDOG_URL` alone now enables the watchdog at 120 s. Without a beacon token it stays off and
  `GET /v1/status` says why (`reason: beacon_token_missing`) instead of refusing to start.

### Changed

- The coalescing field is `coalesce_window_s` everywhere (JSON, MCP, CLI `--coalesce-window-s`, `X-Earshot-Coalesce-Window-S`,
  `earshot_coalesce_window_s`). The 1.1.0 name `coalesce_s` is still accepted on every one of them; sending both with
  different values is `422`.
- Compat routes are text only: a ntfy, Gotify, Pushover or Join request that carries an attachment instead of text is
  `400 empty_text` (1.1.0 could play an audio attachment). Send audio through `POST /v1/messages`.
- Schema v5 adds `pending.compat`, `pending.window_end_at` and the `suppressed_idem` table, migrated in place.

## 1.1.0

Producer compatibility and hygiene for every sender (HTTP, MCP, CLI and the compat routes).

### Added

- **Producer compat** (off by default, `EARSHOT_RELAY_COMPAT`): the relay accepts ntfy, Gotify, Pushover and Join
  request shapes under `/compat/<protocol>` (and at the root of `EARSHOT_RELAY_COMPAT_PORT`), so an existing producer
  switches by changing a URL and a token. A protocol's top priority reaches the loud `alarm` channel only with
  `EARSHOT_RELAY_COMPAT_ALARM=1`. See `docs/compat.md`.
- **Text normalizer**: `EARSHOT_RELAY_NORMALIZE=off|basic` (default `off`), a per-request `normalize` true/false,
  and `EARSHOT_RELAY_MAX_WORDS`. Strips emoji and markdown, says URLs as "link", collapses whitespace, cuts at a
  sentence boundary. Text only, never audio. Responses report `normalized` and `truncated`.
- **Coalesce and cooldown by tag**: `tag` with `coalesce_window_s` holds the write and writes only the latest text once
  (`202`, status `coalescing`, the eventual id); `tag` with `cooldown_s` drops repeats (`200`, status `suppressed`).
  Held messages persist in SQLite and flush at startup. Pre-write by design: the relay holds `PutObject` only and
  cannot replace an object. ntfy sequence ids and Gotify extras are not mapped. Compat routes take the same options as
  `X-Earshot-*` headers or `earshot_*` query parameters. See `docs/hygiene.md`.
- **Delivery watchdog** (needs receipts): `EARSHOT_RELAY_WATCHDOG_S`, `EARSHOT_RELAY_WATCHDOG_URL`,
  `EARSHOT_RELAY_WATCHDOG_COOLDOWN_S`, `EARSHOT_RELAY_WATCHDOG_FORMAT`. One out-of-band notice per incident when a
  stored message gets no playback beacon in time, JSON or ntfy-style text. It watches sends, not idle phones (the
  app sends no heartbeat). State in `GET /v1/status`.
- **ElevenLabs TTS adapter**: `EARSHOT_RELAY_TTS_PROVIDER=openai|elevenlabs`, key from `ELEVENLABS_API_KEY` in the
  environment only, voice id required, model id configurable, mp3 or opus, then the existing lead-in to Ogg/Opus.
  Tested against a stub server only. Docs for local OpenAI-compatible voices (Kokoro-FastAPI, openedai-speech, LocalAI),
  marked untested.
- MCP `say` and CLI `say` take `normalize`, `tag`, `coalesce_window_s` and `cooldown_s`; `GET /v1/status` reports the TTS
  provider, hygiene settings and watchdog state; `earshot-relay doctor` checks the watchdog.

### Fixed (review round 2)

- Normalizer: an inline ```` ```x``` ```` or a stray triple backtick no longer deletes the rest of the message; only a
  line-start fence with a closing line becomes "code". `__init__`-style names keep their underscores. A zero-width
  joiner between letters (Indic scripts) is kept; it is removed only inside emoji sequences.
- Compat: a top-priority send over the alarm rate limit is stored as a normal item (`X-Earshot-Downgraded: alarm`)
  instead of a `429`. `X-Earshot-Promoted: alarm` says when it reached the alarm channel.
- A coalesced message whose write fails every retry reads `write_failed` (was a 404), is counted in
  `hygiene.coalescing_failed` and raises a watchdog notice (`incident_kind: write_failed`). Schema v4 adds
  `failed_pending` (no message text).
- ElevenLabs: a per-request voice of `.` or `..` can no longer rewrite the request path.
- Docs: coalesced text is the one exception to "no message text in the database"; compat wording says "stored", not
  "delivered"; which producers can carry hygiene options (Apprise `ntfy://` and Uptime Kuma cannot); receipts are read on
  the main port; URL tokens land in proxy logs; the Docker and TLS notes for the compat port.

### Added (review round 2)

- `EARSHOT_RELAY_COMPAT_TOKEN`: a send-only token for every compat route, valid nowhere else.

### Changed

- The message index gained `tag`, `normalized` and `truncated` columns and the `pending`, `idem_aliases` and `meta`
  tables (schema v3, migrated in place at startup). Held coalescing text is the one case where message text sits on
  disk, and only until the write.

## 1.0.0

First public release.

- `POST /v1/messages` writes text (`.say`) or audio objects into your own S3-compatible bucket with `PutObject` only;
  the Earshot Android app plays them. Random object keys, per-channel allowlist, `alarm` opt-in, rate limits,
  `Idempotency-Key`.
- Receipts from the phone's beacons: `GET /v1/messages/{id}` with a timeline, `?wait=` long-poll, clock-skew handling,
  `unconfirmed` after a quiet period.
- Optional server-side speech through any OpenAI-compatible `/audio/speech` server with a 700 ms lead-in, never
  falling back silently.
- CLI (`say`, `drop`, `receipt`, `recent`, `status`, `doctor`, `token`), MCP server (streamable HTTP and stdio),
  OpenAPI contract, multi-arch container image on GHCR, examples for Claude Code, Codex, GitHub Actions, n8n and
  Home Assistant.
