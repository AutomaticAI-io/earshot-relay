# earshot-relay

A small self-hosted service that lets scripts, CI jobs and AI agents, including Hermes agents, send speech to your phone. You POST text (or an audio file) to the relay, it drops an object into your own S3-compatible bucket, and the [Earshot](https://store.automaticai.io/earshot/) Android app, when it is listening to that bucket, plays it out loud. The relay also records what the phone reported doing with each message, so you can ask for a receipt.

Put thing in bucket. Hear it on phone. The relay is the "put thing in bucket" part, behind one HTTPS endpoint, a CLI and an MCP server.

## Before you start

- The Earshot app on the phone, already listening to your bucket (the [buyer guide](https://store.automaticai.io/earshot/guide/) covers that). Text messages are spoken by the phone's Android text-to-speech, which needs Earshot **0.1.11 or newer** and a TTS voice installed on the phone (Settings, System, Languages, Text-to-speech output). Audio files need neither.
- Normal channels play at the phone's **media volume** and wait while the phone is on a call; a message older than the app's freshness limit (30 minutes by default) is reported as `missed` and not played.
- A writer key for the same bucket that can `PutObject` ([minimal permissions](docs/permissions.md)). Not the phone's key.

## Run it

The image is `ghcr.io/automaticai-io/earshot-relay` (linux/amd64 and linux/arm64, so a Raspberry Pi works):

```sh
curl -fsSLO https://raw.githubusercontent.com/AutomaticAI-io/earshot-relay/main/.env.example && mv .env.example .env
docker run --rm ghcr.io/automaticai-io/earshot-relay token    # prints a random token for EARSHOT_RELAY_TOKEN
# fill in .env: the bucket, the writer key and that token
docker run -d --name earshot-relay --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8080:8080 -v earshot-relay-data:/data \
  ghcr.io/automaticai-io/earshot-relay
docker exec earshot-relay earshot-relay doctor     # checks the relay and writes one inert test object
```

`:latest` is the newest release, `:1` follows 1.x, and `:edge` is the tip of `main`. With compose, `docker compose up -d` uses the same image; `docker compose up -d --build` builds this checkout instead.

The relay refuses to start while `.env` still holds the example placeholders or an empty token. Without Docker: `uv sync`, then `uv run earshot-relay serve` with the same environment variables (put them in your shell, or `set -a; . ./.env; set +a`).

## 60-second demo

```sh
export EARSHOT_API_URL=http://127.0.0.1:8080
export EARSHOT_API_TOKEN=...        # the EARSHOT_RELAY_TOKEN you set

curl -fsS -X POST "$EARSHOT_API_URL/v1/messages" \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Build finished. All tests passed."}'
# {"id":"msg_...","status":"stored","channel":"phone","key":"phone/....say",...}   <- HTTP 202
```

`202` means the message is stored in the bucket. If the phone is listening, it should speak it within a few seconds. Then ask what the phone did (needs [receipts](docs/receipts.md) set up):

```sh
curl -fsS "$EARSHOT_API_URL/v1/messages/latest?wait=30" -H "Authorization: Bearer $EARSHOT_API_TOKEN"
```

Nothing played? Check the app's **Last 20 items** list on the phone (a failed `.say` row names the cause, such as a missing TTS voice), the media volume, and the receipt's `hint`.

Or use the CLI. In the container it is already installed (`docker exec earshot-relay earshot-relay say "hello"`); from a checkout, run it as `uv run earshot-relay ...`, or install it on your PATH with `uv tool install .`:

```sh
earshot-relay say "Deploy is done"
earshot-relay drop ding.mp3
earshot-relay receipt latest --wait 30     # exit 0 finished, 3 not played on any reporting device, 4 timed out
earshot-relay doctor                       # checks the relay and writes one inert test object
```

Retrying a send? Pass an `Idempotency-Key` header (CLI: `--idempotency-key`): the same key within 24 hours returns the first result instead of playing twice.

## What it does

| | |
| --- | --- |
| `POST /v1/messages` | Text (`{"text": ...}`) or an audio file (multipart). Returns 202 once the object is stored in the bucket, or held (`status: coalescing`) when sent with `coalesce_window_s`. Optional `Idempotency-Key` header. |
| `GET /v1/messages/{id}` | The receipt: `stored`, `detected`, `started`, `finished`, `skipped`, `failed`, `missed` or `unconfirmed`, with a timeline. `?wait=30` long-polls. |
| `GET /v1/messages`, `GET /v1/status` | Recent messages; relay, bucket and device state. |
| `POST /v1/doctor` | Checks configuration and the bucket write. |
| `POST /v1/beacons/{token}` | Where the phone reports what it did. This is what makes receipts possible. |
| `tag`, `coalesce_window_s`, `cooldown_s`, `normalize` | Hygiene for every sender: strip emoji, markdown and URLs before speaking, merge a burst under one tag into one message, drop repeats, and an optional delivery watchdog that posts a notice when sends get no playback report ([docs/hygiene.md](docs/hygiene.md)). |
| `/compat/...` | Optional ntfy, Gotify, Pushover and Join request shapes, so existing producers switch by changing a URL ([docs/compat.md](docs/compat.md)). Off by default. |
| `/mcp` | An MCP server over streamable HTTP, same bearer token. A stdio mode is `earshot-relay mcp`. |

The full contract is [openapi.yaml](openapi.yaml); a readable version is [docs/api.md](docs/api.md).

### Speech

By default the text goes to the bucket as a `.say` object and the **phone's own Android text-to-speech** speaks it. Optionally point the relay at any OpenAI-compatible `/audio/speech` server and it will synthesize audio itself (`"speech": "server"`), with a 700 ms lead-in so the start of the sentence is not swallowed. See [docs/tts.md](docs/tts.md), which also covers ElevenLabs and local OpenAI-compatible voices. A server TTS failure returns an error; it never silently falls back to the phone's voice.

### Claude Code, Codex and other agents

```sh
claude mcp add --transport http earshot https://your-relay.example.com/mcp \
  --header "Authorization: Bearer $EARSHOT_API_TOKEN"

claude mcp add earshot --env EARSHOT_API_URL=http://127.0.0.1:8080 \
  --env EARSHOT_API_TOKEN=$EARSHOT_API_TOKEN -- earshot-relay mcp
```

The first form talks to a running relay over HTTP; the second runs a local stdio server, which needs `earshot-relay` on your PATH: install it once with `uv tool install /absolute/path/to/earshot-relay` (Claude Code starts the server from your project directory, so a relative path such as `uvx --from .` does not work). Tools: `say`, `receipt`, `recent`, `status`, plus `alarm` only if you enabled that channel. See [docs/mcp.md](docs/mcp.md).

Or install it as a Claude Code plugin, which asks for your relay URL and token once: `claude plugin marketplace add AutomaticAI-io/earshot-relay`, then `claude plugin install earshot@earshot-relay`.

For agent hooks (speak when Claude Code needs you, or when it finishes), see [examples/](examples) and read the warning there: **hook examples send a slice of agent text to your bucket, so use a private bucket.**

### Hermes

[Hermes](https://hermes-agent.nousresearch.com/docs/) agents get two opt-in paths: the relay's MCP server (the agent calls `say` when it decides to speak) and a final-reply hook that speaks every reply on allowed surfaces. With Hermes installed and the token in the profile's `.env`:

```sh
hermes config set mcp_servers.earshot.url https://your-relay.example.com/mcp
hermes config set mcp_servers.earshot.headers.Authorization 'Bearer ${EARSHOT_API_TOKEN}'
hermes mcp test earshot     # connects and discovers say, receipt, recent, status
```

`config.yaml` holds only the `${EARSHOT_API_TOKEN}` placeholder; Hermes resolves it from the profile's `.env`. The full recipe — token handling, the opt-in hook, the installable `earshot` skill, approval notes and dated tested-on evidence — is [docs/hermes.md](docs/hermes.md); the hook, skill and config snippet live in [examples/hermes/](examples/hermes/). The hook sends a slice of agent text to your bucket as a plain object, so use a private bucket.

### Switch a producer over by changing a URL

Tools that already push to ntfy, Gotify, Pushover or Join can push to the relay instead. Turn it on with
`EARSHOT_RELAY_COMPAT=ntfy,gotify,pushover,join` (it is off by default), then:

```sh
curl -H "Authorization: Bearer $EARSHOT_API_TOKEN" -H "Title: Backup" -d "Nightly backup finished" \
  http://127.0.0.1:8080/compat/ntfy/phone          # an ntfy publish, stored like a native send
apprise -b "Server rebooted" "gotify://127.0.0.1:8080/compat/gotify/$EARSHOT_API_TOKEN"
```

These sends share the native rate limits, `Idempotency-Key` handling, bucket path and receipts, and a success reply
means stored, not played. A producer's top priority (ntfy `5`, Gotify `8` and up, Pushover `2`) is stored as a normal item
with `X-Earshot-Downgraded: alarm` unless you also set `EARSHOT_RELAY_COMPAT_ALARM=1` and list `alarm`. Unsupported
fields are ignored and named in `X-Earshot-Ignored`. Tested with real clients (curl, Apprise, the ntfy and gotify
CLIs, Uptime Kuma, a Go Pushover library); Pushover and Join clients that hard-code the vendor URL cannot be
redirected. Everything, including what was and was not verified: [docs/compat.md](docs/compat.md).

### Receipts and the beacon URL

The phone can report `detected`, `started` and `finished` to an HTTPS endpoint you choose. Set `EARSHOT_RELAY_BEACON_TOKEN` and `EARSHOT_RELAY_PUBLIC_URL`, run `earshot-relay status`, and paste the printed `https://<host>/v1/beacons/<token>` into the app's **Optional HTTPS beacon endpoint**. The phone must be able to reach that URL, which usually means a tunnel: [docs/tunnels.md](docs/tunnels.md) covers Tailscale serve and Cloudflare Tunnel.

A receipt records what the phone did (device playback). It is **not** proof that a person heard the message, and the relay never words it that way. Details and edge cases: [docs/receipts.md](docs/receipts.md).

## Security notes

- Every endpoint except `/healthz` and the beacon URL needs `Authorization: Bearer <EARSHOT_RELAY_TOKEN>`. The beacon URL carries its own, different token in the path; it can record receipts but cannot send messages.
- The relay needs only `s3:PutObject` on the channel prefixes. It never reads your bucket and never holds the phone's read key ([docs/permissions.md](docs/permissions.md)).
- The default channel list is `phone` only. The loud `alarm` channel cannot be sent to unless you list it in `EARSHOT_RELAY_CHANNELS`, the MCP `alarm` tool exists only then, and `alarm` can never be the default channel.
- What `alarm` does on the phone: a siren, then the message on the alarm stream at high volume, ahead of normal items. It plays during a phone call (unless the app's **Alarms can interrupt calls** is off) and the other party may hear it. Do Not Disturb set to "Total silence", an Android refusal, a stopped service, a powered-off phone, battery restrictions or no network can still silence it. It is **not** an emergency or life-safety service.
- Message text is not stored in the relay's database and is not logged, with one exception: text held for `coalesce_window_s` sits in SQLite until its window closes, then is deleted ([docs/hygiene.md](docs/hygiene.md)). Tokens are never logged. Secrets can be supplied as files (`EARSHOT_RELAY_TOKEN_FILE`, ...).
- Bind to localhost (the compose file does) and put a tunnel or an HTTPS reverse proxy in front. Do not expose the plain-HTTP port to the internet.
- The beacon route has its own rate limit (`EARSHOT_RELAY_BEACON_RATE_LIMIT`, 600 per minute) and accepts at most 16 devices per message, so a leaked beacon URL can forge receipts but cannot flood the relay.
- Messages sit in your bucket as plain objects. Anyone who can read the bucket can read them.
- Rate limits (60 per minute overall, 6 per hour for the alarm channel) are in memory and per process.
- Compat routes ([docs/compat.md](docs/compat.md)) are off by default and take the relay token, or a send-only `EARSHOT_RELAY_COMPAT_TOKEN` that opens nothing but those routes (prefer it: a URL token lands in proxy logs). They never reach `alarm` unless you set `EARSHOT_RELAY_COMPAT_ALARM=1`. If you open `EARSHOT_RELAY_COMPAT_PORT`, publish it only where the producers are.

## What it is not

- Not a hosted service: you run it, with your bucket.
- Not a speech-to-text or chat product, and not a delivery guarantee. The phone has to be on, online, listening and allowed to play audio; a `202` means stored, not played.
- Not proof of attention: `finished` means the phone finished playback.
- Not a way to read your bucket or list your files.
- Not affiliated with any cloud provider. "Earshot" is the name of the Android app; this project's MIT license covers this code only and grants no trademark rights.

## Development

```sh
uv sync
uv run pytest -q                              # moto-backed; no network or real bucket needed
uv run python scripts/gen_openapi.py          # regenerate openapi.yaml after API changes
uv run python scripts/compat_clients.py      # drive real clients (curl, Apprise, ntfy CLI, ...) at an in-process relay; see docs/compat.md
uv run python scripts/docker_smoke.py         # build the image and test it end to end against moto (Docker)
EARSHOT_COMPAT_PROVIDERS=1 uv run pytest -q tests/test_providers.py   # SeaweedFS and Garage in Docker
```

## Docs

[API](docs/api.md) · [Producer compat](docs/compat.md) · [Hygiene](docs/hygiene.md) · [Receipts](docs/receipts.md) · [Server TTS](docs/tts.md) · [Tunnels](docs/tunnels.md) · [Permissions](docs/permissions.md) · [MCP](docs/mcp.md) · [Hermes](docs/hermes.md) · [Configuration](docs/configuration.md) · [Examples](examples)

License: MIT, copyright AutomaticAI.
