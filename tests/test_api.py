"""HTTP API against a moto S3 server: auth, errors, sends, receipts, status, doctor, limits, logs."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app, create_beacon_app
from earshot_relay.beacons import iso_ms
from earshot_relay.store import now_utc

from .conftest import BEACON_TOKEN, BUCKET, TOKEN, golden_beacons

SECRET_PHRASE = "the vault code is zebra-42"


def send(client, auth, text="Build finished", **extra):
    return client.post("/v1/messages", headers=auth, json={"text": text, **extra})


def beacon_for(key, phase, at, *, device="dev-1", skew=-278, error=None, lm="2026-11-20T13:45:26.163Z"):
    body = {"channel": key.split("/")[0], "key": key, "phase": phase, "at": at, "last_modified": lm, "device": device,
            "app_version": "0.1.11", "clock_skew_ms": skew}
    if error:
        body["error"] = error
    return body


def assert_error(resp, status, code, field=None):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    if field:
        assert body["error"]["field"] == field
    return body


# ---- auth ------------------------------------------------------------------------------------------------------


def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("header", [None, "Bearer wrong-token-wrong-token-wrong-token", f"Bearer {BEACON_TOKEN}",
                                    TOKEN, f"Basic {TOKEN}", f"bearer {TOKEN}x"])
def test_api_rejects_missing_or_wrong_bearer(client, header):
    headers = {"Authorization": header} if header else {}
    for method, path in (("GET", "/v1/status"), ("POST", "/v1/messages"), ("GET", "/v1/messages/latest"),
                         ("POST", "/v1/doctor"), ("GET", "/openapi.json"), ("POST", "/mcp")):
        r = client.request(method, path, headers=headers, json={"text": "hi"})
        assert_error(r, 401, "unauthorized")
        assert r.headers["www-authenticate"].startswith("Bearer")


def test_api_token_is_not_a_beacon_token(client, auth):
    r = client.post(f"/v1/beacons/{TOKEN}", json=beacon_for("phone/x.say", "detected", "2026-11-20T13:45:28.723Z"))
    assert_error(r, 404, "not_found")
    r = client.post(f"/v1/beacons/{BEACON_TOKEN}", json=beacon_for("phone/x.say", "detected", "2026-11-20T13:45:28.723Z"))
    assert r.status_code == 200 and r.content == b""
    # A bearer header does not open the beacon route with the wrong path token either.
    r = client.post("/v1/beacons/" + "z" * 40, headers=auth, json={})
    assert_error(r, 404, "not_found")


def test_beacon_route_is_404_when_receipts_are_off(make_relay):
    relay = make_relay(EARSHOT_RELAY_BEACON_TOKEN=None)
    with TestClient(create_app(relay)) as c:
        r = c.post(f"/v1/beacons/{BEACON_TOKEN}", json={})
        assert_error(r, 404, "not_found")


# ---- sending ---------------------------------------------------------------------------------------------------


def test_text_send_writes_a_say(client, auth, s3_admin):
    r = send(client, auth, "  Build finished \U0001F600 ")
    assert r.status_code == 202, r.text
    body = r.json()
    assert re.fullmatch(r"msg_[0-9a-f]{32}", body["id"])
    assert body["key"] == f"phone/{body['id'][4:]}.say"
    assert body == {**body, "type": "text", "speech": "device", "channel": "phone", "status": "stored",
                    "receipt_url": f"/v1/messages/{body['id']}"}
    assert r.headers["location"] == f"/v1/messages/{body['id']}"
    obj = s3_admin.get_object(Bucket=BUCKET, Key=body["key"])
    data = obj["Body"].read()
    assert data == "Build finished \U0001F600".encode()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert obj["ContentType"] == "text/plain; charset=utf-8"


def test_audio_send(client, auth, s3_admin):
    payload = b"ID3" + b"\x00" * 500
    r = client.post("/v1/messages", headers=auth, files={"file": ("clip.MP3", payload, "application/octet-stream")})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["type"] == "audio" and body["speech"] is None
    assert re.fullmatch(r"phone/[0-9a-f]{32}\.mp3", body["key"])
    obj = s3_admin.get_object(Bucket=BUCKET, Key=body["key"])
    assert obj["Body"].read() == payload
    assert obj["ContentType"] == "audio/mpeg"


def test_audio_without_extension_uses_content_type(client, auth, s3_admin):
    r = client.post("/v1/messages", headers=auth, files={"file": ("recording", b"OggS" + b"\x00" * 300, "audio/ogg")},
                    data={"channel": "phone"})
    assert r.status_code == 202, r.text
    assert r.json()["key"].endswith(".ogg")
    assert s3_admin.head_object(Bucket=BUCKET, Key=r.json()["key"])["ContentType"] == "audio/ogg"


def test_audio_errors(make_relay, auth):
    relay = make_relay(EARSHOT_RELAY_MAX_AUDIO_BYTES="2048")
    with TestClient(create_app(relay)) as c:
        assert_error(c.post("/v1/messages", headers=auth, files={"file": ("big.wav", b"x" * 4096, "audio/wav")}),
                     413, "too_large", "file")
        assert_error(c.post("/v1/messages", headers=auth, files={"file": ("huge.wav", b"x" * 200_000, "audio/wav")}),
                     413, "too_large")
        assert_error(c.post("/v1/messages", headers=auth, files={"file": ("empty.wav", b"", "audio/wav")}),
                     422, "empty_file", "file")
        assert_error(c.post("/v1/messages", headers=auth, files={"file": ("notes.txt", b"hello", "text/plain")}),
                     415, "unsupported_media_type")
        assert_error(c.post("/v1/messages", headers=auth, files={"other": ("a.wav", b"x" * 10, "audio/wav")}),
                     422, "unknown_field")
        assert_error(c.post("/v1/messages", headers=auth, data={"channel": "phone"},
                            files={"x": ("", b"", "text/plain")}), 422, "unknown_field")
        assert_error(c.post("/v1/messages", headers=auth, files={"file": ("a.wav", b"x" * 10, "audio/wav")},
                            data={"channel": "Nope"}), 422, "invalid_channel", "channel")


def test_text_errors(client, auth):
    assert_error(client.post("/v1/messages", headers={**auth, "Content-Type": "application/json"}, content=b"{nope"),
                 400, "bad_json")
    assert_error(client.post("/v1/messages", headers=auth, json=["text"]), 400, "bad_json")
    assert_error(client.post("/v1/messages", headers=auth, json={"txt": "hi"}), 422, "unknown_field", "txt")
    assert_error(client.post("/v1/messages", headers=auth, json={"channel": "phone"}), 422, "invalid_field", "text")
    assert_error(send(client, auth, "   "), 422, "empty_text", "text")
    assert_error(send(client, auth, "a" * 4001), 422, "text_too_long", "text")
    assert send(client, auth, "a" * 3998 + "\U0001F600").status_code == 202
    assert_error(send(client, auth, "a" * 3999 + "\U0001F600"), 422, "text_too_long")
    assert_error(send(client, auth, channel="Phone"), 422, "invalid_channel", "channel")
    assert_error(send(client, auth, channel="car"), 403, "channel_not_allowed", "channel")
    body = assert_error(send(client, auth, channel="alarm"), 403, "channel_not_allowed")
    assert "alarm is loud" in body["error"]["message"]
    assert_error(send(client, auth, speech="loud"), 422, "invalid_speech", "speech")
    assert_error(send(client, auth, speech="server"), 422, "tts_not_configured", "speech")
    assert_error(send(client, auth, voice="af_bella"), 422, "invalid_voice", "voice")
    assert_error(send(client, auth, channel=5), 422, "invalid_field", "channel")
    assert_error(client.post("/v1/messages", headers={**auth, "Content-Type": "text/plain"}, content=b"hi"),
                 415, "unsupported_media_type")
    assert_error(client.post("/v1/messages", headers={**auth, "Content-Type": "application/json"},
                             content=b'{"text":"' + b"a" * 300_000 + b'"}'), 413, "too_large")


def test_alarm_when_allowed(make_relay, auth, s3_admin):
    relay = make_relay(EARSHOT_RELAY_CHANNELS="phone,alarm")
    with TestClient(create_app(relay)) as c:
        r = send(c, auth, "Prod is down", channel="alarm")
        assert r.status_code == 202
        assert r.json()["key"].startswith("alarm/")


def test_unknown_routes_and_messages_use_the_error_schema(client, auth):
    assert_error(client.get("/v1/nope", headers=auth), 404, "not_found")
    assert_error(client.get("/v1/messages/msg_" + "0" * 32, headers=auth), 404, "not_found")
    assert_error(client.get("/v1/messages/latest", headers=auth), 404, "not_found")
    assert_error(client.put("/v1/messages", headers=auth), 405, "method_not_allowed")
    assert_error(client.get("/v1/messages?limit=0", headers=auth), 422, "invalid_field", "limit")
    assert_error(client.get("/v1/messages/latest?wait=61", headers=auth), 422, "invalid_field", "wait")


# ---- the minimal-permission guarantee --------------------------------------------------------------------------


def test_only_put_object_is_called(relay, auth):
    calls, headers = [], []
    events = relay.writer.client.meta.events
    events.register("before-call.s3", lambda model, **kw: calls.append(model.name))
    events.register("before-send.s3", lambda request, **kw: headers.append({k.lower() for k in request.headers.keys()}))
    with TestClient(create_app(relay)) as c:
        mid = send(c, auth).json()["id"]
        c.post("/v1/messages", headers=auth, files={"file": ("a.wav", b"RIFF" + b"\x00" * 100, "audio/wav")})
        c.get(f"/v1/messages/{mid}", headers=auth)
        c.get("/v1/messages", headers=auth)
        c.get("/v1/status", headers=auth)
        c.post("/v1/doctor", headers=auth)
        c.post(f"/v1/beacons/{BEACON_TOKEN}", json=beacon_for("phone/x.say", "detected", "2026-11-20T13:45:28.723Z"))
    assert calls and set(calls) == {"PutObject"}, calls
    for h in headers:  # when_required: no flexible checksum on PutObject (S3-compatible stores reject it)
        assert not any(name.startswith("x-amz-checksum-") or name == "x-amz-sdk-checksum-algorithm" for name in h), h


def test_checksum_config_is_when_required(relay):
    cfg = relay.writer.client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.s3["addressing_style"] == "path"
    assert cfg.signature_version == "s3v4"


def test_storage_error_is_502_without_secrets(make_relay, auth, caplog):
    relay = make_relay(EARSHOT_BUCKET="no-such-bucket-here")
    with TestClient(create_app(relay)) as c:
        body = assert_error(send(c, auth), 502, "storage_error")
    assert "NoSuchBucket" in body["error"]["message"]
    for secret in ("writer-secret-value", TOKEN, BEACON_TOKEN):
        assert secret not in json.dumps(body)


# ---- receipts --------------------------------------------------------------------------------------------------


def test_receipt_end_to_end_with_golden_beacons(client, auth):
    sent = send(client, auth).json()
    r = client.get(f"/v1/messages/{sent['id']}", headers=auth).json()
    assert r["status"] == "stored" and r["hint"].startswith("no beacon has reached this relay")
    say = [g["body"] for g in golden_beacons() if g["body"]["key"].endswith(".say")]
    for body in reversed(say):  # out of order on purpose
        assert client.post(f"/v1/beacons/{BEACON_TOKEN}", json={**body, "key": sent["key"]}).status_code == 200
    # A retried beacon is a no-op 200.
    assert client.post(f"/v1/beacons/{BEACON_TOKEN}", json={**say[0], "key": sent["key"]}).status_code == 200
    r = client.get(f"/v1/messages/{sent['id']}", headers=auth).json()
    assert r["status"] == "finished"
    assert [h["phase"] for h in r["hops"]] == ["detected", "started", "finished"]
    assert r["hops"][0]["since_upload_ms"] == 2838
    assert r["object_last_modified"] == "2026-11-20T13:45:26.163Z"
    assert r["hint"] is None
    latest = client.get("/v1/messages/latest", headers=auth).json()
    assert latest["id"] == sent["id"]
    # wait on a terminal message returns at once
    t0 = time.monotonic()
    assert client.get(f"/v1/messages/{sent['id']}?wait=30", headers=auth).json()["status"] == "finished"
    assert time.monotonic() - t0 < 2


def test_receipt_wait_times_out_on_stored(client, auth):
    sent = send(client, auth).json()
    t0 = time.monotonic()
    r = client.get(f"/v1/messages/{sent['id']}?wait=1", headers=auth).json()
    assert r["status"] == "stored"
    assert 0.9 <= time.monotonic() - t0 < 5


def test_receipt_wait_wakes_on_beacon(client, auth):
    sent = send(client, auth).json()
    result = {}

    def waiter():
        t0 = time.monotonic()
        result["receipt"] = client.get(f"/v1/messages/{sent['id']}?wait=20", headers=auth).json()
        result["took"] = time.monotonic() - t0

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.5)
    now = iso_ms(now_utc())
    client.post(f"/v1/beacons/{BEACON_TOKEN}", json=beacon_for(sent["key"], "finished", now, lm=now))
    t.join(25)
    assert result["receipt"]["status"] == "finished"
    assert result["took"] < 5


def test_unconfirmed_after_wait_window(make_relay, auth):
    clock = {"now": now_utc()}
    relay = make_relay(EARSHOT_RELAY_RECEIPT_WAIT_MIN="1", clock=lambda: clock["now"])
    with TestClient(create_app(relay)) as c:
        sent = send(c, auth).json()
        clock["now"] += timedelta(minutes=2)
        r = c.get(f"/v1/messages/{sent['id']}", headers=auth).json()
        assert r["status"] == "unconfirmed" and "beacon" in r["hint"]


def test_beacon_validation_over_http(client):
    url = f"/v1/beacons/{BEACON_TOKEN}"
    assert_error(client.post(url, json={"channel": "phone"}), 400, "invalid_beacon")
    assert_error(client.post(url, content=b"not json", headers={"Content-Type": "application/json"}), 400, "bad_json")
    big = beacon_for("phone/x.say", "failed", "2026-11-20T13:45:28.723Z", error="e" * 5000)
    assert_error(client.post(url, json=big), 413, "too_large")
    hb = {"channel": "phone", "phase": "heartbeat", "at": "2026-11-20T13:45:28.723Z", "device": "d", "app_version": "1"}
    assert client.post(url, json=hb).status_code == 200


def test_foreign_beacons_are_stored_and_show_in_status(client, auth):
    client.post(f"/v1/beacons/{BEACON_TOKEN}",
                json=beacon_for("phone/hand-uploaded.say", "finished", "2026-11-20T13:45:28.723Z", device="pixel"))
    s = client.get("/v1/status", headers=auth).json()
    assert s["receipts"]["devices"][0]["device"] == "pixel"
    assert s["receipts"]["devices"][0]["last_phase"] == "finished"
    assert s["receipts"]["devices"][0]["clock_skew_ms"] == -278
    assert s["receipts"]["last_beacon_at"]


# ---- listing, status, doctor -----------------------------------------------------------------------------------


def test_list_messages(make_relay, auth):
    relay = make_relay(EARSHOT_RELAY_CHANNELS="phone,car")
    with TestClient(create_app(relay)) as c:
        ids = [send(c, auth, f"m{i}", channel="car" if i == 2 else None).json()["id"] for i in range(5)]
        page = c.get("/v1/messages?limit=3", headers=auth).json()
        assert [i["id"] for i in page["items"]] == ids[::-1][:3]
        assert page["next_since"] == ids[-1]
        assert "hops" not in page["items"][0] and page["items"][0]["status"] == "stored"
        after = c.get(f"/v1/messages?since={ids[1]}", headers=auth).json()
        assert [i["id"] for i in after["items"]] == ids[2:]
        assert after["next_since"] == ids[-1]
        empty = c.get(f"/v1/messages?since={ids[-1]}", headers=auth).json()
        assert empty == {"items": [], "next_since": ids[-1]}
        car = c.get("/v1/messages?channel=car", headers=auth).json()
        assert [i["id"] for i in car["items"]] == [ids[2]]
        ts = c.get("/v1/messages?since=2000-01-01T00:00:00Z", headers=auth).json()
        assert [i["id"] for i in ts["items"]] == ids
        assert_error(c.get("/v1/messages?since=yesterday", headers=auth), 422, "invalid_field", "since")
        assert_error(c.get("/v1/messages?since=msg_" + "f" * 32, headers=auth), 404, "not_found")


def test_status(client, auth):
    s = client.get("/v1/status", headers=auth).json()
    assert s["relay"]["version"] == "1.0.0"
    assert s["bucket"]["name"] == BUCKET and s["bucket"]["last_write_ok_at"] is None
    assert s["channels"] == ["phone"]
    assert s["speech"] == {"default": "device", "server_configured": False}
    assert s["receipts"]["configured"] is True
    assert s["receipts"]["beacon_url"] == f"https://relay.example.com/v1/beacons/{BEACON_TOKEN}"
    assert "not proof" in s["receipts"]["note"]
    send(client, auth)
    assert client.get("/v1/status", headers=auth).json()["bucket"]["last_write_ok_at"]


def test_doctor_writes_one_inert_txt(client, auth, s3_admin):
    d = client.post("/v1/doctor", headers=auth).json()
    assert d["ok"] is True and d["write"]["ok"] is True
    assert re.fullmatch(r"phone/relay-doctor-\d{8}T\d{12}Z\.txt", d["write"]["key"])
    obj = s3_admin.get_object(Bucket=BUCKET, Key=d["write"]["key"])
    assert obj["ContentType"] == "text/plain; charset=utf-8"
    assert d["beacon_url"].endswith(BEACON_TOKEN)


def test_doctor_reports_a_failed_write(make_relay, auth):
    relay = make_relay(EARSHOT_BUCKET="no-such-bucket-here")
    with TestClient(create_app(relay)) as c:
        d = c.post("/v1/doctor", headers=auth).json()
    assert d["ok"] is False and "NoSuchBucket" in d["write"]["error"]


# ---- rate limits -----------------------------------------------------------------------------------------------


def test_global_rate_limit(make_relay, auth):
    tick = {"t": 1000.0}
    relay = make_relay(EARSHOT_RELAY_RATE_LIMIT="2/min", monotonic=lambda: tick["t"])
    with TestClient(create_app(relay)) as c:
        assert send(c, auth).status_code == 202
        assert send(c, auth).status_code == 202
        r = send(c, auth)
        assert_error(r, 429, "rate_limited")
        assert 1 <= int(r.headers["retry-after"]) <= 60
        assert_error(send(c, auth, ""), 422, "empty_text")  # validation still answers first
        tick["t"] += 61
        assert send(c, auth).status_code == 202


def test_alarm_rate_limit_is_separate(make_relay, auth):
    tick = {"t": 1000.0}
    relay = make_relay(EARSHOT_RELAY_CHANNELS="phone,alarm", EARSHOT_RELAY_ALARM_RATE_LIMIT="1/hour",
                       monotonic=lambda: tick["t"])
    with TestClient(create_app(relay)) as c:
        assert send(c, auth, channel="alarm").status_code == 202
        r = send(c, auth, channel="alarm")
        assert_error(r, 429, "rate_limited")
        assert "alarm" in r.json()["error"]["message"]
        assert int(r.headers["retry-after"]) > 3000
        assert send(c, auth).status_code == 202  # normal sends still flow


def test_rate_limit_off(make_relay, auth):
    relay = make_relay(EARSHOT_RELAY_RATE_LIMIT="off")
    with TestClient(create_app(relay)) as c:
        for _ in range(5):
            assert send(c, auth).status_code == 202


# ---- separate beacon listener ----------------------------------------------------------------------------------


def test_separate_beacon_port(make_relay, auth):
    relay = make_relay(EARSHOT_RELAY_BEACON_PORT="8099")
    body = beacon_for("phone/x.say", "detected", "2026-11-20T13:45:28.723Z")
    with TestClient(create_app(relay)) as main, TestClient(create_beacon_app(relay)) as beacons:
        assert_error(main.post(f"/v1/beacons/{BEACON_TOKEN}", json=body), 404, "not_found")
        assert beacons.post(f"/v1/beacons/{BEACON_TOKEN}", json=body).status_code == 200
        assert beacons.get("/healthz").status_code == 200
        assert beacons.get("/v1/status", headers=auth).status_code == 404
        assert main.get("/v1/status", headers=auth).json()["receipts"]["separate_beacon_port"] == 8099


# ---- privacy: nothing secret in logs or the index ----------------------------------------------------------------


def test_no_text_or_token_in_logs_or_index(relay, auth, caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(relay)) as c:
        sent = send(c, auth, SECRET_PHRASE).json()
        c.post(f"/v1/beacons/{BEACON_TOKEN}", json=beacon_for(sent["key"], "detected", "2026-11-20T13:45:28.723Z"))
        c.post("/v1/beacons/" + "w" * 40, json={})
        c.get("/v1/status", headers=auth)
        send(c, {"Authorization": "Bearer nope"}, SECRET_PHRASE)
    # Everything the relay process logs; the test's own HTTP client (httpx*) and the fake S3 server (werkzeug) excluded.
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "werkzeug")))
    for secret in (SECRET_PHRASE, TOKEN, BEACON_TOKEN, "w" * 40, "writer-secret-value"):
        assert secret not in text
    access = [json.loads(r.getMessage()) for r in caplog.records if r.name == "earshot_relay" and '"http"' in r.getMessage()]
    paths = {a["path"] for a in access}
    assert "/v1/beacons/{beacon_token}" in paths and "/v1/messages" in paths
    assert any(a.get("message_id") == sent["id"] for a in access)
    with open(relay.settings.db_path, "rb") as fh:
        raw = fh.read()
    import pathlib

    wal = pathlib.Path(relay.settings.db_path + "-wal")
    if wal.exists():
        raw += wal.read_bytes()
    assert SECRET_PHRASE.encode() not in raw
