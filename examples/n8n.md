# n8n

Status: **untested** (written from the API contract; no n8n instance was run against it).

Add an **HTTP Request** node:

| Field | Value |
| --- | --- |
| Method | `POST` |
| URL | `https://<your relay host>/v1/messages` |
| Authentication | Generic credential type, **Header Auth**: name `Authorization`, value `Bearer <EARSHOT_RELAY_TOKEN>` |
| Send Body | on, Body Content Type `JSON` |
| Specify Body | Using JSON: `{"text": "{{ $json.message }}"}` |

Success is HTTP `202` with `{"id": "msg_...", "status": "stored", ...}`. To read the receipt, add a second HTTP Request
node: `GET https://<your relay host>/v1/messages/{{ $json.id }}?wait=30` with the same header. A `finished` status means
the phone finished playing it; it is not proof that a person heard it.

Keep the token in an n8n credential, never in the workflow text.
