"""The relay core: send, receipts, beacons, status and doctor. The HTTP layer (api.py) is a thin shell over this."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO

from starlette.concurrency import run_in_threadpool

from . import RECEIPT_WORDING, __version__
from .beacons import HEARTBEAT, PHASE_ORDER, iso_ms, parse_rfc3339
from .config import RateLimit, Settings
from .keys import (
    ApiError,
    check_voice,
    clean_text,
    content_type_for,
    is_valid_channel,
    new_key,
    utf16_units,
)
from .storage import S3Writer, StorageError
from .store import BeaconRow, MessageRow, Store, now_utc
from .tts import TtsClient

TERMINAL = ("finished", "skipped", "failed", "missed")
# Rank of a device status: furthest phase first, then for terminal ties finished > skipped > failed > missed.
TERMINAL_PREFERENCE = {"finished": 4, "skipped": 3, "failed": 2, "missed": 1}
NO_BEACON_HINT = "no beacon has reached this relay; set the phone's beacon endpoint (see GET /v1/status)"


def channel_hint(channel: str) -> str:
    return (f"no device has reported this message yet; check that the phone is listening and follows channel "
            f"'{channel}'. A receipt the phone parked while offline can still arrive.")


class SlidingWindow:
    """Exact sliding-window counter. In memory: limits reset when the relay restarts."""

    def __init__(self, limit: RateLimit, clock=time.monotonic):
        self.limit = limit
        self.clock = clock
        self.events: deque[float] = deque()

    def retry_after(self) -> int | None:
        now = self.clock()
        while self.events and self.events[0] <= now - self.limit.per_seconds:
            self.events.popleft()
        if len(self.events) < self.limit.count:
            return None
        return max(1, int(self.events[0] + self.limit.per_seconds - now + 0.999))

    def hit(self) -> None:
        self.events.append(self.clock())


class RateLimiter:
    def __init__(self, settings: Settings, clock=time.monotonic):
        self.global_window = SlidingWindow(settings.rate_limit, clock) if settings.rate_limit else None
        self.alarm_window = SlidingWindow(settings.alarm_rate_limit, clock) if settings.alarm_rate_limit else None

    def take(self, channel: str) -> None:
        """Consume one send, or raise 429 without consuming anything."""
        windows = [w for w in (self.global_window, self.alarm_window if channel == "alarm" else None) if w]
        for w in windows:
            wait = w.retry_after()
            if wait is not None:
                what = "alarm sends" if w is self.alarm_window else "sends"
                raise ApiError(429, "rate_limited", f"too many {what} (limit {w.limit.describe()}); retry in {wait} s",
                               retry_after=wait)
        for w in windows:
            w.hit()


# ---- receipt derivation (pure functions, unit-tested) ----------------------------------------------------------


@dataclass
class Hop:
    phase: str
    device: str
    app_version: str
    at: str
    at_server: datetime
    received_at: str
    clock_skew_ms: int | None
    since_upload_ms: int | None
    error: str | None
    last_modified: datetime | None
    seq: int

    @property
    def status(self) -> str:
        return "skipped" if self.phase == "finished" and self.error == "skipped" else self.phase

    @property
    def rank(self) -> tuple[int, int]:
        return PHASE_ORDER[self.phase], TERMINAL_PREFERENCE.get(self.status, 0)

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "device": self.device,
            "at": self.at,
            "at_server": iso_ms(self.at_server),
            "received_at": self.received_at,
            "clock_skew_ms": self.clock_skew_ms,
            "since_upload_ms": self.since_upload_ms,
            "error": self.error,
        }


def server_time(at: datetime, clock_skew_ms: int | None) -> datetime:
    """Move a phone timestamp onto the S3 server clock. Skew is phone minus server, so server = phone - skew."""
    return at - timedelta(milliseconds=clock_skew_ms) if clock_skew_ms is not None else at


def build_hops(beacons: list[BeaconRow]) -> list[Hop]:
    """One hop per (device, phase), keeping the earliest; ordered by (server time, phase order)."""
    hops: dict[tuple[str, str], Hop] = {}
    for seq, b in enumerate(beacons):
        if b.phase == HEARTBEAT or b.phase not in PHASE_ORDER:
            continue
        at = parse_rfc3339(b.at)
        if at is None:
            continue
        at_server = server_time(at, b.clock_skew_ms)
        lm = parse_rfc3339(b.last_modified) if b.last_modified else None
        since = round((at_server - lm).total_seconds() * 1000) if lm else None
        hop = Hop(b.phase, b.device, b.app_version, b.at, at_server, b.received_at, b.clock_skew_ms, since, b.error, lm, seq)
        cur = hops.get((b.device, b.phase))
        if cur is None or (hop.at_server, hop.seq) < (cur.at_server, cur.seq):
            hops[(b.device, b.phase)] = hop
    return sorted(hops.values(), key=lambda h: (h.at_server, PHASE_ORDER[h.phase], h.seq))


def device_states(hops: list[Hop]) -> dict[str, Hop]:
    """The deciding hop of each device: its furthest phase, terminal ties broken by preference."""
    best: dict[str, Hop] = {}
    for h in hops:
        cur = best.get(h.device)
        if cur is None or h.rank > cur.rank:
            best[h.device] = h
    return best


def derive_receipt(msg: MessageRow, beacons: list[BeaconRow], *, now: datetime, wait_min: int, beacons_ever: bool,
                   include_hops: bool = True) -> dict:
    hops = build_hops(beacons)
    best = device_states(hops)
    latest_version: dict[str, str] = {}
    for b in beacons:
        latest_version[b.device] = b.app_version
    created = parse_rfc3339(msg.created_at) or now
    hint = None
    if best:
        decider = max(best.values(), key=lambda h: (h.rank, -h.at_server.timestamp()))
        status, status_at = decider.status, iso_ms(decider.at_server)
    elif now - created > timedelta(minutes=wait_min):
        status, status_at = "unconfirmed", iso_ms(created + timedelta(minutes=wait_min))
    else:
        status, status_at = "stored", msg.created_at
    if status in ("stored", "unconfirmed"):
        if not beacons_ever:
            hint = NO_BEACON_HINT
        elif status == "unconfirmed":
            hint = channel_hint(msg.channel)
    lms = [h.last_modified for h in hops if h.last_modified]
    receipt = {
        "id": msg.id,
        "type": msg.type,
        "speech": msg.speech,
        "channel": msg.channel,
        "key": msg.key,
        "created_at": msg.created_at,
        "status": status,
        "status_at": status_at,
        "object_last_modified": iso_ms(max(lms)) if lms else None,
    }
    if include_hops:
        receipt["hops"] = [h.as_dict() for h in hops]
    receipt["devices"] = [
        {"device": d, "status": h.status, "app_version": latest_version.get(d, h.app_version)}
        for d, h in sorted(best.items(), key=lambda kv: kv[1].at_server)
    ]
    receipt["hint"] = hint
    return receipt


# ---- the relay -------------------------------------------------------------------------------------------------


class BeaconSignal:
    """Wakes receipt long-polls when a beacon lands. Waiters also re-check every second, so a missed wake only delays."""

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _current(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._event is None or self._loop is not loop:
            self._event, self._loop = asyncio.Event(), loop
        return self._event

    def notify(self) -> None:
        event, loop = self._event, self._loop
        self._event = None
        if event is None or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._current().wait(), timeout)
        except TimeoutError:
            pass


class Relay:
    def __init__(self, settings: Settings, store: Store, writer: S3Writer, tts: TtsClient | None = None,
                 clock=now_utc, monotonic=time.monotonic):
        self.settings = settings
        self.store = store
        self.writer = writer
        self.tts = tts
        self.clock = clock
        self.limiter = RateLimiter(settings, monotonic)
        self.signal = BeaconSignal()
        self.last_write_ok_at: str | None = None

    # sending ----------------------------------------------------------------------------------------------------

    def resolve_channel(self, channel: object) -> str:
        if channel is None:
            return self.settings.default_channel
        if not is_valid_channel(channel):
            raise ApiError(422, "invalid_channel",
                           "channel must match ^[a-z0-9][a-z0-9_-]{0,31}$", "channel")
        if channel not in self.settings.channels:
            extra = " (alarm is loud and must be enabled deliberately in EARSHOT_RELAY_CHANNELS)" if channel == "alarm" else ""
            raise ApiError(403, "channel_not_allowed",
                           f"channel '{channel}' is not in EARSHOT_RELAY_CHANNELS{extra}", "channel")
        return channel

    def resolve_speech(self, speech: object, voice: str | None) -> str:
        if speech is None:
            speech = self.settings.default_speech
        if speech not in ("device", "server"):
            raise ApiError(422, "invalid_speech", "speech must be device or server", "speech")
        if speech == "server" and self.tts is None:
            raise ApiError(422, "tts_not_configured",
                           "speech=server needs EARSHOT_RELAY_TTS_URL and EARSHOT_RELAY_TTS_MODEL on the relay", "speech")
        if speech == "device" and voice is not None:
            raise ApiError(422, "invalid_voice",
                           "voice applies only to speech=server; the phone speaks a .say with its own Android TTS voice",
                           "voice")
        return speech

    async def _put(self, key: str, body: bytes | IO[bytes], content_type: str) -> None:
        try:
            await run_in_threadpool(self.writer.put, key, body, content_type)
        except StorageError as exc:
            raise ApiError(502, "storage_error", f"PutObject failed: {exc.code}: {exc.hint}") from None
        self.last_write_ok_at = iso_ms(self.clock())

    def _accepted(self, msg_id: str, kind: str, speech: str | None, channel: str, key: str, created_at: str) -> dict:
        return {
            "id": msg_id,
            "type": kind,
            "speech": speech,
            "channel": channel,
            "status": "stored",
            "created_at": created_at,
            "key": key,
            "receipt_url": f"/v1/messages/{msg_id}",
        }

    async def send_text(self, *, text: object, channel: object = None, speech: object = None, voice: object = None) -> dict:
        clean = clean_text(text)
        chan = self.resolve_channel(channel)
        chosen_voice = check_voice(voice)
        mode = self.resolve_speech(speech, chosen_voice)
        self.limiter.take(chan)
        if mode == "device":
            body = clean.encode("utf-8")
            ext, ctype = "say", content_type_for("say")
        else:
            assert self.tts is not None
            result = await self.tts.render(clean, chosen_voice)
            body, ext, ctype = result.data, result.ext, result.content_type
        nk = new_key(chan, ext)
        await self._put(nk.key, body, ctype)
        created = iso_ms(self.clock())
        self.store.add_message(id=nk.message_id, created_at=created, channel=chan, type="text", speech=mode,
                               key=nk.key, bytes=len(body), text_chars=utf16_units(clean))
        return self._accepted(nk.message_id, "text", mode, chan, nk.key, created)

    async def send_audio(self, *, fileobj: IO[bytes], size: int, ext: str, channel: object = None) -> dict:
        chan = self.resolve_channel(channel)
        if size <= 0:
            raise ApiError(422, "empty_file", "the uploaded file is empty", "file")
        if size > self.settings.max_audio_bytes:
            raise ApiError(413, "too_large", f"the file is over EARSHOT_RELAY_MAX_AUDIO_BYTES ({self.settings.max_audio_bytes})",
                           "file")
        self.limiter.take(chan)
        nk = new_key(chan, ext)
        fileobj.seek(0)
        await self._put(nk.key, fileobj, content_type_for(ext))
        created = iso_ms(self.clock())
        self.store.add_message(id=nk.message_id, created_at=created, channel=chan, type="audio", speech=None,
                               key=nk.key, bytes=size, text_chars=None)
        return self._accepted(nk.message_id, "audio", None, chan, nk.key, created)

    # receipts ---------------------------------------------------------------------------------------------------

    def ingest_beacon(self, clean: dict) -> bool:
        inserted = self.store.add_beacon(clean, iso_ms(self.clock()))
        if inserted:
            self.signal.notify()
        return inserted

    def _receipt_for(self, msg: MessageRow, include_hops: bool = True) -> dict:
        beacons = self.store.beacons_for([msg.key])[msg.key]
        return derive_receipt(msg, beacons, now=self.clock(), wait_min=self.settings.receipt_wait_min,
                              beacons_ever=self.store.any_beacon(), include_hops=include_hops)

    def receipt(self, message_id: str) -> dict:
        msg = self.store.get_message(message_id)
        if msg is None:
            raise ApiError(404, "not_found", "no message with that id" if message_id != "latest" else "no message sent yet")
        return self._receipt_for(msg)

    async def wait_receipt(self, message_id: str, wait_s: float) -> dict:
        msg = self.store.get_message(message_id)
        if msg is None:
            raise ApiError(404, "not_found", "no message with that id" if message_id != "latest" else "no message sent yet")
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            receipt = self._receipt_for(msg)
            left = deadline - time.monotonic()
            if receipt["status"] in TERMINAL or left <= 0:
                return receipt
            await self.signal.wait(min(left, 1.0))

    def list_messages(self, *, channel: str | None, limit: int, since: str | None) -> dict:
        if channel is not None and not is_valid_channel(channel):
            raise ApiError(422, "invalid_channel", "channel must match ^[a-z0-9][a-z0-9_-]{0,31}$", "channel")
        since_created = since_rowid = None
        if since:
            if since.startswith("msg_"):
                anchor = self.store.get_message(since)
                if anchor is None:
                    raise ApiError(404, "not_found", "the since message is unknown (it may have been pruned)", "since")
                since_created, since_rowid = anchor.created_at, anchor.rowid
            else:
                ts = parse_rfc3339(since)
                if ts is None:
                    raise ApiError(422, "invalid_field", "since must be an RFC 3339 time or a message id", "since")
                since_created = iso_ms(ts)
        rows = self.store.list_messages(channel=channel, limit=limit, since_created=since_created, since_rowid=since_rowid)
        beacons = self.store.beacons_for([r.key for r in rows])
        now, ever = self.clock(), self.store.any_beacon()
        items = [derive_receipt(r, beacons[r.key], now=now, wait_min=self.settings.receipt_wait_min, beacons_ever=ever,
                                include_hops=False) for r in rows]
        if since:
            next_since = items[-1]["id"] if items else since
        else:
            next_since = items[0]["id"] if items else None
        return {"items": items, "next_since": next_since}

    # status and doctor ------------------------------------------------------------------------------------------

    def beacon_path(self) -> str | None:
        return f"/v1/beacons/{self.settings.beacon_token}" if self.settings.beacon_token else None

    def beacon_url(self) -> str | None:
        path = self.beacon_path()
        if path and self.settings.public_url:
            return self.settings.public_url + path
        return None

    def status(self) -> dict:
        s = self.settings
        devices = [
            {"device": b.device, "last_seen": b.received_at, "app_version": b.app_version, "last_phase": b.phase,
             "clock_skew_ms": b.clock_skew_ms}
            for b in self.store.devices()
        ]
        last_write = max(filter(None, [self.last_write_ok_at, self.store.last_message_at()]), default=None)
        return {
            "relay": {"version": __version__},
            "bucket": {"endpoint_host": s.endpoint_host, "name": s.bucket, "last_write_ok_at": last_write},
            "channels": list(s.channels),
            "default_channel": s.default_channel,
            "speech": {"default": s.default_speech, "server_configured": self.tts is not None},
            "receipts": {
                "configured": s.beacon_token is not None,
                "beacon_url": self.beacon_url(),
                "beacon_path": self.beacon_path(),
                "separate_beacon_port": s.beacon_port,
                "last_beacon_at": self.store.last_beacon_at(),
                "devices": devices,
                "note": RECEIPT_WORDING,
            },
            "limits": {
                "rate_limit": s.rate_limit.describe() if s.rate_limit else None,
                "alarm_rate_limit": s.alarm_rate_limit.describe() if s.alarm_rate_limit else None,
                "max_audio_bytes": s.max_audio_bytes,
                "receipt_wait_min": s.receipt_wait_min,
                "retention_days": s.retention_days,
            },
        }

    async def doctor(self) -> dict:
        """Write one inert `.txt` (the phone never plays a lone .txt) and report config sanity. Never playable."""
        s = self.settings
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        check("channels", True, f"sendable: {', '.join(s.channels)}; default: {s.default_channel}")
        if s.alarm_allowed:
            check("alarm", True, "alarm is enabled: it plays loudly on the phone's alarm stream, also during calls")
        check("s3_endpoint", s.s3_endpoint.startswith("https://"),
              "https" if s.s3_endpoint.startswith("https://") else "plain http (EARSHOT_RELAY_ALLOW_HTTP_S3): disposable tests only")
        if s.beacon_token:
            if self.beacon_url():
                check("receipts", True, "beacon endpoint configured; paste receipts.beacon_url into the app")
            else:
                check("receipts", False, "EARSHOT_RELAY_PUBLIC_URL is unset, so the full beacon URL is unknown; "
                      "use https://<your public host> + receipts.beacon_path")
        else:
            check("receipts", False, "EARSHOT_RELAY_BEACON_TOKEN is unset: messages send, but no receipts")
        if self.tts is not None:
            leadin = self.tts.settings.leadin_ms
            ff = shutil.which("ffmpeg") is not None
            check("server_tts", ff or leadin == 0,
                  f"{self.tts.settings.url} model {self.tts.settings.model}; lead-in {leadin} ms; ffmpeg {'found' if ff else 'missing'}")
        stamp = self.clock().strftime("%Y%m%dT%H%M%S%fZ")
        key = f"{s.default_channel}/relay-doctor-{stamp}.txt"
        write: dict = {"ok": False, "key": key, "error": None}
        try:
            body = (f"earshot-relay doctor write check at {iso_ms(self.clock())}. A lone .txt is never played; "
                    "this object is safe to delete.\n").encode("utf-8")
            await self._put(key, body, content_type_for("txt"))
            write["ok"] = True
        except ApiError as exc:
            write["error"] = exc.message
        check("write", write["ok"], "PutObject succeeded" if write["ok"] else (write["error"] or "PutObject failed"))
        return {
            "ok": all(c["ok"] for c in checks if c["name"] in ("write",)),
            "relay": {"version": __version__},
            "write": write,
            "checks": checks,
            "beacon_url": self.beacon_url(),
            "beacon_path": self.beacon_path(),
        }
