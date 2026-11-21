"""Regression tests for the 1.1.1 reconciliation of the 1.1.0 main with the ears25-spec-gaps branch.

Each test here fails on v1.1.0 (3dd06bb) and passes on 1.1.1. Where a request only needs a coalescing window, it uses the
1.1.0 name `coalesce_s`, so the test reaches the behaviour under test on both versions and fails for the right reason.
"""

from __future__ import annotations

import json
import uuid

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import load_settings
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer, StorageError
from earshot_relay.store import Store

from .conftest import base_env
from .test_hygiene import Clock, coalescing_relay, objects, post, run
from .test_watchdog import HOOK, Hook, beacon, check


@pytest.fixture
def chan():
    return "r" + uuid.uuid4().hex[:12]


@pytest.fixture
def wd(make_relay, chan):
    def build(**extra):
        clock, hook = Clock(), Hook()
        relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, clock=clock,
                           EARSHOT_RELAY_WATCHDOG_S="120", EARSHOT_RELAY_WATCHDOG_URL=HOOK,
                           watchdog_transport=hook.transport, **extra)
        clock.advance(1)
        return relay, clock, hook, TestClient(create_app(relay))
    return build


# ---- 1. the watchdog correlates per message --------------------------------------------------------------------------


def test_a_receipt_for_b_does_not_hide_a_that_got_none(wd, auth):
    relay, clock, hook, c = wd()
    a = post(c, auth, text="A").json()
    b = post(c, auth, text="B").json()
    clock.advance(30)
    beacon(relay, b["key"], clock)  # the phone reports on B only
    clock.advance(100)
    check(relay)
    assert len(hook.requests) == 1
    body = json.loads(hook.requests[0].content)
    assert body["message_id"] == a["id"]
    assert relay.watchdog.status()["incident_message"] == a["id"]


def test_a_beacon_for_an_unknown_key_covers_nothing(wd, auth, chan):
    relay, clock, hook, c = wd()
    a = post(c, auth, text="A").json()
    clock.advance(30)
    beacon(relay, f"{chan}/{uuid.uuid4().hex}.say", clock)  # a key the relay never stored
    clock.advance(100)
    check(relay)
    assert len(hook.requests) == 1 and json.loads(hook.requests[0].content)["message_id"] == a["id"]


def test_a_message_never_reported_on_is_announced_once_and_does_not_block_later_notices(wd, auth):
    relay, clock, hook, c = wd(EARSHOT_RELAY_WATCHDOG_COOLDOWN_S="60")
    a = post(c, auth, text="A").json()
    clock.advance(121)
    check(relay)
    assert len(hook.requests) == 1
    b = post(c, auth, text="B").json()
    clock.advance(5)
    beacon(relay, b["key"], clock)  # the phone is back for B; A is still unreported
    check(relay)
    assert relay.watchdog.status()["state"] == "idle"
    c2 = post(c, auth, text="C").json()
    clock.advance(200)
    check(relay)
    assert len(hook.requests) == 2 and json.loads(hook.requests[1].content)["message_id"] == c2["id"]
    assert a["id"] not in hook.requests[1].content.decode()


# ---- 2. held writes re-check channel and compat policy at flush ---------------------------------------------------


def _restart(moto_endpoint, tmp_path, name, clock, **extra):
    settings = load_settings(base_env(moto_endpoint, str(tmp_path / name), **extra), check_ffmpeg=False)
    store = Store(settings.db_path)
    return Relay(settings, store, S3Writer(settings), None, clock=clock), store


def test_a_held_message_whose_channel_was_removed_is_not_written_after_a_restart(moto_endpoint, tmp_path, s3_admin,
                                                                                auth, chan):
    clock = Clock()
    other = chan + "x"
    first, first_store = _restart(moto_endpoint, tmp_path, "d", clock, EARSHOT_RELAY_CHANNELS=f"{chan},{other}",
                                  EARSHOT_RELAY_DEFAULT_CHANNEL=other)
    c = TestClient(create_app(first))
    held = c.post("/v1/messages", headers=auth, json={"text": "held", "channel": chan, "tag": "t", "coalesce_s": 5})
    assert held.status_code == 202 and held.json()["status"] == "coalescing"
    mid = held.json()["id"]
    first_store.close()

    second, second_store = _restart(moto_endpoint, tmp_path, "d", clock, EARSHOT_RELAY_CHANNELS=other,
                                    EARSHOT_RELAY_DEFAULT_CHANNEL=other)
    try:
        clock.advance(6)
        assert run(second.flush_due()) == 0
        assert objects(s3_admin, chan) == {}  # nothing was written to a channel that is no longer allowed
        assert second_store.pending_count() == 0
        assert second.receipt(mid)["status"] == "write_failed"
        assert second.status()["hygiene"]["coalescing_failed"] == 1
    finally:
        second_store.close()


def test_a_held_compat_alarm_is_refused_at_flush_when_compat_alarm_is_switched_off(moto_endpoint, tmp_path, s3_admin,
                                                                                    auth):
    from earshot_relay.api import create_compat_app

    clock = Clock()
    env = {"EARSHOT_RELAY_CHANNELS": "phone,alarm", "EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_ALARM": "1"}
    before = set(objects(s3_admin, "alarm"))
    first, first_store = _restart(moto_endpoint, tmp_path, "a", clock, **env)
    c = TestClient(create_compat_app(first))
    held = c.post("/phone?earshot_tag=u&earshot_coalesce_window_s=5", content="danger",
                  headers={**auth, "Priority": "5"})
    assert held.headers["x-earshot-status"] == "coalescing"
    first_store.close()
    second, second_store = _restart(moto_endpoint, tmp_path, "a", clock, **{**env, "EARSHOT_RELAY_COMPAT_ALARM": "0"})
    try:
        clock.advance(6)
        assert run(second.flush_due()) == 0
        assert set(objects(s3_admin, "alarm")) == before
        assert second_store.pending_count() == 0
    finally:
        second_store.close()


# ---- 3. an expired but unflushed window does not take replacement text ----------------------------------------------


def test_an_expired_window_flushes_its_own_text_before_a_new_send_starts_another(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    assert post(c, auth, text="first", tag="w", coalesce_s=5).status_code == 202
    clock.advance(6)  # the window is over, but the flush loop has not run
    r = post(c, auth, text="second", tag="w", coalesce_s=5)
    assert r.status_code == 202 and r.json()["status"] == "coalescing"
    assert list(objects(s3_admin, chan).values()) == [b"first"]  # the first window's text, never replaced
    clock.advance(6)
    run(relay.flush_due())
    assert sorted(objects(s3_admin, chan).values()) == [b"first", b"second"]


def test_an_expired_window_that_cannot_be_written_refuses_replacement_text(make_relay, auth, chan, monkeypatch):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    assert post(c, auth, text="first", tag="w", coalesce_s=5).status_code == 202

    def put(*args, **kwargs):
        raise StorageError("AccessDenied", "nope")

    monkeypatch.setattr(relay.writer, "put", put)
    clock.advance(6)
    r = post(c, auth, text="second", tag="w", coalesce_s=5)
    assert r.status_code == 503 and r.json()["error"]["code"] == "coalesce_busy" and int(r.headers["retry-after"]) >= 1
    assert relay.store.get_pending(chan, "w").text == "first"


# ---- 4. a suppressed request keeps its Idempotency-Key outcome -----------------------------------------------------


def test_suppressed_idempotency_outcome_survives_a_restart(moto_endpoint, tmp_path, s3_admin, auth, chan):
    clock = Clock()
    env = {"EARSHOT_RELAY_CHANNELS": chan, "EARSHOT_RELAY_DEFAULT_CHANNEL": chan}
    first, first_store = _restart(moto_endpoint, tmp_path, "i", clock, **env)
    c = TestClient(create_app(first))
    original = post(c, auth, text="first", tag="x", cooldown_s=10).json()
    headers = {**auth, "Idempotency-Key": "once"}
    body = {"text": "ignored", "tag": "x", "cooldown_s": 10}
    suppressed = c.post("/v1/messages", headers=headers, json=body)
    assert suppressed.status_code == 200 and suppressed.json()["status"] == "suppressed"
    first_store.close()
    second, second_store = _restart(moto_endpoint, tmp_path, "i", clock, **env)
    try:
        clock.advance(11)  # the cooldown is over: a fresh send would now be written
        c2 = TestClient(create_app(second))
        again = c2.post("/v1/messages", headers=headers, json=body)
        assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
        assert again.json() == suppressed.json() and again.json()["id"] == original["id"]
        assert list(objects(s3_admin, chan).values()) == [b"first"]
    finally:
        second_store.close()


# ---- 5. per-channel normalization, and coalesce_window_s with its 1.1.0 name ---------------------------------------


def test_normalization_is_per_channel(make_relay, auth, s3_admin, chan):
    other = chan + "y"
    relay = make_relay(EARSHOT_RELAY_CHANNELS=f"{chan},{other}", EARSHOT_RELAY_DEFAULT_CHANNEL=chan,
                       EARSHOT_RELAY_CHANNEL_HYGIENE=json.dumps({chan: {"normalize": "basic", "max_words": 3}}))
    c = TestClient(create_app(relay))
    raw = "**Build** passed \U0001F389 see https://example.com/x now"
    assert post(c, auth, text=raw, channel=chan).status_code == 202
    assert post(c, auth, text=raw, channel=other).status_code == 202
    assert list(objects(s3_admin, chan).values()) == [b"Build passed see."]  # normalized and cut to 3 words
    assert list(objects(s3_admin, other).values()) == [raw.encode()]  # the other channel keeps the global default (off)
    assert post(c, auth, text=raw, channel=other, normalize=True).status_code == 202  # a request can still override


def test_coalesce_window_s_and_its_1_1_0_name_both_work(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    new = post(c, auth, text="a", tag="n", coalesce_window_s=5)
    old = post(c, auth, text="b", tag="o", coalesce_s=5)
    assert new.status_code == old.status_code == 202 and new.json()["status"] == old.json()["status"] == "coalescing"
    both = post(c, auth, text="c", tag="p", coalesce_window_s=5, coalesce_s=5)
    assert both.status_code == 202
    clash = post(c, auth, text="d", tag="q", coalesce_window_s=5, coalesce_s=9)
    assert clash.status_code == 422 and clash.json()["error"]["code"] == "invalid_field"
    clock.advance(6)
    assert run(relay.flush_due()) == 3
    assert sorted(objects(s3_admin, chan).values()) == [b"a", b"b", b"c"]
