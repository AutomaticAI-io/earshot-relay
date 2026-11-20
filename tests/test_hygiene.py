"""Text hygiene: the normalizer, coalesce-by-tag, cooldown, and how they behave across HTTP, compat, MCP and the CLI."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import ConfigError, load_settings
from earshot_relay.hygiene import normalize_text
from earshot_relay.keys import ApiError
from earshot_relay.service import FLUSH_RETRY_S, Relay
from earshot_relay.storage import S3Writer, StorageError
from earshot_relay.store import Store

from .conftest import BEACON_TOKEN, BUCKET, TOKEN, base_env
from .test_tts import StubTts, tts_env  # noqa: F401  (stub is the OpenAI-compatible TTS stand-in)

T0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """A relay clock tests move by hand: no sleeping through a coalescing window."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run(coro):
    return asyncio.run(coro)


def post(c, auth, **body):
    return c.post("/v1/messages", headers=auth, json=body)


def objects(s3_admin, channel: str) -> dict[str, bytes]:
    out = {}
    for item in s3_admin.list_objects_v2(Bucket=BUCKET, Prefix=f"{channel}/").get("Contents", []):
        out[item["Key"]] = s3_admin.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read()
    return out


@pytest.fixture
def chan():
    """A channel name no other test writes to, so object counts are exact."""
    return "h" + uuid.uuid4().hex[:12]


# ---- the normalizer (pure) ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    ("✅ Build **passed** \U0001F389 see https://example.com/a/b?x=1.", "Build passed see link."),
    ("Docs at www.example.com, then https://x.y/z!", "Docs at link, then link!"),
    ("# Deploy\n- item one\n- item *two*\n1. third\n```py\nprint(1)\n```\nDone `ok`.",
     "Deploy. item one. item two. third. code Done ok."),
    ("snake_case_name and _under_ and __bold__ and ~~gone~~", "snake_case_name and under and bold and gone"),
    ("[the docs](https://example.com/docs) and ![pic](a.png)", "the docs and pic"),
    ("1️⃣ one \U0001F468‍\U0001F469‍\U0001F467 family ❌ fail \U0001F1FA\U0001F1F8", "1 one family fail"),
    ("> quoted\n> text", "quoted text"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "a, b. 1, 2."),
    ("a   b\n\n\n   c\t\td", "a b c d"),
    ("Replace <token> with the real one", "Replace <token> with the real one"),
    ("2 * 3 * 4 and a_b_c stay", "2 * 3 * 4 and a_b_c stay"),
    ("Copyright © 2026 and ™ stay", "Copyright © 2026 and ™ stay"),
    ("ends here.", "ends here."),
    ("¿Qué tal? café 日本語", "¿Qué tal? café 日本語"),
])
def test_normalizer_vectors(raw, want):
    assert normalize_text(raw).text == want


def test_normalizer_reports_change_and_leaves_clean_text_alone():
    clean = normalize_text("Build finished. All tests passed.")
    assert clean.text == "Build finished. All tests passed." and clean.changed is False and clean.truncated is False
    assert normalize_text("Build \U0001F389 finished").changed is True
    assert normalize_text("only \U0001F389\U0001F389").text == "only"
    assert normalize_text("\U0001F389\U0001F389").text == ""


def test_normalizer_max_words_cuts_at_a_sentence_end():
    text = "one two three. four five six seven eight nine ten."
    assert normalize_text(text, max_words=5).text == "one two three."
    assert normalize_text(text, max_words=5).truncated is True
    assert normalize_text(text, max_words=20).truncated is False
    # no sentence end inside the limit: cut at the limit, trailing period, no dangling comma
    long = normalize_text("alpha beta, gamma delta epsilon zeta eta", max_words=4)
    assert long.text == "alpha beta, gamma delta." and long.truncated is True
    assert normalize_text("one two three four, five six", max_words=4).text == "one two three four."


def test_normalizer_is_linear_on_hostile_input():
    start = time.perf_counter()
    for payload in ("```" * 20000, "`a" * 20000, "**a " * 15000, "*a " * 20000, "[a](b " * 10000, "_a " * 20000,
                    "~~a " * 15000, "![" * 20000, "|-" * 30000, "- " * 30000, "http://" * 8000):
        normalize_text(payload)
    assert time.perf_counter() - start < 3


def test_normalizer_refuses_over_the_phone_limit():
    with pytest.raises(ApiError) as exc:
        normalize_text("a" * 70000)
    assert exc.value.code == "text_too_long"


# ---- normalize over HTTP ---------------------------------------------------------------------------------------------


def test_normalize_per_request_and_report(make_relay, auth, s3_admin, chan):
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan)
    with TestClient(create_app(relay)) as c:
        r = post(c, auth, text="✅ **Deploy** done: https://ci.example.com/run/9", normalize=True)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["normalized"] is True and body["truncated"] is False and body["status"] == "stored"
        assert objects(s3_admin, chan)[body["key"]] == b"Deploy done: link"
        plain = post(c, auth, text="Nothing to clean here.", normalize=True).json()
        assert plain["normalized"] is False
        off = post(c, auth, text="✅ keep me").json()  # default is off
        assert off["normalized"] is False and objects(s3_admin, chan)[off["key"]] == "✅ keep me".encode()


def test_normalize_env_default_and_request_override(make_relay, auth, s3_admin, chan):
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, EARSHOT_RELAY_NORMALIZE="basic",
                       EARSHOT_RELAY_MAX_WORDS="6")
    with TestClient(create_app(relay)) as c:
        a = post(c, auth, text="# Title\nOne two three. Four five six seven eight.").json()
        assert a["normalized"] is True and a["truncated"] is True
        assert objects(s3_admin, chan)[a["key"]] == b"Title. One two three."
        b = post(c, auth, text="✅ raw please", normalize=False).json()
        assert b["normalized"] is False and objects(s3_admin, chan)[b["key"]] == "✅ raw please".encode()
        status = c.get("/v1/status", headers=auth).json()["hygiene"]
        assert status == {"normalize": "basic", "max_words": 6, "coalescing_pending": 0, "coalescing_failed": 0}


def test_normalize_emoji_only_is_refused_and_flag_must_be_bool(client, auth):
    r = post(client, auth, text="\U0001F389\U0001F389", normalize=True)
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_text"
    r = post(client, auth, text="hi", normalize="yes")
    assert r.status_code == 422 and r.json()["error"]["field"] == "normalize"


def test_normalize_runs_before_the_length_limit(make_relay, auth, s3_admin, chan):
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan)
    with TestClient(create_app(relay)) as c:
        text = "word " * 600 + "https://example.com/" + "x" * 3000  # 3000 + 3020 units raw; short once cleaned
        assert post(c, auth, text=text).status_code == 422
        assert post(c, auth, text=text, normalize=True).status_code == 202


def test_normalize_never_touches_audio(client, auth, s3_admin):
    payload = b"ID3" + b"\x00" * 500
    r = client.post("/v1/messages", headers=auth, files={"file": ("a.mp3", payload, "audio/mpeg")},
                    data={"normalize": "true"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_field"
    ok = client.post("/v1/messages", headers=auth, files={"file": ("a.mp3", payload, "audio/mpeg")})
    body = ok.json()
    assert body["normalized"] is False and s3_admin.get_object(Bucket=BUCKET, Key=body["key"])["Body"].read() == payload


def test_normalize_applies_to_server_speech_text(make_relay, auth):
    stub = StubTts()
    try:
        relay = make_relay(**tts_env(stub.url, EARSHOT_RELAY_TTS_LEADIN_MS="0", EARSHOT_RELAY_NORMALIZE="basic"))
        with TestClient(create_app(relay)) as c:
            r = post(c, auth, text="✅ **Done** https://x.y/z", speech="server")
            assert r.status_code == 202, r.text
        assert stub.requests[0]["json"]["input"] == "Done link"
    finally:
        stub.close()


# ---- coalesce by tag -------------------------------------------------------------------------------------------------


def coalescing_relay(make_relay, chan, clock, **extra):
    return make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, clock=clock, **extra)


def test_burst_of_five_writes_exactly_one_object_with_the_latest_text(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))  # no lifespan: the test drives the flush
    ids = set()
    for n in range(5):
        r = post(c, auth, text=f"progress {n} of 5", tag="build", coalesce_s=30)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "coalescing" and body["key"] is None and body["tag"] == "build"
        assert body["write_at"] == "2026-11-20T12:00:30.000Z"  # the window is fixed from the first message
        ids.add(body["id"])
        clock.advance(4)
    assert len(ids) == 1, "every message in the window answers with the one id the object will have"
    msg_id = ids.pop()
    assert objects(s3_admin, chan) == {}
    assert run(relay.flush_due()) == 0  # t=20: window still open
    clock.advance(11)  # t=31
    assert run(relay.flush_due()) == 1
    written = objects(s3_admin, chan)
    assert list(written.values()) == [b"progress 4 of 5"]
    assert list(written)[0] == f"{chan}/{msg_id[4:]}.say"
    receipt = c.get(f"/v1/messages/{msg_id}", headers=auth).json()
    assert receipt["status"] == "stored" and receipt["key"] == list(written)[0]
    assert relay.store.pending_count() == 0
    assert run(relay.flush_due()) == 0  # nothing left


def test_a_window_that_closed_starts_a_new_one(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    first = post(c, auth, text="one", tag="t", coalesce_s=10).json()
    clock.advance(11)
    run(relay.flush_due())
    second = post(c, auth, text="two", tag="t", coalesce_s=10).json()
    assert second["id"] != first["id"] and second["status"] == "coalescing"
    clock.advance(11)
    run(relay.flush_due())
    assert sorted(objects(s3_admin, chan).values()) == [b"one", b"two"]


def test_tags_are_per_channel_and_per_tag(make_relay, auth, s3_admin, chan):
    clock = Clock()
    other = chan + "x"
    relay = make_relay(EARSHOT_RELAY_CHANNELS=f"{chan},{other}", EARSHOT_RELAY_DEFAULT_CHANNEL=chan, clock=clock)
    c = TestClient(create_app(relay))
    ids = {post(c, auth, text=t, tag=tag, channel=ch, coalesce_s=5).json()["id"]
           for t, tag, ch in [("a", "x", chan), ("b", "y", chan), ("c", "x", other)]}
    assert len(ids) == 3
    clock.advance(6)
    assert run(relay.flush_due()) == 3
    assert sorted(objects(s3_admin, chan).values()) == [b"a", b"b"] and list(objects(s3_admin, other).values()) == [b"c"]


def test_the_latest_message_wins_including_its_speech(make_relay, auth, s3_admin, chan):
    stub = StubTts()
    try:
        clock = Clock()
        extra = tts_env(stub.url, EARSHOT_RELAY_TTS_LEADIN_MS="0")
        extra.pop("EARSHOT_RELAY_CHANNELS"), extra.pop("EARSHOT_RELAY_DEFAULT_CHANNEL")
        relay = coalescing_relay(make_relay, chan, clock, **extra)
        c = TestClient(create_app(relay))
        for n in range(4):  # server speech: TTS must run once, for the last text only
            r = post(c, auth, text=f"update {n}", tag="s", coalesce_s=5, speech="server")
            assert r.status_code == 202 and r.json()["speech"] == "server"
        assert stub.requests == []
        clock.advance(6)
        assert run(relay.flush_due()) == 1
        assert [q["json"]["input"] for q in stub.requests] == ["update 3"]
        assert [k for k in objects(s3_admin, chan) if k.endswith(".wav")]
    finally:
        stub.close()


def test_pending_receipt_and_long_poll_follow_the_write(make_relay, auth, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    msg = post(c, auth, text="hold", tag="r", coalesce_s=10).json()
    r = c.get(f"/v1/messages/{msg['id']}", headers=auth).json()
    assert r["status"] == "coalescing" and r["key"] is None and "nothing is in the bucket yet" in r["hint"]
    assert c.get("/v1/messages", headers=auth).json()["items"] == []  # the index lists written messages only
    assert c.get("/v1/messages/latest", headers=auth).status_code == 404

    async def scenario():
        waiter = asyncio.create_task(relay.wait_receipt(msg["id"], 1.0))
        await asyncio.sleep(0.1)
        clock.advance(11)
        await relay.flush_due()
        return await waiter

    assert run(scenario())["status"] == "stored"


def test_coalescing_counts_one_send_against_the_rate_limit(make_relay, auth, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock, EARSHOT_RELAY_RATE_LIMIT="2/min")
    c = TestClient(create_app(relay))
    assert all(post(c, auth, text=f"u{n}", tag="g", coalesce_s=30).status_code == 202 for n in range(8))
    assert post(c, auth, text="solo").status_code == 202  # the second of two in the window
    assert post(c, auth, text="solo2").status_code == 429  # and the third is over the limit
    # a rate-limited window opening is refused up front, before anything is held
    r = post(c, auth, text="x", tag="other", coalesce_s=30)
    assert r.status_code == 429 and relay.store.pending_count() == 1
    clock.advance(31)
    assert run(relay.flush_due()) == 1  # the flush is not counted again


def test_idempotency_with_coalescing(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))

    def send(text, key):
        return c.post("/v1/messages", headers={**auth, "Idempotency-Key": key},
                      json={"text": text, "tag": "i", "coalesce_s": 20})

    a = send("first", "key-a")
    b = send("second", "key-b")
    again = send("ignored", "key-a")  # a retry of the first request: no merge, the same answer
    assert a.json()["id"] == b.json()["id"] == again.json()["id"]
    assert again.headers["idempotent-replayed"] == "true" and again.json()["status"] == "coalescing"
    assert relay.store.get_pending(chan, "i").text == "second" and relay.store.get_pending(chan, "i").merged == 2
    clock.advance(21)
    run(relay.flush_due())
    assert list(objects(s3_admin, chan).values()) == [b"second"]
    for key in ("key-a", "key-b"):  # after the write, both keys still replay the one message
        r = send("zzz", key)
        assert r.headers["idempotent-replayed"] == "true"
        assert r.json()["id"] == a.json()["id"] and r.json()["status"] == "stored" and r.json()["key"]
    assert len(objects(s3_admin, chan)) == 1


def test_pending_survives_a_restart_and_overdue_ones_flush_at_startup(moto_endpoint, tmp_path, s3_admin, auth, chan):
    clock = Clock()
    env = base_env(moto_endpoint, str(tmp_path / "persist"), EARSHOT_RELAY_CHANNELS=chan,
                   EARSHOT_RELAY_DEFAULT_CHANNEL=chan)

    def boot() -> tuple[Relay, Store]:
        settings = load_settings(env, check_ffmpeg=False)
        store = Store(settings.db_path)
        return Relay(settings, store, S3Writer(settings), None, clock=clock), store

    relay, store = boot()
    c = TestClient(create_app(relay))
    short = post(c, auth, text="due soon", tag="a", coalesce_s=10).json()
    long = post(c, auth, text="due later", tag="b", coalesce_s=600).json()
    store.close()  # the relay stops here

    clock.advance(60)
    relay2, store2 = boot()
    try:
        assert store2.pending_count() == 2
        assert run(relay2.flush_due()) == 1  # what startup does: the overdue one is written, the other keeps waiting
        assert list(objects(s3_admin, chan).values()) == [b"due soon"]
        assert store2.get_pending_by_id(long["id"]).text == "due later"
        clock.advance(600)
        assert run(relay2.flush_due()) == 1
        assert sorted(objects(s3_admin, chan).values()) == [b"due later", b"due soon"]
        assert short["id"] != long["id"]
    finally:
        store2.close()


def test_text_is_gone_from_the_database_files_after_the_write(make_relay, auth, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    post(c, auth, text="zebra-first-text-9941", tag="p", coalesce_s=5)
    post(c, auth, text="zebra-latest-text-9941", tag="p", coalesce_s=5)
    db = Path(relay.settings.db_path)
    assert b"zebra-latest-text-9941" in db.read_bytes() + Path(str(db) + "-wal").read_bytes()  # held, as documented
    clock.advance(6)
    run(relay.flush_due())
    left = b"".join(p.read_bytes() for p in (db, Path(str(db) + "-wal")) if p.exists())
    assert b"zebra-" not in left


def test_failed_writes_retry_then_drop(make_relay, auth, s3_admin, chan, monkeypatch):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    msg = post(c, auth, text="flaky", tag="f", coalesce_s=5).json()
    real_put = relay.writer.put
    failing = {"on": True}

    def put(*args, **kwargs):
        if failing["on"]:
            raise StorageError("AccessDenied", "nope")
        return real_put(*args, **kwargs)

    monkeypatch.setattr(relay.writer, "put", put)
    clock.advance(6)
    assert run(relay.flush_due()) == 0
    row = relay.store.get_pending(chan, "f")
    assert row.attempts == 1 and row.due_at == "2026-11-20T12:00:16.000Z"  # +10 s
    assert run(relay.flush_due()) == 0  # not due again yet: nothing is retried early
    assert relay.store.get_pending(chan, "f").attempts == 1
    failing["on"] = False
    clock.advance(10)
    assert run(relay.flush_due()) == 1
    assert list(objects(s3_admin, chan).values()) == [b"flaky"] and msg["id"]

    failing["on"] = True  # and a write that never works is dropped after the last retry
    doomed = post(c, auth, text="doomed", tag="d", coalesce_s=1).json()
    for delay in (2, *FLUSH_RETRY_S, 1):
        clock.advance(delay)
        run(relay.flush_due())
    assert relay.store.pending_count() == 0
    failed = c.get(f"/v1/messages/{doomed['id']}", headers=auth)  # not a silent 404: the receipt says it was lost
    assert failed.status_code == 200 and failed.json()["status"] == "write_failed"


# ---- cooldown ---------------------------------------------------------------------------------------------------------


def test_cooldown_drops_repeats_and_names_the_message_that_started_it(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    first = post(c, auth, text="disk is full", tag="disk", cooldown_s=300)
    assert first.status_code == 202 and first.json()["status"] == "stored" and first.json()["tag"] == "disk"
    clock.advance(60)
    repeat = post(c, auth, text="disk is still full", tag="disk", cooldown_s=300)
    assert repeat.status_code == 200, repeat.text
    body = repeat.json()
    assert body["status"] == "suppressed" and body["id"] == first.json()["id"] and body["key"] == first.json()["key"]
    assert body["cooldown_until"] == "2026-11-20T12:05:00.000Z"
    assert repeat.headers["location"] == f"/v1/messages/{first.json()['id']}"
    assert list(objects(s3_admin, chan).values()) == [b"disk is full"]
    assert post(c, auth, text="other tag", tag="net", cooldown_s=300).status_code == 202  # another tag is unaffected
    clock.advance(241)  # past the first message's cooldown
    again = post(c, auth, text="disk is full again", tag="disk", cooldown_s=300)
    assert again.status_code == 202 and again.json()["status"] == "stored"
    assert len(objects(s3_admin, chan)) == 3


def test_suppressed_sends_do_not_use_the_rate_limit(make_relay, auth, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock, EARSHOT_RELAY_RATE_LIMIT="2/min")
    c = TestClient(create_app(relay))
    assert post(c, auth, text="one", tag="q", cooldown_s=60).status_code == 202
    assert all(post(c, auth, text="rep", tag="q", cooldown_s=60).status_code == 200 for _ in range(10))
    assert post(c, auth, text="two").status_code == 202  # the second real send is still allowed


def test_cooldown_after_a_coalesced_write_and_against_a_pending_one(make_relay, auth, s3_admin, chan):
    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    c = TestClient(create_app(relay))
    held = post(c, auth, text="a", tag="k", coalesce_s=30, cooldown_s=3600).json()
    assert held["status"] == "coalescing"
    update = post(c, auth, text="b", tag="k", coalesce_s=30, cooldown_s=3600).json()
    assert update["status"] == "coalescing" and update["id"] == held["id"]  # coalescing wins inside an open window
    only_cooldown = post(c, auth, text="c", tag="k", cooldown_s=3600)
    assert only_cooldown.status_code == 200 and only_cooldown.json()["status"] == "suppressed"
    assert only_cooldown.json()["id"] == held["id"] and only_cooldown.json()["key"] is None
    clock.advance(31)
    run(relay.flush_due())
    after = post(c, auth, text="d", tag="k", coalesce_s=30, cooldown_s=3600)
    assert after.status_code == 200 and after.json()["status"] == "suppressed" and after.json()["key"]
    assert list(objects(s3_admin, chan).values()) == [b"b"]


def test_a_tag_alone_only_labels_the_message(client, auth):
    a = post(client, auth, text="one", tag="label").json()
    b = post(client, auth, text="two", tag="label").json()
    assert a["status"] == b["status"] == "stored" and a["id"] != b["id"] and a["tag"] == "label"


@pytest.mark.parametrize("body, code, field", [
    ({"coalesce_s": 5}, "tag_required", "tag"),
    ({"cooldown_s": 5}, "tag_required", "tag"),
    ({"tag": "bad tag!", "coalesce_s": 5}, "invalid_tag", "tag"),
    ({"tag": "", "coalesce_s": 5}, "invalid_tag", "tag"),
    ({"tag": 5, "coalesce_s": 5}, "invalid_tag", "tag"),
    ({"tag": "t" * 65, "coalesce_s": 5}, "invalid_tag", "tag"),
    ({"tag": "t", "coalesce_s": -1}, "invalid_field", "coalesce_s"),
    ({"tag": "t", "coalesce_s": "5"}, "invalid_field", "coalesce_s"),
    ({"tag": "t", "coalesce_s": True}, "invalid_field", "coalesce_s"),
    ({"tag": "t", "coalesce_s": 3601}, "invalid_field", "coalesce_s"),
    ({"tag": "t", "cooldown_s": 86401}, "invalid_field", "cooldown_s"),
])
def test_hygiene_field_validation(client, auth, body, code, field):
    r = post(client, auth, text="hi", **body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code and r.json()["error"]["field"] == field


def test_zero_seconds_means_off(client, auth):
    r = post(client, auth, text="hi", tag="z", coalesce_s=0, cooldown_s=0)
    assert r.status_code == 202 and r.json()["status"] == "stored"


def test_background_loop_flushes_a_closed_window(make_relay, auth, s3_admin, chan):
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan)
    with TestClient(create_app(relay)) as c:  # lifespan on: the flush loop runs on the real clock
        msg = post(c, auth, text="loop me", tag="loop", coalesce_s=0.2).json()
        deadline = time.time() + 4
        while time.time() < deadline and not objects(s3_admin, chan):
            time.sleep(0.1)
        assert list(objects(s3_admin, chan).values()) == [b"loop me"]
        assert c.get(f"/v1/messages/{msg['id']}", headers=auth).json()["status"] == "stored"


# ---- compat senders get the same hygiene ---------------------------------------------------------------------------


@pytest.fixture
def compat(make_relay, chan):
    clock = Clock()
    relay = make_relay(EARSHOT_RELAY_CHANNELS=chan, EARSHOT_RELAY_DEFAULT_CHANNEL=chan, EARSHOT_RELAY_COMPAT="all",
                       EARSHOT_RELAY_NORMALIZE="basic", EARSHOT_RELAY_COMPAT_NTFY_TOPICS=f"phone={chan}", clock=clock)
    return relay, clock, TestClient(create_app(relay))


def test_compat_senders_are_normalized_by_the_server_default(compat, auth, s3_admin, chan):
    relay, _, c = compat
    r = c.post("/compat/ntfy/phone", headers=auth, content="✅ **Washer** done, see https://x.y/z".encode())
    assert r.status_code == 200, r.text
    assert r.headers["x-earshot-normalized"] == "true"
    assert objects(s3_admin, chan)[f"{chan}/{r.headers['x-earshot-message-id'][4:]}.say"] == b"Washer done, see link"
    off = c.post("/compat/ntfy/phone", headers={**auth, "X-Earshot-Normalize": "false"}, content="✅ raw".encode())
    assert "x-earshot-normalized" not in off.headers
    assert objects(s3_admin, chan)[f"{chan}/{off.headers['x-earshot-message-id'][4:]}.say"] == "✅ raw".encode()
    bad = c.post("/compat/ntfy/phone", headers={**auth, "X-Earshot-Normalize": "maybe"}, content=b"x")
    assert bad.status_code == 400


def test_compat_coalesce_and_cooldown_by_header_or_query(compat, auth, s3_admin, chan):
    relay, clock, c = compat
    ids = set()
    for n in range(3):
        r = c.post("/compat/ntfy/phone", headers={**auth, "X-Earshot-Tag": "backup", "X-Earshot-Coalesce-S": "30"},
                   content=f"backup step {n}".encode())
        assert r.status_code == 200 and r.headers["x-earshot-status"] == "coalescing", r.text
        ids.add(r.headers["x-earshot-message-id"])
    gotify = c.post("/compat/gotify/message?earshot_tag=backup&earshot_coalesce_s=30", headers={"X-Gotify-Key": TOKEN},
                    json={"message": "backup step 3", "title": ""})
    assert gotify.status_code == 200 and gotify.headers["x-earshot-status"] == "coalescing"
    ids.add(gotify.headers["x-earshot-message-id"])
    pushover = c.post("/compat/pushover/1/messages.json?earshot_tag=backup&earshot_coalesce_s=30",
                      data={"token": TOKEN, "user": "u", "message": "backup step 4"})
    join = c.get("/compat/join/_ah/api/messaging/v1/sendPush", params={
        "apikey": TOKEN, "text": "backup step 5", "earshot_tag": "backup", "earshot_coalesce_s": "30"})
    assert pushover.json()["status"] == 1 and join.json()["success"] is True
    assert "x-earshot-ignored" not in join.headers  # the earshot_ parameters are ours, not ignored fields
    ids.add(pushover.headers["x-earshot-message-id"])
    ids.add(join.headers["x-earshot-message-id"])
    assert len(ids) == 1
    clock.advance(31)
    run(relay.flush_due())
    assert list(objects(s3_admin, chan).values()) == [b"backup step 5"]
    again = c.post("/compat/ntfy/phone?earshot_tag=backup&earshot_cooldown_s=600", headers=auth, content=b"again")
    assert again.status_code == 200 and again.headers["x-earshot-status"] == "suppressed"
    assert len(objects(s3_admin, chan)) == 1


def test_compat_audio_ignores_hygiene_options_by_name(compat, auth, s3_admin, chan):
    _, _, c = compat
    r = c.post("/compat/ntfy/phone?earshot_tag=x&earshot_coalesce_s=30", headers={**auth, "Filename": "ding.wav"},
               content=b"RIFF" + b"\x00" * 400)
    assert r.status_code == 200 and "x-earshot-status" not in r.headers
    assert "earshot_tag" in r.headers["x-earshot-ignored"]


# ---- MCP and CLI --------------------------------------------------------------------------------------------------------


def test_cli_say_carries_the_hygiene_flags(live_relay, monkeypatch, capsys, s3_admin):
    from earshot_relay.cli import main

    monkeypatch.setenv("EARSHOT_API_URL", live_relay)
    monkeypatch.setenv("EARSHOT_API_TOKEN", TOKEN)
    assert main(["say", "--json", "--normalize", "✅", "**bold** \U0001F389", "text"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["normalized"] is True
    assert s3_admin.get_object(Bucket=BUCKET, Key=sent["key"])["Body"].read() == b"bold text"
    assert main(["say", "--json", "--tag", "cli-t", "--coalesce-s", "300", "held"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert held["status"] == "coalescing" and held["tag"] == "cli-t"
    assert main(["say", "--tag", "cli-t", "--cooldown-s", "300", "again"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == held["id"] and "suppressed" in out.err
    assert main(["say", "--coalesce-s", "5", "no tag"]) == 1
    assert "tag_required" in capsys.readouterr().err
    assert main(["status"]) == 0
    assert "1 message(s) held for coalescing" in capsys.readouterr().out


def test_mcp_say_exposes_and_forwards_the_options(make_relay, chan):
    from .test_mcp import call, rpc

    clock = Clock()
    relay = coalescing_relay(make_relay, chan, clock)
    with TestClient(create_app(relay), base_url="http://relay.example.com") as c:
        tools = {t["name"]: t for t in rpc(c, "tools/list")["result"]["tools"]}
        assert {"normalize", "tag", "coalesce_s", "cooldown_s"} <= set(tools["say"]["inputSchema"]["properties"])
        assert "coalescing" in tools["say"]["description"]
        sent = call(c, "say", {"text": "\u2705 deploying", "normalize": True, "tag": "mcp",
                               "coalesce_s": 60})["structuredContent"]
        assert sent["status"] == "coalescing" and sent["tag"] == "mcp" and sent["normalized"] is True
        assert "nothing is in the bucket yet" in sent["next"]
        again = call(c, "say", {"text": "x", "tag": "mcp", "cooldown_s": 60})["structuredContent"]
        assert again["status"] == "suppressed" and again["id"] == sent["id"] and "cooldown" in again["next"]


# ---- configuration ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra, expect", [
    ({"EARSHOT_RELAY_NORMALIZE": "loud"}, "EARSHOT_RELAY_NORMALIZE must be one of"),
    ({"EARSHOT_RELAY_MAX_WORDS": "0"}, "EARSHOT_RELAY_MAX_WORDS must be between"),
    ({"EARSHOT_RELAY_MAX_WORDS": "lots"}, "EARSHOT_RELAY_MAX_WORDS must be a whole number"),
])
def test_hygiene_config_errors(moto_endpoint, tmp_path, extra, expect):
    with pytest.raises(ConfigError, match=expect):
        load_settings(base_env(moto_endpoint, str(tmp_path), **extra), check_ffmpeg=False)


def test_hygiene_defaults_are_off(moto_endpoint, tmp_path):
    s = load_settings(base_env(moto_endpoint, str(tmp_path)), check_ffmpeg=False)
    assert s.normalize == "off" and s.max_words is None and s.watchdog_s is None
    assert BEACON_TOKEN not in repr(s)
