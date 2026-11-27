# Home Assistant

Status: **tested**.

Tested on 2026-11-27: Home Assistant 2026.11.3 (`ghcr.io/home-assistant/home-assistant:stable` in Docker, onboarded and driven entirely over its HTTP API) against earshot-relay 1.1.2 (git `db161da`) run from source, writing to a disposable MinIO bucket. The `secrets.yaml` and the `configuration.yaml` `rest_command` block below were installed exactly as written; `rest_command.earshot_say` was then triggered through `POST /api/services/rest_command/earshot_say` with the automation action body below. Both a plain sentence and one containing a double quote, an apostrophe, an em dash and a percent sign arrived byte-identical in the bucket as `phone/<id>.say` objects (the `tojson` quoting held up), and each send had a receipt at `GET /v1/messages/{id}` (`status: stored`). **Not tested:** phone playback (no phone was involved in the test), TLS in front of the relay, and blueprint packaging (this recipe has no blueprint).

`secrets.yaml`:

```yaml
earshot_url: "https://<your relay host>/v1/messages"
earshot_auth: "Bearer <EARSHOT_RELAY_TOKEN>"
```

`configuration.yaml`:

```yaml
rest_command:
  earshot_say:
    url: !secret earshot_url
    method: post
    headers:
      Authorization: !secret earshot_auth
    content_type: "application/json"
    payload: '{"text": {{ message | tojson }}}'
```

Use it from an automation:

```yaml
actions:
  - action: rest_command.earshot_say
    data:
      message: "The garage door has been open for ten minutes."
```

`tojson` quotes the message safely. A `202` response means the message is stored in the bucket, not that the phone
has played it yet.
