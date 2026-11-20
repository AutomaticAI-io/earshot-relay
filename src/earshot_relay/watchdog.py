"""The delivery watchdog: one out-of-band notice when a send gets no playback report from the phone.

It watches SENDS, not phones. When a message has been stored for EARSHOT_RELAY_WATCHDOG_S seconds and no device has
reported anything about it (no `detected`, `started`, `finished`, `failed` or `missed` beacon), that is an incident:
the relay POSTs one notice to EARSHOT_RELAY_WATCHDOG_URL, outside the phone path, so a dead phone does not have to tell
you it is dead. With no traffic it stays silent, and an idle dead phone is invisible to it: the app sends no heartbeat.

Rules, all of them tested with a controllable clock:

- One notice per incident. An incident opens at the first undetected message and clears when any playback beacon
  arrives (any device, any message), which also starts the watch over from that moment.
- EARSHOT_RELAY_WATCHDOG_COOLDOWN_S is a floor between notices, not a mute: an incident that opens inside the cooldown
  waits it out and is announced then, if it is still open.
- A coalesced message the relay accepted but could never write (the bucket was down through every retry) is its own
  kind of incident: one notice, then closed. It never clears on playback, because the message will not play.
- A failing webhook is retried twice more (30 s, then 60 s later), then given up on for that incident.
- The URL may hold a secret (an ntfy topic, a token). It is never logged and never shown, only its host is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import anyio
import httpx

from . import __version__
from .beacons import iso_ms, parse_rfc3339

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
    incident_kind: str = "silent"  # silent: a stored message nobody reported on. write_failed: a held message never written
    incident_count: int = 1
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
        last_ok = store.last_playback_beacon_at()
        if last_ok and last_ok > st.last_clear_at:
            if st.incident_since and st.incident_kind == "silent":
                log.info(json.dumps({"event": "watchdog_cleared", "notified": st.notified}))
                self._close_incident()
            st.last_clear_at = last_ok
        if st.incident_since is None:
            late = store.undetected_message(st.last_clear_at, iso_ms(now - timedelta(seconds=self.settings.watchdog_s)))
            if late is not None:
                self._open_incident(late.id, late.channel, now)
            else:
                lost, count = store.unnotified_failed()
                if lost is not None:
                    self._open_incident(lost.id, lost.channel, now, kind="write_failed", count=count)
        if st.incident_since and not st.notified:
            await self._notify(now)
        if st.incident_since and st.notified and st.incident_kind == "write_failed":
            store.mark_failed_notified()  # told once (or given up on); it never clears by playback
            self._close_incident()
        if asdict(st) != before:
            store.set_meta(**{META_KEY: json.dumps(asdict(st))})
        if self._alive_at is None or (now - self._alive_at).total_seconds() >= ALIVE_EVERY_S:
            self._alive_at = now
            store.set_meta(**{ALIVE_KEY: iso_ms(now)})

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
            return (f"Earshot: the relay accepted message {st.incident_message} (channel {st.incident_channel}) into a "
                    f"coalescing window but could not write it to the bucket after several tries{more}. It was never "
                    "stored and will not play. Check the bucket, then send it again.")
        return (f"Earshot: no device reported on message {st.incident_message} (channel {st.incident_channel}) within "
                f"{w} s of it being stored. The phone may be off, offline or not listening, or the beacon URL is "
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

    @property
    def interval_s(self) -> float:
        return min(5.0, max(1.0, self.settings.watchdog_s / 10))

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 - the watchdog must never take the relay down
                log.warning(json.dumps({"event": "watchdog_failed", "error": type(exc).__name__}))
            await anyio.sleep(self.interval_s)
