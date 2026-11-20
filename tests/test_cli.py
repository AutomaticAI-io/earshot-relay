"""The CLI against a live relay (uvicorn in a thread) and a moto bucket."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import httpx
import pytest

from earshot_relay.beacons import iso_ms
from earshot_relay.cli import format_receipt, main

from .conftest import BEACON_TOKEN, BUCKET, TOKEN


@pytest.fixture
def cli(live_relay, monkeypatch, capsys):
    monkeypatch.setenv("EARSHOT_API_URL", live_relay)
    monkeypatch.setenv("EARSHOT_API_TOKEN", TOKEN)

    def run(*argv: str):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    run.url = live_relay
    return run


def post_beacon(base: str, key: str, last_modified: str, phase: str, at: str, error: str | None = None):
    body = {"channel": "phone", "key": key, "phase": phase, "at": at, "last_modified": last_modified,
            "device": "pytest-phone", "app_version": "0.1.11", "clock_skew_ms": 0}
    if error:
        body["error"] = error
    r = httpx.post(f"{base}/v1/beacons/{BEACON_TOKEN}", json=body)
    assert r.status_code == 200, r.text


def test_say_prints_the_id_and_receipt_round_trip(cli, s3_admin):
    code, out, _ = cli("say", "hello", "from", "the", "cli")
    assert code == 0
    msg_id = out.strip()
    assert msg_id.startswith("msg_")
    key = f"phone/{msg_id[4:]}.say"
    head = s3_admin.head_object(Bucket=BUCKET, Key=key)
    lm = iso_ms(head["LastModified"].astimezone(timezone.utc))
    now = datetime.now(timezone.utc)
    code, out, _ = cli("receipt", msg_id)
    assert code == 0 and "status: stored" in out
    assert out.rstrip().endswith("receipt = device playback, not proof a person heard it")
    for phase in ("detected", "started", "finished"):
        post_beacon(cli.url, key, lm, phase, iso_ms(now))
    code, out, _ = cli("receipt", msg_id, "--wait", "5")
    assert code == 0 and "status: finished" in out and "pytest-phone" in out
    code, out, _ = cli("receipt", "latest", "--json")
    assert json.loads(out)["status"] == "finished"
    code, out, _ = cli("ls", "-n", "3")
    assert msg_id in out and "finished" in out


def test_receipt_exit_codes(cli, s3_admin):
    _, out, _ = cli("say", "skip me")
    msg_id = out.strip()
    key = f"phone/{msg_id[4:]}.say"
    lm = iso_ms(s3_admin.head_object(Bucket=BUCKET, Key=key)["LastModified"].astimezone(timezone.utc))
    code, out, _ = cli("receipt", msg_id, "--wait", "1")
    assert code == 4 and "status: stored" in out  # timed out waiting
    post_beacon(cli.url, key, lm, "finished", iso_ms(datetime.now(timezone.utc)), error="skipped")
    code, out, _ = cli("receipt", msg_id, "--wait", "5")
    assert code == 3 and "status: skipped" in out


def test_drop_sends_audio(cli, tmp_path, s3_admin):
    f = tmp_path / "ding.mp3"
    f.write_bytes(b"ID3" + b"\x00" * 600)
    code, out, _ = cli("drop", str(f), "--json")
    assert code == 0
    body = json.loads(out)
    assert body["type"] == "audio" and body["key"].endswith(".mp3")
    assert s3_admin.get_object(Bucket=BUCKET, Key=body["key"])["ContentType"] == "audio/mpeg"


def test_say_reads_stdin(cli, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    code, out, _ = cli("say", "-", "--json")
    assert code == 0 and json.loads(out)["status"] == "stored"


def test_errors_exit_1_with_a_readable_message(cli):
    code, out, err = cli("say", "x", "-c", "alarm")
    assert code == 1 and "channel_not_allowed" in err and out == ""
    code, _, err = cli("say", "a" * 4001)
    assert code == 1 and "text_too_long" in err


def test_wrong_token_is_reported(cli, monkeypatch):
    monkeypatch.setenv("EARSHOT_API_TOKEN", "z" * 40)
    code, _, err = cli("status")
    assert code == 1 and ("401" in err or "unauthorized" in err.lower())
    code, _, err = cli("doctor")
    assert code == 1 and "does not match" in err


def test_unreachable_relay_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("EARSHOT_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EARSHOT_API_TOKEN", TOKEN)
    assert main(["status"]) == 1
    assert capsys.readouterr().err.startswith("earshot-relay:")


def test_status_and_doctor(cli, s3_admin):
    code, out, _ = cli("status")
    assert code == 0 and "channels   phone" in out and "receipts   beacon URL for the app: https://relay.example.com/v1/beacons/" in out
    code, out, _ = cli("doctor")
    assert code == 0 and "ok   relay" in out and "test object: phone/relay-doctor-" in out
    code, out, _ = cli("doctor", "--json")
    assert code == 0 and json.loads(out)["ok"] is True


def test_token_command(capsys):
    assert main(["token"]) == 0
    tok = capsys.readouterr().out.strip()
    assert len(tok) >= 43


def test_format_receipt_is_plain_text():
    text = format_receipt({"id": "msg_1", "type": "text", "speech": "device", "channel": "phone", "key": "phone/a.say",
                           "created_at": "2026-11-20T13:45:25.600Z", "object_last_modified": None, "hops": [],
                           "status": "stored", "status_at": "2026-11-20T13:45:25.600Z", "hint": "h"})
    assert "status: stored at 2026-11-20T13:45:25.600Z" in text and "hint: h" in text
