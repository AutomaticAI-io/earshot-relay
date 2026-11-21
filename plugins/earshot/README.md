# Earshot plugin for Claude Code

Lets Claude speak short messages on your Android phone through your own [Earshot relay](https://github.com/AutomaticAI-io/earshot-relay), then check what the phone did with them.

This plugin installs one MCP server entry (`earshot`) and one skill (`speak-to-phone`). It runs nothing on your machine and starts no process: Claude Code connects over HTTP to the relay URL you give it. There is no shared Earshot server and nothing goes to AutomaticAI. Every message goes to your relay, which stores it in your own S3 bucket, and the Earshot app on your phone plays it.

## What you need

- A running earshot-relay (Docker, one command: see the [relay README](https://github.com/AutomaticAI-io/earshot-relay#run-it)) and the Earshot Android app listening to the same bucket.
- The relay's `/mcp` URL and its `EARSHOT_RELAY_TOKEN`.

## Install

```sh
claude plugin marketplace add AutomaticAI-io/earshot-relay
claude plugin install earshot@earshot-relay
```

Claude Code asks for two values when the plugin is enabled: the relay MCP URL (for example `https://relay.example.com/mcp` or `http://127.0.0.1:8080/mcp`) and the relay token. The token is stored in your system credential store (it is marked `sensitive`), not in `settings.json`.

## Tools

| Tool | What it does |
| --- | --- |
| `say` | Stores a short message for the phone to speak. Success means stored, not played. |
| `receipt` | What the phone did with a message: device playback status, not proof that a person heard it. |
| `recent` | Recent messages and their status. |
| `status` | Relay version, channels, speech settings and the last report from each phone. |
| `alarm` | Loud alert. Present only when the relay's operator put `alarm` in `EARSHOT_RELAY_CHANNELS`; otherwise the tool does not exist and `say` refuses the alarm channel. It is not an emergency service and can still be silenced. |

Anything Claude says goes into your bucket and is spoken aloud, so the tool descriptions tell the model to keep it short and never include secrets. That is guidance, not enforcement: leave `alarm` out of your relay's channels if you do not want an agent to be able to sound it.

## Other ways to connect

The same tools are available over stdio (`earshot-relay mcp`) and from the official Docker image. See the [MCP guide](https://github.com/AutomaticAI-io/earshot-relay/blob/main/docs/mcp.md).

MIT licensed. Report problems at the [issue tracker](https://github.com/AutomaticAI-io/earshot-relay/issues).
