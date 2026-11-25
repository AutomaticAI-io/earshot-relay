# MinIO workspace setup (standalone)

This is an **operator-only** Python CLI, not part of the relay process. It uses MinIO's admin API via `mc`, and S3 via boto3. It creates one new `earshot-<workspace>` bucket, one put-only writer, and one list/get-only MinIO user per device. No changes are made to the relay or phone. A separate handoff consumer is needed; the relay does not yet read these JSON files automatically. Setup does not create QR codes or print credentials.

## Capability matrix

| Provider | Automated here | Claimed isolation | Caveats |
| --- | --- | --- | --- |
| MinIO with compatible IAM | Yes; a new bucket only | Separate writer and device readers; bucket and `channel/` prefix policies | Real tests run against the local MinIO image. Must be private, correctly administered, and reachable directly; existing buckets are not adopted. |
| AWS S3 / other IAM-policy S3 | No | Depends on the provider and operator-created IAM policies | Test actual list/get/put denials and revocation before use. This CLI does not manage them. |
| Cloudflare R2 | No | Bucket-scoped object Read & Write token only for a writer | R2's standard token model is not write-only or channel-prefix scoped. It cannot meet this isolation contract; do not represent it as equivalent. |
| Backblaze B2 | No | Depends on key capabilities and exact restriction support | Verify current provider documentation and real denials. This CLI does not manage B2. |

## Requirements and plan

Python 3.11+, `boto3` (the repo already declares it), and `mc` on `/usr/local/bin`, `/usr/bin` or `/bin`. TLS verification is required for HTTPS. HTTP is accepted only on the explicit loopback IPs `127.0.0.1` or `::1` for local testing. An explicit port must be in 1–65535. No proxy is used; inherited `SSLKEYLOGFILE` is cleared before provider TLS connections so TLS session secrets are not written to that file. Obtain an explicitly authorized MinIO admin account with the necessary bucket and IAM administration rights. Never run the CLI inside the relay container or give admin access to the relay.

Create a spec containing **no credentials**, e.g. `workspace.json`:

```json
{
  "workspace": "household",
  "bucket": "earshot-household",
  "channels": ["phone", "alarm"],
  "readers": {"speaker": ["phone"], "alert-device": ["alarm"]}
}
```

`workspace` uses lowercase letters, digits and hyphens; bucket must be exactly `earshot-<workspace>`. Channel and device names use `[a-z0-9][a-z0-9_-]{0,31}`; at least one channel must be non-`alarm` so the relay can select a default, and each device grant must be a nonempty subset of requested channels. A workspace owns its bucket; no shared-bucket prefix mapping or existing-bucket adoption is attempted. The tool creates no channel marker objects.

Run from the repository root (or use the absolute script path):

```sh
python tools/provision/minio_setup.py --spec /private/workspace.json
```

This is an offline plan. It validates and prints the normalized spec; it does not read credentials, create a state directory, run `mc`, or contact a provider. There is no hidden auto-apply.

## Apply and handoff

Put an admin credential JSON with exactly `access_key` and `secret_key` in an operator-owned, regular, single-link file with mode 0600 or stricter. Create it using your secret manager's protected file delivery, not shell arguments, environment variables, terminal output, a QR code or an agent prompt. Use a private parent directory. The tool does not fall back to AWS profiles, metadata, `mc` aliases, proxies or environment credentials.

```sh
python tools/provision/minio_setup.py --spec /private/workspace.json --apply \
  --endpoint https://minio.example.org --admin-file /private/admin.json \
  --state-dir /private/earshot-household
```

The state directory is created mode 0700 or must already be owned by the operator and mode 0700 or stricter. `state.json` is a protected recovery journal **containing all generated writer and reader secrets**; keep it safe, back it up securely, and do not put it in the checkout or share it with the relay or devices. Temporary `mc` admin configuration lives under that private directory while the command runs. An abrupt kill or crash can leave `.provider-*` directories containing admin credentials; after reviewing the failed run remove only that run's private temporary directory. Normal completion removes it.

On success the tool writes `writer.json` and `reader-<device>.json`, each mode 0600. Each is version 1 JSON with `provider`, `workspace`, `endpoint`, `region`, `bucket`, `role`, `device`, `channels`, `access_key`, and `secret_key`. No admin credentials are in a handoff. Transfer the writer handoff privately only to the operator configuring the relay, and each reader handoff privately only to its intended device/pairing operator. For the current relay, configure `EARSHOT_S3_ENDPOINT`, `EARSHOT_BUCKET`, `EARSHOT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `EARSHOT_RELAY_CHANNELS` from the writer file using your secret manager; do not source JSON as shell. Reader handoff is **not** a currently implemented automatic pairing flow. Do not paste keys into QR codes, prompts, logs, URLs, tickets or chat.

Rerunning the same spec, endpoint, admin file and state directory verifies existing binding, policies and users, preserves objects and credentials, and writes the same handoffs. A changed spec or endpoint is refused rather than reconciling permissions. A disabled, deleted, modified, or additionally privileged identity is refused rather than silently recreated/re-enabled. The tool cannot prove absence of external access granted to other identities, provider-side global plugins, or later administrative drift. Use a trusted, privately managed MinIO deployment and monitor policy changes.

## Failure and revocation

A nonzero exit means **not complete**, even if some provider operations succeeded. Check the protected journal's `bucket_phase`, `progress`, and identity `*_attempted`/`*_confirmed` flags; attempted is not proof of success. Handoff files from an earlier successful run may still exist and must not be treated as evidence of a later failed run. The tool never deletes a bucket, object, user or policy. Retry only with the same spec/endpoint/state after inspecting provider state. It can resume a confirmed bound bucket and operations whose outcomes are safely inspectable.

If `bucket_phase` is `creating` or `created` after interruption, bucket identity/continuity is unknown: the name may refer to a replacement, even if a marker exists. The CLI refuses to bind or issue credentials; it does not automatically adopt or repair it. An authorized operator must investigate bucket ownership, data and audit history. Do not edit the journal or copy a marker to force adoption. Keep the existing bucket and data untouched; if continuity cannot be established, use a new, explicitly authorized workspace/bucket name and fresh state, or use the protected manual path below. Reconcile any provider resources separately with the owner; never delete them merely to make retry succeed.

An attempted but unconfirmed user might have been created and subsequently revoked or modified. The CLI refuses automatic recovery whether that user is absent or still exists; in particular, it never recreates the same access key. An authorized operator should investigate provider audit history and revocation, preserve the journal, and arrange a fresh identity and secret through a deliberate manual rotation/reprovisioning procedure; no CLI command automatically resolves this uncertainty. The same applies to a confirmed user or attachment that is missing. Do not clear journal flags or restore revoked credentials by hand. For a foreign existing bucket, stop and follow the manual path below.

**Remove local secrets is not revocation.** Removing a handoff or uninstalling the app does not disable a key at MinIO. To revoke a device, an authorized administrator must disable or delete that exact MinIO user (and its child service accounts, if any), and verify the old key is denied. `mc admin user disable <operator-alias> <device-access-key>` is a provider-side example; run only in the operator's explicitly configured admin environment. The access key identifies the user and should be handled privately. Then remove that device's handoff and local copies as appropriate. Do not delete the whole journal casually: it contains recovery information and the other identities. Reapply refuses a disabled or removed confirmed user and does not re-enable it. Rotation/replacement is a separate manual process, not implemented here.

## Existing bucket / other providers (manual, protected)

Do not point this automation at an existing bucket. An operator can instead inventory ownership, bucket policy/anonymous access, existing IAM users and service accounts; choose a private bucket and explicit authorized `channel/` prefixes; create a separate writer restricted to `PutObject` and per-device identities restricted to `ListBucket` with an `s3:prefix` condition and `GetObject` on each authorized prefix. Verify every allowed and denied request on that provider, including anonymous access, neighboring prefixes, another bucket, admin calls and revocation. Never remove or replace an existing bucket policy, data or identities merely to make setup pass. If the provider cannot enforce the boundaries, do not claim it meets the scoped-access contract. Deliver secrets through protected local files or a secret manager, not QR codes, prompts or chat. This is a manual procedure, not another automated provider.

## Test

Unit plan checks do not require Docker. The integration tests start disposable uniquely named MinIO containers on an OS-assigned `127.0.0.1` port using the already-present `minio/minio:latest` image (`--pull=never`), generated credentials, an isolated `mc` config and the local Docker socket. They remove only their own containers; no existing MinIO endpoint, host credential or alias is used. The TLS regression also needs `openssl` and runs against a loopback server with a generated test certificate. Override `TMPDIR` and pytest `--basetemp` to a private scratch location, and do not run this on a host where using its local Docker daemon is unauthorized.

```sh
export EARSHOT_MINIO_TESTS=1 AWS_EC2_METADATA_DISABLED=true NO_PROXY='*'
pytest -q -p no:cacheprovider --basetemp /private/scratch/ears65-test \
  tools/provision/test_minio_setup.py tools/provision/test_extra.py \
  tools/provision/test_revocation.py tools/provision/test_review_regressions.py
```

The test command requires an unused basetemp path; pytest removes that path at startup. It retains private fixture files (including throwaway credentials) under that path after the run, so remove only that test run's scratch directory when finished. Without `EARSHOT_MINIO_TESTS=1`, Docker tests skip rather than using any alternate provider.
