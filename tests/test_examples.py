"""The runnable examples (Claude Code hook, Codex notify) against a live relay; curl.sh when jq is present."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from .conftest import TOKEN

EX = Path(__file__).resolve().parent.parent / "examples"
HOOK = EX / "claude-code" / "earshot_hook.py"
NOTIFY = EX / "codex-notify.py"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load(HOOK)


def run(script: Path, args: list[str], stdin: str | None, url: str, token: str | None = TOKEN, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EARSHOT_")}
    env["EARSHOT_API_URL"] = url
    if token:
        env["EARSHOT_API_TOKEN"] = token
    env.update(extra or {})
    return subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True, text=True, env=env, timeout=30)


def sent(url: str) -> list[dict]:
    return httpx.get(f"{url}/v1/messages?limit=50", headers={"Authorization": f"Bearer {TOKEN}"}).json()["items"]


def message_text(s3_admin, item: dict) -> str:
    from .conftest import BUCKET
    return s3_admin.get_object(Bucket=BUCKET, Key=item["key"])["Body"].read().decode()


# ---- gist ------------------------------------------------------------------------------------------------------


def test_gist_takes_the_first_two_sentences_and_strips_markdown():
    text = "## Done\n\nI fixed the **flaky test** in `foo.py`. See [the PR](https://x.test/pr/1). Then I ran the suite.\n\n```py\nprint(1)\n```"
    assert hook.gist(text) == "Done I fixed the flaky test in foo.py. See the PR."


def test_gist_is_capped_at_300_characters():
    assert len(hook.gist("word " * 200)) <= 300
    long_sentence = "x" * 250 + ". " + "y" * 250 + "."
    assert hook.gist(long_sentence) == "x" * 250 + "."
    assert hook.gist("") == ""


def test_transcript_fallback(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "First answer."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Final answer here. More."}]}},
    ]) + "\nnot json\n")
    assert hook.last_assistant_text(str(t)) == "Final answer here. More."
    assert hook.last_assistant_text(str(tmp_path / "missing")) == ""


# ---- Claude Code hook ----------------------------------------------------------------------------------------


def test_hook_notification_says_needs_attention(live_relay, s3_admin):
    p = run(HOOK, [], json.dumps({"hook_event_name": "Notification", "cwd": "/work/shop", "message": "Claude needs your permission"}), live_relay)
    assert p.returncode == 0 and p.stdout == ""
    assert message_text(s3_admin, sent(live_relay)[0]) == "Claude Code needs attention in shop."


@pytest.mark.parametrize("kind, line", [
    ("permission_prompt", "Claude Code needs your permission in shop."),
    ("idle_prompt", "Claude Code is waiting for you in shop."),
    ("elicitation_dialog", "Claude Code has a question in shop."),
])
def test_hook_notification_types_that_need_you(live_relay, s3_admin, kind, line):
    payload = {"hook_event_name": "Notification", "cwd": "/work/shop", "message": "m", "notification_type": kind}
    assert run(HOOK, [], json.dumps(payload), live_relay).returncode == 0
    assert message_text(s3_admin, sent(live_relay)[0]) == line


@pytest.mark.parametrize("kind", ["auth_success", "elicitation_complete", "agent_completed", "quota_auto_resume_fired"])
def test_hook_notification_types_that_do_not_need_you_stay_silent(live_relay, kind):
    before = sent(live_relay)
    payload = {"hook_event_name": "Notification", "cwd": "/work/shop", "message": "ok", "notification_type": kind}
    assert run(HOOK, [], json.dumps(payload), live_relay).returncode == 0
    assert sent(live_relay) == before


def test_hook_stop_speaks_a_short_gist(live_relay, s3_admin):
    msg = "All tests pass now. I also tidied the imports.\n\n" + "More detail. " * 50
    p = run(HOOK, [], json.dumps({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": msg}),
            live_relay, extra={"EARSHOT_HOOK_STOP": "1"})
    assert p.returncode == 0
    assert message_text(s3_admin, sent(live_relay)[0]) == "All tests pass now. I also tidied the imports."


@pytest.mark.parametrize("value", [None, "", "0", "false", "no"])
def test_hook_stop_defaults_off(live_relay, value):
    """Without an explicit EARSHOT_HOOK_STOP opt-in the Stop hook sends nothing and still exits 0."""
    before = sent(live_relay)
    extra = {} if value is None else {"EARSHOT_HOOK_STOP": value}
    p = run(HOOK, [], json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Done."}), live_relay, extra=extra)
    assert p.returncode == 0 and sent(live_relay) == before


def test_hook_stop_falls_back_to_the_transcript(live_relay, s3_admin, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "From the transcript."}]}}))
    run(HOOK, [], json.dumps({"hook_event_name": "Stop", "transcript_path": str(t)}), live_relay,
        extra={"EARSHOT_HOOK_STOP": "1"})
    assert message_text(s3_admin, sent(live_relay)[0]) == "From the transcript."


def test_hook_skips_when_stop_hook_active(live_relay):
    before = sent(live_relay)
    p = run(HOOK, [], json.dumps({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "Nope."}),
            live_relay, extra={"EARSHOT_HOOK_STOP": "1"})
    assert p.returncode == 0 and sent(live_relay) == before


@pytest.mark.parametrize("stdin, token, url_override", [
    ("not json", TOKEN, None),
    ("[]", TOKEN, None),
    (json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Hi."}), None, None),  # no token
    (json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Hi."}), "z" * 40, None),  # wrong token
    (json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Hi."}), TOKEN, "http://127.0.0.1:9"),  # relay down
])
def test_hook_always_exits_zero(live_relay, stdin, token, url_override):
    assert run(HOOK, [], stdin, url_override or live_relay, token).returncode == 0


def test_hook_settings_json_is_valid_and_async():
    cfg = json.loads((EX / "claude-code" / "settings.hooks.json").read_text())
    assert set(cfg["hooks"]["Notification"][0]["matcher"].split("|")) == set(hook.NOTIFICATION_LINES)
    for event in ("Notification", "Stop"):
        h = cfg["hooks"][event][0]["hooks"][0]
        assert h["type"] == "command" and h["async"] is True and h["timeout"] <= 10 and "earshot_hook.py" in h["command"]


# ---- Codex notify ---------------------------------------------------------------------------------------------


def test_codex_notify(live_relay, s3_admin):
    event = {"type": "agent-turn-complete", "thread-id": "t", "turn-id": "1", "cwd": "/work/api",
             "input-messages": ["go"], "last-assistant-message": "Refactor complete. Tests green. Anything else?"}
    p = run(NOTIFY, [json.dumps(event)], None, live_relay)
    assert p.returncode == 0
    assert message_text(s3_admin, sent(live_relay)[0]) == "Refactor complete. Tests green."
    before = sent(live_relay)
    run(NOTIFY, [json.dumps({**event, "type": "something-else"})], None, live_relay)
    assert sent(live_relay) == before
    assert run(NOTIFY, ["garbage"], None, live_relay).returncode == 0


# ---- curl.sh --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("jq") is None or shutil.which("curl") is None, reason="needs curl and jq")
def test_curl_script(live_relay, s3_admin):
    env = {**os.environ, "EARSHOT_API_URL": live_relay, "EARSHOT_API_TOKEN": TOKEN, "EARSHOT_WAIT": "1"}
    p = subprocess.run([str(EX / "curl.sh"), "Build finished"], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    assert '"status": "stored"' in p.stdout
    assert message_text(s3_admin, sent(live_relay)[0]) == "Build finished"


# ---- Tasker task file ------------------------------------------------------------------------------------------

TASKER = EX / "tasker" / "Earshot_Say.tsk.xml"


def tasker_request(text: str, url: str, token: str) -> httpx.Request:
    """Build the request Tasker's HTTP Request action (code 339) sends: plain %var substitution, no escaping."""
    import xml.etree.ElementTree as ET

    action = next(a for a in ET.parse(TASKER).getroot().iter("Action") if a.findtext("code") == "339")
    args = {e.get("sr"): (e.text or e.get("val") or "") for e in action}
    assert args["arg1"] == "1"  # POST

    def sub(value: str) -> str:
        return value.replace("%EARSHOT_URL", url).replace("%EARSHOT_TOKEN", token).replace("%par1", text)

    headers = dict(line.split(":", 1) for line in sub(args["arg3"]).splitlines())
    return httpx.Request("POST", sub(args["arg2"]), headers=headers, content=sub(args["arg5"]).encode())


def test_tasker_task_file_holds_no_token():
    body = TASKER.read_text()
    assert "%EARSHOT_TOKEN" in body and "%EARSHOT_URL" in body
    assert TOKEN not in body


def test_tasker_task_sends_and_stores(live_relay, s3_admin):
    with httpx.Client() as client:
        r = client.send(tasker_request("Hello from Tasker.", live_relay, TOKEN))
    assert r.status_code == 202, r.text
    assert message_text(s3_admin, sent(live_relay)[0]) == "Hello from Tasker."


def test_tasker_task_rejects_a_raw_quote_as_the_recipe_says(live_relay):
    with httpx.Client() as client:
        r = client.send(tasker_request('say "hi"', live_relay, TOKEN))
    assert r.status_code == 400
    assert sent(live_relay) == []
