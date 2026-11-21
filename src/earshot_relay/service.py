"""The relay core: send, receipts, beacons, status and doctor. The HTTP layer (api.py) is a thin shell over this."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO

from starlette.concurrency import run_in_threadpool

from . import RECEIPT_WORDING, __version__
from .beacons import HEARTBEAT, PHASE_ORDER, TERMINAL, iso_ms, parse_rfc3339, receipt_is_final
from .config import RateLimit, Settings
from .hygiene import (
    MAX_COOLDOWN_S,
    check_flag,
    check_seconds,
    check_tag,
    check_window,
    normalize_text,
)
from .keys import (
    ApiError,
    check_voice,
    clean_text,
    content_type_for,
    is_valid_channel,
    new_id,
    new_key,
    utf16_units,
)
from .storage import S3Writer, StorageError
from .store import BeaconRow, FailedRow, MessageRow, PendingRow, Store, TooManyDevices, now_utc
from .tts import TtsClient
from .watchdog import Watchdog

log = logging.getLogger("earshot_relay")

__all__ = ["TERMINAL", "Relay", "derive_receipt", "receipt_is_final"]

IDEMPOTENCY_WINDOW = timedelta(hours=24)
MAX_DEVICES_PER_KEY = 16  # distinct devices one message can collect beacons from
STATUS_MAX_DEVICES = 20  # devices listed in GET /v1/status
ALARM_CAVEAT = (
    "alarm plays a siren, then the message on the phone's alarm stream at high volume, ahead of normal items. It "
    "plays during a phone call (unless the app's 'Alarms can interrupt calls' is off) and the other party may hear "
    "it. Do Not Disturb set to 'Total silence', an Android refusal, a stopped service, a powered-off phone, battery "
    "restrictions or no network can still silence it. It is not an emergency or life-safety service."
)
# Rank of a device status: furthest phase first, then for terminal ties finished > skipped > failed > missed.
TERMINAL_PREFERENCE = {"finished": 4, "skipped": 3, "failed": 2, "missed": 1}
# A coalesced write that fails (the bucket or the TTS server is down) is retried after these delays, then given up
# on: the text is deleted and the id's receipt reads write_failed (never a silent 404).
FLUSH_RETRY_S = (10, 30, 60)
# Why a held message's write was refused when its window closed (the flush re-checks the policy that admitted it).
# These are final at once: retrying cannot help until the configuration changes.
POLICY_REFUSALS = {
    "channel_not_allowed": "its channel was removed from EARSHOT_RELAY_CHANNELS",
    "compat_alarm_disabled": "compat alarm was switched off (EARSHOT_RELAY_COMPAT_ALARM, or alarm left "
                             "EARSHOT_RELAY_CHANNELS)",
    "tts_not_configured": "server speech is no longer configured",
}
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

    def take(self) -> float | None:
        """Record one event and return its stamp (for refund), or None when over the limit (nothing recorded)."""
        if self.retry_after() is not None:
            return None
        stamp = self.clock()
        self.events.append(stamp)
        return stamp

    def refund(self, stamp: float) -> None:
        try:
            self.events.remove(stamp)
        except ValueError:
            pass  # already slid out of the window

    def remaining(self) -> int:
        """Sends still allowed in the current window (what Pushover clients read from X-Limit-App-Remaining)."""
        self.retry_after()  # slides the window
        return max(0, self.limit.count - len(self.events))


class RateLimiter:
    def __init__(self, settings: Settings, clock=time.monotonic):
        self.global_window = SlidingWindow(settings.rate_limit, clock) if settings.rate_limit else None
        self.alarm_window = SlidingWindow(settings.alarm_rate_limit, clock) if settings.alarm_rate_limit else None

    def take(self, channel: str) -> list[tuple[SlidingWindow, float]]:
        """Reserve one send, or raise 429 without reserving anything. Refund the reservation if the send fails."""
        windows = [w for w in (self.global_window, self.alarm_window if channel == "alarm" else None) if w]
        for w in windows:
            wait = w.retry_after()
            if wait is not None:
                what = "alarm sends" if w is self.alarm_window else "sends"
                raise ApiError(429, "rate_limited", f"too many {what} (limit {w.limit.describe()}); retry in {wait} s",
                               retry_after=wait)
        return [(w, w.take()) for w in windows]

    def alarm_limited(self) -> bool:
        """True when the next alarm send would be refused for the alarm budget alone (not the global limit)."""
        return self.alarm_window is not None and self.alarm_window.retry_after() is not None

    @staticmethod
    def refund(reservation: list[tuple[SlidingWindow, float]]) -> None:
        """Give back a reserved send whose TTS or PutObject failed: nothing reached the bucket, so nothing counts."""
        for w, stamp in reservation:
            if stamp is not None:
                w.refund(stamp)


class KeyedLocks:
    """One asyncio lock per key, dropped when nobody holds or waits on it."""

    def __init__(self) -> None:
        self._locks: dict[str, list] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)


@dataclass(frozen=True)
class Idempotency:
    """`digest` is sha256 of the Idempotency-Key header (None without one); `replay` is the original 202 body."""

    digest: str | None
    replay: dict | None


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
        try:  # validation bounds `at`, but one odd stored row must never fail a whole receipt or list
            at_server = server_time(at, b.clock_skew_ms)
        except OverflowError:
            continue
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
    """Wakes waiters when a beacon lands or a send changes what is stored or held: receipt long-polls (which also
    re-check every second), the coalescing flush loop and the watchdog (which sleep until their next deadline).
    `version` counts notifications, so a loop that read it before working can tell it missed one and go again."""

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.version = 0

    def _current(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._event is None or self._loop is not loop:
            self._event, self._loop = asyncio.Event(), loop
        return self._event

    def notify(self) -> None:
        self.version += 1
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

    async def wait(self, timeout: float | None = None, *, since: int | None = None) -> None:
        event = self._current()
        if since is not None and since != self.version:
            return
        if timeout is None:
            await event.wait()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass


class Relay:
    def __init__(self, settings: Settings, store: Store, writer: S3Writer, tts: TtsClient | None = None,
                 clock=now_utc, monotonic=time.monotonic, watchdog_transport=None):
        self.settings = settings
        self.store = store
        self.writer = writer
        self.tts = tts
        self.clock = clock
        self.limiter = RateLimiter(settings, monotonic)
        self.beacon_window = SlidingWindow(settings.beacon_rate_limit, monotonic) if settings.beacon_rate_limit else None
        self.signal = BeaconSignal()
        self.last_write_ok_at: str | None = None
        self._idem_locks = KeyedLocks()
        self._tag_locks = KeyedLocks()
        self.watchdog = Watchdog(self, watchdog_transport) if settings.watchdog_s and settings.beacon_token else None

    # sending ----------------------------------------------------------------------------------------------------

    def alarm_rate_limited(self) -> bool:
        return self.limiter.alarm_limited()

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

    @asynccontextmanager
    async def idempotency(self, header: str | None) -> AsyncIterator[Idempotency]:
        """Hold the Idempotency-Key for the whole send, so a concurrent retry waits and then gets the first 202 body.

        The same key within 24 hours returns the original body without writing anything or counting toward the
        rate limit. The relay keeps only a hash of the key.
        """
        if header is None:
            yield Idempotency(None, None)
            return
        digest = hashlib.sha256(header.encode("utf-8")).hexdigest()
        async with self._idem_locks.hold(digest):
            cutoff = iso_ms(self.clock() - IDEMPOTENCY_WINDOW)
            replay = self.store.get_suppressed(digest, cutoff)
            row = self.store.get_by_idem(digest, cutoff) if replay is None else None
            replay = replay or (self._accepted_row(row) if row else None)
            if replay is None:  # a key that was merged into a coalesced message (before or after it was written)
                alias = self.store.get_alias(digest, cutoff)
                if alias:
                    written = self.store.get_message(alias)
                    pending = None if written else self.store.get_pending_by_id(alias)
                    replay = (self._accepted_row(written) if written
                              else self._accepted_pending(pending) if pending else None)
            yield Idempotency(digest, replay)

    def _accepted(self, msg_id: str, kind: str, speech: str | None, channel: str, key: str | None, created_at: str, *,
                  status: str = "stored", tag: str | None = None, normalized: bool = False, truncated: bool = False,
                  write_at: str | None = None, cooldown_until: str | None = None) -> dict:
        return {
            "id": msg_id,
            "type": kind,
            "speech": speech,
            "channel": channel,
            "status": status,
            "created_at": created_at,
            "key": key,
            "receipt_url": f"/v1/messages/{msg_id}",
            "tag": tag,
            "normalized": normalized,
            "truncated": truncated,
            "write_at": write_at,
            "cooldown_until": cooldown_until,
        }

    def _accepted_row(self, row: MessageRow) -> dict:
        return self._accepted(row.id, row.type, row.speech, row.channel, row.key, row.created_at, tag=row.tag,
                              normalized=row.normalized, truncated=row.truncated)

    def _accepted_pending(self, p: PendingRow) -> dict:
        return self._accepted(p.id, "text", p.speech, p.channel, None, p.created_at, status="coalescing", tag=p.tag,
                              normalized=p.normalized, truncated=p.truncated, write_at=p.due_at)

    def _prepare_text(self, text: object, normalize: bool | None, channel: str) -> tuple[str, bool, bool]:
        """The text to store, and whether the normalizer changed it and whether it cut it short."""
        if not isinstance(text, str):
            raise ApiError(422, "invalid_field", "text must be a string", "text")
        enabled, max_words = self.settings.hygiene_for(channel)
        if (enabled if normalize is None else normalize):
            result = normalize_text(text, max_words=max_words)
            if not result.text:
                raise ApiError(422, "empty_text", "text is empty after normalization (it held only emoji, markdown "
                                                  "or links)", "text")
            return clean_text(result.text), result.changed, result.truncated
        return clean_text(text), False, False

    async def send_text(self, *, text: object, channel: object = None, speech: object = None, voice: object = None,
                        idem_key: str | None = None, normalize: object = None, tag: object = None,
                        coalesce_window_s: object = None, cooldown_s: object = None, compat: bool = False,
                        coalesce_s: object = None) -> dict:
        """Store text for the phone. `tag` with `coalesce_window_s` holds the write until the window closes (the latest
        text under the tag wins); `tag` with `cooldown_s` drops a repeat of the tag inside the cooldown. Both act before
        anything is written: the relay holds PutObject only and cannot replace an object that already exists.
        `coalesce_s` is the 1.1.0 name of `coalesce_window_s`, still accepted. `compat` marks a compat-ingress send,
        whose alarm promotion is checked again when a held window is written."""
        chan = self.resolve_channel(channel)
        clean, normalized, truncated = self._prepare_text(text, check_flag("normalize", normalize), chan)
        chosen_voice = check_voice(voice)
        mode = self.resolve_speech(speech, chosen_voice)
        tag = check_tag(tag)
        coalesce = check_window(coalesce_window_s, coalesce_s)
        cooldown = check_seconds("cooldown_s", cooldown_s, MAX_COOLDOWN_S)
        if tag is None and (coalesce or cooldown):
            raise ApiError(422, "tag_required", "coalesce_window_s and cooldown_s need a tag", "tag")
        if coalesce or cooldown:
            return await self._send_tagged(text=clean, chan=chan, mode=mode, voice=chosen_voice, idem_key=idem_key,
                                           tag=tag, normalized=normalized, truncated=truncated, coalesce=coalesce,
                                           cooldown=cooldown, compat=compat)
        return await self._write_text(text=clean, chan=chan, mode=mode, voice=chosen_voice, idem_key=idem_key, tag=tag,
                                      normalized=normalized, truncated=truncated)

    async def _write_text(self, *, text: str, chan: str, mode: str, voice: str | None, idem_key: str | None,
                          tag: str | None, normalized: bool, truncated: bool, message_id: str | None = None,
                          reserve: bool = True) -> dict:
        """TTS (when server speech), PutObject and the index row. `reserve=False` is the coalescing flush, whose
        send was already counted when its window opened."""
        reservation = self.limiter.take(chan) if reserve else []
        try:
            if mode == "device":
                body = text.encode("utf-8")
                ext, ctype = "say", content_type_for("say")
            else:
                if self.tts is None:
                    raise ApiError(422, "tts_not_configured", "speech=server needs the relay's TTS settings", "speech")
                result = await self.tts.render(text, voice)
                body, ext, ctype = result.data, result.ext, result.content_type
            nk = new_key(chan, ext, message_id)
            await self._put(nk.key, body, ctype)
        except BaseException:
            self.limiter.refund(reservation)
            raise
        created = iso_ms(self.clock())
        self.store.add_message(id=nk.message_id, created_at=created, channel=chan, type="text", speech=mode,
                               key=nk.key, bytes=len(body), text_chars=utf16_units(text), idem_key=idem_key, tag=tag,
                               normalized=normalized, truncated=truncated)
        self.signal.notify()
        return self._accepted(nk.message_id, "text", mode, chan, nk.key, created, tag=tag, normalized=normalized,
                              truncated=truncated)

    # coalescing and cooldown ------------------------------------------------------------------------------------

    async def _send_tagged(self, *, text: str, chan: str, mode: str, voice: str | None, idem_key: str | None,
                           tag: str, normalized: bool, truncated: bool, coalesce: float | None,
                           cooldown: float | None, compat: bool) -> dict:
        async with self._tag_locks.hold(f"{chan}\x00{tag}"):
            now = self.clock()
            pending = self.store.get_pending(chan, tag)
            if pending and (pending.window_end_at or pending.due_at) <= iso_ms(now):
                # The window has closed but the flush has not run yet (or its write is waiting for a retry). A closed
                # window never takes new text: write it now, under this tag's lock, before looking at this send.
                await self._flush_locked(pending)
                pending = self.store.get_pending(chan, tag)
                if pending and coalesce:
                    # Its write failed and is waiting for a retry: the accepted text is frozen, and a second window
                    # under the same tag cannot open until it is written (or given up on).
                    wait = (parse_rfc3339(pending.due_at) or now) - now
                    raise ApiError(503, "coalesce_busy", f"the previous window under tag '{tag}' closed and its write "
                                   "is waiting for a retry; send again shortly",
                                   retry_after=max(1, int(wait.total_seconds() + 0.999)))
            if pending and coalesce:
                # Inside an open window: the latest text replaces what will be written. Same id, same window.
                self.store.update_pending(pending.id, text=text, speech=mode, voice=voice, normalized=normalized,
                                          truncated=truncated, compat=compat)
                if idem_key:
                    self.store.add_alias(idem_key, pending.id, iso_ms(now))
                return self._accepted_pending(self.store.get_pending_by_id(pending.id) or pending)
            if cooldown:
                if pending:
                    ref = (pending.id, None, pending.speech, pending.created_at, pending.due_at)
                else:
                    recent = self.store.latest_tagged(chan, tag, iso_ms(now - timedelta(seconds=cooldown)))
                    ref = (recent.id, recent.key, recent.speech, recent.created_at, recent.created_at) if recent else None
                if ref:
                    until = parse_rfc3339(ref[4]) + timedelta(seconds=cooldown)
                    result = self._accepted(ref[0], "text", ref[2], chan, ref[1], ref[3], status="suppressed", tag=tag,
                                            cooldown_until=iso_ms(until))
                    if idem_key:  # a retry of this request must get this same answer, not a fresh send
                        self.store.save_suppressed(idem_key, iso_ms(now), result)
                    return result
            if coalesce:
                reservation = self.limiter.take(chan)  # one send, counted when its window opens
                end = iso_ms(now + timedelta(seconds=coalesce))
                row = PendingRow(id=new_id(), channel=chan, tag=tag, created_at=iso_ms(now), due_at=end, text=text,
                                 speech=mode, voice=voice, normalized=normalized, truncated=truncated, merged=1,
                                 attempts=0, compat=compat, window_end_at=end)
                try:
                    self.store.add_pending(row)
                except BaseException:
                    self.limiter.refund(reservation)
                    raise
                if idem_key:
                    self.store.add_alias(idem_key, row.id, iso_ms(now))
                self.signal.notify()  # the flush loop sleeps until the earliest window closes
                return self._accepted_pending(row)
            return await self._write_text(text=text, chan=chan, mode=mode, voice=voice, idem_key=idem_key, tag=tag,
                                          normalized=normalized, truncated=truncated)

    async def flush_due(self) -> int:
        """Write every coalesced message whose window has closed. Runs at startup, then whenever the earliest window
        closes (the flush loop sleeps until then, or until a send changes the queue)."""
        written = 0
        for row in self.store.due_pending(iso_ms(self.clock())):
            if await self._flush_one(row.channel, row.tag):
                written += 1
        return written

    async def _flush_one(self, channel: str, tag: str) -> bool:
        async with self._tag_locks.hold(f"{channel}\x00{tag}"):
            row = self.store.get_pending(channel, tag)  # re-read under the lock: a request may have changed it
            if row is None:
                return False
            return await self._flush_locked(row)

    def _flush_policy(self, row: PendingRow) -> None:
        """The checks that admitted a held message, run again when it is written: the relay may have restarted with
        a channel removed from EARSHOT_RELAY_CHANNELS, compat alarm switched off or server speech unconfigured."""
        self.resolve_channel(row.channel)
        if row.channel == "alarm" and row.compat and not self.settings.compat_alarm_allowed:
            raise ApiError(403, "compat_alarm_disabled", "compat alarm is no longer enabled (EARSHOT_RELAY_COMPAT_ALARM "
                                                         "and alarm in EARSHOT_RELAY_CHANNELS)", "channel")
        self.resolve_speech(row.speech, row.voice)

    async def _flush_locked(self, row: PendingRow) -> bool:
        """Write one held message whose window has closed. The caller holds its tag lock."""
        if row.due_at > iso_ms(self.clock()):
            return False  # still open, or a failed write waiting for its retry
        if self.store.get_message(row.id) is not None:  # written just before a crash: only the row is left over
            self.store.delete_pending(row.id)
            return False
        try:
            self._flush_policy(row)
        except ApiError as exc:
            # Refused for good: retrying cannot help until the configuration changes, and that needs a restart.
            self.store.fail_pending(row, iso_ms(self.clock()), exc.code)
            self.signal.notify()
            log.warning(json.dumps({"event": "coalesce_refused", "message_id": row.id, "error": exc.code}))
            return False
        try:
            await self._write_text(text=row.text, chan=row.channel, mode=row.speech, voice=row.voice, idem_key=None,
                                   tag=row.tag, normalized=row.normalized, truncated=row.truncated,
                                   message_id=row.id, reserve=False)
        except Exception as exc:  # noqa: BLE001 - a failed write is retried, then given up on, never raised into the loop
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            attempts = row.attempts + 1
            if attempts > len(FLUSH_RETRY_S):
                self.store.fail_pending(PendingRow(**{**row.__dict__, "attempts": attempts}),
                                        iso_ms(self.clock()), str(code))
                self.signal.notify()  # the watchdog announces a write_failed message
                log.warning(json.dumps({"event": "coalesce_dropped", "message_id": row.id, "error": code}))
            else:
                due = self.clock() + timedelta(seconds=FLUSH_RETRY_S[attempts - 1])
                self.store.retry_pending(row.id, attempts, iso_ms(due))
                log.warning(json.dumps({"event": "coalesce_retry", "message_id": row.id, "error": code,
                                        "attempt": attempts}))
            return False
        self.store.delete_pending(row.id)
        log.info(json.dumps({"event": "coalesce_flushed", "message_id": row.id, "merged": row.merged}))
        self.signal.notify()
        return True

    async def send_audio(self, *, fileobj: IO[bytes], size: int, ext: str, channel: object = None,
                         idem_key: str | None = None) -> dict:
        chan = self.resolve_channel(channel)
        if size <= 0:
            raise ApiError(422, "empty_file", "the uploaded file is empty", "file")
        if size > self.settings.max_audio_bytes:
            raise ApiError(413, "too_large", f"the file is over EARSHOT_RELAY_MAX_AUDIO_BYTES ({self.settings.max_audio_bytes})",
                           "file")
        reservation = self.limiter.take(chan)
        try:
            nk = new_key(chan, ext)
            fileobj.seek(0)
            await self._put(nk.key, fileobj, content_type_for(ext))
        except BaseException:
            self.limiter.refund(reservation)
            raise
        created = iso_ms(self.clock())
        self.store.add_message(id=nk.message_id, created_at=created, channel=chan, type="audio", speech=None,
                               key=nk.key, bytes=size, text_chars=None, idem_key=idem_key)
        self.signal.notify()
        return self._accepted(nk.message_id, "audio", None, chan, nk.key, created)

    # receipts ---------------------------------------------------------------------------------------------------

    def beacon_admit(self) -> None:
        """Count one beacon request against EARSHOT_RELAY_BEACON_RATE_LIMIT, or raise 429 (the phone retries 429)."""
        if self.beacon_window is None:
            return
        if self.beacon_window.take() is None:
            wait = self.beacon_window.retry_after() or 1
            raise ApiError(429, "rate_limited",
                           f"too many beacons (limit {self.beacon_window.limit.describe()}); retry in {wait} s",
                           retry_after=wait)

    def ingest_beacon(self, clean: dict) -> bool:
        try:
            inserted = self.store.add_beacon(clean, iso_ms(self.clock()), max_devices_per_key=MAX_DEVICES_PER_KEY)
        except TooManyDevices:
            raise ApiError(400, "invalid_beacon",
                           f"this key already has beacons from {MAX_DEVICES_PER_KEY} devices") from None
        if inserted:
            self.signal.notify()
        return inserted

    def _receipt_for(self, msg: MessageRow, include_hops: bool = True) -> dict:
        beacons = self.store.beacons_for([msg.key])[msg.key]
        return derive_receipt(msg, beacons, now=self.clock(), wait_min=self.settings.receipt_wait_min,
                              beacons_ever=self.store.any_beacon(), include_hops=include_hops)

    def _pending_receipt(self, p: PendingRow) -> dict:
        return {
            "id": p.id, "type": "text", "speech": p.speech, "channel": p.channel, "key": None,
            "created_at": p.created_at, "status": "coalescing", "status_at": p.created_at,
            "object_last_modified": None, "hops": [], "devices": [],
            "hint": (f"held for coalescing under tag '{p.tag}': nothing is in the bucket yet. It is written at "
                     f"{p.due_at} with the latest text sent under that tag."),
        }

    def _failed_receipt(self, f: FailedRow) -> dict:
        if f.error in POLICY_REFUSALS:
            hint = (f"the relay accepted this message into a coalescing window under tag '{f.tag}', but when the "
                    f"window closed the relay's configuration no longer allowed it ({f.error}: "
                    f"{POLICY_REFUSALS[f.error]}), so it was not written. Nothing is stored and nothing will play.")
        else:
            hint = (f"the relay accepted this message into a coalescing window under tag '{f.tag}' but could not write "
                    f"it to the bucket after {f.attempts} attempts (last error: {f.error}). Nothing is stored and "
                    "nothing will play. Check the bucket (and the TTS server) and send it again.")
        return {
            "id": f.id, "type": "text", "speech": None, "channel": f.channel, "key": None,
            "created_at": f.created_at, "status": "write_failed", "status_at": f.failed_at,
            "object_last_modified": None, "hops": [], "devices": [], "hint": hint,
        }

    def _missing(self, message_id: str) -> ApiError:
        return ApiError(404, "not_found", "no message with that id" if message_id != "latest" else "no message sent yet")

    def receipt(self, message_id: str) -> dict:
        msg = self.store.get_message(message_id)
        if msg is None:
            pending = self.store.get_pending_by_id(message_id) if message_id != "latest" else None
            if pending is None:
                failed = self.store.get_failed(message_id) if message_id != "latest" else None
                if failed is None:
                    raise self._missing(message_id)
                return self._failed_receipt(failed)
            return self._pending_receipt(pending)
        return self._receipt_for(msg)

    async def wait_receipt(self, message_id: str, wait_s: float) -> dict:
        msg = self.store.get_message(message_id)
        pending_id = None
        if msg is None:
            pending = self.store.get_pending_by_id(message_id) if message_id != "latest" else None
            if pending is None:
                failed = self.store.get_failed(message_id) if message_id != "latest" else None
                if failed is None:
                    raise self._missing(message_id)
                return self._failed_receipt(failed)
            pending_id = pending.id
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            if msg is None and pending_id:
                msg = self.store.get_message(pending_id)  # the window closed and the object was written
            if msg is not None:
                receipt = self._receipt_for(msg)
            else:
                pending = self.store.get_pending_by_id(pending_id)
                if pending is None:
                    failed = self.store.get_failed(pending_id)
                    if failed is None:
                        raise self._missing(pending_id)
                    return self._failed_receipt(failed)
                receipt = self._pending_receipt(pending)
            left = deadline - time.monotonic()
            if receipt_is_final(receipt) or left <= 0:
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
            for b in self.store.devices(STATUS_MAX_DEVICES)
        ]
        last_write = max(filter(None, [self.last_write_ok_at, self.store.last_message_at()]), default=None)
        return {
            "relay": {"version": __version__},
            "bucket": {"endpoint_host": s.endpoint_host, "name": s.bucket, "last_write_ok_at": last_write},
            "channels": list(s.channels),
            "default_channel": s.default_channel,
            "speech": {"default": s.default_speech, "server_configured": self.tts is not None,
                       "provider": self.tts.settings.provider if self.tts else None},
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
            "compat": self.compat_status(),
            "hygiene": {"normalize": s.normalize, "max_words": s.max_words,
                        "coalescing_pending": self.store.pending_count(),
                        "coalescing_failed": self.store.failed_count()},
            "watchdog": self.watchdog.status() if self.watchdog else {
                "enabled": False, "reason": "beacon_token_missing" if s.watchdog_s and not s.beacon_token else "not_configured"},
        }

    def compat_status(self) -> dict:
        from .compat import PREFIXES

        s = self.settings
        return {
            "enabled": list(s.compat),
            "paths": {p: PREFIXES[p] for p in s.compat},
            "port": s.compat_port,
            "alarm": s.compat_alarm_allowed,
            "ntfy_topics": s.ntfy_topics if "ntfy" in s.compat else {},
            "pushover_token": s.compat_pushover_token is not None,
            "send_token": s.compat_token is not None,
        }

    async def doctor(self) -> dict:
        """Write one inert `.txt` (the phone never plays a lone .txt) and report config sanity. Never playable."""
        s = self.settings
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        check("channels", True, f"sendable: {', '.join(s.channels)}; default: {s.default_channel}")
        if s.alarm_allowed:
            check("alarm", True, "alarm is enabled. " + ALARM_CAVEAT[0].upper() + ALARM_CAVEAT[1:])
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
        if s.compat:
            c = self.compat_status()
            where = ", ".join(f"{p} at {c['paths'][p]}" for p in s.compat)
            port = f"; also at the root of port {s.compat_port}" if s.compat_port else ""
            alarm = ("top priority plays on the alarm channel" if c["alarm"]
                     else "top priority plays as a normal item (X-Earshot-Downgraded: alarm)")
            check("compat", True, f"{where}{port}; {alarm}")
        if s.normalize != "off" or s.max_words:
            check("normalize", True, f"mode {s.normalize}" + (f"; at most {s.max_words} words" if s.max_words else "")
                  + " (a request's normalize field overrides the mode)")
        if self.watchdog:
            check("watchdog", True, f"notice after {s.watchdog_s} s without a playback report, to {s.watchdog_host} "
                  f"({self.watchdog.format()} body); it watches sends, not idle phones")
        if s.beacon_port:
            check("beacon_port", True, f"beacons are served only on port {s.beacon_port}, not on {s.port}: publish it "
                  f"too (docker: -p 127.0.0.1:{s.beacon_port}:{s.beacon_port}) and point the tunnel at it")
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
