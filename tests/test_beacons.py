"""Beacon validation, ported from the operator's beacon proxy tests, plus the wire shape of the app's Beacon.kt."""

from __future__ import annotations

import pytest

from earshot_relay.beacons import PLAYBACK_PHASES, iso_ms, parse_rfc3339, validate_beacon

KEY = "phone/5f0c2e7a9b1d4c3e8a6f2b1d0e9c8a7b.say"
BEACON = {"channel": "phone", "key": KEY, "phase": "detected", "at": "2026-10-19T12:00:02.100Z",
          "last_modified": "2026-10-19T12:00:00.000Z", "device": "S26", "app_version": "0.1.0"}
HEARTBEAT = {"channel": "phone", "phase": "heartbeat", "at": "2026-10-19T12:00:09.000Z", "device": "S26",
             "app_version": "0.1.0"}


def test_valid_shape_is_accepted_and_cleaned():
    clean, why = validate_beacon({**BEACON, "extra": "dropped", "error": "boom\nline"})
    assert why is None
    assert "extra" not in clean
    assert clean["error"] == "boom line"
    clean, _ = validate_beacon(BEACON)
    assert "error" not in clean


def test_error_is_truncated_to_300():
    clean, _ = validate_beacon({**BEACON, "phase": "failed", "error": "x" * 500})
    assert len(clean["error"]) == 300


def test_operator_only_telemetry_is_dropped_leniently():
    clean, why = validate_beacon({**BEACON, "audio": {"ringer_mode": "loud??"}, "incall": "speaker", "call": "in_call",
                                  "channels": ["phone", "BAD"]})
    assert why is None
    assert not {"audio", "incall", "call", "channels"} & set(clean)


@pytest.mark.parametrize(
    "beacon, why",
    [
        (None, "not a JSON object"), ([], "not a JSON object"),
        ({**BEACON, "channel": "Phone"}, "bad channel"), ({**BEACON, "channel": 5}, "bad channel"),
        ({**BEACON, "channel": "tablet"}, "bad key"),  # key not under the channel
        ({**BEACON, "key": "phone/"}, "bad key"), ({**BEACON, "key": "phone/../x.ogg"}, "bad key"),
        ({**BEACON, "key": "phone/a\nb.ogg"}, "bad key"), ({**BEACON, "key": "phone/" + "x" * 512}, "bad key"),
        ({**BEACON, "phase": "done"}, "bad phase"), ({k: v for k, v in BEACON.items() if k != "phase"}, "bad phase"),
        ({**BEACON, "at": "yesterday"}, "bad at"), ({**BEACON, "at": "2026-10-19T12:00:02"}, "bad at"),  # no zone
        ({**BEACON, "at": "2026-13-45T12:00:02Z"}, "bad at"),
        ({**BEACON, "last_modified": 1760875200}, "bad last_modified"),
        ({**BEACON, "device": ""}, "bad device"), ({**BEACON, "device": "d" * 81}, "bad device"),
        ({k: v for k, v in BEACON.items() if k != "app_version"}, "bad app_version"),
        ({**BEACON, "error": 7}, "bad error"),
    ],
)
def test_malformed_beacons_are_refused_with_a_reason(beacon, why):
    clean, got = validate_beacon(beacon)
    assert clean is None
    assert why in got


def test_heartbeat_is_accepted_without_key_or_last_modified():
    clean, why = validate_beacon(HEARTBEAT)
    assert why is None
    assert clean["phase"] == "heartbeat"
    assert "key" not in clean and "last_modified" not in clean


def test_heartbeat_with_a_key_still_validates_the_key():
    clean, why = validate_beacon({**HEARTBEAT, "key": KEY, "last_modified": BEACON["last_modified"]})
    assert why is None and clean["key"] == KEY
    _, why = validate_beacon({**HEARTBEAT, "key": "tablet/x.ogg"})
    assert "bad key" in why
    _, why = validate_beacon({**HEARTBEAT, "last_modified": "yesterday"})
    assert "bad last_modified" in why


def test_playback_phases_require_key_and_last_modified():
    for phase in PLAYBACK_PHASES:
        _, why = validate_beacon({k: v for k, v in {**BEACON, "phase": phase}.items() if k != "key"})
        assert "bad key" in why
        _, why = validate_beacon({k: v for k, v in {**BEACON, "phase": phase}.items() if k != "last_modified"})
        assert "bad last_modified" in why


def test_clock_skew_ms_is_numeric_and_bounded():
    for good in (0, 120, -120, 120.4, -86_400_000, 86_400_000):
        clean, why = validate_beacon({**BEACON, "clock_skew_ms": good})
        assert why is None, good
        assert clean["clock_skew_ms"] == int(round(good))
    for bad in (True, "120", float("nan"), float("inf"), 86_400_001, -86_400_001, [120]):
        clean, why = validate_beacon({**BEACON, "clock_skew_ms": bad})
        assert clean is None, bad
        assert "clock_skew_ms" in why
    clean, _ = validate_beacon(BEACON)
    assert "clock_skew_ms" not in clean


def test_rfc3339_parsing():
    assert iso_ms(parse_rfc3339("2026-11-20T13:45:26.163Z")) == "2026-11-20T13:45:26.163Z"
    assert iso_ms(parse_rfc3339("2026-11-20T15:45:26.163+02:00")) == "2026-11-20T13:45:26.163Z"
    assert iso_ms(parse_rfc3339("2026-11-20T13:45:26Z")) == "2026-11-20T13:45:26.000Z"
    assert iso_ms(parse_rfc3339("2026-11-20T13:45:26.123456789Z")) == "2026-11-20T13:45:26.123Z"
    for bad in ("2026-11-20 13:45:26Z", "2026-11-20T13:45:26", "", None, 0):
        assert parse_rfc3339(bad) is None
