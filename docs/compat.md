# Producer compatibility: ntfy, Gotify, Pushover, Join

Plenty of tools already know how to push a notification to ntfy, Gotify, Pushover or Join: Uptime Kuma, Healthchecks,
Apprise, Tasker, shell scripts, cron mailers. With compat on, the relay accepts those request shapes, so you switch
such a producer to Earshot by changing a URL and a token. Everything they send goes through the same path as a
native `POST /v1/messages`: the same rate limits, the same `Idempotency-Key` handling, the same bucket object
(`<channel>/<id>.say`) and the same receipt at `GET /v1/messages/{id}`.

This is the receiving side only. The relay does not forward anything to ntfy, Gotify, Pushover or Join, and it is not
those services; it just understands what their clients send.

## Read this first

- **A success response means stored, not played.** It is the protocol's own success body (ntfy's JSON, Gotify's
  message, Pushover's `status: 1`, Join's `success: true`) and it says the relay wrote the object to your bucket.
  Whether the phone played it is in the receipt (`X-Earshot-Message-Id` and `Location` point to it). A receipt shows
  device playback; it is not proof that a person heard anything.
- **Off by default.** Nothing is reachable until `EARSHOT_RELAY_COMPAT` names a protocol.
- **The alarm channel is never reachable by accident.** See [Priorities and the alarm channel](#priorities-and-the-alarm-channel).
  The alarm is not an emergency or life-safety service, and Do Not Disturb set to "Total silence" can silence it.
- **Every sender gets the same hygiene**: rate limits, the text cap (4000 UTF-16 code units), idempotent retries,
  and an access log that records the protocol, ignored field names and a downgrade, never the text or a token.

## Turn it on

```sh
EARSHOT_RELAY_COMPAT=ntfy,gotify,pushover,join     # or: all
EARSHOT_RELAY_COMPAT_PORT=8082                     # optional: a second listener whose ROOT speaks the protocols
```

| Variable | Default | Notes |
| --- | --- | --- |
| `EARSHOT_RELAY_COMPAT` | off | Comma-separated: `ntfy`, `gotify`, `pushover`, `join`, or `all`. Unknown names stop the relay at startup. |
| `EARSHOT_RELAY_COMPAT_PORT` | unset | A listener where ntfy and Gotify live at the server root (`/`, `/{topic}`, `/message`). Needed for clients that cannot use a path prefix (Apprise `ntfy://`). Must differ from the main and beacon ports. Publish it only where the producers are, for example `127.0.0.1:8082` or your tailnet. |
| `EARSHOT_RELAY_COMPAT_NTFY_TOPICS` | each non-alarm channel is a topic of the same name | Allowlist, `topic` or `topic=channel`, comma-separated: `phone,doorbell=phone`. A topic not listed is a `404` in ntfy's error shape. A topic mapped to `alarm` also needs `EARSHOT_RELAY_COMPAT_ALARM=1`. |
| `EARSHOT_RELAY_COMPAT_ALARM` | off | Lets a protocol's top priority reach the `alarm` channel. Needs `alarm` in `EARSHOT_RELAY_CHANNELS`. |
| `EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN` | unset | An optional 30-character token (letters and digits only) that Pushover-shaped clients accept, valid only on the Pushover route. `earshot-relay token --pushover` makes one. Pushover's documented example keys are refused. |

Two places serve each protocol:

| Protocol | On the main port (`8080`) | On the compat port, if set |
| --- | --- | --- |
| ntfy | `/compat/ntfy` (JSON publish), `/compat/ntfy/{topic}` | `/` (JSON publish), `/{topic}`, `/{topic}/publish` (also `send`, `trigger`) |
| Gotify | `/compat/gotify/message` | `/message` |
| Pushover | `/compat/pushover/1/messages.json` | `/1/messages.json` |
| Join | `/compat/join/_ah/api/messaging/v1/sendPush` | `/_ah/api/messaging/v1/sendPush` |

The compat listener serves only these routes and `/healthz`: no native API, no OpenAPI page, no MCP. The compat
topics `healthz`, `v1`, `compat` and `mcp` (and `message` when Gotify is on) cannot be ntfy topics there.

`GET /v1/status` and `earshot-relay status` show what is enabled and where.

## What gets spoken

All four protocols map the same way.

- Spoken text is `Title. Message.` A title that already ends in punctuation keeps it (`Backup failed: disk full`). A
  message that starts with the title as a whole word is said once (title `Garage`, message `Garage door left open`
  speaks `Garage door left open`). Title only or message only is spoken as is.
- Text over the cap is not truncated: the sender gets a protocol-appropriate error (ntfy `413`, code `41301`;
  Gotify, Pushover and Join `400`) and nothing is stored.
- Fields the relay cannot honor are ignored, never silently: their names come back in the `X-Earshot-Ignored`
  response header and are logged. Examples: ntfy `click`, `tags`, `icon`, `attach`; Pushover `sound`, `url`.
- A delayed ntfy send (`Delay`, `At`, `In`) is refused with `400` instead of being played now. This is a deliberate
  deviation: playing a scheduled message immediately would be wrong.
- Every response carries `X-Earshot-Message-Id` and `Location: /v1/messages/{id}`. Gotify's body also has
  `extras["earshot::message"]` with the id and receipt URL.
- Every protocol accepts an `Idempotency-Key` header (1-255 printable ASCII). A retry with the same key within 24 hours
  returns the first result with `Idempotent-Replayed: true` and stores nothing new.

### Priorities and the alarm channel

| Protocol | Top priority (candidate for alarm) | Everything else |
| --- | --- | --- |
| ntfy | `5`, `max`, `urgent` | `1`-`4`, `min`, `low`, `default`, `high` |
| Gotify | `8` and above | `0`-`7` |
| Pushover | `2` (emergency) | `-2`, `-1`, `0`, `1` (`1` stays normal) |
| Join | none | all |

A top-priority send goes to the `alarm` channel only when `alarm` is in `EARSHOT_RELAY_CHANNELS` **and**
`EARSHOT_RELAY_COMPAT_ALARM=1`. Otherwise it is delivered on its normal channel and the response carries
`X-Earshot-Downgraded: alarm`, so a producer's "critical" never turns your phone into a siren by default.
Mind the defaults of the tools you point at it: Uptime Kuma's ntfy sends `DOWN` at priority `5` and its Gotify sends
priority `8`, so with the alarm switch on every DOWN is loud. Alarm sends also use the stricter `EARSHOT_RELAY_ALARM_RATE_LIMIT`.
The alarm plays a siren on the phone's alarm stream, also during a call unless the app's "Alarms can interrupt calls"
is off; "Total silence" Do Not Disturb, an Android refusal, a stopped service, a powered-off phone or no network can
still silence it.

## Normalizer, tags and coalescing on these routes

The four protocols have no fields for [hygiene](hygiene.md), so Earshot's own options ride beside the request, as
headers or as query parameters (the header wins):

| Header | Query parameter | Value |
| --- | --- | --- |
| `X-Earshot-Normalize` | `earshot_normalize` | `true` or `false` (`1`, `yes`, `on`, `0`, `no`, `off` too) |
| `X-Earshot-Tag` | `earshot_tag` | the tag |
| `X-Earshot-Coalesce-S` | `earshot_coalesce_s` | seconds |
| `X-Earshot-Cooldown-S` | `earshot_cooldown_s` | seconds |

`EARSHOT_RELAY_NORMALIZE=basic` applies to these routes without any header. When a send is held or dropped, the
protocol's own body still reports success (it is accepted, not written), and the response says what really happened
with `X-Earshot-Status: coalescing` or `suppressed`; `X-Earshot-Normalized: true` and `X-Earshot-Truncated: true` report
the text changes. A bad value is a `400` in the protocol's error shape. Join's `earshot_*` query parameters are not
listed as ignored. ntfy's sequence id and Gotify's extras are not mapped (the relay cannot replace a delivered
message; see [hygiene](hygiene.md#coalesce-and-cooldown-by-tag)). Tested with the in-process test suite, not with
real clients.

## ntfy

Use the relay as your ntfy server. Auth is the relay token, in any place ntfy clients put a token:

- `Authorization: Bearer <token>`
- `Authorization: Basic ...` with the token as the **password** (any username, or none)
- `?auth=` with the base64 (URL alphabet, padding optional) of the full `Authorization` header value, which is what
  ntfy's own browser links use

Publish forms: `POST` or `PUT` with the body as the message; headers `Title` (`X-Title`, `t`), `Priority`
(`X-Priority`, `p`: `1`-`5` or `min`, `low`, `default`, `high`, `max`, `urgent`), `Tags` (`X-Tags`); the same names as
query parameters; `GET /{topic}/publish?message=...`; and JSON at the base (`POST /` on the compat port,
`POST /compat/ntfy` on the main port) with `topic`, `message`, `title`, `priority`. The RFC 9218
`Priority: u=3, i` header is ignored, as ntfy does.

Success is ntfy's JSON: `id`, `time`, `expires`, `event: "message"`, `topic`, `message` (plus `title`, `priority`
and `tags` when sent). The id is the relay message id, so `GET /v1/messages/{id}` works with it. Errors use ntfy's
shape (`{"code": 40101, "http": 401, "error": "..."}`).

**Audio.** An ntfy attachment upload whose `Filename` (or `X-Filename`) has an audio extension, or whose
`Content-Type` is audio, is stored as audio and played as audio: `ntfy publish --file ding.wav <topic>` plays the
file. This goes beyond ntfy's own behavior and is capped by `EARSHOT_RELAY_MAX_AUDIO_BYTES`. `title` and `message`
are then ignored and named in `X-Earshot-Ignored`.

Unsupported and ignored (named in `X-Earshot-Ignored` when sent): `tags`, `click`, `icon`, `attach`, `actions`,
`markdown`, `email`, `call`, `template`, `sequence_id`, and `cache`, `firebase` or `unifiedpush` when set to anything
but their defaults. Refused: `delay`, `at` and `in`.

| Setting | Value |
| --- | --- |
| Server URL (compat port) | `http://relay-host:8082` |
| Server URL (main port, clients that accept a path) | `http://relay-host:8080/compat/ntfy` |
| Topic | a channel name, or a key of `EARSHOT_RELAY_COMPAT_NTFY_TOPICS` |

## Gotify

Use the relay as your Gotify server; the application token is the relay token.

- Token in `X-Gotify-Key`, `?token=`, or `Authorization: Bearer`.
- `POST /message` with JSON, form or query fields `title`, `message`, `priority`, `extras`.
- Success is Gotify's message JSON (`id` is a number, `appid`, `message`, `title`, `priority`, `date`).
  Errors are Gotify's `{"error", "errorCode", "errorDescription"}`.
- A path prefix works: set the server URL to `http://relay-host:8080/compat/gotify` and clients append `/message`.
- `extras`: `client::display` markdown, `client::notification` click and image, and `android::action` are ignored and
  named in `X-Earshot-Ignored`.

Gotify posts to the channel in `EARSHOT_RELAY_DEFAULT_CHANNEL` (the Gotify token does not carry a channel). Use ntfy
topics, or the native API, when you need to choose a channel.

## Pushover

`POST /1/messages.json` (form, multipart or JSON) with `token`, `user`, `message`, `title`, `priority`, and optional
`sound`, `url`, `url_title`, `device`, `html`, `timestamp`, `ttl`, `retry`, `expire`, `callback`, `attachment`.
Only `message`, `title` and `priority` are used; every other non-empty field is named in `X-Earshot-Ignored`.

- `token` is the relay token or `EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN`. `user` is accepted and not checked.
- Success `{"status": 1, "request": "<relay message id>"}`. Failure `{"status": 0, "errors": [...]}`, plus the
  offending field set to `"invalid"`.
- Responses carry `X-Limit-App-Limit`, `X-Limit-App-Remaining` and `X-Limit-App-Reset` from the relay's send window
  (`10000` remaining when limits are off), because some libraries require them.
- A `priority` outside `-2..2` is a `400`. Attachments and every other non-empty field are ignored and named.

**Honest limit.** Most Pushover clients hard-code `api.pushover.net`: Apprise `pover://`, Uptime Kuma, Healthchecks,
Grafana and Home Assistant do, and none of them can be pointed at the relay. This route is for scripts, curl, Tasker
HTTP requests, and code that lets you change the endpoint (the Go library `gregdel/pushover` does, and was tested).

## Join

`GET` or `POST /_ah/api/messaging/v1/sendPush` with `text`, `title`, `say`, `apikey` (the relay token), as query or
body (form or JSON). `say` is what is spoken; with `say`, `text` and `title` are ignored. `deviceId`, `deviceIds`,
`url`, `icon`, `smallicon`, `image`, `sound`, `vibration`, `group`, `clipboard`, `file`, `find`, `wallpaper`, `app`,
`actions` and every other non-empty field are named in `X-Earshot-Ignored`.

Success is `{"success": true, "userAuthError": false, "messageId": "<id>"}`. A bad `apikey` is `401`
`{"success": false, "userAuthError": true, "errorMessage": "User Not Authenticated"}`.

**Honest limit.** Join is not run as a service the relay can replace: Apprise's `join://` hard-codes the vendor URL
and checks the key's shape. This route is for Tasker's HTTP Request action, curl and scripts that build the URL
themselves.

## Which real clients were tested, and how

The harness is [`scripts/compat_clients.py`](../scripts/compat_clients.py). It starts a moto bucket and an in-process
relay with all four protocols on (channels `phone,alarm`, rate limits off, a compat listener and a self-signed TLS
copy of it), runs each real client against it, and checks that exactly one message is stored with the expected
channel, text or audio bytes, and a `stored` receipt. Each client's request is recorded to
`tests/fixtures/compat/*.json` and replayed byte for byte by `tests/test_compat.py`. Tokens are dummy values
(`t` x 40, `p` x 30).

Run on 2026-11-20 with curl 8.14.1, Apprise 2.3.0, ntfy CLI 2.22.0, gotify-cli 2.4.0, Go 1.26.7 and Uptime Kuma
1.23.16: **18 passed, 0 failed.**

| Producer | Command or action | Result |
| --- | --- | --- |
| curl, ntfy | `curl -H "Authorization: Bearer $T" -d "washer done" http://H:8080/compat/ntfy/phone` | stored |
| curl, ntfy headers | `curl -u earshot:$T -H "Title: Laundry" -H "Priority: urgent" -H "Tags: tada" -d "Washer done" http://H:8080/compat/ntfy/phone` | `Laundry. Washer done`, downgraded, `tags` ignored |
| curl, ntfy GET | `curl -G http://H:8080/compat/ntfy/phone/publish --data-urlencode "message=Garage door left open" --data-urlencode "title=Garage" --data-urlencode "auth=<b64 of Bearer $T>"` | `Garage door left open` (title said once) |
| curl, ntfy JSON | `curl -H "Authorization: Bearer $T" -d '{"topic":"phone","title":"CI","message":"Build 512 passed","priority":4,"click":"https://ci.example.com/512"}' http://H:8080/compat/ntfy` | stored, `click` ignored |
| Apprise ntfy | `apprise -b "washer done" -t Laundry "ntfy://earshot:$T@H:8082/phone"` | stored, `icon` ignored |
| Apprise ntfys | `apprise ... "ntfys://$T@H:TLS/phone?auth=token&verify=no&priority=max"` | stored, downgraded |
| ntfy CLI | `docker run --rm --network host binwiederhier/ntfy publish --token $T --title Backup --priority high http://H:8080/compat/ntfy/phone "Nightly backup finished"` | stored |
| ntfy CLI, root, Basic | `... publish -u earshot:$T http://H:8082/phone "Dryer done"` | stored |
| ntfy CLI, audio file | `... publish --token $T --file /work/ding.wav http://H:8080/compat/ntfy/phone` | stored as `.wav`, bytes identical |
| Apprise Gotify | `apprise -b "Server rebooted" "gotify://H:8082/$T"`, and `apprise -t Backup -b done "gotify://H:8080/compat/gotify/$T?priority=high"` | both stored (second: downgraded) |
| gotify-cli | `gotify push --url http://H:8080/compat/gotify --token $T -t Deploy -p 5 "Release 1.1 is live"` | stored |
| curl, Pushover | `curl --form-string token=$T --form-string user=$U --form-string "message=Front door opened" --form-string title=Security --form-string priority=2 --form-string sound=siren http://H:8080/compat/pushover/1/messages.json` | stored, downgraded, `sound` ignored |
| Go `gregdel/pushover` v1.3.1 | `APIEndpoint` set to the relay, 30-character token | stored (it requires the `X-Limit-App-*` headers) |
| curl, Join | `curl -G http://H:8080/compat/join/_ah/api/messaging/v1/sendPush --data-urlencode apikey=$T --data-urlencode title=Tasker --data-urlencode "text=Battery low" --data-urlencode "say=Phone battery is low" --data-urlencode deviceId=group.all` | `say` spoken, others ignored |
| Uptime Kuma 1.23.16, ntfy | the notification dialog's Test button, server URL `http://H:8080/compat/ntfy`, topic `phone` | `phone [Uptime-Kuma]. Earshot Testing`, downgraded (priority 5; access-token auth) |
| Uptime Kuma 1.23.16, Gotify | the dialog's Test button, server URL `http://H:8080/compat/gotify` | `Uptime-Kuma. Earshot Testing`, downgraded (its default priority is 8) |

Uptime Kuma ran as `louislam/uptime-kuma:1` in Docker on the host network, driven over its socket.io API (the
same `testNotification` call the dialog's Test button makes). In the table, `$T` is the relay token, `$U` a
30-character user string and `H` the relay host. Apprise was run unpinned through `uvx`.

**Expected failure, kept as evidence:** Apprise `ntfy://` always posts to the server **root** with the topic in the JSON
body and treats every path segment as a topic, so `ntfy://H:8080/compat/ntfy/phone` cannot work (it posts to
`/` on the main port and gets `401`). This is why `EARSHOT_RELAY_COMPAT_PORT` exists.

**Verified from source only, not run:** Healthchecks.io's ntfy integration posts JSON to the URL you give it with a
Bearer token; its Gotify integration joins `message` onto your URL and sends `?token=`. The shapes fit the main-port prefixes by reading the code, but
no Healthchecks instance was run against the relay.

**Not verified:** Home Assistant's ntfy and Pushover integrations, Grafana (it has no ntfy contact point, and its
Pushover URL is fixed), Apprise `pover://` and `join://` (vendor URLs are fixed), and the Tasker app itself (the Join
route is built from the documented HTTP shape, not recorded from Tasker).

## Troubleshooting

| You see | Cause |
| --- | --- |
| `404` on a compat path | The protocol is not in `EARSHOT_RELAY_COMPAT`, or (ntfy) the topic is not in the allowlist. |
| `401` with a Pushover client | `token` is not the relay token or `EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN`. |
| Apprise `ntfy://` gets `401` | It posts to the root: use the compat port, not a path. |
| `413` / `400` on a long message | Over the 4000 UTF-16 code unit cap; shorten it. The relay never cuts text. |
| `400 delay_unsupported` | The producer set an ntfy delay; there is no scheduler. |
| `X-Earshot-Downgraded: alarm` | The producer asked for top priority; see [Priorities and the alarm channel](#priorities-and-the-alarm-channel). |
