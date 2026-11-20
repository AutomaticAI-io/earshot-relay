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
| `coalescing` | Not stored yet: the text is held for `coalesce_window_s` and will be written once, with the latest text under its tag ([hygiene](hygiene.md)). The receipt has `write_at`. Once written, the same id reads as a normal receipt. |
| `stored` | The object is in the bucket. Nothing heard from the phone yet. |
| `detected` | The phone saw the object. |
| `started` | Playback began. |
| `finished` | Playback finished. If the person skipped it, the status is `skipped` instead. |
| `skipped` | The phone reported `finished` with the error `skipped`. |
| `failed` | The phone tried and could not play it (`error` says why). |
| `missed` | The phone did not play it: it went stale (older than the app's freshness limit, 30 minutes by default, for example after being held for a call), audio was unavailable, or the bucket changed. The hop's `error` says which. |
| `unconfirmed` | No beacon arrived within `EARSHOT_RELAY_RECEIPT_WAIT_MIN` minutes. A late beacon still corrects it. |

With several phones, the status is the furthest any device got. If devices end in different final states, the order of preference is `finished`, `skipped`, `failed`, `missed`. `devices[]` shows each phone.

`?wait=N` (and `earshot-relay receipt --wait`) stops early only when waiting longer cannot change the answer: at once on `finished`, and on `skipped`, `failed` or `missed` only when every device that has reported is in a final state. With two phones, one can report `missed` while the other is still playing; the wait keeps going, and a moment later the message reads `finished`. The CLI exits 3 only for a final not-played status; a status that could still change at the deadline exits 4.

## Receipt fields

`GET /v1/messages/{id}` returns `id`, `type`, `speech`, `channel`, `key`, `created_at`, `status`, `status_at`, `object_last_modified`, `devices`, `hint` and `hops`. Each hop has `phase`, `device`, `at` (the phone's clock), `at_server` (moved onto the S3 server clock: the bucket's clock, the same one `LastModified` uses), `received_at` (the relay's clock when the beacon arrived), `clock_skew_ms`, `since_upload_ms` and `error` (null unless the phone sent one). The app version is on each entry of `devices[]`, not on hops.

Clock skew: the app sends `clock_skew_ms` (phone clock minus the S3 server clock, from the listing's `Date` header) when it knows it, and the relay computes `at_server = at - clock_skew_ms`. `since_upload_ms` is the time from the object's `LastModified` (as the phone saw it) to that hop, both on the server clock. Treat it as a rough figure.

Heartbeat beacons update device state in `GET /v1/status` and are never hops. Replayed or duplicate beacons are ignored. Beacons parked on a phone that was offline arrive later and still flip `unconfirmed` to the real status.

## Silent phones

Receipts answer when you ask. To be told without asking, set [the delivery watchdog](hygiene.md#delivery-watchdog): it
posts one notice to a URL you choose when a stored message gets no beacon within `EARSHOT_RELAY_WATCHDOG_S` seconds.
It watches sends, not idle phones (the app sends no heartbeat).

## The beacon wire format

`POST /v1/beacons/<token>` with JSON, at most 4096 bytes: `channel`, `key`, `phase` (`detected`, `started`, `finished`, `failed`, `missed`, `heartbeat`), `at`, `last_modified`, `device`, `app_version`, and optionally `clock_skew_ms` and `error`. A valid beacon returns `200` with an empty body; a malformed one returns `400`. The relay ignores fields it does not know. A beacon whose key it never sent is stored but belongs to no message.

Limits, so a leaked beacon URL can forge receipts but not flood the relay: timestamps must fall between 2000 and 2100, `device` and `app_version` may not contain control characters, one key accepts beacons from at most 16 devices (a 17th is `400`), and over `EARSHOT_RELAY_BEACON_RATE_LIMIT` (default 600 per minute) the answer is `429` with `Retry-After`, which the phone keeps and retries. `GET /v1/status` lists the 20 most recent devices.
