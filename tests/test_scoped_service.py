"""Scoped relay behavior against a disposable control database and loopback Moto S3."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.auth import authenticate
from earshot_relay.beacons import iso_ms
from earshot_relay.config import load_settings
from earshot_relay.control import OWNER, Control, Principal
from earshot_relay.keys import ApiError
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer, StorageError
from earshot_relay.store import Store
from .conftest import TOKEN, base_env


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def rig(tmp_path, moto_endpoint):
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    settings = load_settings(base_env(
        moto_endpoint, str(tmp_path / "data"), EARSHOT_RELAY_CHANNELS="phone,other,alarm",
        EARSHOT_RELAY_CONTROL_DB=str(directory / "control.db"),
        EARSHOT_RELAY_COMPAT="ntfy,pushover", EARSHOT_RELAY_COMPAT_TOKEN="s" * 40,
        EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN="p" * 30,
    ), check_ffmpeg=False)
    contract = {"version": "control.v1", "workspace": {"id": "ws", "endpoint": settings.s3_endpoint,
                "bucket": settings.bucket, "region": settings.region},
                "channels": [{"id": c, "workspace_id": "ws", "name": c} for c in ("phone", "other", "alarm")],
                "agents": [{"id": "agent1", "workspace_id": "ws"}], "devices": []}
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    clock = Clock()
    store = Store(settings.db_path)
    relay = Relay(settings, store, S3Writer(settings), clock=clock)
    issued = []

    def issue(channels=("phone",), *, receipts=True, alarm=False, expires_at=None):
        target = directory / f"token{len(issued)}"
        pid = control.issue(TOKEN, "agent1", list(channels), alarm=alarm, receipts=receipts,
                            expires_at=expires_at, output=target)
        token = target.read_text().strip()
        principal = control.authenticate(token)
        assert principal and principal.id == pid
        issued.append(principal)
        return principal

    yield SimpleNamespace(relay=relay, control=control, clock=clock, issue=issue, settings=settings)
    store.close()


def assert_status(status, call):
    with pytest.raises(ApiError) as exc:
        call()
    assert exc.value.status == status
    assert exc.value.message in ("request not permitted", "channel is reserved") or status in (404, 422)


async def send_idem(relay, principal, header, channel="phone", **kw):
    async with relay.check_replay(header, channel, principal=principal) as idem:
        if idem.replay is not None:
            return idem.replay
        return await relay.send_text(text="hello", channel=channel, idem_key=idem.digest, principal=principal, **kw)


def test_allowed_denied_alarm_replies_audio_and_compat(rig):
    relay = rig.relay
    p = rig.issue()
    assert run(relay.send_text(text="yes", principal=p))["channel"] == "phone"
    for channel in ("other", "alarm", "replies"):
        assert_status(403, lambda: run(relay.send_text(text="no", channel=channel, principal=p)))
    alarm = rig.issue(("alarm",), alarm=True)
    assert run(relay.send_audio(fileobj=io.BytesIO(b"audio"), size=5, ext="mp3", channel="alarm", principal=alarm))["channel"] == "alarm"
    assert_status(403, lambda: run(relay.send_text(text="no", channel="replies")))
    compat = authenticate(relay, "s" * 40, compat=True)
    pushover = authenticate(relay, "p" * 30, compat=True, pushover=True)
    assert compat == Principal("compat", "compat_token")
    assert pushover == Principal("compat", "pushover_token") and compat != pushover
    assert run(relay.send_text(text="compat", channel="other", principal=compat))["channel"] == "other"
    assert_status(403, lambda: relay.get_message("latest", principal=compat))
    assert_status(403, lambda: relay.get_message("latest", principal=Principal("unknown", "x")))


def test_receipts_filtered_before_latest_pagination_since(rig):
    relay = rig.relay
    a, b = rig.issue(), rig.issue()
    first = run(relay.send_text(text="a1", principal=a))
    foreign = run(relay.send_text(text="b", principal=b))
    owner = run(relay.send_text(text="owner"))
    last = run(relay.send_text(text="a2", principal=a))
    run(relay.send_text(text="b2", principal=b))
    assert relay.get_message("latest", principal=a)["id"] == last["id"]
    assert relay.get_message(first["id"], principal=a)["id"] == first["id"]
    for mid in (foreign["id"], owner["id"]):
        assert_status(404, lambda: relay.get_message(mid, principal=a))
        assert_status(404, lambda: relay.recent_messages(channel=None, limit=10, since=mid, principal=a))
    page = relay.recent_messages(channel=None, limit=1, since=None, principal=a)
    assert [i["id"] for i in page["items"]] == [last["id"]]
    page = relay.recent_messages(channel=None, limit=1, since=first["id"], principal=a)
    assert [i["id"] for i in page["items"]] == [last["id"]]
    assert len(relay.recent_messages(channel=None, limit=20, since=None)["items"]) == 5
    no_receipts = rig.issue(receipts=False)
    assert_status(403, lambda: relay.get_message("latest", principal=no_receipts))
    assert_status(403, lambda: relay.recent_messages(channel=None, limit=20, since=None, principal=no_receipts))


def test_namespaces_replay_alias_suppression_and_provenance(rig):
    relay = rig.relay
    a, b = rig.issue(), rig.issue()
    first = run(send_idem(relay, a, "same", tag="tag", coalesce_window_s=60))
    merged = run(send_idem(relay, a, "merge", tag="tag", coalesce_window_s=60))
    second = run(send_idem(relay, b, "same", tag="tag", coalesce_window_s=60))
    assert first["id"] == merged["id"] != second["id"]
    assert run(send_idem(relay, a, "merge"))["id"] == first["id"]
    assert relay.get_message(first["id"], principal=a)["status"] == "coalescing"
    assert_status(404, lambda: relay.get_message(first["id"], principal=b))
    suppressed = run(send_idem(relay, a, "drop", tag="tag", cooldown_s=5))
    assert suppressed["status"] == "suppressed" and suppressed["id"] == first["id"]
    assert run(send_idem(relay, a, "drop")) == suppressed
    rig.clock.advance(61)
    assert run(relay.flush_due()) == 2
    for p, result in ((a, first), (b, second)):
        row = relay.store.get_message(result["id"])
        assert (row.principal_kind, row.principal_id, row.workspace_id) == ("scoped", p.id, "ws")
    with sqlite3.connect(rig.settings.db_path) as db:
        for table in ("idem_aliases", "suppressed_idem"):
            rows = db.execute(f"SELECT principal_kind, principal_id, workspace_id FROM {table}").fetchall()
            assert rows and all(kind == "scoped" and ident in (a.id, b.id) and ws == "ws" for kind, ident, ws in rows)
    assert run(send_idem(relay, a, "merge"))["id"] == first["id"]


def test_replay_reauthorizes_both_destinations(rig):
    relay = rig.relay
    p = rig.issue(("phone", "other"))
    first = run(send_idem(relay, p, "k", channel="phone"))
    assert run(send_idem(relay, p, "k", channel="other"))["id"] == first["id"]
    with rig.control.connect() as db:
        db.execute("DELETE FROM grants WHERE principal_id=? AND channel_id='phone'", (p.id,))
    assert_status(403, lambda: run(send_idem(relay, p, "k", channel="other")))
    assert_status(403, lambda: run(send_idem(relay, p, "k", channel="phone")))


def test_legacy_reserved_reply_rows_are_not_exposed(rig):
    relay = rig.relay
    normal = run(relay.send_text(text="visible"))
    relay.store.add_message(id="msg_legacy_reply", created_at="9999-01-01T00:00:00Z", channel="replies",
                            type="text", speech="device", key="replies/old.say", bytes=1, text_chars=1)
    assert relay.get_message("latest")["id"] == normal["id"]
    assert_status(404, lambda: relay.get_message("msg_legacy_reply"))
    assert_status(404, lambda: relay.recent_messages(channel=None, limit=5, since="msg_legacy_reply"))
    assert [x["id"] for x in relay.recent_messages(channel=None, limit=1, since=None)["items"]] == [normal["id"]]
    assert_status(403, lambda: relay.recent_messages(channel="replies", limit=1, since=None))


def test_compat_tokens_have_separate_hygiene_and_idempotency(rig):
    relay = rig.relay
    first = Principal("compat", "compat_token")
    second = Principal("compat", "pushover_token")
    a = run(send_idem(relay, first, "key", tag="tag", coalesce_window_s=30))
    b = run(send_idem(relay, second, "key", tag="tag", coalesce_window_s=30))
    assert a["id"] != b["id"]
    assert run(send_idem(relay, first, "key"))["id"] == a["id"]
    for p in (first, second):
        assert_status(403, lambda: relay.recent_messages(channel=None, limit=10, since=None, principal=p))


def test_revoked_or_expired_held_messages_never_flush(rig):
    relay = rig.relay
    p = rig.issue()
    held = run(relay.send_text(text="pending secret", tag="x", coalesce_window_s=1, principal=p))
    rig.control.revoke(TOKEN, p.id)
    rig.clock.advance(2)
    assert run(relay.flush_due()) == 0
    row = relay.store.get_failed(held["id"])
    assert row and (row.principal_kind, row.principal_id, row.workspace_id) == ("scoped", p.id, "ws")
    assert relay.get_message(held["id"])["status"] == "write_failed"
    assert_status(403, lambda: relay.get_message(held["id"], principal=p))
    assert_status(403, lambda: run(relay.send_text(text="no", principal=p)))
    expired = rig.issue()
    with rig.control.connect() as db:
        db.execute("UPDATE principals SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expired.id,))
    assert_status(403, lambda: run(relay.send_text(text="no", principal=expired)))


def test_revoke_during_tts_blocks_put(rig):
    relay = rig.relay
    p = rig.issue()
    class TTS:
        async def render(self, text, voice):
            rig.control.revoke(TOKEN, p.id)
            return SimpleNamespace(data=b"audio", ext="mp3", content_type="audio/mpeg")
    relay.tts = TTS()
    calls = []
    relay.writer.put = lambda *a: calls.append(a)
    assert_status(403, lambda: run(relay.send_text(text="no", speech="server", principal=p)))
    assert calls == []


def test_revoke_serialized_with_inflight_put(rig):
    relay = rig.relay
    p = rig.issue()
    started, release, revoked = threading.Event(), threading.Event(), threading.Event()
    real_put = relay.writer.put
    calls = []
    def put(*args):
        calls.append(args[0])
        started.set()
        assert release.wait(5)
        real_put(*args)
    relay.writer.put = put
    errors = []
    def send():
        try:
            run(relay.send_text(text="in flight", principal=p))
        except Exception as exc:
            errors.append(exc)
    def revoke():
        try:
            rig.control.revoke(TOKEN, p.id)
            revoked.set()
        except Exception as exc:
            errors.append(exc)
    sender = threading.Thread(target=send)
    revoker = threading.Thread(target=revoke)
    sender.start()
    try:
        assert started.wait(5)
        revoker.start()
        assert not revoked.wait(0.2)
    finally:
        release.set()
        sender.join(5)
        if revoker.ident is not None:
            revoker.join(5)
    assert not sender.is_alive() and not revoker.is_alive() and not errors and revoked.is_set()
    assert len(calls) == 1
    assert_status(403, lambda: run(relay.send_audio(fileobj=io.BytesIO(b"x"), size=1, ext="mp3", principal=p)))
    assert len(calls) == 1


def test_longpoll_rechecks_revocation(rig):
    relay = rig.relay
    p = rig.issue()
    result = run(relay.send_text(text="hello", principal=p))
    async def probe():
        waiter = asyncio.create_task(relay.wait_message(result["id"], 3, principal=p))
        await asyncio.sleep(0.05)
        rig.control.revoke(TOKEN, p.id)
        relay.signal.notify()
        with pytest.raises(ApiError) as exc:
            await waiter
        assert exc.value.status == 403
    run(probe())


def test_failed_pending_retains_identity(rig):
    relay = rig.relay
    p, other = rig.issue(), rig.issue()
    result = run(relay.send_text(text="pending", tag="x", coalesce_window_s=1, principal=p))
    def fail(*args):
        raise StorageError("AccessDenied", "fixture")
    relay.writer.put = fail
    for seconds in (2, 10, 30, 60):
        rig.clock.advance(seconds)
        run(relay.flush_due())
    row = relay.store.get_failed(result["id"])
    assert row and row.principal_id == p.id and row.workspace_id == "ws"
    assert relay.get_message(result["id"], principal=p)["status"] == "write_failed"
    assert_status(404, lambda: relay.get_message(result["id"], principal=other))


def test_requested_and_original_channel_rechecked_for_suppressed_and_alias(rig):
    relay = rig.relay
    p = rig.issue(("phone", "other"))
    run(send_idem(relay, p, "held", tag="tag", coalesce_window_s=30))
    run(send_idem(relay, p, "dropped", tag="tag", cooldown_s=5))
    with rig.control.connect() as db:
        db.execute("DELETE FROM grants WHERE principal_id=? AND channel_id='phone'", (p.id,))
    for key in ("held", "dropped"):
        assert_status(403, lambda: run(send_idem(relay, p, key, channel="other")))


def test_latest_and_since_skip_removed_channels_before_limiting(rig):
    relay = rig.relay
    p = rig.issue(("phone", "other"))
    first = run(relay.send_text(text="one", channel="phone", principal=p))
    hidden = run(relay.send_text(text="two", channel="other", principal=p))
    with rig.control.connect() as db:
        db.execute("DELETE FROM grants WHERE principal_id=? AND channel_id='other'", (p.id,))
    assert relay.get_message("latest", principal=p)["id"] == first["id"]
    assert [x["id"] for x in relay.recent_messages(channel=None, limit=1, since=None, principal=p)["items"]] == [first["id"]]
    assert_status(403, lambda: relay.get_message(hidden["id"], principal=p))
    assert_status(403, lambda: relay.recent_messages(channel=None, limit=1, since=hidden["id"], principal=p))


def test_failed_flush_after_tts_revocation_preserves_provenance(rig):
    relay = rig.relay
    p = rig.issue()
    class TTS:
        async def render(self, text, voice):
            rig.control.revoke(TOKEN, p.id)
            return SimpleNamespace(data=b"audio", ext="mp3", content_type="audio/mpeg")
    relay.tts = TTS()
    result = run(relay.send_text(text="pending", tag="x", speech="server", coalesce_window_s=1, principal=p))
    calls = []
    relay.writer.put = lambda *a: calls.append(a)
    rig.clock.advance(2)
    assert run(relay.flush_due()) == 0 and not calls
    row = relay.store.get_failed(result["id"])
    assert row and row.principal_id == p.id and row.agent_id == "agent1"


def test_legacy_idempotency_rejects_scoped_and_hashes_do_not_collide(rig):
    relay = rig.relay
    p = rig.issue()
    async def unsafe():
        async with relay.idempotency("same", principal=p):
            pass
    assert_status(403, lambda: run(unsafe()))
    # A printable owner header equal to the scoped hash's JSON input is a distinct namespace.
    owner_header = json.dumps([p.kind, p.id, p.workspace_id, "k"], separators=(",", ":"))
    async def digests():
        async with relay.check_replay("k", "phone", principal=p) as scoped:
            async with relay.check_replay(owner_header, "phone") as owner:
                return scoped.digest, owner.digest
    scoped, owner = run(digests())
    assert scoped != owner


def test_control_unavailable_after_tts_is_safe_denial(rig):
    relay = rig.relay
    p = rig.issue()
    class TTS:
        async def render(self, text, voice):
            relay.control.connect = lambda: (_ for _ in ()).throw(OSError("secret-detail"))
            return SimpleNamespace(data=b"audio", ext="mp3", content_type="audio/mpeg")
    relay.tts = TTS()
    calls = []
    relay.writer.put = lambda *a: calls.append(a)
    assert_status(403, lambda: run(relay.send_text(text="no", speech="server", principal=p)))
    assert not calls


def test_store_idempotency_keys_are_scoped_even_with_identical_digest(rig):
    store = rig.relay.store
    a, b = rig.issue(), rig.issue()
    now = "2026-11-25T00:00:00Z"
    for i, p in enumerate((a, b)):
        store.add_message(id=f"msg_direct_{i}", created_at=now, channel="phone", type="text", speech="device",
                          key=f"phone/direct{i}.say", bytes=1, text_chars=1, idem_key="same-digest", principal=p)
        store.add_alias("same-digest", f"msg_direct_{i}", now, principal=p)
        store.save_suppressed("same-digest", now, {"id": f"msg_direct_{i}"}, principal=p)
    for i, p in enumerate((a, b)):
        assert store.get_by_idem("same-digest", now, principal=p).id == f"msg_direct_{i}"
        assert store.get_alias("same-digest", now, principal=p) == f"msg_direct_{i}"
        assert store.get_suppressed("same-digest", now, principal=p)["id"] == f"msg_direct_{i}"


def test_migrate_legacy_rows_owner_only(tmp_path, rig):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE messages(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,channel TEXT NOT NULL,type TEXT NOT NULL,
            speech TEXT,key TEXT UNIQUE NOT NULL,bytes INTEGER NOT NULL,text_chars INTEGER,idem_key TEXT,tag TEXT,
            normalized INTEGER NOT NULL DEFAULT 0,truncated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE pending(id TEXT PRIMARY KEY,channel TEXT NOT NULL,tag TEXT NOT NULL,created_at TEXT NOT NULL,
            due_at TEXT NOT NULL,text TEXT NOT NULL,speech TEXT NOT NULL,voice TEXT,normalized INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0,merged INTEGER NOT NULL DEFAULT 1,attempts INTEGER NOT NULL DEFAULT 0,
            compat INTEGER NOT NULL DEFAULT 0,window_end_at TEXT,UNIQUE(channel,tag));
        CREATE TABLE idem_aliases(idem_key TEXT PRIMARY KEY,message_id TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE suppressed_idem(idem_key TEXT PRIMARY KEY,created_at TEXT NOT NULL,result TEXT NOT NULL);
        CREATE UNIQUE INDEX messages_idem ON messages(idem_key);
        INSERT INTO idem_aliases VALUES('legacy','msg_old','2026-01-01T00:00:00Z');
        INSERT INTO suppressed_idem VALUES('legacy','2026-01-01T00:00:00Z','{"id":"msg_old"}');
        INSERT INTO messages VALUES('msg_old','2026-01-01T00:00:00Z','phone','text','device','phone/old.say',1,1,'legacy',NULL,0,0);
        INSERT INTO pending VALUES('msg_pending','phone','tag','2026-01-01T00:00:00Z','2099-01-01T00:00:00Z',
            'held','device',NULL,0,0,1,0,0,'2099-01-01T00:00:00Z');
        PRAGMA user_version=5;
        """)
    store = Store(str(path))
    try:
        relay = Relay(rig.settings, store, rig.relay.writer)
        p = rig.issue()
        old = "2026-01-01T00:00:00Z"
        assert store.get_by_idem("legacy", old).id == "msg_old"
        assert store.get_alias("legacy", old) == "msg_old"
        assert store.get_suppressed("legacy", old) == {"id": "msg_old"}
        assert store.get_by_idem("legacy", old, principal=p) is None
        assert store.get_alias("legacy", old, principal=p) is None
        assert store.get_suppressed("legacy", old, principal=p) is None
        store.add_message(id="msg_scoped", created_at=old, channel="phone", type="text", speech="device",
                          key="phone/scoped.say", bytes=1, text_chars=1, idem_key="legacy", principal=p)
        store.add_alias("legacy", "msg_scoped", old, principal=p)
        store.save_suppressed("legacy", old, {"id": "msg_scoped"}, principal=p)
        assert store.get_by_idem("legacy", old).id == "msg_old"
        assert store.get_alias("legacy", old) == "msg_old"
        assert store.get_suppressed("legacy", old) == {"id": "msg_old"}
        assert relay.get_message("msg_old")["id"] == "msg_old"
        assert relay.get_message("msg_pending")["id"] == "msg_pending"
        assert_status(404, lambda: relay.get_message("msg_old", principal=p))
        assert_status(404, lambda: relay.get_message("msg_pending", principal=p))
        held = run(relay.send_text(text="own", tag="tag", coalesce_window_s=60, principal=p))
        assert held["id"] != "msg_pending" and store.pending_count() == 2
    finally:
        store.close()


# This schema is the v5 layout from the pre-scoping relay, not a fresh v6 Store.
LEGACY_SCHEMA = """
CREATE TABLE messages(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,channel TEXT NOT NULL,type TEXT NOT NULL,
    speech TEXT,key TEXT UNIQUE NOT NULL,bytes INTEGER NOT NULL,text_chars INTEGER,idem_key TEXT,tag TEXT,
    normalized INTEGER NOT NULL DEFAULT 0,truncated INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX messages_idem ON messages(idem_key);
CREATE INDEX messages_created ON messages(created_at);
CREATE INDEX messages_tag ON messages(channel,tag,created_at);
CREATE TABLE beacons(id INTEGER PRIMARY KEY,key TEXT NOT NULL,channel TEXT NOT NULL,phase TEXT NOT NULL,
    device TEXT NOT NULL,app_version TEXT NOT NULL,at TEXT NOT NULL,last_modified TEXT,clock_skew_ms INTEGER,
    error TEXT,received_at TEXT NOT NULL,UNIQUE(key,device,phase,at));
CREATE INDEX beacons_key ON beacons(key);
CREATE INDEX beacons_received ON beacons(received_at);
CREATE TABLE pending(id TEXT PRIMARY KEY,channel TEXT NOT NULL,tag TEXT NOT NULL,created_at TEXT NOT NULL,
    due_at TEXT NOT NULL,text TEXT NOT NULL,speech TEXT NOT NULL,voice TEXT,normalized INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,merged INTEGER NOT NULL DEFAULT 1,attempts INTEGER NOT NULL DEFAULT 0,
    compat INTEGER NOT NULL DEFAULT 0,window_end_at TEXT,UNIQUE(channel,tag));
CREATE INDEX pending_due ON pending(due_at);
CREATE TABLE failed_pending(id TEXT PRIMARY KEY,channel TEXT NOT NULL,tag TEXT NOT NULL,created_at TEXT NOT NULL,
    failed_at TEXT NOT NULL,attempts INTEGER NOT NULL,error TEXT NOT NULL,notified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE idem_aliases(idem_key TEXT PRIMARY KEY,message_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE suppressed_idem(idem_key TEXT PRIMARY KEY,created_at TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
PRAGMA user_version=5;
"""


@pytest.mark.parametrize("kind", ["stored", "pending", "suppressed", "stale_alias"])
def test_compat_legacy_retry_fails_closed_after_real_migration(tmp_path, rig, kind, monkeypatch):
    path = tmp_path / "compat-legacy-v5.db"
    raw_key = "legacy-idempotency-fixture"
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    now = iso_ms(rig.clock())
    old_id = "msg_" + "1" * 32
    secret_key = "phone/" + "1" * 32 + ".say"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        if kind == "stored":
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (old_id, now, "phone", "text", "device", secret_key, 6, 6, digest, None, 0, 0))
        elif kind in ("pending", "stale_alias"):
            if kind == "pending":
                due = iso_ms(rig.clock() + timedelta(seconds=300))
                db.execute("INSERT INTO pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (old_id, "phone", "progress", now, due, "legacy secret", "device", None, 0, 0, 1, 0, 1, due))
            db.execute("INSERT INTO idem_aliases VALUES(?,?,?)", (digest, old_id, now))
        else:
            db.execute("INSERT INTO suppressed_idem VALUES(?,?,?)", (digest, now, json.dumps({
                "id": old_id, "channel": "phone", "key": secret_key, "status": "suppressed"})))
    store = Store(str(path))
    relay = Relay(rig.settings, store, rig.relay.writer, clock=rig.clock)
    calls = []
    monkeypatch.setattr(relay.writer, "put", lambda *args: calls.append(args))
    principal = Principal("compat", "compat_token")
    try:
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 6
            before_counts = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                             for t in ("messages", "pending", "idem_aliases", "suppressed_idem")}
            table = {"stored": "messages", "pending": "pending", "suppressed": "suppressed_idem",
                     "stale_alias": "idem_aliases"}[kind]
            assert db.execute(f"SELECT principal_kind,principal_id,workspace_id FROM {table}").fetchone() == (
                "owner", "owner", "")
        before = store.pending_count()
        with TestClient(create_app(relay, mcp=False)) as client:
            headers = {"Authorization": "Bearer " + "s" * 40, "Idempotency-Key": raw_key,
                       "X-Earshot-Tag": "progress", "X-Earshot-Coalesce-Window-S": "300"}
            response = client.post("/compat/ntfy/phone", content="retry", headers=headers)
            assert response.status_code == 409
            for hidden in (old_id, secret_key, raw_key, "legacy secret"):
                assert hidden not in response.text and hidden not in str(response.headers)
            plain = client.post("/compat/ntfy/phone", content="retry", headers={
                "Authorization": headers["Authorization"], "Idempotency-Key": raw_key})
            assert plain.status_code == 409 and old_id not in plain.text and secret_key not in plain.text
            pushover = client.post("/compat/pushover/1/messages.json", data={
                "token": "p" * 30, "user": "u" * 30, "message": "retry"},
                headers={"Idempotency-Key": raw_key})
            assert pushover.status_code == 409 and old_id not in pushover.text and secret_key not in pushover.text
            assert not calls and store.pending_count() == before
            with sqlite3.connect(path) as db:
                assert before_counts == {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                                         for t in before_counts}
            if kind == "stored":
                assert relay.get_message(old_id)["id"] == old_id
            elif kind == "pending":
                row = store.get_pending_by_id(old_id)
                assert row and row.principal_kind == "owner" and row.merged == 1 and row.text == "legacy secret"
            fresh = client.post("/compat/ntfy/phone", content="fresh", headers={**headers,
                                "Idempotency-Key": "fresh-compat-key", "X-Earshot-Tag": "new-tag"})
            assert fresh.status_code == 200
            assert store.pending_count() == before + 1 and not calls
            replay = client.post("/compat/ntfy/phone", content="again", headers={**headers,
                                 "Idempotency-Key": "fresh-compat-key", "X-Earshot-Tag": "new-tag"})
            assert replay.status_code == 200 and replay.headers.get("idempotent-replayed") == "true"
            assert store.pending_count() == before + 1
            own_pending = store.get_pending_by_id(fresh.headers["x-earshot-message-id"])
            assert own_pending and own_pending.principal_id == principal.id
            new_send = client.post("/compat/ntfy/phone", content="fresh", headers={
                "Authorization": headers["Authorization"], "Idempotency-Key": "fresh-direct"})
            assert new_send.status_code == 200 and len(calls) == 1
            new_replay = client.post("/compat/ntfy/phone", content="different", headers={
                "Authorization": headers["Authorization"], "Idempotency-Key": "fresh-direct"})
            assert new_replay.status_code == 200 and new_replay.headers.get("idempotent-replayed") == "true"
            assert len(calls) == 1
    finally:
        store.close()


def test_owner_collision_preserves_existing_compat_and_scoped_replays(rig):
    relay = rig.relay
    compat, scoped = Principal("compat", "compat_token"), rig.issue()
    compat_result = run(send_idem(relay, compat, "same"))
    scoped_result = run(send_idem(relay, scoped, "same"))
    owner_result = run(send_idem(relay, OWNER, "same"))
    assert len({compat_result["id"], scoped_result["id"], owner_result["id"]}) == 3
    assert run(send_idem(relay, compat, "same"))["id"] == compat_result["id"]
    assert run(send_idem(relay, scoped, "same"))["id"] == scoped_result["id"]
    assert run(send_idem(relay, OWNER, "same"))["id"] == owner_result["id"]
    assert run(send_idem(relay, Principal("compat", "pushover_token"), "other"))["status"] == "stored"
    assert run(send_idem(relay, scoped, "owner-only"))["status"] == "stored"
    run(send_idem(relay, OWNER, "different"))
    with pytest.raises(ApiError) as exc:
        run(send_idem(relay, Principal("compat", "pushover_token"), "different"))
    assert exc.value.status == 409 and exc.value.message == "request could not be completed"


def test_legacy_retry_retained_until_pending_or_record_pruned(tmp_path, rig, monkeypatch):
    path = tmp_path / "old-compat.db"
    old = iso_ms(rig.clock() - timedelta(hours=25))
    due = iso_ms(rig.clock() + timedelta(hours=1))
    raw = "very-old-key"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    old_id = "msg_" + "2" * 32
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            old_id, "phone", "progress", old, due, "legacy secret", "device", None, 0, 0, 1, 0, 1, due))
        db.execute("INSERT INTO idem_aliases VALUES(?,?,?)", (digest, old_id, old))
    store = Store(str(path))
    relay = Relay(rig.settings, store, rig.relay.writer, clock=rig.clock)
    monkeypatch.setattr(relay.writer, "put", lambda *args: None)
    principal = Principal("compat", "compat_token")
    try:
        store.prune(1, now=rig.clock() + timedelta(days=1))
        with pytest.raises(ApiError) as exc:
            run(send_idem(relay, principal, raw, tag="progress", coalesce_window_s=60))
        assert exc.value.status == 409 and store.pending_count() == 1
        store.delete_pending(old_id)
        with pytest.raises(ApiError) as exc:
            run(send_idem(relay, principal, raw))
        assert exc.value.status == 409  # an owner alias is still indistinguishable from legacy
        store.prune(1, now=rig.clock() + timedelta(days=1))
        fresh = run(send_idem(relay, principal, raw))
        assert fresh["status"] == "stored"
    finally:
        store.close()


def test_scoped_receipt_hint_independent_of_other_callers_and_heartbeats(rig, monkeypatch):
    relay = rig.relay
    principal, other = rig.issue(), rig.issue()
    own = run(relay.send_text(text="own", principal=principal))
    foreign = run(relay.send_text(text="other", principal=other))
    owner = run(relay.send_text(text="owner"))
    assert relay.get_message(owner["id"])["hint"] is not None
    before = relay.get_message(own["id"], principal=principal)
    assert before["hint"] is None
    assert run(relay.wait_message(own["id"], 0, principal=principal))["hint"] is None
    assert relay.recent_messages(channel=None, limit=10, since=None, principal=principal)["items"][0]["hint"] is None
    assert "hint" not in run(relay.send_text(text="another", principal=principal))
    now = iso_ms(rig.clock())
    relay.ingest_beacon({"key": foreign["key"], "channel": "phone", "phase": "detected", "device": "foreign",
                         "app_version": "1", "at": now, "last_modified": now})
    relay.ingest_beacon({"channel": "phone", "phase": "heartbeat", "device": "foreign",
                         "app_version": "1", "at": now})
    assert relay.get_message(owner["id"])["hint"] is None
    assert relay.get_message(own["id"], principal=principal) == before
    rig.clock.advance((relay.settings.receipt_wait_min + 1) * 60)
    with TestClient(create_app(relay, mcp=False)) as client:
        token = (rig.control.path.parent / "token0").read_text().strip()
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get(f"/v1/messages/{own['id']}", headers=auth).json()["hint"] is None
        assert client.get(f"/v1/messages/{own['id']}?wait=1", headers=auth).json()["hint"] is None
        assert all(item["hint"] is None for item in client.get("/v1/messages", headers=auth).json()["items"])
    monkeypatch.setattr(relay.store, "any_beacon", lambda: (_ for _ in ()).throw(AssertionError("global beacon lookup")))
    receipt = relay.get_message(own["id"], principal=principal)
    assert receipt["status"] == "unconfirmed" and receipt["hint"] is None
    assert run(relay.wait_message(own["id"], 0, principal=principal))["hint"] is None
    assert all(row["hint"] is None for row in relay.recent_messages(
        channel=None, limit=10, since=None, principal=principal)["items"])
    relay.ingest_beacon({"key": own["key"], "channel": "phone", "phase": "finished", "device": "own",
                         "app_version": "1", "at": now, "last_modified": now})
    result = relay.get_message(own["id"], principal=principal)
    assert result["status"] == "finished" and result["hint"] is None and result["devices"][0]["device"] == "own"
