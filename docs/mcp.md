# MCP

The relay is also an MCP server, so an agent can speak to your phone and check receipts without shell access.

## Two transports, same tools

**Streamable HTTP**, served by the relay at `/mcp`, behind the same bearer token as the API:

```sh
claude mcp add --transport http earshot https://your-relay.example.com/mcp \
  --header "Authorization: Bearer $EARSHOT_API_TOKEN"
```

Use `http://127.0.0.1:8080/mcp` if the agent runs on the same machine. The server is stateless and answers with plain JSON.

**stdio**, a local process that calls the relay's HTTP API (the relay can be anywhere):

```sh
claude mcp add earshot \
  --env EARSHOT_API_URL=http://127.0.0.1:8080 --env EARSHOT_API_TOKEN=$EARSHOT_API_TOKEN \
  -- earshot-relay mcp
```

`earshot-relay mcp` must be on the PATH of the process that starts it. Install it once with `uv tool install /absolute/path/to/earshot-relay` (or `pip install /absolute/path/to/earshot-relay`). Claude Code starts a stdio server from the project you are working in, not from the relay checkout, so a relative form such as `uvx --from . earshot-relay mcp` fails there; `uvx --from /absolute/path/to/earshot-relay earshot-relay mcp` works if you prefer not to install.

Other MCP clients use the same two shapes: a URL plus an `Authorization` header, or a command plus environment variables.

## Tools

| Tool | Purpose |
| --- | --- |
| `say(text, channel?, speech?, voice?, normalize?, tag?, coalesce_window_s?, cooldown_s?)` (`coalesce_s`, the 1.1.0 name of `coalesce_window_s`, is still accepted) | Store a short message for the phone to speak. A success means stored, not played; with `coalesce_window_s` it means held, to be written once when the window closes ([hygiene](hygiene.md)). Refuses the `alarm` channel (and the default channel can never be `alarm`). |
| `alarm(text)` | LOUD. Exists only when `alarm` is in `EARSHOT_RELAY_CHANNELS`. See the caveat below. |
| `receipt(id="latest", wait_seconds=0)` | What the phone did with a message, plus `final` (whether waiting longer could still change it). Describes device playback, not that a person heard it. |
| `recent(limit=10, channel?)` | Recent messages with their status. |
| `status()` | Relay version, channels, default channel, speech settings, hygiene and watchdog state, last beacon and devices. Never returns the beacon URL (it contains a secret). |

**The alarm caveat.** The `alarm` tool's description gives the model the same caveat: the app plays a siren, then the message on the phone's alarm stream at high volume. It usually gets through Do Not Disturb (not "Total silence") and plays during a phone call, where the other party may hear it, unless the app's **Alarms can interrupt calls** is off. Total-silence Do Not Disturb, an Android refusal, a stopped service, a powered-off phone, battery restrictions or no network can still silence it. It is not an emergency or life-safety service, and the tool tells the model never to promise the user it will wake them.

Tools return structured results; a failure is an MCP tool error whose text starts with the relay's error code (`rate_limited`, `channel_not_allowed`, ...).

The server's instructions tell the model to keep messages short, never to put secrets in them, and not to claim a person heard something because a receipt says `finished`. They are guidance, not enforcement: anything a connected agent says goes into your bucket.

## Security

- An unauthenticated MCP request is a 401, like the rest of the API.
- DNS-rebinding protection is turned off for `/mcp` because tunnels send arbitrary `Host` headers; the bearer token is the protection.
- Rate limits apply to MCP sends exactly as they do to the API.
- Leave `alarm` out of `EARSHOT_RELAY_CHANNELS` and no agent can sound the alarm channel, by any route.
