---
name: earshot
description: Use when the user wants a short message spoken aloud on their Android phone through their own earshot-relay, or asks whether the phone played it. Call the discovered mcp__earshot__say tool, then check mcp__earshot__receipt.
---

# Earshot: speak on the user's phone

The user runs their own earshot-relay with the `earshot` MCP server configured.
The relay stores your text in the user's own S3 bucket, and the Earshot Android
app speaks it when the phone is listening.

## When to call say

Call `mcp__earshot__say` when the user asked to be told aloud:

- a long job finished and the user is away from the screen
- the agent needs the user's attention (a decision, an approval, a failure)
- the user said "tell me when …", "let me know out loud when …", or similar

Do not speak every reply by default. If the user wants every final reply spoken,
that is the opt-in shell hook's job (the Earshot Hermes recipe), not this
skill's — and running both speaks a turn twice.

## How to speak well

- One or two plain sentences. No markdown, code, URLs, emoji or long lists.
- Never secrets, tokens, passwords or private data: the text is stored in the
  user's bucket as a plain object and spoken out loud.
- A successful call returns a message id with `status: stored`. Stored is not
  played — say "stored" or "sent to the phone", not "you heard".

## When to inspect a receipt

Call `mcp__earshot__receipt` with the message id when the user asks whether the
phone actually played a message, or before you claim it played. Word the answer
the way the receipt words it:

- `finished` means the phone finished device playback. It is not proof a person
  heard it and not a locked-screen test.
- `stored`, `detected` or `started` mean the message is on its way.
- `skipped` means the phone **started** playback but the user skipped it —
  neither "played to the end" nor "did not play"; say that it started and was
  skipped, and that how much was heard is not established.
- `failed` means the phone tried and could not play it; `missed` means it did
  not play (it went stale, the audio was unavailable, or the bucket changed).
- Word your answer from the receipt's own `summary`, its timeline hops and the
  hop `error`/`hint`. When `hint` is null, say there is no hint — never invent
  a reason.

Use `mcp__earshot__recent` to list the last few messages and
`mcp__earshot__status` for relay, bucket and device state when something looks
wrong.

## Limits

- Android only and one way: the phone cannot reply through Earshot.
- An `alarm` tool exists only when the user enabled the alarm channel; use it
  only for urgency the user asked for.
