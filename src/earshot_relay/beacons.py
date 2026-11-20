"""Beacon validation: the phone's receipt JSON (`core/Beacon.kt` in the app), checked before it is stored.

Ported from the operator's beacon proxy. Required: channel, phase, at, device, app_version. The five playback
phases also need key (under that channel) and last_modified. `heartbeat` is accepted for forward compatibility
(buyer builds do not send it). Operator-only telemetry (audio, channels, incall, call) and unknown fields are dropped.
Stricter than the proxy in two ways, because a leaked beacon URL must not be able to break the relay or the
operator's terminal: timestamps must fall in 2000-2100, and device and app_version may not hold control characters.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .keys import CHANNEL_RE

PLAYBACK_PHASES = ("detected", "started", "finished", "failed", "missed")
HEARTBEAT = "heartbeat"
PHASE_ORDER = {"detected": 1, "started": 2, "finished": 3, "failed": 3, "missed": 3}
MAX_SKEW_MS = 86_400_000
MAX_BODY_BYTES = 4096
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")
# C0, DEL and C1 controls. Beacon text reaches the operator's terminal (`earshot-relay receipt`, `status`), so an
# escape sequence in a forged beacon must never survive.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Timestamps outside this window are refused: no real phone or bucket reports them, and near datetime.min/max the
# skew correction (at - clock_skew_ms) would overflow.
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)
LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)


def parse_rfc3339(value: object) -> datetime | None:
    """A timezone-aware UTC datetime for an RFC 3339 timestamp with a zone, else None. Fractions past 6 digits are cut."""
    if not isinstance(value, str) or not RFC3339_RE.match(value):
        return None
    text = value.replace("z", "Z")
    main, zone = (text[:-1], "+00:00") if text.endswith("Z") else (text[:-6], text[-6:])
    if "." in main:
        head, frac = main.split(".", 1)
        main = f"{head}.{frac[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(main + zone).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def in_window(value: object) -> bool:
    """An RFC 3339 timestamp between 2000-01-01 and 2100-01-01 UTC."""
    dt = parse_rfc3339(value)
    return dt is not None and EARLIEST <= dt < LATEST


TERMINAL = ("finished", "skipped", "failed", "missed")


def receipt_is_final(receipt: dict) -> bool:
    """True when waiting longer cannot change the answer much.

    `finished` outranks everything, so it is final at once. Any other terminal status is final only when every
    device that has reported is terminal too: with two phones, one can report `missed` while the other is still
    playing, and a moment later the message reads `finished`.
    """
    status = receipt.get("status")
    if status in ("finished", "write_failed"):  # write_failed: nothing was stored, so no phone will ever report
        return True
    return status in TERMINAL and all(d.get("status") in TERMINAL for d in receipt.get("devices", []))


def iso_ms(dt: datetime) -> str:
    """`2026-11-20T13:45:26.163Z`: UTC with millisecond precision, the format the app writes."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _clean_skew(value: object) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        return None, "bad clock_skew_ms"
    ms = int(round(value))
    if abs(ms) > MAX_SKEW_MS:
        return None, "bad clock_skew_ms"
    return ms, None


def validate_beacon(beacon: object) -> tuple[dict | None, str | None]:
    """(clean beacon, None) for a well-formed beacon, else (None, reason)."""
    if not isinstance(beacon, dict):
        return None, "body is not a JSON object"
    channel, key, phase = beacon.get("channel"), beacon.get("key"), beacon.get("phase")
    if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
        return None, "bad channel"
    heartbeat = phase == HEARTBEAT
    if phase not in PLAYBACK_PHASES and not heartbeat:
        return None, "bad phase"
    key_bad = (
        not isinstance(key, str)
        or not 0 < len(key) <= 512
        or not key.startswith(channel + "/")
        or len(key) == len(channel) + 1
        or ".." in key.split("/")
        or CONTROL_RE.search(key) is not None
    )
    if key is None:
        if not heartbeat:
            return None, "bad key"
    elif key_bad:
        return None, "bad key"
    last_modified = beacon.get("last_modified")
    if last_modified is None:
        if not heartbeat:
            return None, "bad last_modified"
    elif not in_window(last_modified):
        return None, "bad last_modified"
    if not in_window(beacon.get("at")):
        return None, "bad at"
    for name in ("device", "app_version"):
        v = beacon.get(name)
        if not isinstance(v, str) or not 0 < len(v) <= 80 or CONTROL_RE.search(v) is not None:
            return None, f"bad {name}"
    err = beacon.get("error")
    if err is not None and not isinstance(err, str):
        return None, "bad error"
    skew, why = _clean_skew(beacon.get("clock_skew_ms"))
    if why:
        return None, why
    clean = {
        "channel": channel,
        "phase": phase,
        "at": beacon["at"],
        "device": beacon["device"],
        "app_version": beacon["app_version"],
    }
    if key is not None:
        clean["key"] = key
    if last_modified is not None:
        clean["last_modified"] = last_modified
    if err:
        clean["error"] = CONTROL_RE.sub(" ", err)[:300]
    if skew is not None:
        clean["clock_skew_ms"] = skew
    return clean, None
