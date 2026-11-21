---
name: speak-to-phone
description: Use when the user asks Claude to tell them something out loud, ping their phone, or say whether a long job finished while they are away from the screen. Sends short text to the Earshot app on their Android phone and checks the playback receipt.
---

# Speak to the user's phone

The Earshot MCP tools reach the user's own relay and bucket. The Earshot Android app, when it is listening, reads the message out loud.

- Call `say` with one or two plain sentences. No markdown, code, URLs, lists, secrets, passwords or tokens: the text is stored in the user's bucket and spoken aloud.
- A successful `say` means the message is stored. It does not mean the phone played it. Call `receipt` with the returned id to learn what the phone did.
- A receipt of `finished` means the phone finished playing it. Never tell the user that a person heard it.
- `alarm` exists only when the user switched the alarm channel on in their relay. It is loud, usually gets through Do Not Disturb, and can still be silenced. Use it only for urgency the user asked to be interrupted for, and never promise it will wake them.
- For a burst of progress updates, pass a `tag` with `coalesce_window_s` so one message is spoken instead of ten.
- Use `status` to see the relay's channels, whether the alarm is enabled and when each phone last reported.
