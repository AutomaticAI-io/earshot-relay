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

`earshot-relay mcp` needs the package installed (`pip install .`, or `uvx --from . earshot-relay mcp` from a checkout).

Other MCP clients use the same two shapes: a URL plus an `Authorization` header, or a command plus environment variables.

## Tools

| Tool | Purpose |
| --- | --- |
| `say(text, channel?, speech?, voice?)` | Speak a short message on the phone. Refuses the `alarm` channel. |
| `alarm(text)` | LOUD. Exists only when `alarm` is in `EARSHOT_RELAY_CHANNELS`. Speaks on the alarm channel, which may interrupt a phone call; the other party may hear it. |
| `receipt(id="latest", wait_seconds=0)` | What the phone did with a message. Describes device playback, not that a person heard it. |
| `recent(limit=10, channel?)` | Recent messages with their status. |
| `status()` | Channels, speech settings, limits, devices. Never returns the beacon URL (it contains a secret). |

Tools return structured results; a failure is an MCP tool error whose text starts with the relay's error code (`rate_limited`, `channel_not_allowed`, ...).

The server's instructions tell the model to keep messages short, never to put secrets in them, and not to claim a person heard something because a receipt says `finished`. They are guidance, not enforcement: anything a connected agent says goes into your bucket.

## Security

- An unauthenticated MCP request is a 401, like the rest of the API.
- DNS-rebinding protection is turned off for `/mcp` because tunnels send arbitrary `Host` headers; the bearer token is the protection.
- Rate limits apply to MCP sends exactly as they do to the API.
- Leave `alarm` out of `EARSHOT_RELAY_CHANNELS` and no agent can sound the alarm channel, by any route.
