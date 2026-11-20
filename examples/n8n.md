# n8n

Status: **untested** (written from the API contract; no n8n instance was run against it).

Add an **HTTP Request** node:

| Field | Value |
| --- | --- |
| Method | `POST` |
| URL | `https://<your relay host>/v1/messages` |
| Authentication | Generic credential type, **Header Auth**: name `Authorization`, value `Bearer <EARSHOT_RELAY_TOKEN>` |
| Send Body | on, Body Content Type `JSON` |
| Specify Body | Using JSON: `{{ JSON.stringify({ text: $json.message }) }}` |

Build the body with `JSON.stringify`, never by pasting `{{ $json.message }}` inside quotes: a message with a double
quote, a backslash or a newline (agent and CI text often has them) would make the JSON invalid. To make retries safe,
add a header `Idempotency-Key` with a value that is stable for one event (for example `{{ $execution.id }}`, which
stays the same while the node's Retry On Fail repeats the request): a repeat of the same key within 24 hours returns
the first result instead of playing twice.

Success is HTTP `202` with `{"id": "msg_...", "status": "stored", ...}`. To read the receipt, add a second HTTP Request
node: `GET https://<your relay host>/v1/messages/{{ $json.id }}?wait=30` with the same header. A `finished` status means
the phone finished playing it; it is not proof that a person heard it. A `202` means the message is stored in the
bucket, not that it played.

Keep the token in an n8n credential, never in the workflow text.
