# EARS-64 local verification checkpoint

**Local checkpoint only; not pushed, merged to main, deployed or rolled out.** This
supersedes the earlier scaffold checkpoint that denied all scoped ingress. The
verified implementation is in the existing relay worktree, not the Earshot Android
checkout:

- Worktree: `/home/delorenj/code/earshot-relay/.worktrees/ears-64-scoped-access`
- Branch: `ears-64-scoped-access`
- Tested code commit: `9cfbf042f5d18725f1a0d7a650991e50853063d8`
- Original base: `4206b81bdc789d7f166a1c4580c681d8292e9768`
- Frozen test tree: `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-parent-final-iqUuuq/tree`

The documentation-only commit containing this checkpoint is a child of the tested
code commit. To recover the code and documentation locally, inspect the worktree
and branch with `git status`, `git log` and `git show`; do not switch, reset, delete,
push or merge either checkout. The [control schema](../../schemas/control-v1.schema.json)
is present in this local checkpoint, not on main as a result of this work. Consumers
must use the schema and matching runtime validator from this candidate, not assume
that the running relay or main already provides it.

## Acceptance matrix

These are local implementation and fixture results, not claims about a deployed
service or storage provider.

| Criterion | Local result and evidence |
| --- | --- |
| **AC1 — versioned one-workspace control contract** | `control.v1` defines workspace, channel, agent and device identifiers and binds the one workspace to the configured endpoint, bucket and region. Foreign workspace selectors, reserved `replies`, invalid names and ungranted alarm access are refused. Channel names and grants do not create buckets. `tests/test_control.py` and `tests/test_scoped_adapters.py`; full-suite log below. |
| **AC2 — owner administration and scoped principals** | Local owner-authenticated init/create/revoke uses protected owner input and exclusive token output; token digests, not raw tokens, are stored. Scoped sends and permitted receipts are constrained by principal and channel. Unknown, expired and revoked credentials fail closed; scoped users do not get owner status, doctor, bucket listing or membership administration. Fixture CLI, collision, revocation, redaction and receipt tests are in `tests/test_scoped_admin.py`, `tests/test_control.py` and `tests/test_scoped_service.py`. See [scoped access](../scoped-access.md) for protected-file usage and proxy logging limits. |
| **AC3 — all applicable ingress and owner compatibility** | Native text/audio/replay/receipts, HTTP and stdio MCP, and prefixed and root ntfy, Gotify, Pushover and Join are exercised with scoped identity. Alarm requires the explicit grant; resolved destinations are authorized before writes and retries. HTTP MCP forwards each request's own bearer; legacy compat tokens remain distinct broad send-only principals. Owner behavior is retained, subject to reserved/invalid channel checks. `tests/test_scoped_adapters.py`, `tests/test_compat.py` and `tests/test_mcp.py`; see the legacy migration restriction below. |
| **AC4 — isolation and real relay regressions** | The frozen candidate's full existing suite finished **713 passed, 7 skipped**. It covers anonymous and foreign access, revoked/expired credentials, receipts, concurrency and legacy compatibility using isolated local fixtures and dummy credentials. Six skipped Docker provider cases and the EARS-48 namespace-only case are not provider or device proof. |
| **AC5 — independent specification/security review and evidence** | The pre-implementation specification review was **PASS_WITH_REQUIRED_CORRECTIONS**, which were adopted; its 280-test baseline and two probes are reported without retained logs. Security review passed on `bac6959`, and the subsequent service/store/test delta was independently reviewed **PASS** on exact code commit `9cfbf04`. Review scope, execution results and local logs are listed below. |

## Legacy migration restriction

Pre-scoping history is retained as **owner-only**; scoped and compatibility
principals must not see or replay it. A compatibility retry whose old idempotency
key is ambiguous with retained owner history returns **409
`legacy_retry_ambiguous`** rather than risk a duplicate or disclose history. A
compatibility coalescing request can also receive 409 while an owner pending row
has the same channel and tag, even when the request is fresh: the old row may lack
an alias after a crash. The conservative refusal lasts until the relevant pending
work or retained ambiguity is resolved; it is not a generic retry-success promise.
The real-base crash-gap fixture showed one original pending message flushed, no
second write on the refused retry, and a fresh send/replay succeeding after flush.
New pending and alias changes commit or roll back together.

## Evidence and review provenance

Protected scratch logs are local and may contain fixture details; do not treat
them as published reports. Scratch may be pruned, so preserve any required evidence
through an authorized handoff.

- Parent full run on the frozen `9cfbf04` tree: `full-suite-rerun.log` in
  `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-parent-final-iqUuuq/`,
  **713 passed, 7 skipped in 95.91 s**; generated OpenAPI check exit 0,
  `openapi.log` in the same directory. The latter is empty on success.
- The initial `full-suite.log` had **1 failure and 2 setup errors** because the
  harness blocked the static documented-admin shell and base `git archive`
  fixture. The parent copied the final reviewer's corrected allowlist guard,
  changed its scratch root, and set `GIT_DIR` for the read-only base archive;
  **no code was changed between runs**. The rerun, not the initial run, is the
  final suite result.
- Documentation command verification: executing the Bash block below from the
  updated checkpoint returned 0 with **713 passed, 7 skipped in 101.60 s** and
  OpenAPI check exit 0. Output:
  `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-doc-command-U7OKNj/reproduce.log`.
- Independent specification review `sa0-9d24b8b1`: **PASS_WITH_REQUIRED_CORRECTIONS**
  before implementation; corrections adopted. The reported baseline was 280 tests
  and two probes; no review logs were retained.
- Independent security review at `bac6959`: **PASS**, including **482 focused
  tests**, 25 targeted cases, real local admin credential-overlap and lock probes,
  revocation serialization, and actual base-relay MCP compatibility. Logs:
  `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-auth-rereview-3IEKf1rz/`
  (`security-suite.log`, `targeted.log`, `prior-collision.log`, `prior-legacy.log`,
  `cli-lock.log`, `actual-mcp.log`). The provider operation in the lock probe was
  a controlled fixture, not a live storage service.
- After `bac6959`, code changes were confined to service, store and their tests.
  The core worker reported **29 focused tests** on `9cfbf04`. Independent reviewer
  `sa1-92fbc7ca` returned **PASS** on that exact commit: **132 focused tests**,
  **5 transaction/isolation probes**, and a real-base crash-gap probe. Logs:
  `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-final-review-ItHbv7/`
  (`focused.log`, `transactions.log`, `independent.log`).

## Reproduce locally

The following Bash commands create a fresh protected scratch tree without touching either
checkout or the earlier logs. They require the cited local repository, venv and
retained final guard. Inspect the guard first. Its Python socket checks allow only
registered ephemeral loopback listeners and block external DNS; it allowlists
specific subprocesses, including fixture scripts, three static admin commands,
`ffmpeg`, Python and the read-only base archive. This is **not OS-level network
or subprocess isolation**, and the guard is not a general sandbox for arbitrary
programs. Do not replace fixture endpoints with live targets or enable provider
Docker tests.

```sh
set -euo pipefail
umask 077
BASE=/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch
REPO=/home/delorenj/code/earshot-relay
GUARD="$BASE/ears64-parent-final-iqUuuq/safe-guard/sitecustomize.py"
PY="$REPO/.venv/bin/python"
R=$(mktemp -d "$BASE/ears64-repro-XXXXXX")
mkdir -m 700 "$R/tree" "$R/home" "$R/tmp" "$R/safe-guard"
git -C "$REPO" archive 9cfbf042f5d18725f1a0d7a650991e50853063d8 | tar -x -C "$R/tree"
"$PY" -c 'from pathlib import Path; import sys; src, root = map(Path, sys.argv[1:]); old = "/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch/ears64-parent-final-iqUuuq"; text = src.read_text(); assert old in text; (root / "safe-guard/sitecustomize.py").write_text(text.replace(old, str(root)))' "$GUARD" "$R"
cd "$R/tree"
env -i HOME="$R/home" TMPDIR="$R/tmp" PATH="$REPO/.venv/bin:/usr/bin:/bin" \
  GIT_DIR="$REPO/.git" PYTHONPATH="$R/safe-guard:$R/tree/src:$R/tree" \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  AWS_ACCESS_KEY_ID=fixture AWS_SECRET_ACCESS_KEY=fixture AWS_DEFAULT_REGION=us-east-1 \
  AWS_EC2_METADATA_DISABLED=true NO_PROXY=127.0.0.1,localhost \
  "$PY" -m pytest -q -p no:cacheprovider --basetemp "$R/pytest" tests
env -i HOME="$R/home" TMPDIR="$R/tmp" PATH="$REPO/.venv/bin:/usr/bin:/bin" \
  PYTHONPATH="$R/safe-guard:$R/tree/src:$R/tree" PYTHONDONTWRITEBYTECODE=1 \
  AWS_EC2_METADATA_DISABLED=true "$PY" scripts/gen_openapi.py --check
printf 'Local scratch evidence: %s\n' "$R"
```

## Limits

OS `unshare` was denied (`Operation not permitted`); no private network namespace
was available for the skipped EARS-48 test. The tests used clean environments,
dummy credentials, private scratch, fixture writers and loopback services; no live
target fallback was used. Moto and in-memory writes do **not** establish provider
IAM, bucket privacy, actual phone playback, human hearing, deployment or rollout.
No provisioning tools were edited, and no bucket, provider, device, live relay
configuration or credentials were changed by this checkpoint.
