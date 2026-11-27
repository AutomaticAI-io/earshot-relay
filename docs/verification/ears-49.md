# EARS-49 verification: Hermes recipe publication candidate

Date: 2026-11-27. Candidate branch `ears-49-hermes-recipe`, worktree
`/home/delorenj/code/earshot-relay/.worktrees/ears-49-hermes-recipe`, base `96c1afc` (main, clean at start).
All fixture credentials below are synthetic and were deleted after the run. No live profile, phone, bucket
policy or board was touched. **Scope note (corrected 2026-11-27 after the post-landing audit):** the statement
"no push, tag, workflow dispatch or release was touched" describes the initial pre-release fixture/candidate
phase (AC1–AC5 evidence below) only. The later release phase did push `main`, create/move tag `v1.1.3`, run
the image workflow and dispatch `publish-mcp.yml` exactly once; see "Release evidence (2026-11-27)" and
"Post-landing audit and process record (2026-11-27)" for that chronology, including one process deviation
recorded there without retroactive approval.

## Environment

- Hermes `v0.23.4+390.g07e5bbf` (2026.9.24), git install `07e5bbfc854a` (`hermes --version`, captured
  2026-11-27).
- Relay under test: candidate worktree, served by `uv run earshot-relay serve` on `127.0.0.1:18249` with a
  synthetic `EARSHOT_RELAY_TOKEN`, channels default (`phone` only, alarm disabled).
- Bucket: disposable MinIO container (`minio/minio`, `127.0.0.1:19399`, synthetic root credentials), bucket
  `ears49-fixture`, removed after the run. Relay `doctor` reported `ok: true` with `write.ok: true` before the
  MCP test; the doctor's `s3_endpoint` check correctly flagged plain HTTP as disposable-only.
- Fixture processes ran with `env -i` (no inherited `EARSHOT_*`, `AWS_*`, `VIRTUAL_ENV` or `PYTHONPATH`);
  TMPDIR pointed at the active-profile scratch.

## AC1 — isolated profile, authenticated HTTP MCP, tool discovery

Disposable profile under the active-profile scratch (`HERMES_HOME` override; nothing live was read or written).
Its 0600 `.env` held only the synthetic `EARSHOT_API_TOKEN`; `config.yaml` was written by `hermes config set`
and held only the placeholder:

```yaml
mcp_servers:
  earshot:
    url: http://127.0.0.1:18249/mcp
    headers:
      Authorization: Bearer ${EARSHOT_API_TOKEN}
```

`hermes mcp test earshot` (exit 0, verbatim output):

```text
Testing 'earshot'...
  Transport: HTTP → http://127.0.0.1:18249/mcp
    Authorization: *** (redacted by Hermes)
  ✓ Connected (5556ms)
  ✓ Tools discovered: 4
    say       Send a short message to be spoken aloud on the user's p...
    receipt   Did the phone play it? Returns the device-playback rece...
    recent    List recent messages sent through this relay with their...
    status    Relay status: sendable channels, speech modes, and the ...
```

Four tools — `say`, `receipt`, `recent`, `status` — with `alarm` correctly absent (channel list `phone` only).
Installed-client names follow the installed Hermes naming rule `mcp__<server>__<tool>`
(`tools/mcp_tool_schema.py`, `MCP_TOOL_NAME_PREFIX = "mcp__"`): `mcp__earshot__say`, `mcp__earshot__receipt`,
`mcp__earshot__recent`, `mcp__earshot__status`.

Negative control, same profile shape with a wrong synthetic token: exit 1, HTTP 401 (`unauthorized`) on both
Streamable HTTP and SSE probes — the bearer token is genuinely enforced. The relay's own log shows the same
sequence: 200s for the correct token, 401s for the wrong one. No message was sent by either test.

Limit: the fixture proved auth and discovery over loopback HTTP; the recipe documents HTTPS for real relays.
No phone playback was involved or claimed.

## AC2 — hook preservation

`examples/hermes/earshot-say.sh` is byte-identical to the EARS-48 tested revision: SHA-256
`fa730115f8befe3092a0594cea2639d207926e5dd7303fbc2dbeded32591aceb` before and after this work. Executable
behavior unchanged, so no rerun of the device verification is triggered. `tests/test_hermes_hook.py` passes;
its private-namespace acceptance test skips by design outside the EARS-48 harness and inherits the EARS-48
run for these unchanged bytes. The recipe's Path B section carries the privacy warning beside the opt-in
setup, the before-delivery (`post_llm_call`) semantics, and the MCP-plus-hook duplication warning.

## AC3 — SKILL.md layout, install and removal

Layout checked against the installed Hermes rules (`hermes v0.23.4+390.g07e5bbf`):

- Direct-URL installs are claimed by `UrlSource` for bare HTTP(S) URLs ending in `.md`
  (`tools/skills_hub_sources.py`); the skill name must match `^[a-z][a-z0-9_-]*$` (`earshot` qualifies) and
  come from frontmatter `name:`; `description` is required for publication. `skills tap` defaults to
  GitHub repos with skills under `skills/`; this file installs by direct URL instead.
- The guarded fetch blocks private/loopback URLs by default (SSRF guard, `tools/url_safety.py`). The
  documented toggle `HERMES_ALLOW_PRIVATE_URLS` was set only in the disposable profile's `.env` for the local
  check and removed with it.

Install and removal, real `hermes skills` commands in the disposable profile, SKILL.md served from the
candidate worktree over `127.0.0.1:18349`:

```text
$ hermes skills install http://127.0.0.1:18349/SKILL.md --yes
Fetching: http://127.0.0.1:18349/SKILL.md
Scan: earshot (http://127.0.0.1:18349/SKILL.md/community)  Verdict: SAFE
Decision: ALLOWED — Allowed (community source, safe verdict)
Installed: earshot
Files: SKILL.md

$ hermes skills list → earshot | url | community | enabled

$ hermes skills uninstall earshot --yes
Uninstalled 'earshot' from earshot
```

`skills/earshot` was confirmed gone after uninstall (exit 0). No live profile was installed into or altered.
That first run installed the pre-correction bytes (SHA-256 `bdf3c11df2d12f146ecb945b390049f015dc6529257cd8dfc4b6dd676ee6e62c`);
the corrected final bytes (SHA-256 `0e5a41252bd56582ef927f14943b6d7191d0a2adc8244accf77676654b232f89`) were
installed and removed in a fresh disposable profile afterward — see the post-review corrections section below.

## AC5 — metadata

`server.json` description is exactly `Make Hermes and other agents speak on your Android phone. Own S3 bucket,
receipts, opt-in alarm` — 95 characters, inside the registry's 100 (`tests/test_registry_files.py` enforces
it). The Claude Code plugin metadata adds `hermes` and `hermes-agent` keywords; the file still describes a
Claude Code plugin and makes no Hermes-plugin claim.

## Tests

At candidate content, run with a scrubbed environment:

```text
$ uv run pytest -q
718 passed, 7 skipped in 103.69s
```

Skips are the opt-in Docker provider tests (`EARSHOT_COMPAT_PROVIDERS=1`) and the EARS-48
private-namespace acceptance test above. Targeted files also ran separately:
`tests/test_registry_files.py tests/test_hermes_hook.py tests/test_examples.py tests/test_mcp.py` →
65 passed, 1 skipped in 48.72s. `scripts/gen_openapi.py --check` passes; `openapi.yaml`'s only diff is the
version line. No test embeds a credential (`test_registry_files.py` enforces `isSecret` and no default).

## Release mechanics discovered during this run

Bumping to 1.1.3 exposed a real fail-closed pin: `_legacy_capabilities` in `src/earshot_relay/mcp_server.py`
accepted only `version == "1.1.2"` in a legacy relay's status, so two scoped-adapter tests failed against a
1.1.3 build (`test_mcp_capabilities_legacy_status_fallback`,
`test_stdio_owner_legacy_relay_capabilities`). The 1.1.3 status schema is unchanged from 1.1.2, so the release
candidate widens the pin to exactly `{"1.1.2", "1.1.3"}` — unknown versions still fail closed. This is the
only source change in the candidate and is called out in CHANGELOG.md.

## AC mapping

| AC | Disposition |
| --- | --- |
| 1 | **PASS:** README Hermes subsection links the self-contained docs/hermes.md and examples/hermes/; real isolated `hermes mcp test earshot` discovered `say`, `receipt`, `recent`, `status` with alarm disabled, plus a 401 negative control; tool names, `${EARSHOT_API_TOKEN}` placeholder, prerequisites and tested Hermes version recorded; no private-doc dependency. |
| 2 | **PASS:** hook bytes unchanged (SHA-256 above); warning beside opt-in; before-delivery and duplication notes; hook tests pass; no executable change, so no rerun triggered. |
| 3 | **PASS after B1 correction:** short SKILL.md (say/receipt guidance); layout validated against installed rules; real install and removal in a disposable profile repeated on the final bytes; no live profile touched. See the corrections section. |
| 4 | **PASS:** examples/README.md and docs/hermes.md agree with the EARS-48 dated evidence; only exercised cases are Tested; Desktop stays Untested; stored vs finished and no human-heard/locked-screen/Autoplay claims retained. |
| 5 | **PASS:** exact 95-character description; plugin keywords added without a Hermes-plugin claim; `test_registry_files.py` plus the affected recipe/hook checks pass; no embedded credentials. |
| 6 | **PASS — RELEASED.** Docs-only updates shipped with the single relay 1.1.3 release; tag `v1.1.3`; tag image run succeeded; `publish-mcp.yml` dispatched exactly once; the registry entry read back with the exact Hermes description and `ghcr.io/automaticai-io/earshot-relay:1.1.3`. Details in "Release evidence (2026-11-27)" below. |
| 7 | **PENDING (ticket-side).** Recipe, skill-install, and release evidence is recorded here; the PM attaches the links to the ticket at completion (no board writes from this lane). |

## Post-review corrections (2026-11-27, transparent record)

The independent specification review of candidate `0580a1f6bd2c8035cc17099570ad599f58f38077` returned FAIL on
one bounded defect and one numbering note; the independent quality review passed the same pin with nonblocking
notes. Corrections on this branch:

- **B1 (recipe skill receipt semantics, fixed):** the first SKILL.md (raw SHA-256
  `bdf3c11df2d12f146ecb945b390049f015dc6529257cd8dfc4b6dd676ee6e62c`, Hermes scanner bundle
  `742789805c69b53d6799089375df8f0afa720e07c23b82fe2033112a68c78c61`) told the agent that `skipped` means the
  phone did not play. The real formatter derives `skipped` from a `finished` beacon with `error: skipped` and
  answers `summary: "The phone started it but the user skipped it."` with `hint: null`. SKILL.md now
  distinguishes `skipped` (started, then skipped; how much was heard is not established) from `failed` (tried,
  could not play) and `missed` (did not play; stale/audio unavailable/bucket changed), and instructs the agent
  to word its answer from the receipt's own `summary`, timeline hops and hop `error`/`hint`, saying there is no
  hint instead of inventing a reason when `hint` is null. A bounded semantic probe
  (`skill-semantic-probe.out`, synthetic beacons through the real `derive_receipt` + actual `SUMMARIES`/
  `receipt_is_final`/hint formatter) reproduced the reviewer's exact skipped output
  `{"status":"skipped","final":true,"summary":"The phone started it but the user skipped it.","hint":null}` and
  contrasted `failed`/`missed`, then asserted none of them words `skipped` as "did not play".
  Disposable install/list/uninstall was repeated on the corrected bytes in a fresh disposable profile: raw
  SHA-256 `0e5a41252bd56582ef927f14943b6d7191d0a2adc8244accf77676654b232f89` (installed bytes identical to the
  candidate), scanner verdict SAFE/ALLOWED, audit INSTALL 21:59:28Z and UNINSTALL 21:59:41Z with the directory
  confirmed removed. No live profile, phone or credential was used.
- **N1 (evidence numbering, fixed):** this ledger previously called the tests row AC6 and the release row AC7.
  The ticket's real numbering is metadata **and tests** = AC5, release/image/registry = AC6, ticket evidence
  links = AC7; the mapping above now follows that. The earlier numbering is preserved here as a recorded
  reviewer finding, not silently rewritten.
- **Quality note (trust default, clarified nonblocking):** an MCP server without an explicit `trust` key
  defaults to `trust: full` (installed source `tools/mcp_tool.py`: "Missing trust = full"), so the recipe and
  `examples/hermes/config.yaml` now state that default explicitly and show the optional
  `mcp_servers.earshot.trust untrusted` setting instead of implying approval prompts by default.

Fixture cleanup: relay and file-server processes stopped, the MinIO container removed, both disposable
profile `.env` files and the synthetic token/password files deleted; sanitized command outputs are retained
in the task scratch (self-pruning after 24 h). Nothing in this file carries a usable credential.

## Release evidence (2026-11-27)

Recorded after independent final review; observed facts only. Review receipts (each summary is the durable
record; the full reviewer reports live in profile scratch and self-prune after 24 h):

- **Spec review, round 1** of `0580a1f6bd2c8035cc17099570ad599f58f38077`: **FAIL**. One blocker (B1 —
  `examples/hermes/SKILL.md` wrongly said `skipped` means "the phone did not play"; the real formatter
  answers "The phone started it but the user skipped it." with possibly-null `hint`) and one numbering note
  (N1 — this ledger's AC rows were misnumbered). Reproduced the exact skipped-receipt output
  `{"status":"skipped","final":true,"summary":"The phone started it but the user skipped it.","hint":null}`.
  Fixed in `7dd4de7c702a0ce8d85790c08f2d9b873fafbe03`. Report: `ears49-spec-review.md` (scratch).
- **Spec review, final** of `7dd4de7c702a0ce8d85790c08f2d9b873fafbe03`: **PASS** — approves ordinary landing
  and the authorized 1.1.3 release at that exact pin; verified the B1 fix against the real formatter, the
  re-run disposable install/uninstall on the corrected bytes, and the corrected AC numbering. Report:
  `ears49-spec-review-final.md` (scratch).
- **Quality review** of `0580a1f6bd2c8035cc17099570ad599f58f38077`: **PASS** for ordinary landing with
  nonblocking notes (trust-default wording; the delta to `7dd4de7` was docs-only so runtime conclusions
  carried over). Report: `ears49-quality-review.md` (scratch).
- **Post-landing quality/security audit** of `133b13bb2457dd1b3c37cd31fc0292b7d2d55e3a`: **technical PASS,
  process FAIL** — see "Post-landing audit and process record" below. Report: `ears49-release-audit.md`
  (scratch).

Landing (shared checkout, fast-forward merges on `main`, `git pull --no-rebase` first; `origin/main` was
still `96c1afc221bdb9121e6c2ffc1d15c985b7422381`, no outside advance):

- `main` pushed `96c1afc` → `7dd4de7c702a0ce8d85790c08f2d9b873fafbe03` →
  `133b13bb2457dd1b3c37cd31fc0292b7d2d55e3a` (https://github.com/AutomaticAI-io/earshot-relay/commits/main).
- Commits: `c875023` recipe docs, `0580a1f` release 1.1.3 metadata, `7dd4de7` post-review correction,
  `133b13b` CI repair (below).

CI repair discovered during the release sequence (commit `133b13b`, workflow-only):

- The first tag image run at `7dd4de7` (run 46074922686) failed its test job: two
  `tests/test_scoped_service.py` fixtures replay the legacy relay with `git archive 4206b81`, which a
  depth-1 `actions/checkout` does not contain (exit 128; 709 passed, 14 skipped, 2 errors).
- The same defect predates this candidate. Two earlier main image runs also failed (run IDs corrected
  2026-11-27 after the post-landing audit found the originally recorded IDs returned 404):
  [run 46063041114](https://github.com/AutomaticAI-io/earshot-relay/actions/runs/46063041114) at `96c1afc`
  (2026-11-27 20:21Z) and [run 46035784655](https://github.com/AutomaticAI-io/earshot-relay/actions/runs/46035784655)
  at `db161da` (2026-11-27 16:41Z). Same-cause was re-verified from their actual job logs: both fail at
  setup of the `test_real_base_crash_gap` fixtures with `git archive 4206b81 src tests` raising
  `CalledProcessError` in the depth-1 checkout.
- `ci.yml` already carries the established repair (`fetch-depth: 0` with the same comment);
  `image.yml` drifted and received the identical 4-line repair. No product content changed — `server.json`
  is byte-identical between `7dd4de7` and `133b13b`.

Tag:

- Annotated tag `v1.1.3` was first created at `7dd4de7` (tag object `fcb8f4b`), then moved to `133b13b`
  (tag object `b5bbc81`) so the tag build could run with the repaired workflow. Moved before any 1.1.3 image
  or registry entry existed; recorded here transparently.

Image (tag build):

- Run 46075354122 — https://github.com/AutomaticAI-io/earshot-relay/actions/runs/46075354122 — test success,
  publish success. Pushed `ghcr.io/automaticai-io/earshot-relay:1.1.3` (plus `:1.1`, `:1`, `:latest`),
  digest `sha256:498dda0085b944a8f54fecc06b0e4a8d2c986d82bf6dae165d3b50a773b15207`.
- Verified after the build, independently of the workflow log: `docker manifest inspect` shows the
  multi-arch index (linux/amd64 + linux/arm64); `docker pull` of the digest then showed label
  `io.modelcontextprotocol.server.name=io.github.AutomaticAI-io/earshot-relay` (matches `server.json`),
  and `--version` printed `earshot-relay 1.1.3`.

Registry publication:

- `publish-mcp.yml` dispatched exactly once with `--ref v1.1.3`: run 46076248244 —
  https://github.com/AutomaticAI-io/earshot-relay/actions/runs/46076248244 — success.
- Registry API read-back: GET
  `https://registry.modelcontextprotocol.io/v0/servers/io.github.AutomaticAI-io%2Fearshot-relay/versions/1.1.3`
  returned `name: io.github.AutomaticAI-io/earshot-relay`, `version: 1.1.3`, description exactly
  "Make Hermes and other agents speak on your Android phone. Own S3 bucket, receipts, opt-in alarm"
  (95 characters), package `oci ghcr.io/automaticai-io/earshot-relay:1.1.3`. Parsed output saved to
  `ears49/registry-readback.out` (profile scratch).

Other checks after landing: CI on main (run 46074901248) success; edge image build on main
(run 46075324247) success; `git log origin/main..HEAD` empty (nothing unpushed — re-confirmed with the
required machine-wide `git unpushed` scan: this worktree's branch reports "NO UPSTREAM" and its commits are
contained in the pushed `origin/main`; the shared checkout's `main` reports no unpushed commits). No live
service restarts, no live profiles/phones/buckets touched, no store/outreach changes.

## Post-landing audit and process record (2026-11-27)

An independent post-landing quality/security audit (`ears49-release-audit.md`, scratch) reviewed the exact
workflow-only commit `133b13bb2457dd1b3c37cd31fc0292b7d2d55e3a` against
`7dd4de7c702a0ce8d85790c08f2d9b873fafbe03` and returned **technical PASS, process FAIL**. Its substance,
recorded here permanently:

> Post-landing independent quality/security audit of workflow-only commit `133b13bb2457dd1b3c37cd31fc0292b7d2d55e3a`
> against `7dd4de7c702a0ce8d85790c08f2d9b873fafbe03`: technical PASS. Full-history checkout repairs the
> reproduced legacy-archive failure; both affected tests pass. Image run 46075354122, registry run
> 46076248244 and public OCI/registry read-backs match the released pin/version. The release worker landed
> this new workflow commit and moved `v1.1.3` before obtaining review; earlier approvals did not cover that
> delta. This audit is post-landing and does not erase the review-before-landing breach. No automatic
> revert/retag/republish is recommended. AC7 remains pending ticket-side evidence links.

Recorded process deviation, without retroactive approval:

- The reviewed candidate was `7dd4de7`. During the release sequence the tag image build failed on a
  pre-existing shallow-checkout defect (above), and the worker landed the workflow-only repair `133b13b` and
  moved tag `v1.1.3` to it without first sending that new pin for independent review. The pre-landing spec
  and quality approvals covered `7dd4de7` only; the delta was out-of-candidate. The post-landing audit is
  the first independent review of `133b13b`; it is explicitly not a retroactive approval of the sequence.
- Following the audit's recommendation, the now-published tag, image and registry entry are preserved as-is
  (no automatic rollback, retag or republish), and any further source/tag/workflow change must be reviewed
  before execution.

Audit's independent technical checks (sanitized facts):

- Semantic YAML comparison of `7dd4de7..133b13b`: only `.github/workflows/image.yml` changed, and only by
  adding `jobs.test.steps[0].with.fetch-depth: 0`; `ci.yml` already had the same setting. No trigger, action
  version, command, permissions, secret use, publish job, dependency or product-content change.
- Reproduced the missing-history boundary in a scratch local-file clone: depth-1 `git archive 4206b81 src
  tests` → exit 128 (`fatal: not a valid object name`); after scratch-only `fetch --unshallow --tags` →
  exit 0. No shared repository refs changed.
- Ran the affected tests at exact `133b13b` in that isolated clone:
  `pytest tests/test_scoped_service.py -k test_real_base_crash_gap` → 2 passed, 27 deselected.
- `git diff --check 7dd4de7 133b13b` and staged `git diff --cached --check`: exit 0.
- Public read-backs: remote `main` and peeled `v1.1.3` = `133b13b`; tag object
  `b5bbc8118f7aa706f9cb1fa1cedf7ee047e12cb3`; locally retained previous tag object `fcb8f4b` targets
  `7dd4de7`, corroborating the move. OCI `:1.1.3` manifest digest matches
  `498dda0085b944a8f54fecc06b0e4a8d2c986d82bf6dae165d3b50a773b15207` (amd64+arm64); config labels match
  server name, revision `133b13b`, version `1.1.3`. Registry workflow history has exactly one run for
  `v1.1.3` (46076248244, `workflow_dispatch`, success). Live registry 1.1.3 entry matches name, version, the
  exact 95-character description and `ghcr.io/automaticai-io/earshot-relay:1.1.3`.
- Audit limits, recorded for accuracy: unauthenticated raw job logs for run 46074922686 returned 403, so the
  audit did not independently re-establish the `709 passed / 14 skipped / 2 errors` counts (they remain this
  worker's evidence from the live log at the time); the containerized `--version` execution is likewise
  worker evidence, not the auditor's execution. The audit's verdicts are release/publication read-backs,
  not phone playback, hearing, locked-screen or live-profile proof.
