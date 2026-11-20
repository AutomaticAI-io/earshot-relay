# Configuration

All settings are environment variables. Every secret also accepts a `_FILE` variant (for example `EARSHOT_RELAY_TOKEN_FILE=/run/secrets/token`); setting both is an error. The relay checks everything at startup and refuses to start with a list of problems that names variables, never values.

## Bucket (the writer)

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_S3_ENDPOINT` | required | `https://` URL of the S3 API (path-style). Plain `http://` only with `EARSHOT_RELAY_ALLOW_HTTP_S3=1`, for disposable tests. |
| `EARSHOT_BUCKET` | required | Bucket name. No slash. |
| `EARSHOT_REGION` | required | Region string your provider expects (`us-east-1` for many S3-compatible services). |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | required | A key that can `PutObject`. See [permissions](permissions.md). |

## Service

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_TOKEN` | required | Bearer token for callers. 32 or more printable characters. `earshot-relay token` makes one. A value still holding an `.env.example` placeholder (`replace...`) is refused, for every secret. |
| `EARSHOT_RELAY_HOST` / `EARSHOT_RELAY_PORT` | `127.0.0.1` / `8080` | The container image sets host `0.0.0.0`; publish it on loopback only. |
| `EARSHOT_RELAY_DATA_DIR` | `~/.local/share/earshot-relay` (`/data` in the image) | Holds the SQLite index (message ids and receipts; no message text). |
| `EARSHOT_RELAY_CHANNELS` | `phone` | Comma-separated channels the relay may send to. `alarm` is loud and is sendable only if listed here. |
| `EARSHOT_RELAY_DEFAULT_CHANNEL` | `phone` if listed, else the first non-alarm channel | Must be in the list, and can never be `alarm`: every send without a channel (CLI `say`, MCP `say`, hooks) uses it. |
| `EARSHOT_RELAY_RATE_LIMIT` | `60/min` | Overall send limit, or `off`. |
| `EARSHOT_RELAY_ALARM_RATE_LIMIT` | `6/hour` | Applies to the `alarm` channel, or `off`. A send whose TTS or PutObject fails does not count. |
| `EARSHOT_RELAY_MAX_AUDIO_BYTES` | `26214400` | Largest accepted audio upload (25 MiB). |
| `EARSHOT_RELAY_RETENTION_DAYS` | `7` | Index rows older than this are pruned daily. Bucket objects are never touched. |

## Producer compat (optional, off by default)

Details, URLs and what was tested: [compat.md](compat.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_COMPAT` | off | `ntfy`, `gotify`, `pushover`, `join` (comma-separated) or `all`. An unknown name is a startup error, and so is any other `EARSHOT_RELAY_COMPAT_*` variable set without this one. |
| `EARSHOT_RELAY_COMPAT_PORT` | unset | A listener where ntfy and Gotify live at the server root, for clients that cannot use a path prefix (Apprise `ntfy://`). Differs from the main and beacon ports. |
| `EARSHOT_RELAY_COMPAT_NTFY_TOPICS` | every non-alarm channel, by name | `topic` or `topic=channel`, comma-separated. Anything else is `404`. |
| `EARSHOT_RELAY_COMPAT_ALARM` | off | A protocol's top priority reaches `alarm` only with this on and `alarm` in `EARSHOT_RELAY_CHANNELS`; otherwise it is delivered normally with `X-Earshot-Downgraded: alarm`. |
| `EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN` | unset | Exactly 30 letters and digits (`earshot-relay token --pushover`), accepted only on the Pushover route, so Pushover-shaped libraries pass their own token check. A secret (also `..._FILE`). |

## Receipts

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_BEACON_TOKEN` | unset (receipts off) | 32+ URL-safe characters, different from the API token. It becomes the last part of the beacon URL. |
| `EARSHOT_RELAY_PUBLIC_URL` | unset | `https://` base the phone can reach. Used only to print the beacon URL in `status`. |
| `EARSHOT_RELAY_BEACON_PORT` | unset | Serve only the beacon endpoint on a second port, so a tunnel can expose just that. Needs a beacon token. |
| `EARSHOT_RELAY_RECEIPT_WAIT_MIN` | `60` | After this many minutes without a beacon, a message becomes `unconfirmed`. |
| `EARSHOT_RELAY_BEACON_RATE_LIMIT` | `600/min` | Beacons accepted per window before `429` (the phone retries), or `off`. |

## Hygiene (optional)

Details: [hygiene.md](hygiene.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_NORMALIZE` | `off` | `off` or `basic`: strip emoji and markdown, say links as "link", collapse whitespace. A request's `normalize` true/false overrides it. |
| `EARSHOT_RELAY_MAX_WORDS` | unset | 1 to 1000. Cuts normalized text at a sentence boundary. Needs the normalizer to run (setting or request). |
| `EARSHOT_RELAY_CHANNEL_HYGIENE` | unset | JSON object keyed by allowed channel, e.g. `{"phone":{"normalize":"basic","max_words":70},"alerts":{"normalize":"off"}}`. Each setting inherits the global default when omitted; request `normalize` overrides the selected channel mode. |

Coalescing (`tag`, `coalesce_window_s`, `cooldown_s`) is per request and has no global setting.

## Delivery watchdog (optional, needs receipts)

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_WATCHDOG_S` | off | 10 to 86400 seconds without a matching per-key `detected` beacon. With URL but no threshold, defaults to 120 s. If beacon token is absent, watchdog is disabled without blocking startup. |
| `EARSHOT_RELAY_WATCHDOG_URL` | required with the above | `http(s)` URL that gets the notice. May hold a secret (an ntfy topic); also `..._URL_FILE`. Never logged or shown. |
| `EARSHOT_RELAY_WATCHDOG_COOLDOWN_S` | `1800` | 60 to 604800. Minimum seconds between notices. |
| `EARSHOT_RELAY_WATCHDOG_FORMAT` | `auto` | `auto`, `json` or `text`. Auto sends plain text to ntfy-style hosts and JSON elsewhere. |

Setting cooldown or format without a watchdog URL/threshold is a startup error; a URL alone enables the 120 s default.

## Server-side speech (optional)

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_TTS_PROVIDER` | `openai` | `openai`, `elevenlabs`, or `gemini`. See [tts.md](tts.md). |
| `EARSHOT_RELAY_TTS_URL` | unset | Base URL of an OpenAI-compatible API (the relay calls `<url>/audio/speech`). No credentials in the URL; use the API key variable. With `elevenlabs` it defaults to `https://api.elevenlabs.io` and is only for a proxy or a test server. |
| `EARSHOT_RELAY_TTS_MODEL` | required with the URL | ElevenLabs: optional, default `eleven_multilingual_v2`. |
| `EARSHOT_RELAY_TTS_VOICE` | required with the URL or ElevenLabs | Default voice (OpenAI voice name, or an ElevenLabs voice id); a request can override it. |
| `EARSHOT_RELAY_TTS_API_KEY` | unset | Sent as a bearer token to the OpenAI-compatible TTS server. |
| `ELEVENLABS_API_KEY` | required with `elevenlabs` | Environment (or `ELEVENLABS_API_KEY_FILE`) only. Sent as `xi-api-key`. `EARSHOT_RELAY_TTS_API_KEY` is accepted as a fallback. |
| `GEMINI_API_KEY` | required with `gemini` | Environment or `_FILE` secret. Sent as `x-goog-api-key`, never in a URL. `EARSHOT_RELAY_TTS_API_KEY` is a fallback. |
| `EARSHOT_RELAY_TTS_FORMAT` | `mp3` | `mp3` or `wav`; ElevenLabs: `mp3` or `opus`; Gemini: `pcm` (24 kHz mono s16le, converted to Ogg/Opus). |
| `EARSHOT_RELAY_TTS_TIMEOUT_S` | `60` | |
| `EARSHOT_RELAY_TTS_LEADIN_MS` | `700` | Silence prepended with ffmpeg. `0` writes the TTS output unchanged. |
| `EARSHOT_RELAY_DEFAULT_SPEECH` | `device` | `device` (phone voice) or `server` (needs the TTS URL). |

## CLI and MCP stdio client

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_API_URL` | `http://127.0.0.1:8080` | The relay to talk to. |
| `EARSHOT_API_TOKEN` | falls back to `EARSHOT_RELAY_TOKEN` | Also `EARSHOT_API_TOKEN_FILE`. |
