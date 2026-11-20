"""Second review round: the normalizer's fence and joiner handling, the alarm budget for compat senders, a held message
whose write failed, the send-only compat token and the ElevenLabs voice path."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app, create_compat_app
from earshot_relay.config import ConfigError, load_settings
from earshot_relay.hygiene import normalize_text
from earshot_relay.service import FLUSH_RETRY_S
from earshot_relay.storage import StorageError
from earshot_relay.tts import TtsClient, TtsSettings

from .conftest import TOKEN, base_env
from .test_compat import BEARER, COMPAT_ENV, known_ids, message_key, new_ids
from .test_hygiene import Clock, post, run
from .test_watchdog import HOOK, Hook, beacon, chan, check, setup  # noqa: F401

# ---- normalizer -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    # an inline triple-backtick span is inline code, and the lines after it survive
    ("ran ```ls``` fine\nnext line here", "ran ls fine next line here"),
    ("see ```code``` now\nline", "see code now line"),
    ("```code```\nafter", "code after"),
    # a stray run with no closer leaves the whole message alone
    ("Build ``` failed\nin step 3. Ok", "Build ``` failed in step 3. Ok"),
    ("```\nnever closed\nstill here", "``` never closed still here"),
    # a real fence is still one "code"; the closer may be longer, and tilde fences work
    ("Stack:\n```\nTraceback...\nValueError\n```\nDone", "Stack: code Done"),
    ("a\n```py\nx = 1\n````\nb", "a code b"),
    ("a\n~~~\nx\n~~~\nb", "a code b"),
    # a shorter line is not a closer
    ("a\n````\nx\n```\ny\n````\nb", "a code b"),
    # a dunder is a name, a lone word between double underscores is still bold
    ("snake_case and __init__ method", "snake_case and __init__ method"),
    ("call __post_init__ then __bold__ text", "call __post_init__ then bold text"),
    ("__Important__ and **loud**", "Important and loud"),
    # the zero-width joiner stays between letters, goes with an emoji sequence
    ("ന്‍ x क्‍ष", "ന്‍ x क्‍ष"),
    ("Hi‌there", "Hi‌there"),
    ("\U0001F469‍\U0001F4BB dev ok", "dev ok"),
])
def test_normalizer_review_vectors(raw, want):
    assert normalize_text(raw).text == want


@pytest.mark.parametrize("body", [
    "```a\n" * 13000,
    "```\n" * 16000,
    "````\n```\n" * 8000,
    "".join("`" * k + "x\n" for k in range(3, 300)),
    "~~~a\n```b\n" * 6000,
])
def test_fence_scan_stays_fast_on_hostile_input(body):
    start = time.perf_counter()
    normalize_text(body[:65000])
    assert time.perf_counter() - start < 1.0


# ---- alarm budget -----------------------------------------------------------------------------------------------


def test_compat_top_priority_over_the_alarm_budget_is_stored_normally_not_refused(compat_relay):
    relay = compat_relay(EARSHOT_RELAY_COMPAT_ALARM="1", EARSHOT_RELAY_ALARM_RATE_LIMIT="2/hour")
    with TestClient(create_app(relay), base_url="http://relay.test") as main, \
            TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        seen = []
        for i in range(4):
            before = known_ids(main)
            r = root.post("/phone", content=f"down {i}", headers={**BEARER, "Priority": "urgent"})
            assert r.status_code == 200, (i, r.text)
            (mid,) = new_ids(main, before)
            seen.append((message_key(main, BEARER, mid)["channel"], r.headers.get("x-earshot-promoted"),
                         r.headers.get("x-earshot-downgraded")))
        assert seen == [("alarm", "alarm", None), ("alarm", "alarm", None), ("phone", None, "alarm"),
                        ("phone", None, "alarm")]
        # the native API still refuses an alarm send over the budget: that caller chose the channel
        r = main.post("/v1/messages", headers=BEARER, json={"text": "x", "channel": "alarm"})
        assert r.status_code == 429


@pytest.fixture
def compat_relay(make_relay):
    def factory(**extra):
        env = {**COMPAT_ENV, **extra}
        return make_relay(**{k: v for k, v in env.items() if v is not None})
    return factory


# ---- send-only compat token ---------------------------------------------------------------------------------------

SEND = "s" * 40


def test_send_only_token_works_on_every_compat_route_and_nowhere_else(compat_relay):
    relay = compat_relay(EARSHOT_RELAY_COMPAT_TOKEN=SEND)
    with TestClient(create_app(relay), base_url="http://relay.test") as main, \
            TestClient(create_compat_app(relay), base_url="http://relay.test") as root:
        auth = {"Authorization": f"Bearer {SEND}"}
        assert main.post("/compat/ntfy/phone", content="a", headers=auth).status_code == 200
        assert main.get("/compat/ntfy/phone/publish", params={"message": "a", "auth": _b64(f"Bearer {SEND}")}
                        ).status_code == 200
        assert root.post("/message", json={"message": "a"}, params={"token": SEND}).status_code == 200
        assert root.post("/message", json={"message": "a"}, headers={"X-Gotify-Key": SEND}).status_code == 200
        assert main.post("/compat/pushover/1/messages.json", data={"token": SEND, "user": "u" * 30, "message": "a"}
                         ).status_code == 200
        assert main.post("/compat/join/_ah/api/messaging/v1/sendPush", data={"apikey": SEND, "text": "a"}
                         ).status_code == 200
        # it opens nothing else: not the native API, the doctor, the MCP endpoint or a receipt
        assert main.get("/v1/status", headers=auth).status_code == 401
        assert main.post("/v1/doctor", headers=auth).status_code == 401
        assert main.post("/v1/messages", headers=auth, json={"text": "x"}).status_code == 401
        assert main.get("/v1/messages/latest", headers=auth).status_code == 401
        assert main.post("/mcp", headers=auth, json={}).status_code == 401
        # the main token still works everywhere, and the status shows the token is set but never its value
        c = main.get("/v1/status", headers=BEARER).json()["compat"]
        assert c["send_token"] is True and SEND not in json.dumps(c)


def _b64(value: str) -> str:
    import base64
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


@pytest.mark.parametrize("extra, needle", [
    ({"EARSHOT_RELAY_COMPAT_TOKEN": "short"}, "at least 32"),
    ({"EARSHOT_RELAY_COMPAT_TOKEN": TOKEN}, "must differ"),
    ({"EARSHOT_RELAY_COMPAT_TOKEN": SEND}, "names no protocol"),
])
def test_send_only_token_config_errors(moto_endpoint, tmp_path, extra, needle):
    env = base_env(moto_endpoint, str(tmp_path / "d"), **extra)
    if needle != "names no protocol":
        env["EARSHOT_RELAY_COMPAT"] = "ntfy"
    with pytest.raises(ConfigError) as err:
        load_settings(env, check_ffmpeg=False)
    assert needle in str(err.value) and extra["EARSHOT_RELAY_COMPAT_TOKEN"] not in str(err.value).replace(
        "EARSHOT_RELAY_COMPAT_TOKEN", "")


# ---- a held message that could not be written ---------------------------------------------------------------------


def _fail_every_write(relay, monkeypatch):
    def put(*args, **kwargs):
        raise StorageError("AccessDenied", "nope")
    monkeypatch.setattr(relay.writer, "put", put)


def _exhaust(relay, clock, first_delay):
    for delay in (first_delay, *FLUSH_RETRY_S, 1):
        clock.advance(delay)
        run(relay.flush_due())


def test_a_held_message_that_cannot_be_written_reads_write_failed_not_404(make_relay, auth, chan, monkeypatch):  # noqa: F811
    clock = Clock()
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, clock=clock)
    c = TestClient(create_app(relay))
    doomed = post(c, auth, text="SECRET-DOOMED-TEXT", tag="d", coalesce_s=1).json()
    _fail_every_write(relay, monkeypatch)
    _exhaust(relay, clock, 2)
    assert relay.store.pending_count() == 0
    r = c.get(f"/v1/messages/{doomed['id']}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "write_failed" and body["key"] is None and body["channel"] == chan
    assert "Nothing is stored" in body["hint"] and "SECRET-DOOMED-TEXT" not in r.text
    # a long-poll is final at once, and the status page counts it
    t0 = time.monotonic()
    waited = c.get(f"/v1/messages/{doomed['id']}", headers=auth, params={"wait": 30}).json()
    assert waited["status"] == "write_failed" and time.monotonic() - t0 < 5
    assert c.get("/v1/status", headers=auth).json()["hygiene"]["coalescing_failed"] == 1
    # the text is gone from the database file and its write-ahead log
    for name in ("relay.db", "relay.db-wal"):
        path = Path(relay.settings.data_dir) / name
        assert not path.exists() or b"SECRET-DOOMED-TEXT" not in path.read_bytes()
    # retention prunes the record
    relay.store.prune(1, now=clock() + timedelta(days=3))
    assert c.get(f"/v1/messages/{doomed['id']}", headers=auth).status_code == 404


def test_a_dropped_coalesce_is_a_watchdog_incident_that_playback_does_not_clear(setup, auth, monkeypatch):  # noqa: F811
    relay, clock, hook, c = setup()
    first = post(c, auth, text="before").json()
    doomed = post(c, auth, text="door open", tag="door", coalesce_s=1).json()
    _fail_every_write(relay, monkeypatch)
    _exhaust(relay, clock, 2)
    check(relay)
    assert len(hook.requests) == 1
    body = json.loads(hook.requests[0].content)
    assert body["event"] == "earshot.watchdog.write_failed" and body["message_id"] == doomed["id"]
    assert "was never stored" in body["text"] and "heard" not in body["text"]
    s = relay.watchdog.status()
    assert s["state"] == "idle" and s["notices_sent"] == 1  # one notice, told once, closed
    check(relay)
    clock.advance(500)
    check(relay)
    assert len(hook.requests) == 1  # never repeated
    assert first["id"]


# ---- ElevenLabs voice path ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("voice", ["..", ".", "a.b", "../v1"])
def test_elevenlabs_voice_stays_one_path_segment(voice):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"\xff\xfb" * 2000)

    settings = TtsSettings(provider="elevenlabs", url="https://api.elevenlabs.io", model="m", voice=None, api_key="k",
                           response_format="mp3", timeout_s=5, leadin_ms=0)
    client = TtsClient(settings, 1_000_000, transport=httpx.MockTransport(handler))
    run(client.synthesize("hi", voice))
    assert seen[0].startswith("https://api.elevenlabs.io/v1/text-to-speech/")
    path = seen[0].split("?")[0][len("https://api.elevenlabs.io"):]
    assert path.count("/") == 3 and "/." not in path

