# Hermes → Earshot

Make a [Hermes](https://hermes-agent.nousresearch.com/docs/) agent speak on your Android phone through your own
earshot-relay. Two opt-in paths, usable on their own:

- **Path A — MCP tools.** Hermes connects to the relay's `/mcp` endpoint; the agent calls `say` when it decides to
  speak and `receipt` to check what the phone did.
- **Path B — final-reply hook.** A shell hook speaks a slice of every final reply on allowed surfaces. Off until you
  install it.

Both paths store text in **your own bucket as a plain object**: anyone who can read the bucket can read it. Use a
private bucket.

## Prerequisites

- A running earshot-relay reachable over HTTPS (or HTTP on localhost) and its bearer token (its
  `EARSHOT_RELAY_TOKEN`). Setup: the [README](../README.md#run-it).
- The [Earshot](https://store.automaticai.io/earshot/) Android app listening to the same bucket (the
  [buyer guide](https://store.automaticai.io/earshot/guide/) covers that). Android only; text is spoken by the
  phone's own text-to-speech, audio files play as they are.
- Hermes installed with MCP support ([official docs](https://hermes-agent.nousresearch.com/docs/)). This recipe was
  exercised with Hermes `v0.23.4+390.g07e5bbf` (`07e5bbfc854a918659c20dbd06040a2a7b0df37f`, git install) on
  2026-11-27; the dated surface-by-surface evidence is in [Tested on](#tested-on) and the ledger below.

## Path A — MCP: the agent chooses when to speak

Put the token in the profile's own 0600 `.env` (`~/.hermes/.env`, or `~/.hermes/profiles/<name>/.env` for a named
profile) as `EARSHOT_API_TOKEN=...`, then point the profile at your relay — [`examples/hermes/config.yaml`](../examples/hermes/config.yaml)
is the same snippet with comments:

```sh
hermes config set mcp_servers.earshot.url https://your-relay.example.com/mcp
hermes config set mcp_servers.earshot.headers.Authorization 'Bearer ${EARSHOT_API_TOKEN}'
```

`config.yaml` holds only the `${EARSHOT_API_TOKEN}` placeholder; Hermes resolves it from the profile's `.env` at
startup. Then verify the connection:

```sh
hermes mcp test earshot
```

A healthy test connects to `https://your-relay.example.com/mcp` with the bearer token and lists four discovered
tools: `say`, `receipt`, `recent` and `status`. The `alarm` tool exists only when you enabled the relay's alarm
channel (`EARSHOT_RELAY_CHANNELS`); with the default `phone`-only channel list it is correctly absent, and `alarm`
can never be the default channel.

Inside the agent the discovered tools are registered as `mcp__earshot__say`, `mcp__earshot__receipt`,
`mcp__earshot__recent` and `mcp__earshot__status` (Hermes prefixes every MCP tool `mcp__<server>__<tool>`). A
server without an explicit `trust` key defaults to `trust: full`, so `say` runs without prompting. Set
`mcp_servers.earshot.trust: untrusted` (`hermes config set mcp_servers.earshot.trust untrusted`) to make Hermes
ask before write-capable calls such as `say`; to pre-approve just that one tool in a throwaway profile,
allowlist `mcp:earshot/say` without weakening the server's trust tier.

`say` stores your text in the bucket (`status: stored`); `receipt` with the returned id reports what the phone
reported doing. **Stored is not played, and `finished` is device playback completion — not proof a person heard
it, and not a locked-screen test.**

## Path B — the opt-in final-reply hook

[`examples/hermes/earshot-say.sh`](../examples/hermes/earshot-say.sh) speaks a slice of each final reply through
the relay CLI. Nothing runs until you install it.

> **Privacy warning:** the hook writes a slice of what your agent wrote into your bucket as a plain `.say`
> object — anyone who can read the bucket can read it. Use a private bucket, and skip this hook if the agent works
> with anything you would not say aloud.

1. Install the relay CLI and `jq` on the hook's PATH (`uv tool install .` from a relay checkout). **Verify the
   launcher actually executes `earshot-relay say`** — a zero exit from an inert wrapper is not delivery.
2. Keep a 0600 `earshot.env` in the **active profile's** `$HERMES_HOME` (`~/.hermes/earshot.env` for the default
   profile) holding direct values:

   ```sh
   EARSHOT_API_URL=https://your-relay.example.com
   EARSHOT_API_TOKEN=...        # the relay's EARSHOT_RELAY_TOKEN
   ```

   The hook reads the active profile's file, not `$HOME`: under a multiplexed gateway each profile is scoped, and
   inheriting the launcher's environment is not sufficient. It clears inherited relay settings, requires direct
   non-whitespace URL and token values, and rejects ambiguous `*_FILE` or relay fallback settings — failing closed
   (sending nothing) rather than falling back to a localhost default or reused credentials.
3. Register it in that profile's `config.yaml` with an absolute path (also in
   [`examples/hermes/config.yaml`](../examples/hermes/config.yaml)):

   ```yaml
   hooks:
     post_llm_call:
       - command: /absolute/path/to/earshot-say.sh
   ```

   Give the hook explicit consent: Hermes asks once per (event, command) pair, or set `hooks_auto_accept: true`, or
   run the gateway with `--accept-hooks`.
4. Semantics and limits:
   - `post_llm_call` fires when the turn finalizes, **before the gateway delivers the reply** to the platform. It
     speaks what the agent said — never word it as "what Telegram delivered".
   - The default platform allowlist is `telegram discord slack signal whatsapp sms email ntfy cli tui`; `cron` and
     `subagent` are always excluded, and Desktop stays excluded until a real Desktop platform value is observed.
     Narrow the list with `EARSHOT_HERMES_PLATFORMS` in `earshot.env`.
   - The send detaches with `setsid`, so the reply is not blocked by a bucket write.
   - Running both path A and path B can speak one turn twice: enable the path you intend.
   - Multiplexed ntfy gateways: point the profile at your self-hosted server explicitly, prove the effective
     destination with a **nonsecret canary** and a fail-closed outbound allowlist before any real turn, and do not
     equate a startup "connected" line with successful asynchronous auth. The ledger below records why.
   - The tested hook's SHA-256 is `fa730115f8befe3092a0594cea2639d207926e5dd7303fbc2dbeded32591aceb`
     (`sha256sum examples/hermes/earshot-say.sh`). If you change executable behavior, the affected verification in
     the ledger must be rerun before the hook can be called Tested again.

## The `earshot` skill

[`examples/hermes/SKILL.md`](../examples/hermes/SKILL.md) is a short recipe skill that tells the agent when to call
the discovered `mcp__earshot__say` and when to inspect a receipt. Install it into a profile directly from this
repo, and remove it the same way:

```sh
hermes skills install https://raw.githubusercontent.com/AutomaticAI-io/earshot-relay/main/examples/hermes/SKILL.md
hermes skills list                  # shows earshot
hermes skills uninstall earshot     # removes it again
```

Its layout follows the current Hermes skill rules — YAML frontmatter with a `name` and `description`, installable
from a direct SKILL.md URL. It is a recipe skill, not the native Hermes plugin and not a generated onboarding kit.

## Tested on

Everything exercised, dated, with its limits; the full proof ledger is below and the example status rows live in
[`examples/README.md`](../examples/README.md). Only exercised cases are Tested; Desktop and other unrun paths
remain Untested.

| Surface / path | Status | Evidence and limits |
| --- | --- | --- |
| Telegram → HTTP MCP `say` | **Tested** 2026-11-25 | Real owner DM, exact stored text, physical S24 `finished` 16:51:38.773 UTC. [Ledger](#follow-up-on-2026-11-25-queued-telegram-turn-current-hook) |
| Telegram → current hook | **Tested delivery; reply obedience failed** 2026-11-25 | Full actual response (with onboarding paragraph) stored after whitespace normalization, S24 `finished`. [Ledger](#follow-up-on-2026-11-25-queued-telegram-turn-current-hook) |
| Multiplexed ntfy → current hook | **Tested delivery; reply obedience failed** 2026-11-27 | New genuine inbound turn, own secondary credentials, self-hosted subscription, S24 `finished` 15:33:11.295 UTC. [Ledger](#2026-11-27-current-hook-verification-required-delivery-chains-complete) |
| CLI → current hook | **Tested** 2026-11-27 | Byte-equal stored text, S24 `finished` 15:48:03.814 UTC. [Ledger](#2026-11-27-current-hook-verification-required-delivery-chains-complete) |
| Ink TUI → current hook | **Tested** 2026-11-27 | Byte-equal stored text, S24 `finished` 15:53:13.024 UTC. [Ledger](#2026-11-27-current-hook-verification-required-delivery-chains-complete) |
| Bounded cron → hook exclusion | **Tested** 2026-11-27 | Real `cron` payload produced zero hook objects. Not wall-clock scheduling. [Ledger](#2026-11-27-current-hook-verification-required-delivery-chains-complete) |
| Isolated profile → `hermes mcp test earshot` | **Tested** 2026-11-27 | Authenticated HTTP MCP against a local relay and disposable MinIO bucket discovered `say`, `receipt`, `recent`, `status` with alarm disabled; skill install and removal exercised in a disposable profile. No phone playback. [EARS-49 verification](verification/ears-49.md) |
| Desktop → hook | **Untested** | No packaged app or build dependencies; no platform value guessed. |

## What this is not

- Android only: the Earshot app is Android; there is no iPhone path.
- Your own bucket and your own relay; there is no shared Earshot server.
- One way: the phone cannot reply through Earshot, and nothing here is delivered-reply Autoplay — the hook speaks
  the final reply before delivery is confirmed, and no path replays what a platform delivered.
- Not proof of attention: `stored` means the object is in the bucket, and `finished` means the phone finished
  device playback — not that a person heard it, and not a locked-screen test.
- Not an emergency or life-safety service, including the opt-in `alarm` channel.

---

## EARS-48 verification ledger (evidence, not setup)

The sections below are the dated proof record the Tested rows above summarize. They are evidence, not
instructions; preserved failure and incident notes are kept, not rewritten as passes.

## 2026-11-27 current-hook verification: required delivery chains complete

**Required EARS-48 delivery checks / G1 receipt criterion: PASS; unpublished review candidate.** Genuine Telegram HTTP MCP and current hook reached the physical S24 November 25. A **new genuine inbound multiplexed ntfy turn**, after correcting the disposable wrapper, reached the same selected S24 `finished` November 27. Current CLI and Ink TUI also completed; actual bounded cron produced zero hook objects. Desktop is explicitly **Untested** (permitted disposition). The complete actual response was delivered after whitespace normalization, but exact one-sentence obedience **failed** on Telegram/ntfy due to onboarding. Device completion is not human hearing, current screen-state, locked-screen behavior, every gateway or delivery-acknowledged Autoplay.

### Tested-on and fail-closed preflight

- Hermes `07e5bbfc854a918659c20dbd06040a2a7b0df37f`, `v0.23.4-390-g07e5bbfc85`, local untracked files present; relay 1.1.2 at `fef4e224db6ddf721048797cea3ca34bc0f5bcd9`; S24 Ultra `SM-S928U`, Android 17, buyer `io.automaticai.earshot` 0.1.12 (1012). No Hermes source/app change or install.
- Current hook SHA-256 before inbound and after all turns: `fa730115f8befe3092a0594cea2639d207926e5dd7303fbc2dbeded32591aceb`. Correct disposable wrapper: `python -m earshot_relay`, hash `b7f674ec3da818175164fdbb451521de78b87c26332c49992531d9bf2b002c93`. Nonsecret root/secondary config hashes: `5204b9f2559fbbd4cfa793e43ff38ee8275b8a30b5baa5458c70ac8e2216f9cd` / `999e7db75f28222c4c4f769dd61a1218aa51c2fab0b5751fbfdac9fbce62f61b`.
- New throwaway multiplexed root and `ears48-secondary`, each with its own credentials. Root `NTFY_TOPIC` unset; input/output topics distinct. Secondary `.env` explicitly selected `NTFY_SERVER_URL=https://ntfy.delo.sh`; own 0600 `earshot.env` supplied the direct relay URL/token. Subscription was to the self-hosted ntfy service, **not** the relay's publish-only endpoint.
- Before real credentials, a **nonsecret canary** proved effective self-hosted selection; its asynchronous HTTP 401 was expected, not authenticated readiness. Private PID/network namespaces had no direct outbound route; a Unix CONNECT boundary allowed **only** `ntfy.delo.sh:443` and `api.automaticai.io:443`. After canary teardown, approved existing credentials were loaded. Effective self-hosted connect logged **15:31:33.832Z** (10:31:33.832 EST) before inbound; independent real-auth poll HTTP 200. Captured egress shows **zero public ntfy requests**. Background/dependency destinations denied by the boundary were not contacts. Existing approved route `automaticai/auto/fast`, no new paid configuration.
- The owner authorized the pre-existing private R2 rehearsal bucket **and S24 playback**, with distinct approved writer/phone-reader credentials. It is not a clean empty-bucket installation or exclusive listener rig. No fresh pre-send S24 current poll was available: last observed S24 `finished` was the separate 15:10:13.398Z diagnostic. Selection instead used the previously established exact private S24 identity and the owner-authorized **new exact-key receipt fallback** below. No S24 command/settings change, audibility or current screen-state claim.

### New actual hook → exact object → selected physical S24 joins

All three turns captured real `.extra.platform` / `.extra.assistant_response`, active `hermes/profiles/ears48-secondary`, own `earshot.env` present. A task-owned forwarder joined actual device beacons from the existing HTTPS rehearsal sink to **exact local relay SQLite keys**. All phases and `/v1/messages/ID` lookups returned HTTP 200. Private device ID matched the historical S24 and is redacted; per-join deadline 90 seconds.

| Real surface / session | Exact relay ID / object key | S24 detected → started → finished (UTC, Nov 27) | Actual text |
| --- | --- | --- | --- |
| ntfy `20261127_103231_486b54a5` | `msg_39ed8317b78041ada8fb8648835a4a88` / `phone/39ed8317b78041ada8fb8648835a4a88.say` | 15:32:50.392 → 15:32:54.915 → **15:33:11.295Z** | Complete response below, stored after whitespace normalization. |
| CLI `local-13f83a605befde8a62906b6b8ae65008a288e45359bbc7d6192048ef505e93af` | `msg_42831c8149c1493e8ec25288ea7ec23e` / `phone/42831c8149c1493e8ec25288ea7ec23e.say` | 15:47:54.735 → 15:47:57.133 → **15:48:03.814Z** | `EARS48 CLI current hook 20261127.`; actual/stored bytes equal. |
| Ink TUI `local-7cb365daa43ba58b5fb1afa8860d1d898064ebb338e405ccbd6e0e68c0132d99` | `msg_abfcacd371e648afaf6e25d16f2548b1` / `phone/abfcacd371e648afaf6e25d16f2548b1.say` | 15:53:05.214 → 15:53:06.633 → **15:53:13.024Z** | `EARS48 TUI current hook 20261127.`; actual/stored bytes equal. |

Inbound publish HTTP 200 requested `Reply with exactly this sentence and no tools: EARS48 ntfy physical current hook 20261127T153231Z.` Actual hook platform was `ntfy`; complete `.extra.assistant_response`:

```text
EARS48 ntfy physical current hook 20261127T153231Z.

Welcome — /help shows commands. If you'd like, I can build a short profile of you so I can be more useful; you can decline or do it later.
```

Stored text contains **all** of it, with the paragraph break replaced by one space via `--normalize`: raw byte equality **false**, full whitespace-normalized equality **true**, exact one-sentence obedience **false**. No other device was observed for these three keys; this is not an exclusivity claim.

**Current cron exclusion:** external-scheduler repeat-1 job `c9f4c20ac288` executed directly once; execution `b22572e61fb04b5cb0cb93476ea1daf1` completed, run/remove exits 0. Actual hook `.extra.platform: cron`, `.assistant_response: EARS48 bounded cron final 20261127.`; relay-index objects **3 → 3**, zero new hook objects over **8 seconds**. No wall-clock scheduling claim; historical explicit cron MCP playback is separate.

### Preserved failure, multi-device diagnostic and cleanup

The first November 27 real ntfy session `20261127_100324_2da401b6` produced valid actual hook fields but **zero object** because the scratch wrapper invoked inert `python -m earshot_relay.cli`. It is still a failed harness attempt. After correction to `python -m earshot_relay`, a separate direct diagnostic `msg_4d517d40c39e45d4b06bf0880e492f0c` / `phone/4d517d40c39e45d4b06bf0880e492f0c.say` reached detected/started/finished HTTP 200 on **both known authorized listeners**: S24 finished **15:10:13.398Z**, no-audio Android 16 emulator finished **15:10:17.524Z**. No invented exclusive-bucket/new-consent barrier applies. The diagnostic is supporting multi-device evidence, **not** the later genuine ntfy hook chain, physical isolated-device proof or human audibility.

The owner explicitly authorized **existing ntfy token reuse without rotation** November 27 after EARS-72 identified/fixed the earlier env-seed/YAML override (`6b5857f772`). This test used older source with scoped `NTFY_SERVER_URL` and guarded egress, not a production update. The historical November 25 wrong-public-host/possible-disclosure event below remains recorded; actual on-wire disclosure was not captured. It is not a new rotation hold. Keep nonsecret effective-destination preflight and fail-closed outbound restrictions even on fixed source.

**Cleanup at 15:57:21Z:** three exact new indexed keys removed (earlier diagnostic already removed), **three original keys/ETags unchanged**, zero owned keys/processes, bounded job removed. Both November 27 owned runtimes and their raw logs/config/binary stores were removed; known-value scan found six credential-bearing files before cleanup and zero afterward. Only sanitized receipts and nonsecret helpers retained. No production restart, S24 command in the fresh run, S26 command, bucket policy/key change, board action, push, release, deployment, install or version bump.

**Historical retirement at 16:20:33Z:** raw checkpoints/logs/databases/images and the credential-free test snapshot were removed from all four task-owned EARS-48 November 24/25/27 scratch directories, without restarting retired profiles. Sanitized event records and original artifact hashes were retained, with zero owned processes and zero known credential matches remaining. Historical retention statements below describe the original run date, not present retention.

| AC | Current disposition |
| --- | --- |
| 1 | **PASS with recorded rig limits:** versions/config/approved private identities; fresh exact-key S24 joins, but no fresh pre-send current poll; reused nonempty bucket, anonymous-denial diagnostic remains inconclusive. |
| 2 | **PASS:** genuine Telegram HTTP MCP exact text/key and S24 finished (November 25 below). |
| 3 | **PASS delivery:** actual current Telegram/ntfy hook chains finished; full normalized response equality, reply-obedience failures retained; bounded cron excluded. |
| 4 | **PASS permitted disposition:** real current CLI/TUI finished; Desktop **Untested**, missing packaged app/build dependencies. |
| 5 | **PASS:** literal HTTP Bearer interpolation retained; new own-secondary credentials, explicit effective self-host, canary and fail-closed egress. |
| 6 | **PASS:** write annotations/untrusted refusal/narrow preauthorization and explicit cron MCP retained; actual current automatic-cron exclusion; no trust broadening. |
| 7 | **PASS:** corrected checked-in hook rerun on actual Telegram/ntfy/CLI/TUI; direct diagnostic not substituted. |
| 8 | **PASS:** self-contained tested-on recipe and examples matrix, canonical sanitized ledger updated. |
| 9 | **PASS:** exact owned objects/jobs/processes and secret-bearing November 27 runtimes removed; original baseline preserved. |

**Current local checks:** frozen relay source `fef4e224db6ddf721048797cea3ca34bc0f5bcd9` (archive SHA-256 `875c5e012f055175415104cce2a0758ca3e3625832c5061a14a78c514b2fcb61`) ran credential-free in private PID/network namespaces: `tests/test_hermes_hook.py tests/test_mcp.py` → **35 passed in 53.40s**, exit 0, including actual CLI private-wire/no-fallback coverage. Evidence consistency, relative links, shell syntax and diff checks passed. These fixtures are separate from the live receipts. No new full-suite, app build or install is claimed for documentation-only changes.

G1 is the **physical S24 receipt gate**, paths A/B across at least two gateways including Telegram (Telegram + ntfy). It is not human-heard or locked-screen proof. This public recipe is self-contained and does not require access to private Earshot documentation. No publication has occurred; independent review comes first.

---

## Follow-up on 2026-11-25: queued Telegram turn (current hook)

This was partial as of November 25, not then a G1 pass. Hermes `v0.23.4+387.gf421dc0.dirty` (`f421dc027a2d6bdd3925a7cc59ada4dc90a16e6a`, untracked files present), relay 1.1.2 at `4206b81bdc789d7f166a1c4580c681d8292e9768`, S24 Ultra `SM-S928U` / Android 17 / buyer beta 0.1.12 (1012). The current hook's SHA-256 was `fa730115f8befe3092a0594cea2639d207926e5dd7303fbc2dbeded32591aceb`. Before the turn the buyer UI showed the approved private rehearsal bucket name, SigV4, service running, and `Last poll: OK at 11:50:44 (2.1 s ago)`, three objects, `phone, alarm`. The writer and reader were separate approved credentials for the same bucket. Screen on; no settings changed.

The dedicated bot's real queued owner DM requested two distinct texts. Session `20261125_115121_f8e9b774` called `mcp__earshot__say` (`call_ccbfb8a92bc141bbbd8c2e74`), with input `text: EARS48 telegram MCP check` and tool result `status: stored`, `channel: phone`, `speech: device`, and stored exact `EARS48 telegram MCP check`, ID `msg_3dbfe85909514004b7c76b436a9bb9c6`, key `phone/3dbfe85909514004b7c76b436a9bb9c6.say`: `detected` 16:51:34.040, `started` 16:51:35.252, `finished` 16:51:38.773 UTC.

The actual current hook received `.extra.platform: telegram`; its `.extra.assistant_response` started `EARS48 telegram hook check` but **also contained an unsolicited first-contact profile-offer paragraph**. Therefore the owner's exact-one-sentence instruction failed. The full normalized actual response was stored under distinct `msg_be14e09068e245be8ff0ac0ee315dde8`, key `phone/be14e09068e245be8ff0ac0ee315dde8.say`, and reached `detected` 16:51:39.261, `started` 16:51:39.966, `finished` 16:51:54.087 UTC. This proves the hook ran with the actual final response, **not** that the exact requested reply was followed.

The pre-existing HTTPS rehearsal sink supplied beacons; a new private forwarder joined exact keys against this relay's SQLite index and forwarded each phase with HTTP 200. The private device ID matched the historical S24 finished receipt. Both indexed objects were removed; all three baseline keys/ETags were preserved. `finished` is device playback completion, not human hearing or locked-screen proof.

**Historical November 25 multiplexed ntfy warning (superseded action, incident retained):** a subsequent isolated gateway contacted public `https://ntfy.sh` despite the intended self-hosted HTTPS ntfy server, returning 401 twice. The exact effective incident configuration was deleted, so its cause was unproven at that time. The profile held the real ntfy credential and the adapter constructs an authorization header, but the actual on-wire header was not captured: treat the credential as **potentially disclosed**, not confirmed disclosed. Rotation/audit was requested then; the owner later explicitly authorized reuse without rotation after EARS-72 identified and fixed the environment-seed override. The November 25 current-hook multiplexed ntfy retest was **blocked**, not passed. Before any real gateway use, verify the effective destination with nonsecret credentials and restricted outbound access. Desktop remains untested. No production Hermes profile, S26, board, deploy, release, bucket policy or credential was changed.

**Separate synthetic hazard reproduction:** Independent review used a scratch multiplexed profile, synthetic token and fake HTTP transport (no external request). On tested Hermes source `f421dc027a2d6bdd3925a7cc59ada4dc90a16e6a` (dirty), scoped `NTFY_TOPIC` without scoped `NTFY_SERVER_URL` made the ntfy plugin seed the public default (`plugins/platforms/ntfy/adapter.py:311–321`), and the gateway registry overlaid that seed on YAML `extra.server` (`gateway/config_env.py:224–229`). The fake transport saw the default URL and a synthetic bearer header. Supplying scoped `NTFY_SERVER_URL`, or using YAML-only topic and server, selected the respective synthetic server. This demonstrates a possible hazard, **not the deleted incident configuration or its cause**.

---

## Historical run: 2026-11-24

**Historical partial real-turn verification.** Hermes v0.23.4+386 (`d6525ab61b9f`), relay 1.1.2 (`4bd761a`), Android buyer beta 0.1.12 (1012) on S24 Ultra SM-S928U / Android 17. A private R2 rehearsal bucket and distinct writer/phone reader identities were used. The bucket had three existing objects; this was **not** a clean installation. The throwaway relay was local HTTP `127.0.0.1:18848`, channel `phone`, server TTS off. It forwarded only receipts for keys in its own SQLite index from the existing HTTPS receipt sink. No production Hermes profile, S26, existing EARS-29 relay, release or deploy was changed.

## Path A: agent-directed MCP

Set `mcp_servers.earshot.url: https://YOUR-RELAY/mcp` and `mcp_servers.earshot.headers.Authorization: "Bearer ${EARSHOT_API_TOKEN}"` with the token in **that profile's** 0600 `.env`; use `hermes config set`, not an inline token in the YAML. `hermes mcp test earshot` resolved the placeholder, connected to HTTP `/mcp`, and listed `say`, `receipt`, `recent`, `status` (four tools). `say` advertises `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`, `openWorldHint: true`. `receipt` advertises `readOnlyHint: true`. With `trust: untrusted`, an unapproved real CLI `mcp__earshot__say` returned **"The user did not approve ... command was NOT run"**; no stored object containing the requested MCP probe text. The independently enabled final-reply hook did store the refusal summary; that is a separate side effect, not a successful MCP call. For this private, pre-authorized test only, `command_allowlist: ["mcp:earshot/say"]` granted that *one tool* in the throwaway profile, without weakening the server's trust tier. A new real CLI turn stored `msg_7eb82c389308446aba6bfbda10ad33d2`; matching phone beacons: detected 23:30:53.033Z, started 23:30:53.376Z, finished 23:30:58.137Z. Storing is not playback; `finished` means device playback completed, not that a person heard it.

A second bounded, explicitly invoked Hermes job (`scheduler: external`, `hermes cron run ... --wait --timeout 120`) called MCP `say` and stored `msg_cc01d5bfb5d64b3fad6f8574234e1c20`; its device receipt reached `finished` at 23:39:57.049Z. The no-tool job completed with `.extra.platform: cron`, with **zero** new object from the hook. Both jobs were removed afterward. No native recurring clock or Candybar registration was made.

## Path B: final-reply hook

See [`../examples/hermes/earshot-say.sh`](../examples/hermes/earshot-say.sh) (Linux: Bash, `jq`, `nohup`, `setsid`, installed relay CLI). The real hook payload has `.extra.assistant_response` and `.extra.platform`. The command loads `earshot.env` from the **active `$HERMES_HOME`**, not `$HOME`, because a multiplexed gateway scopes each profile; this run inherited the token but **not** the API URL, so inheritance alone was insufficient. Keep that file 0600 with direct `EARSHOT_API_URL` and `EARSHOT_API_TOKEN`; this hook rejects missing/whitespace values and ambiguous `*_FILE`/relay fallback settings rather than inheriting them. Install the relay CLI and `jq` on the hook's PATH; **verify that the launcher actually executes `earshot-relay say`** rather than treating a zero exit from an inert wrapper as delivery. Register the executable as `hooks.post_llm_call[].command`, using an absolute path; give the hook explicit consent (`hooks_auto_accept` or `hermes gateway run --accept-hooks`). It detaches with `setsid` so the reply is not blocked by a bucket write. Never put secrets or private replies in spoken output; the text is a plaintext private bucket object. The default allowlist includes the **observed** `cli`, `tui`, `ntfy` values and messaging names not verified here. Explicit `cron` and `subagent` are always excluded; Desktop is excluded until observed. Set `EARSHOT_HERMES_PLATFORMS` to narrow the allowlist. Running both path A and B can cause two spoken objects for one agent turn: enable only the path you intend. For multiplexed ntfy, first enforce an exact outbound destination allowlist and confirm the adapter's effective self-hosted server with a **nonsecret canary**; on older Hermes revisions set profile-local `NTFY_SERVER_URL` explicitly, then independently verify authenticated subscription before a real turn. Do not equate a startup `connected` line with successful asynchronous auth. Use storage and listeners within the actual owner authorization; a shared approved rig can establish exact-device completion but not exclusive-device proof. No new consent barrier is implied for the already-approved S24 rehearsal path.

**POST-LIVE isolated-fixture correction:** The historical reference hook is relay commit `a53d3400967bb33e4aa8288401f809014257aee0` (script SHA-256 `e818135c0740a2e82977762beb387817cebebb58743f00de44bcd6f02294117a`). The retained runtime hook hashes to `2f28d54b65ccc71729b28f7f5612449e0d62b864d3da34bcd3e757bbfd9a34c1` and differs from that reference only in one comment; executable lines are identical. Its mtime predates the TUI turn, but no contemporaneous per-turn hash was captured, so exact executed bytes are unproven. Independent review found the reference hook could reuse launcher credentials or normalize a whitespace-only URL to the CLI's localhost default. The current script instead clears inherited relay settings, requires direct, non-whitespace profile URL/token, rejects ambiguous alternatives and prevents that fallback. It was tested against synthetic profiles and the actual relay CLI in a private network/PID namespace; **as of 2026-11-24 it had not yet been rerun on S24**. The 2026-11-25 Telegram follow-up above ran it on S24; the later November 27 new genuine ntfy/CLI/TUI turns also reached the selected S24 `finished`. The historical rows below do not establish device verification of the corrected version.

A real CLI reply `EARS48 CLI corrected hook verification.` produced `.extra.platform: cli`, with the reply under `.extra.assistant_response`; relay object `msg_b662f66a1ff44c5b8837a0ea27d81f0a` has the matching `finished` receipt. A real multiplexed ntfy turn directly from a self-hosted HTTPS ntfy server produced `.extra.platform: ntfy`, `.extra.assistant_response: EARS48 ntfy hook verification.`, and relay object `msg_fea72474eb9b46c2b2aab8f44ee681c1` reached `finished`. A real Ink TUI turn produced `.extra.platform: tui`, `.extra.assistant_response: EARS48 TUI verified speak hook.`, and relay object `msg_2a2371cd1627453a9dbef3ad4e7aa5ef` reached `finished`. A test-only local ntfy proxy was briefly used to diagnose profile scoping before a new direct subscription to the self-hosted HTTPS ntfy server succeeded; the final ntfy row did not use the proxy. The multiplexed secondary profile's 0600 `.env` needed its *own* provider and ntfy credentials; an environment variable in the launch profile did not carry over. A publisher-controlled ntfy title is not an authorization identity: a private topic and authenticated subscription are required.

| Surface, transport | Status on 2026-11-24 | Expected / actual |
| --- | --- | --- |
| CLI → HTTP MCP | **Tested** | `say` stored, S24 `finished`; untrusted call without explicit per-tool approval correctly refused. |
| External-scheduler one-shot cron → HTTP MCP | **Tested** | Explicit `say` stored, S24 `finished`; hook excluded cron. No recurring job installed. |
| CLI → hook | **Tested** | Observed platform `cli` and final text; detached relay write and phone `finished`. |
| ntfy multiplexed gateway → hook | **Tested** | Observed platform `ntfy` and final text; detached write and phone `finished`. ntfy subscribed to the self-hosted HTTPS ntfy server, **not** the relay. |
| Ink TUI → hook | **Tested** | Observed platform `tui` and final text; relay stored, S24 `finished`. |
| Desktop → hook | **Untested** | `hermes desktop --skip-build` exited 1: no packaged app under `apps/desktop/release`; the read-only `assert-root-install.mjs` alternative also exited 1 (missing build dependencies including Vite and Electron). No production dependency install was repaired; no Desktop turn or platform value was observed. Do not add a guessed value. |
| Telegram → either path | **Untested, owner-deferred** | Dedicated test bot waits on EARS-62; no PM bot borrowed, no Telegram receipt or transcript. |

**Historical acceptance gate as of 2026-11-24 remained partial** because Telegram was deferred and Desktop was not run; see the November 25 Telegram and November 27 completed current-hook follow-ups above. Cleanup removed all 12 indexed task-owned objects; the three pre-existing bucket keys/ETags were preserved. The throwaway home was retired with credential files removed, and task-owned processes/listeners stopped; inactive checkpoints were kept privately at that time. The final ntfy row belonged only to `ears48-ntfy:ntfy`; a stale root-profile `ntfy_unauthorized` fatal entry from an earlier failed attempt remained in gateway state. Do not interpret that historical run as a fully healthy gateway installation. Its raw artifacts included `mcp-approved2.jsonl`, `hooks.jsonl`, `forwarded.jsonl`, `cron-run2.log`, `cron-mcp.log` and `desktop-attempt.log`; those artifacts and all raw checkpoints were subsequently removed during the November 27 16:20:33Z historical retirement. Only sanitized events and historical hashes remain. A `finished` receipt is not human audibility or locked-screen proof.
