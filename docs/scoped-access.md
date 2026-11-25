# Scoped relay access

A relay can bind one control workspace to its configured endpoint, bucket and
region. An owner-issued principal can send only to its granted channels;
`alarm` additionally requires the explicit alarm grant. `replies` is reserved
for every caller. No request can choose a different workspace. The optional
`X-Earshot-Workspace` or `X-Earshot-Workspace-Id` header, `workspace` or
`workspace_id` query parameter, or native/compat body field confirms the configured workspace;
a mismatch is refused. HTTP MCP rejects foreign workspace selectors in its JSON body;
workspace fields are not supported tool arguments. The sender still needs a grant for the resolved channel,
including the relay's default channel if the request omits one.

Native HTTP uses `Authorization: Bearer <credential>`. A scoped principal can read
receipts only when its receipt grant is enabled, and only for its own messages
on currently authorized channels. Listing, `latest`, long-polling, replay and
pagination apply the same principal boundary. Status and doctor remain owner-only.
`GET /v1/capabilities` returns only the caller's sendable channels and authorized
default, without bucket, device or beacon information. It is not a liveness or
playback check.

## Local owner administration

Prepare a protected relay configuration as described in [Configuration](configuration.md),
including the existing writer endpoint, bucket, region, channel list and owner token.
Use the same configuration for the admin commands and relay; secrets can be supplied
with the documented `_FILE` variables, not command arguments. Set
`EARSHOT_RELAY_CONTROL_DB` to a new database path in a directory owned by the relay
user with mode `0700`. Its ancestors must not be writable by other users (unless
protected by an earlier private directory); do not use a shared temporary directory.

Write a contract matching [`control-v1.schema.json`](../schemas/control-v1.schema.json)
into a protected file. This example is illustrative: replace the endpoint, bucket,
region and channel with the configured writer values and use an agent ID of your choice.
The workspace binding must match the relay configuration exactly.

```json
{
  "version": "control.v1",
  "workspace": {"id": "home", "endpoint": "https://storage.example.invalid", "bucket": "earshot", "region": "us-east-1"},
  "channels": [{"id": "phone", "name": "phone", "workspace_id": "home"}],
  "agents": [{"id": "assistant", "workspace_id": "home"}],
  "devices": []
}
```

Run these commands as the relay's trusted user. `$PRIVATE` is the existing `0700`
directory, `$OWNER_FILE` is the protected file holding the configured owner token,
and `$PRINCIPAL_ID` is the non-secret ID printed by `admin create`. Open a fresh
file descriptor for each command; the credential is read from it, never from argv.
The output file must not exist: creation is exclusive, mode `0600`, and its parent
must be a protected directory. Deliver the issued credential privately to the agent.

```sh
export EARSHOT_RELAY_CONTROL_DB="$PRIVATE/control.db"
earshot-relay admin init --contract "$PRIVATE/contract.json" --owner-fd 3 3<"$OWNER_FILE"
earshot-relay admin create --agent assistant --channel phone --receipts \
  --output "$PRIVATE/assistant.token" --owner-fd 3 3<"$OWNER_FILE"
earshot-relay admin revoke "$PRINCIPAL_ID" --owner-fd 3 3<"$OWNER_FILE"
```

Repeat `--channel` for additional grants. Granting `alarm` requires both
`--channel alarm` and `--alarm`; `--receipts` is optional. After revocation, the
issued token is no longer accepted. Restrict access to the control database,
contract, configuration, owner credential and token files; anyone with the same OS
user's access can change the local authorization boundary.

HTTP MCP forwards the caller's bearer credential on each internal request. The
stdio MCP client discovers alarm availability with `/v1/capabilities`. It falls back to
the relay's 1.1.2 owner-only status endpoint only when capabilities returns 404
and that authenticated response matches the known status schema. Denied or
unknown capability responses do not trigger this fallback. Its configured
`EARSHOT_API_TOKEN` remains the caller identity. The alarm tool is
listed only when the caller can send to alarm, and the server still checks each
call. A scoped `status` tool call is refused.

The ntfy, Gotify, Pushover and Join compatibility endpoints accept scoped
credentials in their normal protocol positions, on both the main and root
listeners. ntfy topics resolve to channels before authorization. For a high
priority request, promotion requires the alarm grant and the relay's compatibility
alarm setting. A downgrade is allowed only if the original normal channel is
also granted. Direct alarm topics require an alarm grant. Legacy
`EARSHOT_RELAY_COMPAT_TOKEN` and `EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN` remain
broad **send-only** compatibility credentials, not owner or scoped credentials;
they cannot access native HTTP, MCP or receipts. Owner tokens retain owner access.

Compatibility protocols may place credentials in query strings or form bodies.
The relay access log avoids raw URLs and bodies, but reverse proxies, tracing,
client logs and shell history can still record them. Configure those systems to
redact query strings and credentials; prefer headers where the protocol permits.
Never place tokens in spoken content, field names, filenames or error reports.

These checks are an application authorization boundary, not storage IAM. Anyone
with the writer key or same-user access to relay files can bypass it. A relay
still uses its configured writer and bucket; this feature does not provision
bucket policies, phone access or provider credentials. Provisioning, phone
pairing, external verification and deployment are outside these adapter changes.
