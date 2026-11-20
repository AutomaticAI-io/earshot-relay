# API

The machine-readable contract is [`openapi.yaml`](../openapi.yaml) (generated from the app and checked by a test). Everything except `GET /healthz` and `POST /v1/beacons/{token}` needs `Authorization: Bearer <EARSHOT_RELAY_TOKEN>`. `/openapi.json` is served too, behind the same token.

## POST /v1/messages

Send text:

```sh
curl -fsS -X POST http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Deploy finished", "channel": "phone"}'
```

JSON fields: `text` (required), `channel`, `speech` (`device` or `server`), `voice` (server speech only). Unknown fields are rejected. Text is trimmed and must be 1 to 4000 UTF-16 code units and at most 64 KiB, the limits the phone enforces.

Send audio (multipart, one `file` part, optional `channel`):

```sh
curl -fsS -X POST http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" -F file=@ding.mp3
```

Accepted: `ogg`, `opus`, `mp3`, `m4a`, `aac`, `wav`, `flac`, `webm`, up to `EARSHOT_RELAY_MAX_AUDIO_BYTES`. The type comes from the file name, else the part's media type.

Success is `202`:

```json
{"id": "msg_1765de3b463943b78a2a6b8f8d8de600", "type": "text", "speech": "device", "channel": "phone",
 "status": "stored", "created_at": "2026-11-20T16:41:44.370Z",
 "key": "phone/1765de3b463943b78a2a6b8f8d8de600.say",
 "receipt_url": "/v1/messages/msg_1765de3b463943b78a2a6b8f8d8de600"}
```

`202` means the object is in the bucket (`stored`), not that the phone has played it. Object keys are always random (`<channel>/<32 hex>.<ext>`), never derived from the text, and the message id is the same hex.

## GET /v1/messages/{id}

`id` is a message id or `latest`. `?wait=N` (0 to 60 seconds) holds the request until the status is final. See [receipts](receipts.md) for the fields and statuses.

## GET /v1/messages

`?channel=`, `?limit=` (1 to 200, default 20), `?since=<message id>`. Newest first; with `since`, the messages after that one, oldest first. Returns `{"items": [...], "next_since": "msg_..."}`. This is the relay's own index, not a bucket listing.

## GET /v1/status

Relay version, bucket host and last successful write, channels and default channel, speech defaults, limits, and the receipt setup including the phone's beacon URL.

## POST /v1/doctor

Checks configuration and writes one inert `<default-channel>/relay-doctor-<timestamp>.txt` object (the phone ignores a lone `.txt`). Returns `ok`, per-check results and the beacon URL. The CLI equivalent is `earshot-relay doctor`.

## POST /v1/beacons/{beacon_token}

For the phone, not for you. Unauthenticated apart from the token in the path (404 on a wrong token). Body at most 4096 bytes. See [receipts](receipts.md).

## GET /healthz

`{"ok": true}`, no auth. Used by the container healthcheck.

## Errors

Every error has the same shape:

```json
{"error": {"code": "text_too_long", "message": "...", "field": "text"}}
```

| Status | Codes |
| --- | --- |
| 400 | `bad_json`, `bad_request`, `invalid_beacon` |
| 401 | `unauthorized` (with `WWW-Authenticate: Bearer`) |
| 403 | `channel_not_allowed` |
| 404 | `not_found` |
| 413 | `too_large` |
| 415 | `unsupported_media_type` |
| 422 | `empty_text`, `text_too_long`, `invalid_text`, `invalid_field`, `unknown_field`, `invalid_channel`, `invalid_speech`, `invalid_voice`, `tts_not_configured`, `empty_file`, `missing_file` |
| 429 | `rate_limited` (with `Retry-After`) |
| 502 | `storage_error` (the message carries a hint such as AccessDenied or NoSuchBucket), `tts_failed` |
| 504 | `tts_timeout` |

Request logs are one JSON line per request: method, path template, status, duration and message id. No text and no tokens.
