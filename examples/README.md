# Examples

| Example | Status |
| --- | --- |
| [`curl.sh`](curl.sh) | **Tested** in the test suite against a live relay and a moto bucket (needs `curl` and `jq`). |
| [`hermes/`](hermes/) (`config.yaml`, `earshot-say.sh`, `SKILL.md`, [recipe](../docs/hermes.md)) | **Tested on named surfaces; Desktop Untested.** Genuine Telegram MCP/current hook S24 `finished` November 25, new genuine multiplexed ntfy current hook plus real CLI/TUI S24 `finished` November 27. Actual bounded cron produced zero hook objects. Telegram/ntfy exact-sentence obedience failed, full normalized actual response delivered. Earlier inert-wrapper failure and authorized multi-device diagnostic retained separately. `SKILL.md` install and removal were exercised in a disposable profile on 2026-11-27 ([evidence](../docs/verification/ears-49.md)); no live profile was changed. |
| [`claude-code/`](claude-code) hook (`earshot_hook.py`, `settings.hooks.json`) | **Tested on 2026-11-27**: the suite runs the script against a live relay and a moto bucket (each Notification type, Stop gist, transcript fallback, `stop_hook_active`, default-off Stop, always exit 0), and a manual run piped docs-shaped Stop and Notification payloads through a from-source relay into a disposable local MinIO bucket: with no `EARSHOT_HOOK_STOP` the Stop payload stored nothing; with `EARSHOT_HOOK_STOP=1` the object held the trimmed two-sentence gist and `GET /v1/messages/{id}` returned a `stored` receipt for both sends. **Not** run inside a real Claude Code session (the payload shapes follow the hooks reference, including `last_assistant_message` on Stop); phone playback is not part of this test. |
| [`codex-notify.py`](codex-notify.py) | **Tested** with a hand-built `agent-turn-complete` event following the Codex `notify` documentation. **Not** run inside a real Codex session. |
| [`github-actions.yml`](github-actions.yml) | **Untested.** No workflow was run. |
| [`n8n.md`](n8n.md) | **Untested.** |
| [`home-assistant.md`](home-assistant.md) (`rest_command`) | **Tested** 2026-11-27 with Home Assistant 2026.11.3 (Docker, HTTP API only) against relay 1.1.2 (`db161da`) and a disposable MinIO bucket: both recipe sends landed in the bucket byte-identical, receipts `stored`. Phone playback **not** tested. |
| [`tasker.md`](tasker.md) ([`tasker/Earshot_Say.tsk.xml`](tasker/Earshot_Say.tsk.xml)) | **Tested** 2026-11-28 with Tasker 6.7.9 (direct-download trial) on an Android 16 Play emulator: the imported task's HTTP Request reached relay 1.1.3 (`981b4ba`), the object held the exact text, receipt `stored`. Text with `"`, `\` or a newline breaks its JSON body (`400`). Phone playback, HTTPS and a physical device **not** tested. |
| [`apprise.md`](apprise.md) (Apprise `ntfy://`, `ntfys://`, `gotify://`) | **Tested** with Apprise 2.3.0 against a live relay (`scripts/compat_clients.py`); the requests are replayed in `tests/test_compat.py`. |
| [`uptime-kuma.md`](uptime-kuma.md) (ntfy and Gotify providers) | **Tested** with Uptime Kuma 1.23.16 (its notification Test call). Version 2 was **not** run. |
| Hermes `send --to ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](../docs/verification/ears-56-hermes-ntfy.md#fresh-espeak-emulator-follow-up--playback-completion-2026-11-25-00340035-utc) | **Tested: isolated emulator playback completion.** On 2026-11-25, Hermes `d6525ab`, relay 1.1.2 frozen `e1021e8`, Earshot beta-debug 0.1.12, Android 16 / API 36 and eSpeak NG 1.52.0: manual `msg_6e0e746418b0428786cbc8f4700f3fb3`, object `phone/6e0e746418b0428786cbc8f4700f3fb3.say` / `EARS56-device-manual-20261125T003451Z`, selected-device `detected → started → finished` at **00:35:12.083 UTC**. Earlier selected-device attempt failed TTS initialization. No-audio emulator: not physical phone, audibility or locked-screen proof. Positional `hermes send ntfy:phone "text"` is unsupported; use `--to`. |
| Hermes one-shot cron `--no-agent --deliver ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](../docs/verification/ears-56-hermes-ntfy.md#fresh-espeak-emulator-follow-up--playback-completion-2026-11-25-00340035-utc) | **Tested: explicit trigger and isolated emulator playback completion.** Same versions and 2026-11-25 setup: `hermes cron run b3d93b9f831c`, fire `cb5818ad86e5492a9915108f774de14f` completed (`source=direct`), distinct `msg_e3d5cb29ed6d4c7081a38c617e664f7d`, object `phone/e3d5cb29ed6d4c7081a38c617e664f7d.say` / `EARS56-device-cron-d5715f2f61bc5f1bb0f9`; selected-device `detected → started → finished` at **00:35:23.013 UTC**. Earlier attempt failed TTS. Automatic wall-clock firing, real phone, audibility and locked-screen playback **untested**. Recurring scheduling belongs to Candybar. |

**Required delivery checks / G1 receipt gate passed, unpublished candidate:** the [self-contained receipt](../docs/hermes.md#2026-11-27-current-hook-verification-required-delivery-chains-complete) records new genuine ntfy, CLI and TUI current-hook S24 completion plus bounded cron exclusion, combined with actual Telegram MCP/current-hook completion. The historical wrong-public-host/possible-disclosure event and inert-wrapper failure remain recorded, not rewritten as passes. Owner-authorized existing-token reuse followed a nonsecret canary, explicit self-host selection and fail-closed egress (zero public ntfy requests). The earlier separate diagnostic completed on the **known authorized** S24 and no-audio emulator; it is not the new hook proof or exclusive-device evidence. Exact Telegram/ntfy sentence obedience failed separately. Desktop remains Untested. Historical byte ambiguity does not transfer to the new current-hook evidence.

| Current follow-up / surface | Status, November 25–27 | Evidence and limits |
| --- | --- | --- |
| Telegram HTTP MCP | **Tested** | Actual owner DM and `say` transcript; `msg_3dbfe85909514004b7c76b436a9bb9c6`, key `phone/3dbfe85909514004b7c76b436a9bb9c6.say`, exact text `EARS48 telegram MCP check`, S24 `finished` November 25 16:51:38.773 UTC. |
| Telegram current hook | **Tested delivery; reply obedience failed** | Hook hash `fa730115f8befe3092a0594cea2639d207926e5dd7303fbc2dbeded32591aceb`; actual `telegram` response included onboarding. `msg_be14e09068e245be8ff0ac0ee315dde8`, key `phone/be14e09068e245be8ff0ac0ee315dde8.say`, S24 `finished` November 25 16:51:54.087 UTC. |
| New genuine multiplexed ntfy current hook | **Tested delivery; reply obedience failed** | Actual session `20261127_103231_486b54a5`, own secondary credentials, self-hosted subscription. `msg_39ed8317b78041ada8fb8648835a4a88`, key `phone/39ed8317b78041ada8fb8648835a4a88.say`; full actual response matches after whitespace normalization, S24 `finished` November 27 **15:33:11.295Z**. No fresh pre-send S24 poll; exact-device receipt fallback authorized, no screen-state/audibility claim. |
| Current CLI hook | **Tested** | Actual `cli` / `EARS48 CLI current hook 20261127.`, byte-equal stored text; `msg_42831c8149c1493e8ec25288ea7ec23e`, key `phone/42831c8149c1493e8ec25288ea7ec23e.say`, S24 `finished` November 27 **15:48:03.814Z**. |
| Current Ink TUI hook | **Tested** | Actual `tui` / `EARS48 TUI current hook 20261127.`, byte-equal stored text; `msg_abfcacd371e648afaf6e25d16f2548b1`, key `phone/abfcacd371e648afaf6e25d16f2548b1.say`, S24 `finished` November 27 **15:53:13.024Z**. |
| Current bounded cron hook exclusion | **Tested** | Repeat-1 external job `c9f4c20ac288` completed direct execution `b22572e61fb04b5cb0cb93476ea1daf1`, actual `cron` payload, **3 → 3** indexed objects and zero new hook objects over **8 seconds**; removed afterward. Not wall-clock scheduling. |
| Desktop | **Untested** | No packaged app/build dependencies; no platform guessed or install performed. |
| SMS (Twilio) | **Blocked, not run** 2026-11-28 | No Hermes SMS gateway could be given a working number: the only available gateway number (Telnyx, local US) is carrier-rejected for outbound A2P SMS without 10DLC registration (real probe: Verizon answered `40010 "Not 10DLC registered"`), and the personal Twilio vault item holds a scope-limited API key with no usable number. Registration fees were outside the approved spend. Full evidence lives in the private Earshot repo (`docs/verification/ears-75.md`, owner-readable only). |

The following rows are **historical** and do not establish current corrected-hook results:

| Recipe / surface | Status, 2026-11-24 | Evidence and limits |
| --- | --- | --- |
| [Hermes HTTP MCP](../docs/hermes.md#path-a-agent-directed-mcp), CLI | **Tested** | Hermes v0.23.4+386, relay 1.1.2; S24 Ultra Android 17 / buyer beta 0.1.12 (1012), matching `finished`. `say` is write-capable and untrusted execution without approval refused. |
| Hermes explicit MCP in bounded external-scheduler cron | **Tested** | One explicit run completed with a matching S24 `finished`; automatic hook speech stayed excluded. Not a wall-clock schedule test. |
| [Hermes final-reply hook](hermes/earshot-say.sh), CLI | **Tested** | Real `.extra.platform: cli` and `.extra.assistant_response`; matching S24 `finished`. |
| Hermes final-reply hook, multiplexed ntfy | **Tested** | Real `.extra.platform: ntfy`; own active-profile credential file; self-hosted HTTPS ntfy server subscription, matching S24 `finished`. |
| Hermes final-reply hook, Ink TUI | **Tested** | Real `.extra.platform: tui`; allowlist correction rerun, matching S24 `finished`. |
| Hermes Desktop hook | **Untested** | No packaged app; read-only build preflight also failed because dependencies are absent. No platform value guessed. |
| Hermes Telegram HTTP MCP and hook | **Untested, owner-deferred** | Dedicated bot waits on EARS-62; no PM bot used. |

`finished` is device playback completion, not proof a person heard it or a locked-screen test.

The agent examples send a slice of agent text to your bucket. Use a private bucket (see
[docs/permissions.md](../docs/permissions.md)).

## Install the Claude Code hook

```sh
mkdir -p ~/.claude/hooks
cp examples/claude-code/earshot_hook.py ~/.claude/hooks/
```

Then add the `Notification` and `Stop` entries from [`settings.hooks.json`](claude-code/settings.hooks.json) to the
`hooks` object of `~/.claude/settings.json` (or a project's `.claude/settings.json`). If the file has no `hooks` yet,
copy the whole `hooks` object in; if it has some, append these entries to the existing `Notification` and `Stop`
arrays rather than replacing them.

The hook reads `EARSHOT_API_URL` (default `http://127.0.0.1:8080`) and `EARSHOT_API_TOKEN` from the environment
Claude Code starts with, so export them in the shell you launch `claude` from, or point `EARSHOT_API_TOKEN_FILE` at a
file only you can read. Optional: `EARSHOT_HOOK_CHANNEL` sends to another channel (never `alarm` for this).

The Notification entry speaks only when Claude Code needs you: a permission prompt, an idle prompt waiting for input,
or a question. The Stop entry speaks the first sentence or two of the last reply (at most about 300 characters), and
it is **off by default**: even with the Stop entry installed, the hook sends nothing until you opt in by setting
`EARSHOT_HOOK_STOP=1` (in the `env` object of the same `settings.json`, or exported in the launching shell).

**Warning:** the Stop hook writes a slice of what the agent wrote into your bucket as a plain `.say` object — anyone
who can read the bucket can read it. Use a private bucket and a private channel, and leave the Stop hook off if the
agent works with anything you would not say aloud.

## Install the Codex notify program

Copy `codex-notify.py` anywhere and set `notify = ["python3", "/absolute/path/to/codex-notify.py"]` in
`~/.codex/config.toml`. It reads the same environment variables as the Claude Code hook.
