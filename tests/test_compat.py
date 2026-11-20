"""Producer-compat ingress: ntfy, Gotify, Pushover and Join request shapes accepted by the relay.

The fixtures in tests/fixtures/compat/ are the requests REAL clients put on the wire, recorded by
scripts/compat_clients.py (Apprise, the ntfy CLI, the gotify CLI, curl, gregdel/pushover, Uptime Kuma).
They are replayed byte for byte here. A 200 from a compat route means stored, never played.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app, create_compat_app
from earshot_relay.compat import authorization_secret, ntfy_query_auth, spoken
from earshot_relay.config import ConfigError, load_settings

from .conftest import BUCKET, TOKEN, base_env

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "compat"
PUSHOVER_TOKEN = "p" * 30
COMPAT_ENV = {
    "EARSHOT_RELAY_COMPAT": "all",
    "EARSHOT_RELAY_CHANNELS": "phone,alarm",
    "EARSHOT_RELAY_COMPAT_PORT": "8082",
    "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN": PUSHOVER_TOKEN,
    "EARSHOT_RELAY_RATE_LIMIT": "off",
    "EARSHOT_RELAY_ALARM_RATE_LIMIT": "off",
}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


@pytest.fixture
def compat_relay(make_relay):
    def factory(**extra):
        env = {**COMPAT_ENV, **extra}
        return make_relay(**{k: v for k, v in env.items() if v is not None})

    return factory


@pytest.fixture
def clients(compat_relay):
    relay = compat_relay()
    with TestClient(create_app(relay), base_url="http://relay.test") as main, \
            TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        yield main, root, relay


def stored_text(s3_admin, key: str) -> str:
    return s3_admin.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()


def message_key(client, auth, message_id: str) -> dict:
    r = client.get(f"/v1/messages/{message_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def new_ids(main, before: set[str]) -> list[str]:
    r = main.get("/v1/messages", headers=BEARER, params={"n": 200})
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"] if i["id"] not in before]


def known_ids(main) -> set[str]:
    r = main.get("/v1/messages", headers=BEARER, params={"n": 200})
    return {i["id"] for i in r.json()["items"]}


# ---- replay of real-client requests ----------------------------------------------------------------------------


@pytest.mark.parametrize("path", [p for p in fixtures() if p.stem != "ntfy_cli_file_audio"], ids=lambda p: p.stem)
def test_real_client_request_is_accepted(path, clients, s3_admin):
    main, root, _ = clients
    fx = json.loads(path.read_text())
    req = fx["request"]
    target = root if fx["listener"] == "compat" else main
    before = known_ids(main)
    headers = {k: v for k, v in req["headers"] if k not in ("host", "content-length", "transfer-encoding")}
    url = req["path"] + (("?" + req["query"]) if req["query"] else "")
    body = base64.b64decode(req["body_b64"])
    r = target.request(req["method"], url, headers=headers, content=body)
    assert r.status_code == fx["response_status"], r.text

    ids = new_ids(main, before)
    assert len(ids) == 1, "exactly one message stored per compat send"
    info = message_key(main, BEARER, ids[0])
    assert info["channel"] == fx["expect_channel"]
    assert info["status"] == "stored"
    obj = s3_admin.get_object(Bucket=BUCKET, Key=info["key"])["Body"].read()
    if fx["expect_audio_b64"]:
        assert obj == base64.b64decode(fx["expect_audio_b64"])
    else:
        assert obj.decode() == fx["expect_text"]
    # the relay message id travels in a header on every protocol, and the receipt URL works
    assert r.headers["x-earshot-message-id"] == ids[0]
    for name, value in fx["response_headers"].items():
        assert r.headers.get(name) == value, name


@pytest.mark.parametrize("path", [p for p in fixtures() if p.stem != "ntfy_cli_file_audio"], ids=lambda p: p.stem)
def test_fixture_response_shape_is_the_protocols(path, clients):
    main, root, _ = clients
    fx = json.loads(path.read_text())
    req = fx["request"]
    target = root if fx["listener"] == "compat" else main
    headers = {k: v for k, v in req["headers"] if k not in ("host", "content-length", "transfer-encoding")}
    url = req["path"] + (("?" + req["query"]) if req["query"] else "")
    r = target.request(req["method"], url, headers=headers, content=base64.b64decode(req["body_b64"]))
    got, want = r.json(), fx["response"]
    assert set(want) <= set(got) | {"extras"}, (sorted(want), sorted(got))
    for key in ("event", "status", "success", "topic"):
        if key in want:
            assert got[key] == want[key]


# ---- configuration ---------------------------------------------------------------------------------------------


def cfg(moto_endpoint, tmp_path, **extra):
    return load_settings(base_env(moto_endpoint, str(tmp_path / "d"), **extra), check_ffmpeg=False)


def test_compat_is_off_by_default(moto_endpoint, tmp_path):
    s = cfg(moto_endpoint, tmp_path)
    assert s.compat == () and not s.compat_alarm and s.compat_port is None


@pytest.mark.parametrize("extra, needle", [
    ({"EARSHOT_RELAY_COMPAT": "ntfy,slack"}, "unknown protocol 'slack'"),
    ({"EARSHOT_RELAY_COMPAT_ALARM": "1"}, "names no protocol"),
    ({"EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "phone"}, "names no protocol"),
    ({"EARSHOT_RELAY_COMPAT": "gotify", "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "phone"}, "does not include ntfy"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "phone=nowhere"}, "not in EARSHOT_RELAY_CHANNELS"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_CHANNELS": "phone,alarm",
      "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "boom=alarm"}, "also needs EARSHOT_RELAY_COMPAT_ALARM=1"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "bad topic!"}, "invalid topic"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "phone,phone=phone2",
      "EARSHOT_RELAY_CHANNELS": "phone,phone2"}, "twice"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_PORT": "8082",
      "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "healthz=phone"}, "collides"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy,gotify", "EARSHOT_RELAY_COMPAT_PORT": "8082",
      "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "message=phone"}, "collides"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_PORT": "8080"}, "must differ"),
    ({"EARSHOT_RELAY_COMPAT": "pushover", "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN": "short"}, "exactly 30"),
    ({"EARSHOT_RELAY_COMPAT": "pushover",
      "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN": "azGDORePK8gMaC0QOYAMyEEuzJnyUi"}, "documentation"),
    ({"EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN": "p" * 30}, "does not include pushover"),
])
def test_compat_config_problems(moto_endpoint, tmp_path, extra, needle):
    with pytest.raises(ConfigError) as exc:
        cfg(moto_endpoint, tmp_path, **extra)
    assert needle in str(exc.value)


def test_default_ntfy_topics_are_the_quiet_channels(moto_endpoint, tmp_path):
    s = cfg(moto_endpoint, tmp_path, EARSHOT_RELAY_COMPAT="ntfy", EARSHOT_RELAY_CHANNELS="phone,kitchen,alarm")
    assert s.ntfy_topics == {"phone": "phone", "kitchen": "kitchen"}


def test_pushover_token_is_a_secret(moto_endpoint, tmp_path):
    s = cfg(moto_endpoint, tmp_path, EARSHOT_RELAY_COMPAT="pushover", EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN=PUSHOVER_TOKEN)
    assert PUSHOVER_TOKEN not in repr(s)


# ---- helpers ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("title, message, want", [
    ("", "washer done", "washer done"),
    ("Laundry", "", "Laundry"),
    ("Laundry", "washer done", "Laundry. washer done"),
    ("Laundry:", "washer done", "Laundry: washer done"),
    ("Garage", "Garage door left open", "Garage door left open"),
    ("Garage", "Garages are cold", "Garage. Garages are cold"),
    ("Done!", "all clear", "Done! all clear"),
])
def test_spoken_title_and_message(title, message, want):
    assert spoken(title, message) == want


def test_authorization_secret_forms():
    assert authorization_secret("Bearer abc") == "abc"
    assert authorization_secret("bearer abc") == "abc"
    basic = "Basic " + base64.b64encode(b"anyone:abc").decode()
    assert authorization_secret(basic) == "abc"
    assert authorization_secret("Digest x") is None
    assert authorization_secret(None) is None


def test_ntfy_query_auth_decodes_with_or_without_padding():
    value = "Bearer abc"
    std = base64.b64encode(value.encode()).decode()
    url = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    assert ntfy_query_auth(std) == value and ntfy_query_auth(url) == value
    assert not ntfy_query_auth("!!!")  # garbage never yields a usable header value


# ---- ntfy ------------------------------------------------------------------------------------------------------


def test_ntfy_response_is_ntfys_json(clients):
    _, root, _ = clients
    r = root.post("/phone", content="washer done", headers={**BEARER, "Title": "Laundry"})
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "message" and body["topic"] == "phone" and body["message"] == "washer done"
    assert body["title"] == "Laundry" and body["id"].startswith("msg_") and isinstance(body["time"], int)


@pytest.mark.parametrize("headers", [
    {"Authorization": f"Bearer {TOKEN}"},
    {"Authorization": "Basic " + base64.b64encode(f"whoever:{TOKEN}".encode()).decode()},
    {"Authorization": "Basic " + base64.b64encode(f":{TOKEN}".encode()).decode()},
])
def test_ntfy_auth_header_forms(clients, headers):
    _, root, _ = clients
    assert root.post("/phone", content="hi", headers=headers).status_code == 200


def test_ntfy_auth_query_param(clients):
    _, root, _ = clients
    auth = base64.urlsafe_b64encode(f"Bearer {TOKEN}".encode()).decode().rstrip("=")
    assert root.get("/phone/publish", params={"message": "hi", "auth": auth}).status_code == 200
    assert root.get("/phone/publish", params={"message": "hi", "auth": "bogus"}).status_code == 401


def test_ntfy_missing_or_wrong_token_is_401_in_ntfys_shape(clients):
    _, root, _ = clients
    for headers in ({}, {"Authorization": "Bearer nope"}):
        r = root.post("/phone", content="hi", headers=headers)
        assert r.status_code == 401
        assert r.json()["code"] == 40101 and r.json()["http"] == 401


def test_ntfy_unknown_topic_is_404_in_ntfys_shape(clients):
    _, root, _ = clients
    r = root.post("/nosuch", content="hi", headers=BEARER)
    assert r.status_code == 404
    assert r.json()["http"] == 404 and "error" in r.json()


def test_ntfy_topic_mapping_and_alarm_topic(compat_relay, s3_admin):
    relay = compat_relay(EARSHOT_RELAY_COMPAT_NTFY_TOPICS="kitchen=phone,hall=phone")
    with TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        r = root.post("/kitchen", content="hi", headers=BEARER)
        assert r.status_code == 200 and r.json()["topic"] == "kitchen"
        assert root.post("/phone", content="hi", headers=BEARER).status_code == 404  # only the allowlist counts


@pytest.mark.parametrize("value, high", [("5", True), ("max", True), ("urgent", True), ("4", False), ("high", False),
                                         ("3", False), ("default", False), ("1", False), ("min", False)])
def test_ntfy_priority_high_set(clients, value, high):
    _, root, _ = clients
    r = root.post("/phone", content="hi", headers={**BEARER, "Priority": value})
    assert r.status_code == 200
    assert ("x-earshot-downgraded" in r.headers) is high
    assert r.headers.get("x-earshot-downgraded", "alarm") == "alarm"


def test_ntfy_bad_priority_is_400(clients):
    _, root, _ = clients
    r = root.post("/phone", content="hi", headers={**BEARER, "Priority": "loud"})
    assert r.status_code == 400 and r.json()["code"] == 40007


def test_ntfy_rfc9218_priority_header_is_ignored(clients):
    _, root, _ = clients
    assert root.post("/phone", content="hi", headers={**BEARER, "Priority": "u=3, i"}).status_code == 200


def test_ntfy_ignored_fields_are_named(clients):
    _, root, _ = clients
    r = root.post("/", json={"topic": "phone", "message": "hi", "click": "https://x.example", "tags": ["a"],
                             "actions": [], "icon": "https://x.example/i.png"}, headers=BEARER)
    assert r.status_code == 200
    ignored = set(r.headers["x-earshot-ignored"].split(", "))
    assert {"click", "tags", "icon"} <= ignored


def test_ntfy_delay_is_refused_not_silently_dropped(clients):
    _, root, _ = clients
    r = root.post("/phone", content="hi", headers={**BEARER, "Delay": "30m"})
    assert r.status_code == 400 and "delay" in r.json()["error"].lower()


def test_ntfy_text_over_the_cap_is_413_in_ntfys_shape(clients):
    _, root, _ = clients
    r = root.post("/phone", content="a" * 4001, headers=BEARER)
    assert r.status_code == 413 and r.json()["code"] == 41301


def test_ntfy_empty_message_is_400(clients):
    _, root, _ = clients
    assert root.post("/phone", content="", headers=BEARER).status_code == 400


def test_ntfy_unicode_title_header_is_decoded(clients, s3_admin):
    main, root, _ = clients
    before = known_ids(main)
    r = root.post("/phone", content="déjà vu".encode(), headers={**BEARER, "Title": "=?UTF-8?B?Q2Fmw6k=?="})
    assert r.status_code == 200
    (mid,) = new_ids(main, before)
    assert stored_text(s3_admin, message_key(main, BEARER, mid)["key"]) == "Café. déjà vu"


def test_ntfy_audio_attachment_is_rejected_without_text(clients):
    main, root, _ = clients
    before = known_ids(main)
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    r = root.put("/phone", content=wav, headers={**BEARER, "X-Filename": "ding.wav"})
    assert r.status_code == 400 and "attachments are unsupported" in r.json()["error"]
    assert new_ids(main, before) == []


def test_ntfy_attachment_with_message_ignores_binary(clients, s3_admin):
    main, root, _ = clients
    r = root.put("/phone", content=b"RIFF" + b"\x00" * 32,
                 headers={**BEARER, "X-Filename": "ding.wav", "Message": "A message"})
    assert r.status_code == 200 and "attachment" in r.headers["x-earshot-ignored"]
    info = message_key(main, BEARER, r.headers["x-earshot-message-id"])
    assert info["type"] == "text" and stored_text(s3_admin, info["key"]) == "A message"


def test_ntfy_prefix_routes_on_the_main_app(clients):
    main, _, _ = clients
    assert main.post("/compat/ntfy/phone", content="hi", headers=BEARER).status_code == 200
    assert main.post("/compat/ntfy/phone/send", content="hi", headers=BEARER).status_code == 200
    assert main.post("/compat/ntfy/phone/bogus", content="hi", headers=BEARER).status_code in (404, 405)


# ---- alarm gate ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flag,allowlist,expect_alarm", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_alarm_requires_both_switches(compat_relay, flag, allowlist, expect_alarm):
    relay = compat_relay(EARSHOT_RELAY_CHANNELS="phone,alarm" if allowlist else "phone",
                         EARSHOT_RELAY_COMPAT_ALARM="1" if flag else "0")
    with TestClient(create_compat_app(relay)) as c:
        r = c.post("/phone", content="smoke", headers={**BEARER, "Priority": "5"})
        assert r.status_code == 200
        assert bool(r.headers.get("x-earshot-downgraded")) is not expect_alarm
        msg = relay.store.get_message(r.headers["x-earshot-message-id"])
        assert msg.channel == ("alarm" if expect_alarm else "phone")


def test_top_priority_goes_to_alarm_only_when_both_switches_are_on(compat_relay, s3_admin):
    relay = compat_relay(EARSHOT_RELAY_COMPAT_ALARM="1")
    with TestClient(create_app(relay), base_url="http://relay.test") as main, \
            TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        before = known_ids(main)
        r = root.post("/phone", content="smoke detected", headers={**BEARER, "Priority": "5"})
        assert r.status_code == 200
        (mid,) = new_ids(main, before)
        assert message_key(main, BEARER, mid)["channel"] == "alarm"
        assert "x-earshot-downgraded" not in r.headers
        # a normal send is still normal
        before = known_ids(main)
        root.post("/phone", content="fyi", headers=BEARER)
        (mid,) = new_ids(main, before)
        assert message_key(main, BEARER, mid)["channel"] == "phone"


def test_top_priority_is_downgraded_when_alarm_is_not_listed(compat_relay):
    relay = compat_relay(EARSHOT_RELAY_CHANNELS="phone")
    with TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        r = root.post("/phone", content="x", headers={**BEARER, "Priority": "5"})
        assert r.status_code == 200 and r.headers["x-earshot-downgraded"] == "alarm"


def test_top_priority_is_downgraded_when_compat_alarm_is_off(clients):
    main, root, _ = clients
    before = known_ids(main)
    r = root.post("/phone", content="x", headers={**BEARER, "Priority": "urgent"})
    assert r.headers["x-earshot-downgraded"] == "alarm"
    (mid,) = new_ids(main, before)
    assert message_key(main, BEARER, mid)["channel"] == "phone"


# ---- gotify ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["header", "query", "bearer"])
def test_gotify_token_placement(clients, kind):
    _, root, _ = clients
    kwargs = {"json": {"title": "T", "message": "m"}}
    if kind == "header":
        kwargs["headers"] = {"X-Gotify-Key": TOKEN}
    elif kind == "query":
        kwargs["params"] = {"token": TOKEN}
    else:
        kwargs["headers"] = BEARER
    r = root.post("/message", **kwargs)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["id"], int) and body["message"] == "m" and body["title"] == "T"


def test_gotify_form_body_and_priority(clients):
    _, root, _ = clients
    r = root.post("/message", data={"title": "T", "message": "m", "priority": "8"}, headers={"X-Gotify-Key": TOKEN})
    assert r.status_code == 200
    assert r.headers["x-earshot-downgraded"] == "alarm"
    r = root.post("/message", data={"message": "m", "priority": "7"}, headers={"X-Gotify-Key": TOKEN})
    assert "x-earshot-downgraded" not in r.headers


def test_gotify_errors_are_gotifys_shape(clients):
    _, root, _ = clients
    r = root.post("/message", json={"message": "m"})
    assert r.status_code == 401
    assert r.json()["errorCode"] == 401 and "error" in r.json()
    r = root.post("/message", json={"title": "x"}, headers={"X-Gotify-Key": TOKEN})
    assert r.status_code == 400 and r.json()["errorCode"] == 400


def test_gotify_text_over_the_cap_is_400(clients):
    _, root, _ = clients
    r = root.post("/message", json={"message": "a" * 4001}, headers={"X-Gotify-Key": TOKEN})
    assert r.status_code == 400 and r.json()["errorCode"] == 400


def test_gotify_extras_are_named_not_followed(clients):
    _, root, _ = clients
    r = root.post("/message", headers={"X-Gotify-Key": TOKEN}, json={
        "message": "m", "extras": {"client::display": {"contentType": "text/markdown"},
                                   "client::notification": {"click": {"url": "https://x.example"}}}})
    assert r.status_code == 200
    assert {"markdown", "click"} <= set(r.headers["x-earshot-ignored"].split(", "))


def test_gotify_response_carries_the_receipt_link(clients):
    _, root, _ = clients
    r = root.post("/message", json={"message": "m"}, headers={"X-Gotify-Key": TOKEN})
    meta = r.json()["extras"]["earshot::message"]
    assert meta["id"] == r.headers["x-earshot-message-id"] and meta["receipt_url"].endswith(meta["id"])


# ---- pushover --------------------------------------------------------------------------------------------------

PUSHOVER_BASE = {"token": PUSHOVER_TOKEN, "user": "u" * 30, "message": "hello"}


def test_pushover_form_success_shape_and_limit_headers(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", data=PUSHOVER_BASE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 1 and body["request"] == r.headers["x-earshot-message-id"]
    for h in ("x-limit-app-limit", "x-limit-app-remaining", "x-limit-app-reset"):
        assert h in r.headers


def test_pushover_json_body_and_relay_token(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", json={**PUSHOVER_BASE, "token": TOKEN, "title": "T"})
    assert r.status_code == 200


def test_pushover_pushover_token_is_valid_only_on_the_pushover_route(clients):
    main, root, _ = clients
    assert main.post("/compat/ntfy/phone", content="x", headers={"Authorization": f"Bearer {PUSHOVER_TOKEN}"}).status_code == 401
    assert root.post("/message", json={"message": "m"}, headers={"X-Gotify-Key": PUSHOVER_TOKEN}).status_code == 401
    assert main.get("/v1/status", headers={"Authorization": f"Bearer {PUSHOVER_TOKEN}"}).status_code == 401


def test_pushover_bad_token_is_status_0(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", data={**PUSHOVER_BASE, "token": "x" * 30})
    assert r.status_code == 401
    assert r.json()["status"] == 0 and r.json()["errors"]


def test_pushover_missing_message_is_status_0(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", data={**PUSHOVER_BASE, "message": ""})
    assert r.status_code == 400 and r.json()["status"] == 0


@pytest.mark.parametrize("prio, downgraded", [("-2", False), ("0", False), ("1", False), ("2", True)])
def test_pushover_priority_2_is_the_only_high(clients, prio, downgraded):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", data={**PUSHOVER_BASE, "priority": prio})
    assert r.status_code == 200 and ("x-earshot-downgraded" in r.headers) is downgraded


def test_pushover_priority_out_of_range_is_400(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json", data={**PUSHOVER_BASE, "priority": "9"})
    assert r.status_code == 400 and r.json()["status"] == 0


def test_pushover_unsupported_fields_are_named(clients):
    main, _, _ = clients
    r = main.post("/compat/pushover/1/messages.json",
                  data={**PUSHOVER_BASE, "sound": "siren", "url": "https://x.example", "device": "d"})
    assert {"sound", "url", "device"} <= set(r.headers["x-earshot-ignored"].split(", "))


# ---- join ------------------------------------------------------------------------------------------------------

JOIN = "/compat/join/_ah/api/messaging/v1/sendPush"


def test_join_say_wins_over_text(clients, s3_admin):
    main, _, _ = clients
    before = known_ids(main)
    r = main.get(JOIN, params={"apikey": TOKEN, "say": "spoken", "text": "shown", "title": "T", "deviceId": "group.all"})
    assert r.status_code == 200 and r.json()["success"] is True
    (mid,) = new_ids(main, before)
    assert stored_text(s3_admin, message_key(main, BEARER, mid)["key"]) == "spoken"
    assert {"text", "title", "deviceId"} <= set(r.headers["x-earshot-ignored"].split(", "))


def test_join_post_form_and_json(clients):
    main, _, _ = clients
    assert main.post(JOIN, data={"apikey": TOKEN, "text": "hi"}).status_code == 200
    assert main.post(JOIN, json={"apikey": TOKEN, "text": "hi"}).status_code == 200


def test_join_bad_key_is_joins_auth_error(clients):
    main, _, _ = clients
    r = main.get(JOIN, params={"apikey": "nope", "text": "hi"})
    assert r.status_code == 401
    assert r.json() == {"success": False, "userAuthError": True, "errorMessage": "User Not Authenticated"}


def test_join_without_text_is_an_error(clients):
    main, _, _ = clients
    r = main.get(JOIN, params={"apikey": TOKEN})
    assert r.status_code == 400 and r.json()["success"] is False


# ---- shared behavior: disabled routes, idempotency, rate limits, logs -------------------------------------------


def test_disabled_protocols_are_404_and_get_no_bearer_bypass(compat_relay):
    relay = compat_relay(EARSHOT_RELAY_COMPAT="gotify", EARSHOT_RELAY_COMPAT_PORT=None,
                         EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN=None)
    with TestClient(create_app(relay), base_url="http://relay.test") as main:
        assert main.post("/compat/ntfy/phone", content="x", headers=BEARER).status_code == 404
        # a disabled prefix is not exempt from the relay's own auth either
        assert main.post("/compat/ntfy/phone", content="x").status_code == 401
        assert main.post("/compat/gotify/message", json={"message": "m"}, headers={"X-Gotify-Key": TOKEN}).status_code == 200
        assert main.post("/v1/messages", json={"text": "x"}).status_code == 401


def test_compat_off_means_no_compat_routes(make_relay):
    relay = make_relay()
    with TestClient(create_app(relay), base_url="http://relay.test") as main:
        assert main.post("/compat/ntfy/phone", content="x", headers=BEARER).status_code == 404
        assert main.get("/v1/status", headers=BEARER).json()["compat"]["enabled"] == []


def test_native_routes_still_need_the_bearer(clients):
    main, _, _ = clients
    assert main.get("/v1/status").status_code == 401
    assert main.get("/v1/messages").status_code == 401


def test_idempotency_key_replays_across_protocols(clients):
    main, root, _ = clients
    before = known_ids(main)
    first = root.post("/phone", content="once", headers={**BEARER, "Idempotency-Key": "abc-123"})
    second = root.post("/phone", content="once", headers={**BEARER, "Idempotency-Key": "abc-123"})
    assert first.status_code == second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert first.headers["x-earshot-message-id"] == second.headers["x-earshot-message-id"]
    assert len(new_ids(main, before)) == 1


def test_bad_idempotency_key_is_400(clients):
    _, root, _ = clients
    assert root.post("/phone", content="x", headers={**BEARER, "Idempotency-Key": "k" * 300}).status_code == 400


def test_rate_limit_is_429_with_retry_after_in_each_shape(compat_relay):
    relay = compat_relay(EARSHOT_RELAY_RATE_LIMIT="2/min")
    with TestClient(create_app(relay), base_url="http://relay.test") as main, \
            TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        assert root.post("/phone", content="1", headers=BEARER).status_code == 200
        assert root.post("/phone", content="2", headers=BEARER).status_code == 200
        r = root.post("/phone", content="3", headers=BEARER)
        assert r.status_code == 429 and r.json()["http"] == 429 and int(r.headers["retry-after"]) >= 1
        r = root.post("/message", json={"message": "m"}, headers={"X-Gotify-Key": TOKEN})
        assert r.status_code == 429 and "retry-after" in r.headers
        r = main.post("/compat/pushover/1/messages.json", data=PUSHOVER_BASE)
        assert r.status_code == 429 and r.json()["status"] == 0 and "retry-after" in r.headers
        r = main.get(JOIN, params={"apikey": TOKEN, "text": "x"})
        assert r.status_code == 429 and r.json()["success"] is False
        # one shared window with the native API
        assert main.post("/v1/messages", json={"text": "x"}, headers=BEARER).status_code == 429


def test_status_reports_compat(clients):
    main, _, _ = clients
    c = main.get("/v1/status", headers=BEARER).json()["compat"]
    assert c["enabled"] == ["ntfy", "gotify", "pushover", "join"]
    assert c["port"] == 8082 and c["alarm"] is False and c["pushover_token"] is True
    assert PUSHOVER_TOKEN not in json.dumps(c)


def test_compat_listener_serves_health_and_no_docs(clients):
    _, root, _ = clients
    assert root.get("/healthz").status_code == 200
    assert root.get("/openapi.json").status_code in (404, 405)
    assert root.get("/docs").status_code in (404, 405)


def test_access_log_names_ignored_and_downgraded_but_never_the_text(clients, caplog):
    _, root, _ = clients
    caplog.set_level(logging.INFO)
    secret_text = "the secret sentence about the washer"
    root.post("/", json={"topic": "phone", "message": secret_text, "priority": 5, "click": "https://x.example"},
              headers=BEARER)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_text not in text and TOKEN not in text
    access = [json.loads(r.getMessage()) for r in caplog.records if r.name == "earshot_relay" and '"http"' in r.getMessage()]
    assert access and access[-1]["compat"] == "ntfy"
    assert "click" in access[-1]["ignored"] and access[-1]["downgraded"] == "alarm"


def test_openapi_documents_the_compat_routes(compat_relay):
    from earshot_relay.api import openapi_document

    doc = openapi_document(compat_relay())
    paths = doc["paths"]
    for p in ("/compat/ntfy/{topic}", "/compat/gotify/message", "/compat/pushover/1/messages.json",
              "/compat/join/_ah/api/messaging/v1/sendPush"):
        assert p in paths, p
