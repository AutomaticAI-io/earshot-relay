# Apprise

Status: **tested** with Apprise 2.3.0 (`uvx apprise`) against a live relay, see `scripts/compat_clients.py` and
[docs/compat.md](../docs/compat.md). Needs `EARSHOT_RELAY_COMPAT=ntfy,gotify` on the relay.

A success means the relay stored the message in your bucket, not that the phone played it.

## ntfy:// and ntfys://: use the compat port

Apprise posts ntfy messages to the server **root** and treats every path segment as a topic, so a path prefix cannot
work. Set `EARSHOT_RELAY_COMPAT_PORT=8082`, publish that port where Apprise runs, and use the relay token as the
password or as a token:

```sh
apprise -t "Laundry" -b "washer done" "ntfy://earshot:$EARSHOT_API_TOKEN@relay-host:8082/phone"
# TLS in front of the compat port (a reverse proxy or tunnel):
apprise -b "washer done" "ntfys://$EARSHOT_API_TOKEN@relay.example.com/phone?auth=token"
```

`phone` is a channel name (or a key of `EARSHOT_RELAY_COMPAT_NTFY_TOPICS`). Apprise sends an `X-Icon` header by
default; the relay ignores it and names it in `X-Earshot-Ignored`. `priority=max` is a top priority: it is delivered
normally with `X-Earshot-Downgraded: alarm` unless `EARSHOT_RELAY_COMPAT_ALARM=1`.

## gotify://: works with or without a path prefix

```sh
# on the main port, with the compat prefix in the path
apprise -t "Backup" -b "done" "gotify://relay-host:8080/compat/gotify/$EARSHOT_API_TOKEN"
# on the compat port
apprise -b "Server rebooted" "gotify://relay-host:8082/$EARSHOT_API_TOKEN"
```

The token is the last path segment. Gotify messages go to the relay's default channel.

## Not possible

`pover://` (Pushover) and `join://` hard-code the vendor's URL and cannot be pointed at a relay.
