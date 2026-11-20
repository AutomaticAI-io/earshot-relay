# Changelog

All notable changes. Versions follow semver. A `202` always means stored (or, with `coalesce_s`, held), never played;
receipts describe device playback, never that a person heard anything.

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
- **Coalesce and cooldown by tag**: `tag` with `coalesce_s` holds the write and writes only the latest text once
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
- MCP `say` and CLI `say` take `normalize`, `tag`, `coalesce_s` and `cooldown_s`; `GET /v1/status` reports the TTS
  provider, hygiene settings and watchdog state; `earshot-relay doctor` checks the watchdog.

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
