# Examples

| Example | Status |
| --- | --- |
| [`curl.sh`](curl.sh) | **Tested** in the test suite against a live relay and a moto bucket (needs `curl` and `jq`). |
| [`claude-code/`](claude-code) hook (`earshot_hook.py`, `settings.hooks.json`) | **Tested**: the script runs against a live relay in the test suite (each Notification type, Stop gist, transcript fallback, `stop_hook_active`, always exit 0). The hook JSON is checked for shape only; it was **not** run inside a real Claude Code session. |
| [`codex-notify.py`](codex-notify.py) | **Tested** with a hand-built `agent-turn-complete` event following the Codex `notify` documentation. **Not** run inside a real Codex session. |
| [`github-actions.yml`](github-actions.yml) | **Untested.** No workflow was run. |
| [`n8n.md`](n8n.md) | **Untested.** |
| [`home-assistant.md`](home-assistant.md) | **Untested.** |
| [`apprise.md`](apprise.md) (Apprise `ntfy://`, `ntfys://`, `gotify://`) | **Tested** with Apprise 2.3.0 against a live relay (`scripts/compat_clients.py`); the requests are replayed in `tests/test_compat.py`. |
| [`uptime-kuma.md`](uptime-kuma.md) (ntfy and Gotify providers) | **Tested** with Uptime Kuma 1.23.16 (its notification Test call). Version 2 was **not** run. |
| Hermes `send --to ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](../docs/verification/ears-56-hermes-ntfy.md#fresh-espeak-emulator-follow-up--playback-completion-2026-11-25-00340035-utc) | **Tested: isolated emulator playback completion.** On 2026-11-25, Hermes `d6525ab`, relay 1.1.2 frozen `e1021e8`, Earshot beta-debug 0.1.12, Android 16 / API 36 and eSpeak NG 1.52.0: manual `msg_6e0e746418b0428786cbc8f4700f3fb3`, object `phone/6e0e746418b0428786cbc8f4700f3fb3.say` / `EARS56-device-manual-20261125T003451Z`, selected-device `detected → started → finished` at **00:35:12.083 UTC**. Earlier selected-device attempt failed TTS initialization. No-audio emulator: not physical phone, audibility or locked-screen proof. Positional `hermes send ntfy:phone "text"` is unsupported; use `--to`. |
| Hermes one-shot cron `--no-agent --deliver ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](../docs/verification/ears-56-hermes-ntfy.md#fresh-espeak-emulator-follow-up--playback-completion-2026-11-25-00340035-utc) | **Tested: explicit trigger and isolated emulator playback completion.** Same versions and 2026-11-25 setup: `hermes cron run b3d93b9f831c`, fire `cb5818ad86e5492a9915108f774de14f` completed (`source=direct`), distinct `msg_e3d5cb29ed6d4c7081a38c617e664f7d`, object `phone/e3d5cb29ed6d4c7081a38c617e664f7d.say` / `EARS56-device-cron-d5715f2f61bc5f1bb0f9`; selected-device `detected → started → finished` at **00:35:23.013 UTC**. Earlier attempt failed TTS. Automatic wall-clock firing, real phone, audibility and locked-screen playback **untested**. Recurring scheduling belongs to Candybar. |

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
or a question. The Stop entry speaks the first sentence or two of the last reply; leave it out if the agent works with
anything you would not say aloud.

## Install the Codex notify program

Copy `codex-notify.py` anywhere and set `notify = ["python3", "/absolute/path/to/codex-notify.py"]` in
`~/.codex/config.toml`. It reads the same environment variables as the Claude Code hook.
