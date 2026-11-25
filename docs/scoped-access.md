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

Native HTTP uses `Authorization: Bearer <token>`. A scoped principal can read
receipts only when its receipt grant is enabled, and only for its own messages
on currently authorized channels. Listing, `latest`, long-polling, replay and
pagination apply the same principal boundary. Status and doctor remain owner-only.
`GET /v1/capabilities` returns only the caller's sendable channels and authorized
default, without bucket, device or beacon information. It is not a liveness or
playback check.

HTTP MCP forwards the caller's bearer credential on each internal request. The
stdio MCP client discovers alarm availability with `/v1/capabilities`; its
configured `EARSHOT_API_TOKEN` remains the caller identity. The alarm tool is
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
