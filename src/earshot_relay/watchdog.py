"""The delivery watchdog: one out-of-band notice when a send gets no playback report from the phone.

It watches SENDS, not phones. When a message has been stored for EARSHOT_RELAY_WATCHDOG_S seconds and no device has
reported anything about THAT message (no `detected`, `started`, `finished`, `failed` or `missed` beacon for its key),
that is an incident: the relay POSTs one notice to EARSHOT_RELAY_WATCHDOG_URL, outside the phone path, so a dead phone
does not have to tell you it is dead. With no traffic it stays silent, and an idle dead phone is invisible to it: the
app sends no heartbeat.

Rules, all of them tested with a controllable clock:

- Correlation is per message. A beacon only ever accounts for its own key: a receipt for message B never covers an
  earlier message A that got none, and a beacon for a key the relay never stored covers nothing.
- One notice per incident. Until its notice goes out, an incident follows the oldest overdue message still without a
  report (it closes, silently, if every one of them reports late). Once announced, it covers every message that falls
  overdue while it is open, and it clears when a beacon received after the notice reports on its message or on any
  later one: the phone is back. A message never reported on is announced at most once.
- EARSHOT_RELAY_WATCHDOG_COOLDOWN_S is a floor between notices, not a mute: an incident that opens inside the cooldown
  waits it out and is announced then, if it is still open.
- A coalesced message the relay accepted but never wrote (the bucket was down through every retry, or its channel or
  compat alarm was switched off before its window closed) is its own kind of incident: one notice, then closed. It
  never clears on playback, because the message will not play.
- A failing webhook is retried twice more (30 s, then 60 s later), then given up on for that incident.
- The URL may hold a secret (an ntfy topic, a token). It is never logged and never shown, only its host is.

It runs on events, not on a timer: the loop sleeps until the next deadline it knows of (a message falling overdue, a
retry, the end of a cooldown) or until a send or a beacon wakes it, and with nothing outstanding it sleeps until then.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from . import __version__
from .beacons import iso_ms, parse_rfc3339

log = logging.getLogger("earshot_relay")

MAX_ATTEMPTS = 3
RETRY_STEP_S = 30
POST_TIMEOUT_S = 10
META_KEY = "watchdog"
ALIVE_KEY = "watchdog_alive_at"  # refreshed by checks (at most once a minute) and when the relay stops
ALIVE_EVERY_S = 60
STALE_AFTER_S = 600  # a saved state older than this is from before the watchdog was last off: start over
NOTE = ("Watches sends, not idle phones: the app sends no heartbeat, so a dead phone with no traffic raises nothing. "
        "A notice means no device reported on a message in time, not that the phone is off.")
# Write-failed reasons that are a refusal by the relay's own (changed) configuration, not a bucket failure.
POLICY_REFUSALS = ("channel_not_allowed", "compat_alarm_disabled", "tts_not_configured")


def is_ntfy_style(url: str) -> bool:
    """ntfy.sh, or a host with an `ntfy` label (ntfy.example.com): a plain text POST is a publish there."""
    host = (urlsplit(url).hostname or "").lower()
    return host == "ntfy.sh" or "ntfy" in host.split(".")


@dataclass
class WatchdogState:
    enabled_since: str
    last_clear_at: str  # when the last incident cleared (informational)
    incident_since: str | None = None
    incident_message: str | None = None
    incident_channel: str | None = None
    incident_kind: str = "silent"  # silent: a stored message nobody reported on. write_failed: a held message never written
    incident_count: int = 1
    notified: bool = False
    attempts: int = 0
    next_attempt_at: str | None = None
    last_notice_at: str | None = None
    last_notice_ok: bool | None = None
    last_error: str | None = None
    notices_sent: int = 0
    # Messages created at or before this were announced (or were all reported on): never looked at again. None in a
    # state saved by 1.1.0, whose last_clear_at then serves.
    covered_until: str | None = None


STATE_FIELDS = {f.name for f in fields(WatchdogState)}


class Watchdog:
    def __init__(self, relay, transport: httpx.AsyncBaseTransport | None = None):
        self.relay = relay
        self.settings = relay.settings
        self._transport = transport
        now = iso_ms(relay.clock())
        saved = relay.store.get_meta(META_KEY)
        alive = parse_rfc3339(relay.store.get_meta(ALIVE_KEY))
        state = None
        self._alive_at = alive
        if saved and alive and (relay.clock() - alive).total_seconds() <= STALE_AFTER_S:
            try:
                data = json.loads(saved)
                state = WatchdogState(**{k: v for k, v in data.items() if k in STATE_FIELDS})
            except (ValueError, TypeError, AttributeError):
                state = None
        self.state = state or WatchdogState(enabled_since=now, last_clear_at=now, covered_until=now)

    # ---- one pass --------------------------------------------------------------------------------------------

    @property
    def covered_until(self) -> str:
        st = self.state
        return st.covered_until or st.last_clear_at or st.enabled_since

    def _overdue_before(self, now: datetime) -> str:
        return iso_ms(now - timedelta(seconds=self.settings.watchdog_s))

    async def check(self) -> None:
        """Clear a finished incident, open a new one, and send the notice that is due. Cheap: a few small queries."""
        st, store = self.state, self.relay.store
        before = asdict(st)
        now = self.relay.clock()
        overdue = self._overdue_before(now)
        if st.incident_since and st.incident_kind == "silent":
            if st.notified:
                # Announced: everything falling overdue while it is open is part of it. It clears when the phone
                # reports, after the notice, on its message or a later one.
                st.covered_until = max(self.covered_until, overdue)
                first = store.get_message(st.incident_message or "")
                if first is None or store.reported_since(first.created_at, st.last_notice_at or st.incident_since):
                    self._clear("reported")
            else:
                # Not announced yet (cooldown or webhook retry): follow the oldest message still without a report.
                late = store.undetected_message(self.covered_until, overdue)
                if late is None:
                    self._clear("reported_before_notice")
                else:
                    st.incident_message, st.incident_channel = late.id, late.channel
                    st.incident_count = store.undetected_count(self.covered_until, overdue)
        if st.incident_since is None:
            late = store.undetected_message(self.covered_until, overdue)
            if late is not None:
                self._open_incident(late.id, late.channel, now,
                                    count=store.undetected_count(self.covered_until, overdue))
            else:
                st.covered_until = max(self.covered_until, overdue)  # everything up to here reported in
                lost, count = store.unnotified_failed()
                if lost is not None:
                    self._open_incident(lost.id, lost.channel, now, kind="write_failed", count=count)
        if st.incident_since and not st.notified:
            await self._notify(now)
            if st.notified and st.incident_kind == "silent":
                st.covered_until = max(self.covered_until, overdue)  # announced, all of them
        if st.incident_since and st.notified and st.incident_kind == "write_failed":
            store.mark_failed_notified()  # told once (or given up on); it never clears by playback
            self._close_incident()
        if asdict(st) != before:
            store.set_meta(**{META_KEY: json.dumps(asdict(st))})
        if self._alive_at is None or (now - self._alive_at).total_seconds() >= ALIVE_EVERY_S:
            self.mark_alive(now)

    def mark_alive(self, now: datetime | None = None) -> None:
        """Record that the watchdog is running (a restart soon after keeps its state). Also called at shutdown."""
        now = now or self.relay.clock()
        self._alive_at = now
        self.relay.store.set_meta(**{ALIVE_KEY: iso_ms(now)})

    def _clear(self, why: str) -> None:
        st = self.state
        log.info(json.dumps({"event": "watchdog_cleared", "notified": st.notified, "why": why}))
        st.last_clear_at = iso_ms(self.relay.clock())
        self._close_incident()

    def _close_incident(self) -> None:
        st = self.state
        st.incident_since = st.incident_message = st.incident_channel = st.next_attempt_at = None
        st.incident_kind, st.incident_count = "silent", 1
        st.notified, st.attempts = False, 0

    def _open_incident(self, message_id: str, channel: str, now: datetime, *, kind: str = "silent",
                       count: int = 1) -> None:
        st = self.state
        st.incident_since, st.incident_message, st.incident_channel = iso_ms(now), message_id, channel
        st.incident_kind, st.incident_count = kind, count
        st.notified, st.attempts, st.next_attempt_at = False, 0, None
        log.info(json.dumps({"event": "watchdog_incident", "kind": kind, "message_id": message_id, "channel": channel}))

    async def _notify(self, now: datetime) -> None:
        st = self.state
        if st.last_notice_at:
            last = parse_rfc3339(st.last_notice_at)
            if last and now - last < timedelta(seconds=self.settings.watchdog_cooldown_s):
                return  # the cooldown holds the notice back; the incident stays open and is announced after it
        if st.next_attempt_at:
            retry = parse_rfc3339(st.next_attempt_at)
            if retry and now < retry:
                return
        error = await self._post()
        st.attempts += 1
        if error is None:
            st.notified, st.last_notice_ok, st.last_error = True, True, None
            st.last_notice_at, st.notices_sent = iso_ms(now), st.notices_sent + 1
            log.info(json.dumps({"event": "watchdog_notice", "message_id": st.incident_message}))
            return
        st.last_notice_ok, st.last_error = False, error
        if st.attempts >= MAX_ATTEMPTS:
            st.notified, st.last_notice_at = True, iso_ms(now)  # given up: the cooldown now keeps it from hammering
            log.warning(json.dumps({"event": "watchdog_notice_failed", "error": error, "attempts": st.attempts}))
        else:
            st.next_attempt_at = iso_ms(now + timedelta(seconds=RETRY_STEP_S * st.attempts))

    # ---- the notice --------------------------------------------------------------------------------------------

    def format(self) -> str:
        fmt = self.settings.watchdog_format
        if fmt == "auto":
            return "text" if is_ntfy_style(self.settings.watchdog_url or "") else "json"
        return fmt

    def message_text(self) -> str:
        st, w = self.state, self.settings.watchdog_s
        if st.incident_kind == "write_failed":
            more = f" ({st.incident_count} messages in all; see GET /v1/status)" if st.incident_count > 1 else ""
            failed = self.relay.store.get_failed(st.incident_message or "")
            if failed is not None and failed.error in POLICY_REFUSALS:
                return (f"Earshot: the relay accepted message {st.incident_message} (channel {st.incident_channel}) "
                        f"into a coalescing window, but when the window closed its configuration no longer allowed "
                        f"it ({failed.error}){more}. It was never stored and will not play.")
            return (f"Earshot: the relay accepted message {st.incident_message} (channel {st.incident_channel}) into a "
                    f"coalescing window but could not write it to the bucket after several tries{more}. It was never "
                    "stored and will not play. Check the bucket, then send it again.")
        more = f" (nor on {st.incident_count - 1} later one(s))" if st.incident_count > 1 else ""
        return (f"Earshot: no device reported on message {st.incident_message} (channel {st.incident_channel}) within "
                f"{w} s of it being stored{more}. The phone may be off, offline or not listening, or the beacon URL is "
                "wrong. The relay watches sends, so it said nothing before this one.")

    async def _post(self) -> str | None:
        """POST the notice. None on success, else a short error with no URL in it."""
        st, url = self.state, self.settings.watchdog_url or ""
        text = self.message_text()
        headers = {"User-Agent": f"earshot-relay-watchdog/{__version__}"}
        if self.format() == "text":
            title = ("Earshot: message not written" if st.incident_kind == "write_failed"
                     else "Earshot: no playback report")
            headers.update({"Content-Type": "text/plain; charset=utf-8", "Title": title,
                            "Tags": "warning", "Priority": "4"})
            kwargs: dict = {"content": text.encode("utf-8")}
        else:
            kwargs = {"json": {
                "event": ("earshot.watchdog.write_failed" if st.incident_kind == "write_failed"
                          else "earshot.watchdog.no_playback_report"),
                "text": text, "message_id": st.incident_message,
                "channel": st.incident_channel, "incident_since": st.incident_since,
                "after_s": self.settings.watchdog_s, "relay_version": __version__,
            }}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(POST_TIMEOUT_S), transport=self._transport,
                                         follow_redirects=False) as client:
                resp = await client.post(url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            return f"could not reach the watchdog URL ({type(exc).__name__})"
        if not 200 <= resp.status_code < 300:
            return f"the watchdog URL answered HTTP {resp.status_code}"
        return None

    # ---- status and loop --------------------------------------------------------------------------------------

    def status(self) -> dict:
        st = self.state
        if st.incident_since is None:
            phase = "idle"
        else:
            phase = "notified" if st.notified else "incident"
        return {
            "enabled": True,
            "after_s": self.settings.watchdog_s,
            "cooldown_s": self.settings.watchdog_cooldown_s,
            "format": self.format(),
            "target_host": self.settings.watchdog_host,
            "state": phase,
            "incident_since": st.incident_since,
            "incident_message": st.incident_message,
            "incident_kind": st.incident_kind if st.incident_since else None,
            "last_notice_at": st.last_notice_at,
            "last_notice_ok": st.last_notice_ok,
            "last_error": st.last_error,
            "notices_sent": st.notices_sent,
            "note": NOTE,
        }

    def next_check_at(self) -> datetime | None:
        """When a check could next change something, or None: nothing is due until a send or a beacon wakes it."""
        st, store, now = self.state, self.relay.store, self.relay.clock()
        cooldown_end = None
        if st.last_notice_at:
            cooldown_end = (parse_rfc3339(st.last_notice_at) or now) + timedelta(seconds=self.settings.watchdog_cooldown_s)
        if st.incident_since and not st.notified:  # a notice waits for its retry slot or the cooldown
            due = max(now, cooldown_end or now)
            if st.next_attempt_at:
                due = max(due, parse_rfc3339(st.next_attempt_at) or now)
            return due
        if st.incident_since:  # announced: only a beacon (it may clear it) or a send changes anything
            return None
        due = []
        first = store.undetected_message(self.covered_until, iso_ms(now))
        if first is not None:  # the oldest message still without a report falls overdue then
            due.append((parse_rfc3339(first.created_at) or now) + timedelta(seconds=self.settings.watchdog_s))
        if store.unnotified_failed()[0] is not None:
            due.append(max(now, cooldown_end or now))
        return min(due) if due else None

    async def run(self) -> None:
        """Check, then sleep until the next deadline or until a send or a beacon arrives. No idle timer."""
        while True:
            try:
                version = self.relay.signal.version
                await self.check()
                due = self.next_check_at()
                timeout = None if due is None else max(0.01, (due - self.relay.clock()).total_seconds())
                await self.relay.signal.wait(timeout, since=version)
            except Exception as exc:  # noqa: BLE001 - the watchdog must never take the relay down
                log.warning(json.dumps({"event": "watchdog_failed", "error": type(exc).__name__}))
                await self.relay.signal.wait(30)
