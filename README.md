# earshot-relay

A small self-hosted service that lets scripts, CI jobs and AI agents make your phone speak. You POST text (or an audio file) to the relay, it drops an object into your own S3-compatible bucket, and the [Earshot](https://store.automaticai.io/earshot/) Android app, which is watching that bucket, reads it out loud. It also records what the phone did with each message, so you can ask for a receipt.

Put thing in bucket. Hear it on phone. The relay is the "put thing in bucket" part, behind one HTTPS endpoint, a CLI and an MCP server.

## Run it

You need a bucket the phone already listens to, and a writer key for it ([minimal permissions](docs/permissions.md)).

```sh
cp .env.example .env        # fill in the bucket, the writer key and a token
docker run -d --name earshot-relay --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8080:8080 -v earshot-relay-data:/data \
  earshot-relay:local
```

No image is published yet, so build it once: `docker build -t earshot-relay:local .` (or `docker compose up -d --build`). Generate the token with `docker run --rm earshot-relay:local token`. Without Docker: `uv run earshot-relay serve` with the same environment variables.

## 60-second demo

```sh
export EARSHOT_API_URL=http://127.0.0.1:8080
export EARSHOT_API_TOKEN=...        # the EARSHOT_RELAY_TOKEN you set

curl -fsS -X POST "$EARSHOT_API_URL/v1/messages" \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Build finished. All tests passed."}'
# {"id":"msg_...","status":"stored","channel":"phone","key":"phone/....say",...}   <- HTTP 202
```

The phone speaks it. Then ask what happened (needs [receipts](docs/receipts.md) set up):

```sh
curl -fsS "$EARSHOT_API_URL/v1/messages/latest?wait=30" -H "Authorization: Bearer $EARSHOT_API_TOKEN"
```

Or use the CLI (same image: `docker exec earshot-relay earshot-relay say "hello"`):

```sh
earshot-relay say "Deploy is done"
earshot-relay drop ding.mp3
earshot-relay receipt latest --wait 30     # exit 0 finished, 3 skipped/failed/missed, 4 timed out
earshot-relay doctor                       # checks the relay and writes one inert test object
```

## What it does

| | |
| --- | --- |
| `POST /v1/messages` | Text (`{"text": ...}`) or an audio file (multipart). Returns 202 once the object is in the bucket. |
| `GET /v1/messages/{id}` | The receipt: `stored`, `detected`, `started`, `finished`, `skipped`, `failed`, `missed` or `unconfirmed`, with a timeline. `?wait=30` long-polls. |
| `GET /v1/messages`, `GET /v1/status` | Recent messages; relay, bucket and device state. |
| `POST /v1/doctor` | Checks configuration and the bucket write. |
| `POST /v1/beacons/{token}` | Where the phone reports what it did. This is what makes receipts possible. |
| `/mcp` | An MCP server over streamable HTTP, same bearer token. A stdio mode is `earshot-relay mcp`. |

The full contract is [openapi.yaml](openapi.yaml); a readable version is [docs/api.md](docs/api.md).

### Speech

By default the text goes to the bucket as a `.say` object and the **phone's own Android text-to-speech** speaks it. Optionally point the relay at any OpenAI-compatible `/audio/speech` server and it will synthesize audio itself (`"speech": "server"`), with a 700 ms lead-in so the start of the sentence is not swallowed. See [docs/tts.md](docs/tts.md). A server TTS failure returns an error; it never silently falls back to the phone's voice.

### Claude Code, Codex and other agents

```sh
claude mcp add --transport http earshot https://your-relay.example.com/mcp \
  --header "Authorization: Bearer $EARSHOT_API_TOKEN"

claude mcp add earshot --env EARSHOT_API_URL=http://127.0.0.1:8080 \
  --env EARSHOT_API_TOKEN=$EARSHOT_API_TOKEN -- earshot-relay mcp
```

The first form talks to a running relay over HTTP; the second runs a local stdio server (install the package with `pip install .` or use `uvx --from . earshot-relay mcp`). Tools: `say`, `receipt`, `recent`, `status`, plus `alarm` only if you enabled that channel. See [docs/mcp.md](docs/mcp.md).

For agent hooks (speak when Claude Code needs you, or when it finishes), see [examples/](examples) and read the warning there: **hook examples send a slice of agent text to your bucket, so use a private bucket.**

### Receipts and the beacon URL

The phone can report `detected`, `started` and `finished` to an HTTPS endpoint you choose. Set `EARSHOT_RELAY_BEACON_TOKEN` and `EARSHOT_RELAY_PUBLIC_URL`, run `earshot-relay status`, and paste the printed `https://<host>/v1/beacons/<token>` into the app's **Optional HTTPS beacon endpoint**. The phone must be able to reach that URL, which usually means a tunnel: [docs/tunnels.md](docs/tunnels.md) covers Tailscale serve and Cloudflare Tunnel.

A receipt records what the phone did (device playback). It is **not** proof that a person heard the message, and the relay never words it that way. Details and edge cases: [docs/receipts.md](docs/receipts.md).

## Security notes

- Every endpoint except `/healthz` and the beacon URL needs `Authorization: Bearer <EARSHOT_RELAY_TOKEN>`. The beacon URL carries its own, different token in the path; it can record receipts but cannot send messages.
- The relay needs only `s3:PutObject` on the channel prefixes. It never reads your bucket and never holds the phone's read key ([docs/permissions.md](docs/permissions.md)).
- The default channel list is `phone` only. The loud `alarm` channel cannot be sent to unless you list it in `EARSHOT_RELAY_CHANNELS`, and the MCP `alarm` tool exists only then.
- Message text is not stored in the relay's database and is not logged. Tokens are never logged. Secrets can be supplied as files (`EARSHOT_RELAY_TOKEN_FILE`, ...).
- Bind to localhost (the compose file does) and put a tunnel or an HTTPS reverse proxy in front. Do not expose the plain-HTTP port to the internet.
- Messages sit in your bucket as plain objects. Anyone who can read the bucket can read them.
- Rate limits (60 per minute overall, 6 per hour for the alarm channel) are in memory and per process.

## What it is not

- Not a hosted service: you run it, with your bucket.
- Not a speech-to-text or chat product, and not a delivery guarantee. The phone has to be on, online and allowed to play audio.
- Not proof of attention: `finished` means the phone finished playback.
- Not a way to read your bucket or list your files.
- Not affiliated with any cloud provider. "Earshot" is the name of the Android app; this project's MIT license covers this code only and grants no trademark rights.

## Development

```sh
uv sync
uv run pytest -q                              # moto-backed; no network or real bucket needed
uv run python scripts/gen_openapi.py          # regenerate openapi.yaml after API changes
```

## Docs

[API](docs/api.md) · [Receipts](docs/receipts.md) · [Server TTS](docs/tts.md) · [Tunnels](docs/tunnels.md) · [Permissions](docs/permissions.md) · [MCP](docs/mcp.md) · [Configuration](docs/configuration.md) · [Examples](examples)

License: MIT, copyright AutomaticAI.
