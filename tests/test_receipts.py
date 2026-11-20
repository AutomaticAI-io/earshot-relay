"""Receipt derivation: status, ties, skipped, dedupe, late and out-of-order beacons, unconfirmed, skew math."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from earshot_relay.beacons import iso_ms, parse_rfc3339, validate_beacon
from earshot_relay.service import NO_BEACON_HINT, build_hops, derive_receipt, server_time
from earshot_relay.store import BeaconRow, MessageRow

from .conftest import golden_beacons

KEY = "phone/0123456789abcdef0123456789abcdef.say"
CREATED = "2026-11-20T13:45:26.000Z"
LM = "2026-11-20T13:45:26.163Z"
NOW = parse_rfc3339("2026-11-20T13:46:00.000Z")


def msg(key=KEY, created=CREATED, channel="phone") -> MessageRow:
    return MessageRow(1, "msg_" + key.split("/")[1].split(".")[0], created, channel, "text", "device", key, 10, 10)


def b(phase, at, device="dev-a", skew=None, error=None, lm=LM, received=None, key=KEY, version="0.1.11") -> BeaconRow:
    return BeaconRow(key, "phone", phase, device, version, at, lm, skew, error, received or at)


def receipt(beacons, *, now=NOW, ever=None, wait_min=60, created=CREATED):
    return derive_receipt(msg(created=created), beacons, now=now, wait_min=wait_min,
                          beacons_ever=bool(beacons) if ever is None else ever)


def test_skew_correction_math():
    at = parse_rfc3339("2026-11-20T13:45:28.723Z")
    # Skew is phone minus server: a phone 278 ms behind (-278) is moved 278 ms later onto the server clock.
    assert iso_ms(server_time(at, -278)) == "2026-11-20T13:45:29.001Z"
    assert iso_ms(server_time(at, 1000)) == "2026-11-20T13:45:27.723Z"
    assert server_time(at, None) == at
    assert server_time(at, 0) == at


def test_stored_with_hint_when_no_beacon_ever():
    r = receipt([], ever=False)
    assert r["status"] == "stored"
    assert r["status_at"] == CREATED
    assert r["hint"] == NO_BEACON_HINT
    assert r["hops"] == [] and r["devices"] == []


def test_stored_without_hint_once_beacons_flow():
    r = receipt([], ever=True)
    assert r["status"] == "stored" and r["hint"] is None


def test_unconfirmed_after_the_wait_window():
    late = parse_rfc3339(CREATED) + timedelta(minutes=61)
    r = receipt([], now=late, ever=False)
    assert r["status"] == "unconfirmed"
    assert r["status_at"] == iso_ms(parse_rfc3339(CREATED) + timedelta(minutes=60))
    assert r["hint"] == NO_BEACON_HINT
    r = receipt([], now=late, ever=True)
    assert r["status"] == "unconfirmed" and "follows channel 'phone'" in r["hint"]


def test_unconfirmed_flips_when_a_parked_beacon_replays():
    late = parse_rfc3339(CREATED) + timedelta(hours=3)
    r = receipt([b("detected", "2026-11-20T13:45:28.000Z", received="2026-11-20T16:45:00.000Z"),
                 b("finished", "2026-11-20T13:45:33.000Z", received="2026-11-20T16:45:01.000Z")], now=late)
    assert r["status"] == "finished" and r["hint"] is None


def test_furthest_phase_wins_regardless_of_arrival_order():
    beacons = [b("finished", "2026-11-20T13:45:34.000Z"), b("detected", "2026-11-20T13:45:28.000Z"),
               b("started", "2026-11-20T13:45:29.000Z")]
    r = receipt(beacons)
    assert r["status"] == "finished"
    assert [h["phase"] for h in r["hops"]] == ["detected", "started", "finished"]
    assert r["devices"] == [{"device": "dev-a", "status": "finished", "app_version": "0.1.11"}]


def test_skip_is_a_finished_beacon_with_error_skipped():
    r = receipt([b("detected", "2026-11-20T13:45:28.000Z"), b("started", "2026-11-20T13:45:29.000Z"),
                 b("finished", "2026-11-20T13:45:30.000Z", error="skipped")])
    assert r["status"] == "skipped"
    assert r["hops"][-1]["error"] == "skipped"


def test_terminal_ties_prefer_finished_then_skipped_then_failed_then_missed():
    t = "2026-11-20T13:45:30.000Z"
    cases = [
        ([("a", "missed", None), ("b", "failed", None)], "failed"),
        ([("a", "failed", "boom"), ("b", "finished", "skipped")], "skipped"),
        ([("a", "finished", "skipped"), ("b", "finished", None)], "finished"),
        ([("a", "missed", None), ("b", "finished", None), ("c", "failed", "x")], "finished"),
    ]
    for devices, want in cases:
        r = receipt([b(phase, t, device=d, error=e) for d, phase, e in devices])
        assert r["status"] == want, devices


def test_highest_order_device_decides():
    r = receipt([b("detected", "2026-11-20T13:45:28.000Z", device="a"), b("started", "2026-11-20T13:45:29.000Z", device="b")])
    assert r["status"] == "started"
    assert {d["device"]: d["status"] for d in r["devices"]} == {"a": "detected", "b": "started"}


def test_dedupe_keeps_the_earliest_per_device_and_phase():
    r = receipt([b("detected", "2026-11-20T13:45:29.000Z", received="2026-11-20T13:45:40.000Z"),
                 b("detected", "2026-11-20T13:45:28.000Z", received="2026-11-20T13:45:41.000Z"),
                 b("detected", "2026-11-20T13:45:28.500Z", device="other")])
    hops = r["hops"]
    assert [(h["device"], h["at"]) for h in hops] == [("dev-a", "2026-11-20T13:45:28.000Z"),
                                                       ("other", "2026-11-20T13:45:28.500Z")]


def test_hops_order_by_server_time_then_phase_order():
    # Two devices with different skews: server time, not phone time, decides the order.
    r = receipt([b("detected", "2026-11-20T13:45:30.000Z", device="fast", skew=3000),  # server 13:45:27.000
                 b("detected", "2026-11-20T13:45:28.000Z", device="slow", skew=0),  # server 13:45:28.000
                 b("started", "2026-11-20T13:45:28.000Z", device="slow", skew=0)])  # same instant: phase order
    assert [(h["device"], h["phase"]) for h in r["hops"]] == [("fast", "detected"), ("slow", "detected"), ("slow", "started")]
    assert r["hops"][0]["at_server"] == "2026-11-20T13:45:27.000Z"
    assert r["hops"][0]["since_upload_ms"] == 837


def test_heartbeats_are_not_hops():
    r = receipt([b("heartbeat", "2026-11-20T13:45:28.000Z", key="", lm=None)])
    assert r["hops"] == [] and r["status"] == "stored"


def test_golden_beta_receipts():
    rows = []
    for rec in golden_beacons():
        clean, why = validate_beacon(rec["body"])
        assert why is None, why
        received = iso_ms(parse_rfc3339(rec["received_at"]))
        rows.append(BeaconRow(clean["key"], clean["channel"], clean["phase"], clean["device"], clean["app_version"],
                              clean["at"], clean.get("last_modified"), clean.get("clock_skew_ms"), clean.get("error"),
                              received))
    say_key = "phone/5f0c2e7a9b1d4c3e8a6f2b1d0e9c8a7b.say"
    wav_key = "phone/a3d9e1c47b2f4e6d9c8b7a6f5e4d3c2b.wav"
    now = datetime(2026, 11, 20, 13, 46, tzinfo=timezone.utc)
    say = derive_receipt(msg(say_key, "2026-11-20T13:45:25.600Z"), [r for r in rows if r.key == say_key], now=now,
                         wait_min=60, beacons_ever=True)
    assert say["status"] == "finished"
    assert say["object_last_modified"] == "2026-11-20T13:45:26.163Z"
    assert [h["phase"] for h in say["hops"]] == ["detected", "started", "finished"]
    assert [h["since_upload_ms"] for h in say["hops"]] == [2838, 3476, 8682]
    assert say["hops"][0]["at_server"] == "2026-11-20T13:45:29.001Z"
    assert say["devices"][0]["app_version"] == "0.1.11"
    wav = derive_receipt(msg(wav_key, "2026-11-20T13:45:46.300Z"), [r for r in rows if r.key == wav_key], now=now,
                         wait_min=60, beacons_ever=True)
    assert wav["status"] == "finished"
    assert [h["since_upload_ms"] for h in wav["hops"]] == [2146, 2331, 5376]
    assert build_hops([r for r in rows if r.key == wav_key])[0].clock_skew_ms == -329
