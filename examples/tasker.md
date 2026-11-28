# Tasker

Status: **tested** on 2026-11-28. Tasker sent a message through this recipe, the relay stored it in the bucket, and its
receipt was `stored`. Phone playback was **not** tested.

The recipe is a task called `Earshot Say` with one HTTP Request action. Any profile, widget or other task can call it
with the text to speak.

## Install

1. Copy [`tasker/Earshot_Say.tsk.xml`](tasker/Earshot_Say.tsk.xml) to the phone, for example to `Tasker/tasks/`.
2. In Tasker, long-press the **Tasks** tab, choose **Import Task** and pick the file. Tasker warns that you should
   review imported items. The task holds one HTTP Request and a Flash that shows the relay's answer.
3. Tap the check mark (**Save & Apply**).
4. Set two global variables, in the **Vars** tab or with Variable Set actions:
   - `%EARSHOT_URL`: the relay's base URL with no trailing slash, for example `https://earshot.example.net`.
   - `%EARSHOT_TOKEN`: the relay token (`EARSHOT_RELAY_TOKEN`).

   The token stays in your Tasker data. It is not in the task file, so you can share the file without leaking the
   token. A Tasker backup or a TaskerNet project that includes your variables does carry it.

## Use

Call it from any task with a **Perform Task** action:

- **Name:** `Earshot Say`
- **Parameter 1 (%par1):** the text to speak

The HTTP Request posts this to `%EARSHOT_URL/v1/messages`, with `Authorization:Bearer %EARSHOT_TOKEN` and
`Content-Type:application/json`:

```json
{"text":"%par1"}
```

The Flash then shows the HTTP code and the relay's JSON. `202` means the message is stored. The message `id` in that
JSON gives its receipt at `GET /v1/messages/{id}`.

**Text with a double quote (`"`), a backslash or a line break breaks the JSON.** Tasker puts `%par1` in as it is, and
the relay answers `400` without storing anything. Keep such characters out of `%par1`, or escape them first, for
example with Variable Search Replace (escaping was not tested).

A success means the relay stored the message, not that the phone played it. Receipts
([receipts.md](../docs/receipts.md)) tell you that.

## What was tested

| Item | Value |
| --- | --- |
| Tasker | 6.7.9 (`net.dinglisch.android.taskerm`, versionCode 5455), the direct download from tasker.joaoapps.com (trial), signed by Tasker's developer |
| Android | 16 (API 36) Google Play emulator image, `EARS82_Play_API36` |
| Relay | earshot-relay 1.1.3 (`981b4ba`) run from source on the host, reached from the emulator at `http://10.0.2.2:18082` |
| Bucket | A disposable local MinIO bucket (plain HTTP, `EARSHOT_RELAY_ALLOW_HTTP_S3=1`), deleted after the test |
| Steps | The task file above was imported unchanged. A second task set both globals and ran **Perform Task** `Earshot Say` with `%par1` = `Hello from Tasker, sent by its HTTP Request action.` |
| Result | Relay log `POST /v1/messages 202`. The object `phone/<id>.say` held exactly that sentence (51 bytes). `GET /v1/messages/<id>` returned `status: stored`. |

A body with a raw double quote in the text, sent with curl the way Tasker would substitute it, got `400`.

**Not tested:**

- Playback on a phone.
- HTTPS to the relay. The test used plain HTTP on the emulator's host alias.
- The paid Play Store build, or one installed from Play.
- A TaskerNet share link.
- Escaping quotes in `%par1`.
- A physical device.
