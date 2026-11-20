# Home Assistant

Status: **untested** (written from the API contract; no Home Assistant instance was run against it).

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
action:
  - service: rest_command.earshot_say
    data:
      message: "The garage door has been open for ten minutes."
```

`tojson` quotes the message safely. A `202` response means the message is in the bucket.
