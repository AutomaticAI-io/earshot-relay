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
| Hermes `send --to ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](https://github.com/delorenj/earshot/blob/main/docs/verification/ears-56.md) | **Tested transport; playback failed.** 2026-11-24 isolated Moto-only run (Hermes `d6525ab`, relay 1.1.2): exit 0 and correlated `stored` receipt, no device. 2026-11-25 dedicated Android 16 / Earshot 0.1.12 emulator: actual manual ID `msg_915bcbbc69ef4d45ac79e9843ccf4904`, exact object `phone/915bcbbc69ef4d45ac79e9843ccf4904.say` / `EARS56-device-manual-20261125T001957Z`, selected-device `detected` → `failed`: `TTS synthesis failed: TTS initialization failed (-1) [step=tts]`. No TTS service/default synth installed. **Not playback Tested**, not a demo. Positional `hermes send ntfy:phone "text"` is unsupported (exit 2); use `--to`. |
| Hermes one-shot cron `--no-agent --deliver ntfy:phone` → [ntfy compat spike](../docs/verification/ears-56-hermes-ntfy.md) / [device follow-up](https://github.com/delorenj/earshot/blob/main/docs/verification/ears-56.md) | **Tested explicit scheduler delivery; playback failed.** 2026-11-25 Android 16 / Earshot 0.1.12 emulator, `hermes cron run 375e0d795e79`: fire `c95db6e1575f44ac8e123118ad7fb7f2` completed (`source=direct`); distinct `msg_65414fa83c6f4d61b95ac92e3c43a862`, `phone/65414fa83c6f4d61b95ac92e3c43a862.say` / `EARS56-device-cron-2d2774c8eb44bd3e41b8`; selected-device `detected` → `failed` with the same TTS initialization error. **Not playback Tested**; automatic wall-clock firing **untested**. Recurring scheduling, if later needed, belongs to Candybar. |

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
