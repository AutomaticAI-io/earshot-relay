"""The delivery watchdog: one out-of-band notice per incident, driven by a clock the test moves by hand."""

from __future__ import annotations

import json
import logging
import time
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.beacons import iso_ms, validate_beacon
from earshot_relay.config import ConfigError, load_settings
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer
from earshot_relay.store import Store
from earshot_relay.watchdog import MAX_ATTEMPTS, is_ntfy_style

from .conftest import TOKEN, base_env
from .test_hygiene import Clock, post, run

HOOK = "https://hooks.example.com/services/T000/SECRET-PATH-xyz"
NTFY = "https://ntfy.sh/my-private-topic-77"


class Hook:
    """A stand-in for the notice endpoint: records every POST, answers with a status the test chooses."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status if self.status != -1 else 200)


@pytest.fixture
def chan():
    return "w" + uuid.uuid4().hex[:12]


@pytest.fixture
def setup(make_relay, chan):
    def build(url: str = HOOK, hook: Hook | None = None, **extra):
        clock, hook = Clock(), hook or Hook()
        relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, clock=clock,
                           EARSHOT_RELAY_WATCHDOG_S="120", EARSHOT_RELAY_WATCHDOG_URL=url,
                           watchdog_transport=hook.transport, **extra)
        clock.advance(1)  # the watch starts at boot; messages must be stored after it
        return relay, clock, hook, TestClient(create_app(relay))
    return build


def beacon(relay, key: str, clock: Clock, phase: str = "detected", channel: str | None = None) -> None:
    stamp = iso_ms(clock())
    clean, why = validate_beacon({"channel": channel or key.split("/")[0], "key": key, "phase": phase, "at": stamp,
                                  "last_modified": stamp, "device": "pixel", "app_version": "0.1.11"})
    assert clean, why
    relay.ingest_beacon(clean)


def check(relay) -> None:
    run(relay.watchdog.check())


def test_no_notice_when_the_phone_reports_in_time(setup, auth):
    relay, clock, hook, c = setup()
    msg = post(c, auth, text="ping").json()
    clock.advance(30)
    beacon(relay, msg["key"], clock)
    clock.advance(300)
    check(relay)
    assert hook.requests == [] and relay.watchdog.status()["state"] == "idle"


def test_phone_off_means_exactly_one_notice_after_n_seconds(setup, auth, chan):
    relay, clock, hook, c = setup()
    msg = post(c, auth, text="anyone there?").json()
    clock.advance(119)
    check(relay)
    assert hook.requests == []  # not yet: 119 s of 120
    clock.advance(2)
    check(relay)
    assert len(hook.requests) == 1
    req = hook.requests[0]
    assert str(req.url) == HOOK and req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["event"] == "earshot.watchdog.no_playback_report" and body["message_id"] == msg["id"]
    assert body["channel"] == chan and body["after_s"] == 120
    assert "heard" not in body["text"]  # receipt wording: device playback, never "heard"
    # more undetected sends, a lot more time: still the same incident, still one notice
    for _ in range(3):
        post(c, auth, text="again")
        clock.advance(500)
        check(relay)
    assert len(hook.requests) == 1
    s = relay.watchdog.status()
    assert s["state"] == "notified" and s["notices_sent"] == 1 and s["incident_message"] == msg["id"]


def test_a_detected_beacon_clears_the_incident_and_the_next_one_waits_out_the_cooldown(setup, auth):
    relay, clock, hook, c = setup(EARSHOT_RELAY_WATCHDOG_COOLDOWN_S="1800")
    first = post(c, auth, text="one").json()
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1
    clock.advance(10)
    beacon(relay, first["key"], clock)  # the phone is back (a parked, late beacon counts)
    check(relay)
    assert relay.watchdog.status()["state"] == "idle"
    clock.advance(5)
    post(c, auth, text="two")
    clock.advance(121)
    check(relay)  # a new incident, but inside the cooldown: held back, not dropped
    assert len(hook.requests) == 1 and relay.watchdog.status()["state"] == "incident"
    clock.advance(1700)
    check(relay)
    assert len(hook.requests) == 2 and relay.watchdog.status()["state"] == "notified"


def test_an_incident_only_clears_after_all_missing_keys_are_detected(setup, auth):
    relay, clock, hook, c = setup(EARSHOT_RELAY_WATCHDOG_COOLDOWN_S="1800")
    first = post(c, auth, text="one").json()
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1
    second = post(c, auth, text="two").json()
    clock.advance(121)
    check(relay)
    clock.advance(60)
    beacon(relay, second["key"], clock)
    clock.advance(2000)
    check(relay)
    assert len(hook.requests) == 1 and relay.watchdog.status()["state"] == "notified"
    beacon(relay, first["key"], clock)
    check(relay)
    assert relay.watchdog.status()["state"] == "idle"


def test_only_detected_phase_counts_and_heartbeat_does_not(setup, auth):
    relay, clock, hook, c = setup()
    msg = post(c, auth, text="x").json()
    chan = msg["channel"]
    heartbeat, why = validate_beacon({"channel": chan, "phase": "heartbeat", "at": iso_ms(clock()), "device": "pixel",
                                      "app_version": "0.1.11"})
    assert heartbeat, why
    relay.ingest_beacon(heartbeat)
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1  # a heartbeat does not prove the phone is reading the bucket
    relay2, clock2, hook2, c2 = setup()
    m2 = post(c2, auth, text="y").json()
    clock2.advance(30)
    beacon(relay2, m2["key"], clock2, phase="started")
    clock2.advance(300)
    check(relay2)
    assert len(hook2.requests) == 1
    beacon(relay2, m2["key"], clock2, phase="detected")
    check(relay2)
    assert relay2.watchdog.status()["state"] == "idle"


def test_ntfy_style_urls_get_a_plain_text_body(setup, auth):
    relay, clock, hook, c = setup(url=NTFY)
    post(c, auth, text="x")
    clock.advance(121)
    check(relay)
    req = hook.requests[0]
    assert str(req.url) == NTFY and req.headers["content-type"].startswith("text/plain")
    assert req.headers["title"] == "Earshot: no playback report" and req.headers["tags"] == "warning"
    assert req.content.decode().startswith("Earshot: no device reported on message msg_")
    assert relay.watchdog.status()["format"] == "text"


@pytest.mark.parametrize("url, fmt, expect", [
    ("https://ntfy.sh/topic", "auto", True), ("https://ntfy.example.com/topic", "auto", True),
    ("https://hooks.example.com/ntfyx", "auto", False), ("https://hooks.slack.com/services/x", "auto", False),
])
def test_ntfy_detection(url, fmt, expect):
    assert is_ntfy_style(url) is expect


def test_format_can_be_forced(setup, auth):
    relay, clock, hook, c = setup(url="https://alerts.lan/topic", EARSHOT_RELAY_WATCHDOG_FORMAT="text")
    post(c, auth, text="x")
    clock.advance(121)
    check(relay)
    assert hook.requests[0].headers["content-type"].startswith("text/plain")


def test_a_failing_webhook_is_retried_then_given_up_on(setup, auth, caplog):
    caplog.set_level(logging.INFO, logger="earshot_relay")
    hook = Hook(status=500)
    relay, clock, _, c = setup(hook=hook)
    post(c, auth, text="x")
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1 and relay.watchdog.status()["state"] == "incident"
    check(relay)  # no hammering: the retry waits 30 s
    assert len(hook.requests) == 1
    clock.advance(31)
    check(relay)
    assert len(hook.requests) == 2
    clock.advance(61)
    check(relay)
    assert len(hook.requests) == MAX_ATTEMPTS == 3
    s = relay.watchdog.status()
    assert s["state"] == "notified" and s["notices_sent"] == 0 and s["last_notice_ok"] is False
    assert s["last_error"] == "the watchdog URL answered HTTP 500"
    clock.advance(5000)
    check(relay)
    assert len(hook.requests) == 3  # given up for this incident
    assert HOOK not in json.dumps(s) and "SECRET-PATH" not in caplog.text + json.dumps(s)


def test_an_unreachable_url_is_an_error_without_the_url(setup, auth):
    def boom(request):
        raise httpx.ConnectError("connection refused to https://hooks.example.com/services/T000/SECRET-PATH-xyz")

    relay, clock, _, c = setup()
    relay.watchdog._transport = httpx.MockTransport(boom)
    post(c, auth, text="x")
    clock.advance(121)
    check(relay)
    assert relay.watchdog.status()["last_error"] == "could not reach the watchdog URL (ConnectError)"


def test_status_shows_the_host_never_the_url(setup, auth):
    relay, clock, hook, c = setup()
    s = c.get("/v1/status", headers=auth).json()["watchdog"]
    assert s["enabled"] is True and s["after_s"] == 120 and s["cooldown_s"] == 1800 and s["state"] == "idle"
    assert s["target_host"] == "hooks.example.com" and "sends, not idle phones" in s["note"]
    assert "SECRET-PATH" not in json.dumps(s) and "SECRET-PATH" not in repr(relay.settings)


def test_disabled_by_default(client, auth):
    assert client.get("/v1/status", headers=auth).json()["watchdog"]["enabled"] is False


def test_messages_from_before_the_watchdog_was_on_are_not_an_incident(moto_endpoint, tmp_path, auth, chan):
    clock, hook = Clock(), Hook()
    env = base_env(moto_endpoint, str(tmp_path / "old"), EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan)

    def boot(**extra) -> tuple[Relay, Store]:
        settings = load_settings({**env, **extra}, check_ffmpeg=False)
        store = Store(settings.db_path)
        return Relay(settings, store, S3Writer(settings), None, clock=clock, watchdog_transport=hook.transport), store

    relay, store = boot()
    post(TestClient(create_app(relay)), auth, text="sent while the watchdog was off")
    store.close()
    clock.advance(3600)
    relay2, store2 = boot(EARSHOT_RELAY_WATCHDOG_S="120", EARSHOT_RELAY_WATCHDOG_URL=HOOK)
    try:
        clock.advance(500)
        run(relay2.watchdog.check())
        assert hook.requests == []
        post(TestClient(create_app(relay2)), auth, text="sent with it on")
        clock.advance(121)
        run(relay2.watchdog.check())
        assert len(hook.requests) == 1
    finally:
        store2.close()


def test_state_survives_a_restart_so_one_incident_is_one_notice(moto_endpoint, tmp_path, auth, chan):
    clock, hook = Clock(), Hook()
    env = base_env(moto_endpoint, str(tmp_path / "restart"), EARSHOT_RELAY_CHANNELS=chan,
                   EARSHOT_RELAY_DEFAULT_CHANNEL=chan, EARSHOT_RELAY_WATCHDOG_S="120", EARSHOT_RELAY_WATCHDOG_URL=HOOK)

    def boot() -> tuple[Relay, Store]:
        settings = load_settings(env, check_ffmpeg=False)
        store = Store(settings.db_path)
        return Relay(settings, store, S3Writer(settings), None, clock=clock, watchdog_transport=hook.transport), store

    relay, store = boot()
    clock.advance(1)
    c = TestClient(create_app(relay))
    post(c, auth, text="x")
    clock.advance(121)
    run(relay.watchdog.check())
    assert len(hook.requests) == 1
    store.close()
    clock.advance(30)
    relay2, store2 = boot()
    try:
        post(TestClient(create_app(relay2)), auth, text="more traffic")
        clock.advance(500)
        run(relay2.watchdog.check())
        assert len(hook.requests) == 1 and relay2.watchdog.status()["state"] == "notified"
    finally:
        store2.close()


def test_the_background_loop_is_wired_into_the_app(setup, auth):
    relay, clock, hook, c = setup()
    post(c, auth, text="x")
    clock.advance(121)  # the window has already closed when the app starts: its first pass sends the notice
    with TestClient(create_app(relay)):
        deadline = time.time() + 3
        while time.time() < deadline and not hook.requests:
            time.sleep(0.05)
    assert len(hook.requests) == 1


# ---- configuration ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra, expect", [
    ({"EARSHOT_RELAY_WATCHDOG_S": "120"}, "EARSHOT_RELAY_WATCHDOG_URL is required"),
    ({"EARSHOT_RELAY_WATCHDOG_S": "5", "EARSHOT_RELAY_WATCHDOG_URL": HOOK}, "between 10 and 86400"),
    ({"EARSHOT_RELAY_WATCHDOG_S": "abc", "EARSHOT_RELAY_WATCHDOG_URL": HOOK}, "must be a whole number"),
    ({"EARSHOT_RELAY_WATCHDOG_S": "120", "EARSHOT_RELAY_WATCHDOG_URL": "ftp://x/y"}, "http\\(s\\) URL"),
    ({"EARSHOT_RELAY_WATCHDOG_S": "120", "EARSHOT_RELAY_WATCHDOG_URL": HOOK, "EARSHOT_RELAY_WATCHDOG_FORMAT": "xml"},
     "FORMAT must be one of"),
    ({"EARSHOT_RELAY_WATCHDOG_S": "120", "EARSHOT_RELAY_WATCHDOG_URL": HOOK, "EARSHOT_RELAY_WATCHDOG_COOLDOWN_S": "5"},
     "COOLDOWN_S must be between"),
    ({"EARSHOT_RELAY_WATCHDOG_COOLDOWN_S": "100"}, "COOLDOWN_S is set but EARSHOT_RELAY_WATCHDOG_S is not"),
])
def test_watchdog_config_errors(moto_endpoint, tmp_path, extra, expect):
    with pytest.raises(ConfigError, match=expect):
        load_settings(base_env(moto_endpoint, str(tmp_path), **extra), check_ffmpeg=False)


def test_watchdog_url_uses_default_threshold_and_missing_beacons_disable_it(make_relay, moto_endpoint, tmp_path):
    s = load_settings(base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_WATCHDOG_URL=HOOK), check_ffmpeg=False)
    assert s.watchdog_s == 120
    disabled = make_relay(EARSHOT_RELAY_WATCHDOG_URL=HOOK, EARSHOT_RELAY_BEACON_TOKEN=None)
    assert disabled.watchdog is None


def test_unrelated_detection_never_clears_missing_key(setup, auth):
    relay, clock, hook, c = setup()
    first = post(c, auth, text="missing").json()
    second = post(c, auth, text="detected").json()
    beacon(relay, second["key"], clock)
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1
    assert relay.watchdog.status()["incident_message"] == first["id"]
    beacon(relay, first["key"], clock)
    check(relay)
    assert relay.watchdog.status()["state"] == "idle"


def test_watchdog_off_values_and_url_file(moto_endpoint, tmp_path):
    off = load_settings(base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_WATCHDOG_S="off"), check_ffmpeg=False)
    assert off.watchdog_s is None
    secret = tmp_path / "hook"
    secret.write_text(NTFY + "\n")
    s = load_settings(base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_WATCHDOG_S="120",
                               EARSHOT_RELAY_WATCHDOG_URL_FILE=str(secret)), check_ffmpeg=False)
    assert s.watchdog_url == NTFY and s.watchdog_host == "ntfy.sh" and s.watchdog_cooldown_s == 1800
    assert TOKEN not in repr(s) and "my-private-topic" not in repr(s)
