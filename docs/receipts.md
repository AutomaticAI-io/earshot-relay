# Receipts

A receipt answers "what did the phone do with this message?" It records device playback. It is **not** proof that a person heard the message: the phone can finish playing into an empty room, a pocket or headphones nobody is wearing. The relay words every receipt that way.

## How it works

1. You send a message. The relay writes the object and remembers the id, channel, key and time (never the text).
2. The phone polls the bucket, plays the object, and reports `detected`, `started` and `finished` (or `failed` or `missed`) to the beacon URL.
3. The relay matches each beacon to the message by object key and builds the timeline.

If no beacon URL is set in the app, sending still works. You just never get past `stored`, and the receipt says why.

## Set it up

1. Choose a beacon token: `earshot-relay token`. Set `EARSHOT_RELAY_BEACON_TOKEN` (it must differ from the API token) and `EARSHOT_RELAY_PUBLIC_URL`.
2. Make the relay reachable from the phone over HTTPS: [tunnels](tunnels.md).
3. Run `earshot-relay status` and copy the `beacon URL for the app` line into the Earshot app's **Optional HTTPS beacon endpoint** (no query string, no fragment).
4. Send a message and run `earshot-relay receipt latest --wait 30`.

The token is part of the URL, so it lives in the phone's settings and in your proxy's access logs. A leaked beacon URL can forge receipts but cannot send messages; the API token is a different secret.

## Statuses

| Status | Meaning |
| --- | --- |
| `stored` | The object is in the bucket. Nothing heard from the phone yet. |
| `detected` | The phone saw the object. |
| `started` | Playback began. |
| `finished` | Playback finished. If the person skipped it, the status is `skipped` instead. |
| `skipped` | The phone reported `finished` with the error `skipped`. |
| `failed` | The phone tried and could not play it (`error` says why). |
| `missed` | The message was already too old when the phone could play it (the app's freshness limit, 30 minutes by default), so it was not played. |
| `unconfirmed` | No beacon arrived within `EARSHOT_RELAY_RECEIPT_WAIT_MIN` minutes. A late beacon still corrects it. |

With several phones, the status is the furthest any device got. If devices end in different final states, the order of preference is `finished`, `skipped`, `failed`, `missed`. `devices[]` shows each phone.

## Receipt fields

`GET /v1/messages/{id}` returns `id`, `type`, `speech`, `channel`, `key`, `created_at`, `status`, `status_at`, `object_last_modified`, `devices`, `hint` and `hops`. Each hop has `phase`, `device`, `app_version`, `at` (the phone's clock), `at_server` (corrected to the relay's clock), `since_upload_ms`, `clock_skew_ms` and, when present, `error`.

Clock skew: the app sends `clock_skew_ms` (phone clock minus server clock) when it knows it, and the relay subtracts it. `since_upload_ms` is the time from the object's `LastModified` (as the phone saw it) to that hop. Treat it as a rough figure.

Heartbeat beacons update device state in `GET /v1/status` and are never hops. Replayed or duplicate beacons are ignored. Beacons parked on a phone that was offline arrive later and still flip `unconfirmed` to the real status.

## The beacon wire format

`POST /v1/beacons/<token>` with JSON, at most 4096 bytes: `channel`, `key`, `phase` (`detected`, `started`, `finished`, `failed`, `missed`, `heartbeat`), `at`, `last_modified`, `device`, `app_version`, and optionally `clock_skew_ms` and `error`. A valid beacon returns `200` with an empty body; a malformed one returns `400`. The relay ignores fields it does not know. A beacon whose key it never sent is stored but belongs to no message.
