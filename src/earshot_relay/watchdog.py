"""The delivery watchdog: one out-of-band notice when a send gets no playback report from the phone.

It watches sends, not idle phones. A stored key without its own `detected` beacon after
EARSHOT_RELAY_WATCHDOG_S seconds opens an incident. Unrelated beacons and other phases cannot clear it.
The relay POSTs one notice to EARSHOT_RELAY_WATCHDOG_URL outside the phone path. With no traffic it stays silent.

Rules, tested with a controllable clock:

- One notice per incident; it clears once every overdue send has its own detected receipt.
- EARSHOT_RELAY_WATCHDOG_COOLDOWN_S is a floor between notices, not a mute: an incident that opens inside the cooldown
  waits it out and is announced then, if it is still open.
- A failing webhook is retried twice more (30 s, then 60 s later), then given up on for that incident.
- The URL may hold a secret (an ntfy topic, a token). It is never logged and never shown, only its host is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from . import __version__
from .beacons import iso_ms, parse_rfc3339
from .store import MessageRow

log = logging.getLogger("earshot_relay")

MAX_ATTEMPTS = 3
RETRY_STEP_S = 30
POST_TIMEOUT_S = 10
META_KEY = "watchdog"
ALIVE_KEY = "watchdog_alive_at"  # refreshed about once a minute while the watchdog runs
ALIVE_EVERY_S = 60
STALE_AFTER_S = 600  # a saved state older than this is from before the watchdog was last off: start over
NOTE = ("Watches sends, not idle phones: the app sends no heartbeat, so a dead phone with no traffic raises nothing. "
        "A notice means no device reported on a message in time, not that the phone is off.")


def is_ntfy_style(url: str) -> bool:
    """ntfy.sh, or a host with an `ntfy` label (ntfy.example.com): a plain text POST is a publish there."""
    host = (urlsplit(url).hostname or "").lower()
    return host == "ntfy.sh" or "ntfy" in host.split(".")


@dataclass
class WatchdogState:
    enabled_since: str
    last_clear_at: str
    incident_since: str | None = None
    incident_message: str | None = None
    incident_channel: str | None = None
    notified: bool = False
    attempts: int = 0
    next_attempt_at: str | None = None
    last_notice_at: str | None = None
    last_notice_ok: bool | None = None
    last_error: str | None = None
    notices_sent: int = 0


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
                state = WatchdogState(**json.loads(saved))
            except (ValueError, TypeError):
                state = None
        self.state = state or WatchdogState(enabled_since=now, last_clear_at=now)

    # ---- one pass --------------------------------------------------------------------------------------------

    async def check(self) -> None:
        """Clear a finished incident, open a new one, and send the notice that is due. Cheap: three small queries."""
        st, store = self.state, self.relay.store
        before = asdict(st)
        now = self.relay.clock()
        late = store.undetected_message(st.enabled_since, iso_ms(now - timedelta(seconds=self.settings.watchdog_s)))
        if st.incident_since and late is None:
            log.info(json.dumps({"event": "watchdog_cleared", "notified": st.notified}))
            self._close_incident()
        if st.incident_since is None and late is not None:
            self._open_incident(late, now)
        elif st.incident_since and late is not None and st.incident_message != late.id:
            self._open_incident(late, now)
        if st.incident_since and not st.notified:
            await self._notify(now)
        if asdict(st) != before:
            store.set_meta(**{META_KEY: json.dumps(asdict(st))})
        if self._alive_at is None or (now - self._alive_at).total_seconds() >= ALIVE_EVERY_S:
            self._alive_at = now
            store.set_meta(**{ALIVE_KEY: iso_ms(now)})

    def _close_incident(self) -> None:
        st = self.state
        st.incident_since = st.incident_message = st.incident_channel = st.next_attempt_at = None
        st.notified, st.attempts = False, 0

    def _open_incident(self, message: MessageRow, now: datetime) -> None:
        st = self.state
        st.incident_since, st.incident_message, st.incident_channel = iso_ms(now), message.id, message.channel
        st.notified, st.attempts, st.next_attempt_at = False, 0, None
        log.info(json.dumps({"event": "watchdog_incident", "message_id": message.id, "channel": message.channel}))

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
        return (f"Earshot: no device reported on message {st.incident_message} (channel {st.incident_channel}) within "
                f"{w} s of it being stored. The phone may be off, offline or not listening, or the beacon URL is "
                "wrong. The relay watches sends, so it said nothing before this one.")

    async def _post(self) -> str | None:
        """POST the notice. None on success, else a short error with no URL in it."""
        st, url = self.state, self.settings.watchdog_url or ""
        text = self.message_text()
        headers = {"User-Agent": f"earshot-relay-watchdog/{__version__}"}
        if self.format() == "text":
            headers.update({"Content-Type": "text/plain; charset=utf-8", "Title": "Earshot: no playback report",
                            "Tags": "warning", "Priority": "4"})
            kwargs: dict = {"content": text.encode("utf-8")}
        else:
            kwargs = {"json": {
                "event": "earshot.watchdog.no_playback_report", "text": text, "message_id": st.incident_message,
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
            "last_notice_at": st.last_notice_at,
            "last_notice_ok": st.last_notice_ok,
            "last_error": st.last_error,
            "notices_sent": st.notices_sent,
            "note": NOTE,
        }

    def next_check_at(self) -> datetime | None:
        st, now = self.state, self.relay.clock()
        if st.incident_since:
            if st.notified:
                return None  # only new sends or matching receipts can change this incident
            due = now
            if st.next_attempt_at:
                due = max(due, parse_rfc3339(st.next_attempt_at) or now)
            if st.last_notice_at:
                due = max(due, (parse_rfc3339(st.last_notice_at) or now)
                          + timedelta(seconds=self.settings.watchdog_cooldown_s))
            return due
        first = self.relay.store.undetected_message(st.enabled_since, iso_ms(now))
        if first is None:
            return None
        return (parse_rfc3339(first.created_at) or now) + timedelta(seconds=self.settings.watchdog_s)

    async def run(self) -> None:
        """Wake on a send/receipt or the next outstanding deadline; no idle timer."""
        while True:
            try:
                await self.check()
                due = self.next_check_at()
                await self.relay.signal.wait(max(0.01, (due - self.relay.clock()).total_seconds()) if due else None)
            except Exception as exc:  # noqa: BLE001 - watchdog failures cannot take the API down
                log.warning(json.dumps({"event": "watchdog_failed", "error": type(exc).__name__}))
                await self.relay.signal.wait(30)
