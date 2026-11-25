# EARS-64 local checkpoint — incomplete, not for deployment

This checkpoint is a work-in-progress, **not scoped send access**. An authenticated
scoped token is denied on native HTTP and MCP, and compatibility ingress does not
accept it. Do not deploy or advertise this checkpoint as implementing EARS-64.

The checked-in `schemas/control-v1.schema.json` and its runtime validator define
workspace, channel, agent and device identifiers. A control database binds one
workspace to the relay's configured S3 endpoint, bucket and region. The local
admin command can initialize that database and issue or revoke individual
high-entropy credentials, with only their SHA-256 hashes stored. The credentials
are created once in an exclusive 0600 file under a 0700 directory. Supply the
owner credential through `--owner-fd` or non-echoing terminal input, never as an
argument. The control database directory must be owned by the relay user and
inaccessible to other users. Same-user processes and root are trusted: filesystem
modes are not a security boundary against them. The database's binding currently
uses a hash of the owner token and must not be treated as supporting owner-token
rotation until that workflow is designed.

`replies` is refused in relay channel configuration and at the service's channel
resolver, including owner sends. Existing legacy compatibility credentials remain
unchanged and broad on compatibility paths. No bucket privacy or IAM guarantee is
made by this application control database. No provisioning, phone pairing, device
authentication, deployment or provider changes are included.

## Remaining acceptance work

- End-to-end scoped authorization on native and compatibility paths; HTTP MCP
  caller isolation and stdio capability discovery.
- Workspace/principal provenance for every message, pending/failed and replay
  state, isolation before receipt pagination, and safe historical migration.
- Reauthorization after TTS and during delayed flush, and an interprocess lock
  covering revocation versus actual PUT initiation.
- Receipt, replay, alarm, expiry, cross-workspace and adapter security matrices;
  independent code review and fixes.
- Strict fail-closed database and file protections including crash consistency,
  WAL/journal behavior and token-output failure cleanup under adversarial tests.
- Safe administrative documentation, credential redaction on all malformed input
  and the reverse-proxy query-token logging warning.

## Local evidence

Base: `4206b81bdc789d7f166a1c4580c681d8292e9768`.
Tests were run in a clean environment with fixture-only dummy credentials, a
Python non-loopback connect guard, a private scratch HOME and TMPDIR, disabled
AWS metadata discovery and pytest plugin autoload, and no provider Docker tests.
The guard permits loopback; it is not an OS network namespace. Namespace creation
was attempted and refused (`Operation not permitted`); no live service was used.

- `python -m pytest -q -p no:cacheprovider --basetemp=<scratch>/tmp/full tests`:
  **565 passed, 7 skipped**. Six skips were the explicitly disabled provider
  Docker tests; one was the separate EARS-48 private-namespace-only test.
- `python scripts/gen_openapi.py --check`: exit 0.
- `tests/test_control.py`: 5 passed, including a deliberate scoped-request denial.

These tests do not establish the missing security properties above or provider
IAM enforcement. The unredacted local test logs contain fixture information and
remain only under the active-profile protected scratch directory.
