"""Regression tests for the v1 review: config guards, beacon hardening, rate-limit refunds, idempotency, auth parsing."""

from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.beacons import receipt_is_final, validate_beacon
from earshot_relay.cli import format_receipt
from earshot_relay.config import ConfigError, load_settings
from earshot_relay.service import MAX_DEVICES_PER_KEY, derive_receipt
from earshot_relay.store import BeaconRow, MessageRow, Store

from .conftest import BEACON_TOKEN, TOKEN, base_env
from .test_api import assert_error, beacon_for, send

ROOT = Path(__file__).resolve().parent.parent
AT = "2026-11-20T13:45:28.723Z"


def env(tmp_path, **extra):
    return base_env("https://s3.example.com", str(tmp_path), **extra)


def dotenv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


# ---- config ----------------------------------------------------------------------------------------------------


def test_env_example_verbatim_does_not_start(tmp_path):
    values = dotenv(ROOT / ".env.example")
    with pytest.raises(ConfigError) as e:
        load_settings({**values, "EARSHOT_RELAY_DATA_DIR": str(tmp_path)}, check_ffmpeg=False)
    assert "EARSHOT_RELAY_TOKEN is required" in str(e.value)
    assert "AWS_ACCESS_KEY_ID still holds a placeholder" in str(e.value)


@pytest.mark.parametrize("var, value", [
    ("EARSHOT_RELAY_TOKEN", "replace-with-a-long-random-token"),
    ("EARSHOT_RELAY_BEACON_TOKEN", "replace-with-a-different-long-random-token"),
    ("AWS_SECRET_ACCESS_KEY", "replace-me"),
])
def test_placeholder_secrets_are_refused(tmp_path, var, value):
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, **{var: value}), check_ffmpeg=False)
    assert f"{var} still holds a placeholder" in str(e.value)
    assert value not in str(e.value)


def test_default_channel_can_never_be_alarm(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, EARSHOT_RELAY_CHANNELS="phone,alarm", EARSHOT_RELAY_DEFAULT_CHANNEL="alarm"),
                      check_ffmpeg=False)
    assert "cannot be alarm" in str(e.value)
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, EARSHOT_RELAY_CHANNELS="alarm"), check_ffmpeg=False)
    assert "besides alarm" in str(e.value)


def test_default_channel_is_phone_else_the_first_quiet_channel(tmp_path):
    s = load_settings(env(tmp_path, EARSHOT_RELAY_CHANNELS="deploys,phone"), check_ffmpeg=False)
    assert s.default_channel == "phone"
    s = load_settings(env(tmp_path, EARSHOT_RELAY_CHANNELS="alarm,deploys,ci"), check_ffmpeg=False)
    assert s.default_channel == "deploys"


def test_tts_url_credentials_and_missing_voice_are_refused(tmp_path):
    base = {"EARSHOT_RELAY_TTS_URL": "https://user:sekrit-pass@tts.example.com/v1", "EARSHOT_RELAY_TTS_MODEL": "m",
            "EARSHOT_RELAY_TTS_VOICE": "alloy"}
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, **base), check_ffmpeg=False)
    assert "must not carry credentials" in str(e.value) and "sekrit-pass" not in str(e.value)
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, **{**base, "EARSHOT_RELAY_TTS_URL": "https://tts.example.com/v1",
                                       "EARSHOT_RELAY_TTS_VOICE": None}), check_ffmpeg=False)
    assert "EARSHOT_RELAY_TTS_VOICE is required" in str(e.value)


# ---- beacon hardening ------------------------------------------------------------------------------------------


def good_beacon(**over):
    return {"channel": "phone", "key": "phone/a.say", "phase": "detected", "at": AT,
            "last_modified": "2026-11-20T13:45:26.163Z", "device": "dev-1", "app_version": "0.1.11",
            "clock_skew_ms": -278, **over}


@pytest.mark.parametrize("over, why", [
    ({"at": "0001-01-01T00:00:00+01:00"}, "bad at"),
    ({"at": "9999-12-31T23:59:59Z"}, "bad at"),
    ({"at": "1999-12-31T23:59:59Z"}, "bad at"),
    ({"last_modified": "0001-01-01T00:00:00+01:00"}, "bad last_modified"),
    ({"last_modified": "9999-12-31T23:30:00-01:00"}, "bad last_modified"),
    ({"device": "\x1b]52;c;Y3VybCBldmlsLnNofHNoCg==\x07"}, "bad device"),
    ({"app_version": "\x1b[2J"}, "bad app_version"),
    ({"device": "dev\u009b31m"}, "bad device"),
])
def test_out_of_range_times_and_control_characters_are_refused(over, why):
    clean, reason = validate_beacon(good_beacon(**over))
    assert clean is None and reason == why


def test_c1_controls_in_error_are_replaced():
    clean, _ = validate_beacon(good_beacon(phase="failed", error="boom\u009b2J\x1bend"))
    assert clean["error"] == "boom 2J end"


def test_overflow_beacons_never_500(client, auth):
    sent = send(client, auth).json()
    url = f"/v1/beacons/{BEACON_TOKEN}"
    r = client.post(url, json=beacon_for(sent["key"], "detected", "0001-01-01T00:00:00+01:00"))
    assert_error(r, 400, "invalid_beacon")
    r = client.post(url, json={**beacon_for(sent["key"], "detected", "9999-12-31T23:59:59Z"), "clock_skew_ms": -86_400_000})
    assert_error(r, 400, "invalid_beacon")
    assert client.get(f"/v1/messages/{sent['id']}", headers=auth).status_code == 200
    assert client.get("/v1/messages", headers=auth).status_code == 200


def test_a_bad_stored_row_cannot_break_a_receipt():
    msg = MessageRow(1, "msg_x", "2026-11-20T13:45:26.000Z", "phone", "text", "device", "phone/a.say", 5, 5)
    rows = [BeaconRow("phone/a.say", "phone", "detected", "evil", "1", "9999-12-31T23:59:59Z", None, -86_400_000,
                      None, AT),
            BeaconRow("phone/a.say", "phone", "finished", "dev-1", "0.1.11", AT, None, None, None, AT)]
    from datetime import datetime, timezone
    r = derive_receipt(msg, rows, now=datetime(2026, 11, 20, 14, tzinfo=timezone.utc), wait_min=60, beacons_ever=True)
    assert r["status"] == "finished" and [h["device"] for h in r["hops"]] == ["dev-1"]


def test_cli_escapes_control_characters_from_stored_beacons():
    receipt = {"id": "msg_x", "type": "text", "speech": "device", "channel": "phone", "key": "phone/a.say",
               "created_at": AT, "status": "failed", "status_at": AT, "object_last_modified": None, "hint": None,
               "devices": [], "hops": [{"phase": "failed", "device": "d\x1b[2J", "at_server": AT, "since_upload_ms": 1,
                                        "clock_skew_ms": None, "error": "x\x9b31m"}]}
    text = format_receipt(receipt)
    assert "\x1b" not in text and "\x9b" not in text
    assert "d\\x1b[2J" in text and "x\\x9b31m" in text


def test_beacon_rate_limit_answers_429(make_relay):
    tick = {"t": 1000.0}
    relay = make_relay(EARSHOT_RELAY_BEACON_RATE_LIMIT="2/min", monotonic=lambda: tick["t"])
    url = f"/v1/beacons/{BEACON_TOKEN}"
    with TestClient(create_app(relay)) as c:
        assert c.post(url, json=good_beacon()).status_code == 200
        assert c.post(url, json=good_beacon(phase="started")).status_code == 200
        r = c.post(url, json=good_beacon(phase="finished"))
        assert_error(r, 429, "rate_limited")
        assert int(r.headers["retry-after"]) >= 1
        assert_error(c.post("/v1/beacons/" + "z" * 40, json=good_beacon()), 404, "not_found")  # token still first
        tick["t"] += 61
        assert c.post(url, json=good_beacon(phase="finished")).status_code == 200


def test_devices_per_key_are_capped(client, auth):
    url = f"/v1/beacons/{BEACON_TOKEN}"
    for i in range(MAX_DEVICES_PER_KEY):
        assert client.post(url, json=good_beacon(device=f"d{i}")).status_code == 200
    assert_error(client.post(url, json=good_beacon(device="one-too-many")), 400, "invalid_beacon")
    # A device that already reported this key can still add phases.
    assert client.post(url, json=good_beacon(device="d0", phase="finished")).status_code == 200
    assert len(client.get("/v1/status", headers=auth).json()["receipts"]["devices"]) <= 20


# ---- multi-device wait -----------------------------------------------------------------------------------------


def test_one_missed_device_is_not_final_while_another_plays():
    assert not receipt_is_final({"status": "missed", "devices": [{"status": "missed"}, {"status": "started"}]})
    assert receipt_is_final({"status": "missed", "devices": [{"status": "missed"}, {"status": "failed"}]})
    assert receipt_is_final({"status": "finished", "devices": [{"status": "finished"}, {"status": "detected"}]})
    assert not receipt_is_final({"status": "stored", "devices": []})


def test_wait_keeps_polling_while_a_device_is_still_playing(client, auth):
    sent = send(client, auth).json()
    url = f"/v1/beacons/{BEACON_TOKEN}"
    client.post(url, json=beacon_for(sent["key"], "started", AT, device="A"))
    client.post(url, json=beacon_for(sent["key"], "missed", AT, device="B", error="missed: stale"))
    r = client.get(f"/v1/messages/{sent['id']}?wait=1", headers=auth).json()
    assert r["status"] == "missed" and not receipt_is_final(r)


# ---- rate-limit refunds ----------------------------------------------------------------------------------------


def test_failed_writes_do_not_use_up_the_rate_limit(make_relay, auth):
    relay = make_relay(EARSHOT_BUCKET="no-such-bucket-here", EARSHOT_RELAY_CHANNELS="phone,alarm",
                       EARSHOT_RELAY_ALARM_RATE_LIMIT="2/hour", EARSHOT_RELAY_RATE_LIMIT="3/min")
    with TestClient(create_app(relay)) as c:
        for _ in range(5):
            assert_error(send(c, auth, channel="alarm"), 502, "storage_error")
    assert len(relay.limiter.alarm_window.events) == 0
    assert len(relay.limiter.global_window.events) == 0


# ---- idempotency -----------------------------------------------------------------------------------------------


def test_idempotency_key_returns_the_original_202(client, auth, s3_admin):
    h = {**auth, "Idempotency-Key": "build-42"}
    first = client.post("/v1/messages", headers=h, json={"text": "Deploy done"})
    second = client.post("/v1/messages", headers=h, json={"text": "Deploy done"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true" and "idempotent-replayed" not in first.headers
    assert second.headers["location"] == first.headers["location"]
    other = client.post("/v1/messages", headers={**auth, "Idempotency-Key": "build-43"}, json={"text": "Deploy done"})
    assert other.json()["id"] != first.json()["id"]
    plain = [send(client, auth).json()["id"] for _ in range(2)]
    assert plain[0] != plain[1]


def test_idempotency_applies_to_audio_and_skips_the_rate_limit(make_relay, auth):
    tick = {"t": 1000.0}
    relay = make_relay(EARSHOT_RELAY_RATE_LIMIT="1/min", monotonic=lambda: tick["t"])
    h = {**auth, "Idempotency-Key": "clip-1"}
    with TestClient(create_app(relay)) as c:
        a = c.post("/v1/messages", headers=h, files={"file": ("a.wav", b"RIFF" + b"\x00" * 300, "audio/wav")})
        b = c.post("/v1/messages", headers=h, files={"file": ("a.wav", b"RIFF" + b"\x00" * 300, "audio/wav")})
        assert a.status_code == b.status_code == 202 and a.json() == b.json()


def test_idempotency_window_is_24_hours(make_relay, auth):
    from datetime import timedelta

    from earshot_relay.store import now_utc
    clock = {"now": now_utc()}
    relay = make_relay(clock=lambda: clock["now"])
    h = {**auth, "Idempotency-Key": "nightly"}
    with TestClient(create_app(relay)) as c:
        first = c.post("/v1/messages", headers=h, json={"text": "night"}).json()
        clock["now"] += timedelta(hours=23)
        assert c.post("/v1/messages", headers=h, json={"text": "night"}).json()["id"] == first["id"]
        clock["now"] += timedelta(hours=2)
        assert c.post("/v1/messages", headers=h, json={"text": "night"}).json()["id"] != first["id"]


def test_concurrent_retries_with_one_key_send_once(live_relay, auth):
    import httpx

    h = {**auth, "Idempotency-Key": "race"}

    def post(_):
        return httpx.post(f"{live_relay}/v1/messages", headers=h, json={"text": "once"}, timeout=10).json()["id"]

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        ids = set(pool.map(post, range(8)))
    assert len(ids) == 1


def test_bad_idempotency_key_is_422(client, auth):
    r = client.post("/v1/messages", headers={**auth, "Idempotency-Key": "x" * 256}, json={"text": "hi"})
    assert_error(r, 422, "invalid_field", "Idempotency-Key")


def test_idempotency_key_is_stored_hashed(relay, auth):
    with TestClient(create_app(relay)) as c:
        c.post("/v1/messages", headers={**auth, "Idempotency-Key": "visible-key-value"}, json={"text": "hi"})
    db = sqlite3.connect(relay.settings.db_path)
    (stored,) = db.execute("SELECT idem_key FROM messages").fetchone()
    db.close()
    assert stored and "visible-key-value" not in stored and len(stored) == 64


def test_version_1_database_is_migrated(tmp_path):
    path = tmp_path / "relay.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE messages(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, channel TEXT NOT NULL, type TEXT NOT NULL,
          speech TEXT, key TEXT UNIQUE NOT NULL, bytes INTEGER NOT NULL, text_chars INTEGER);
        INSERT INTO messages VALUES('msg_old', '2026-11-20T00:00:00.000Z', 'phone', 'text', 'device', 'phone/o.say', 1, 1);
        PRAGMA user_version=1;
    """)
    db.close()
    store = Store(str(path))
    try:
        assert store.get_message("msg_old").key == "phone/o.say"
        store.add_message(id="msg_new", created_at="2026-11-20T01:00:00.000Z", channel="phone", type="text",
                          speech="device", key="phone/n.say", bytes=1, text_chars=1, idem_key="h" * 64)
        assert store.get_by_idem("h" * 64, "2026-11-19T00:00:00.000Z").id == "msg_new"
    finally:
        store.close()


# ---- request parsing -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header", [f"bearer {TOKEN}", f"BEARER {TOKEN}", f"Bearer  {TOKEN}", f"Bearer\t{TOKEN}",
                                    f"  Bearer {TOKEN}  "])
def test_bearer_scheme_is_case_and_space_tolerant(client, header):
    assert client.get("/v1/status", headers={"Authorization": header}).status_code == 200


def test_deeply_nested_json_is_400(client, auth):
    body = b'{"text":' + b"[" * 200_000
    r = client.post("/v1/messages", headers={**auth, "Content-Type": "application/json"}, content=body)
    assert_error(r, 400, "bad_json")


def test_empty_multipart_channel_is_invalid_not_default(client, auth):
    r = client.post("/v1/messages", headers=auth, files={"file": ("a.wav", b"RIFF" + b"\x00" * 300, "audio/wav")},
                    data={"channel": ""})
    assert_error(r, 422, "invalid_channel", "channel")
