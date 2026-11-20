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
| `EARSHOT_RELAY_TOKEN` | required | Bearer token for callers. 32 or more printable characters. `earshot-relay token` makes one. |
| `EARSHOT_RELAY_HOST` / `EARSHOT_RELAY_PORT` | `127.0.0.1` / `8080` | The container image sets host `0.0.0.0`; publish it on loopback only. |
| `EARSHOT_RELAY_DATA_DIR` | `~/.local/share/earshot-relay` (`/data` in the image) | Holds the SQLite index (message ids and receipts; no message text). |
| `EARSHOT_RELAY_CHANNELS` | `phone` | Comma-separated channels the relay may send to. `alarm` is loud and is sendable only if listed here. |
| `EARSHOT_RELAY_DEFAULT_CHANNEL` | first channel | Must be in the list. |
| `EARSHOT_RELAY_RATE_LIMIT` | `60/min` | Overall send limit, or `off`. |
| `EARSHOT_RELAY_ALARM_RATE_LIMIT` | `6/hour` | Applies to the `alarm` channel, or `off`. |
| `EARSHOT_RELAY_MAX_AUDIO_BYTES` | `26214400` | Largest accepted audio upload (25 MiB). |
| `EARSHOT_RELAY_RETENTION_DAYS` | `7` | Index rows older than this are pruned daily. Bucket objects are never touched. |

## Receipts

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_BEACON_TOKEN` | unset (receipts off) | 32+ URL-safe characters, different from the API token. It becomes the last part of the beacon URL. |
| `EARSHOT_RELAY_PUBLIC_URL` | unset | `https://` base the phone can reach. Used only to print the beacon URL in `status`. |
| `EARSHOT_RELAY_BEACON_PORT` | unset | Serve only the beacon endpoint on a second port, so a tunnel can expose just that. Needs a beacon token. |
| `EARSHOT_RELAY_RECEIPT_WAIT_MIN` | `60` | After this many minutes without a beacon, a message becomes `unconfirmed`. |

## Server-side speech (optional)

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_TTS_URL` | unset | Base URL of an OpenAI-compatible API (the relay calls `<url>/audio/speech`). |
| `EARSHOT_RELAY_TTS_MODEL` | required with the URL | |
| `EARSHOT_RELAY_TTS_VOICE` | unset | Default voice; a request can override it. |
| `EARSHOT_RELAY_TTS_API_KEY` | unset | Sent as a bearer token to the TTS server. |
| `EARSHOT_RELAY_TTS_FORMAT` | `mp3` | `mp3` or `wav`. |
| `EARSHOT_RELAY_TTS_TIMEOUT_S` | `60` | |
| `EARSHOT_RELAY_TTS_LEADIN_MS` | `700` | Silence prepended with ffmpeg. `0` writes the TTS output unchanged. |
| `EARSHOT_RELAY_DEFAULT_SPEECH` | `device` | `device` (phone voice) or `server` (needs the TTS URL). |

## CLI and MCP stdio client

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_API_URL` | `http://127.0.0.1:8080` | The relay to talk to. |
| `EARSHOT_API_TOKEN` | falls back to `EARSHOT_RELAY_TOKEN` | Also `EARSHOT_API_TOKEN_FILE`. |
