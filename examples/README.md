# Examples

| Example | Status |
| --- | --- |
| [`curl.sh`](curl.sh) | **Tested** in the test suite against a live relay and a moto bucket (needs `curl` and `jq`). |
| [`claude-code/`](claude-code) hook (`earshot_hook.py`, `settings.hooks.json`) | **Tested**: the script runs against a live relay in the test suite (Notification, Stop gist, transcript fallback, `stop_hook_active`, always exit 0). The hook JSON is checked for shape only; it was **not** run inside a real Claude Code session. |
| [`codex-notify.py`](codex-notify.py) | **Tested** with a hand-built `agent-turn-complete` event following the Codex `notify` documentation. **Not** run inside a real Codex session. |
| [`github-actions.yml`](github-actions.yml) | **Untested.** No workflow was run. |
| [`n8n.md`](n8n.md) | **Untested.** |
| [`home-assistant.md`](home-assistant.md) | **Untested.** |

The agent examples send a slice of agent text to your bucket. Use a private bucket (see
[docs/permissions.md](../docs/permissions.md)).
