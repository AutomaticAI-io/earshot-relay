# Uptime Kuma

Status: **tested** with Uptime Kuma 1.23.16 (`louislam/uptime-kuma:1`), by calling the same `testNotification` socket
event the dialog's Test button uses. Version 2 was not run. Needs `EARSHOT_RELAY_COMPAT=ntfy` (or `gotify`) on the
relay. A success means stored, not played.

## ntfy provider

Settings, Notifications, Setup Notification, type **Ntfy**:

| Field | Value |
| --- | --- |
| Server URL | `http://relay-host:8080/compat/ntfy` (the prefix works here) |
| Topic | `phone` (a channel, or a key of `EARSHOT_RELAY_COMPAT_NTFY_TOPICS`) |
| Authentication Method | Access Token |
| Access Token | the relay token (`EARSHOT_RELAY_TOKEN`) |

Test result: the stored text is `phone [Uptime-Kuma]. Earshot Testing` (title, then message). Kuma's DOWN priority is 5, which is a top priority: without `EARSHOT_RELAY_COMPAT_ALARM=1` it is
delivered normally with `X-Earshot-Downgraded: alarm`. **With** the alarm switch on, every DOWN plays the loud alarm
channel (a siren; not an emergency service, and Total-silence Do Not Disturb can still block it). Lower the priority
in the provider if that is not what you want. Kuma's `tags` are ignored.

## Gotify provider

| Field | Value |
| --- | --- |
| Server URL | `http://relay-host:8080/compat/gotify` |
| Application Token | the relay token |
| Priority | Kuma defaults to 8, a top priority (same alarm behavior as above); use 5 for a normal message |

Gotify sends go to the relay's default channel.

## Healthchecks.io

Its ntfy and Gotify integrations send the same shapes (JSON with a Bearer token for ntfy; `?token=` for Gotify), so
they should work against the main-port prefixes. This was checked in Healthchecks' source only; no instance was run
against a relay, so no example is provided.
