"""SQLite index in the data dir (WAL). Holds message metadata and beacons: never message text of a sent message, never
tokens. The one exception is a message held for coalescing (see `pending`): its text sits here until the window
closes and the object is written, then the row is deleted (secure_delete is on, so the freed pages are zeroed)."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .beacons import PLAYBACK_PHASES, iso_ms
from .control import OWNER, Principal


def provenance(principal: Principal) -> tuple[str, str, str]:
    return principal.kind, principal.id, principal.workspace_id or ""


def principal_for(row) -> Principal:
    return Principal(row.principal_kind, row.principal_id, row.workspace_id or None, row.agent_id or None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  type TEXT NOT NULL,
  speech TEXT,
  key TEXT UNIQUE NOT NULL,
  bytes INTEGER NOT NULL,
  text_chars INTEGER,
  idem_key TEXT,
  tag TEXT,
  normalized INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  principal_kind TEXT NOT NULL DEFAULT 'owner',
  principal_id TEXT NOT NULL DEFAULT 'owner',
  workspace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS beacons(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  channel TEXT NOT NULL,
  phase TEXT NOT NULL,
  device TEXT NOT NULL,
  app_version TEXT NOT NULL,
  at TEXT NOT NULL,
  last_modified TEXT,
  clock_skew_ms INTEGER,
  error TEXT,
  received_at TEXT NOT NULL,
  UNIQUE(key, device, phase, at)
);
CREATE INDEX IF NOT EXISTS beacons_key ON beacons(key);
CREATE INDEX IF NOT EXISTS beacons_received ON beacons(received_at);
CREATE INDEX IF NOT EXISTS messages_created ON messages(created_at);
CREATE TABLE IF NOT EXISTS pending(
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  tag TEXT NOT NULL,
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  text TEXT NOT NULL,
  speech TEXT NOT NULL,
  voice TEXT,
  normalized INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  merged INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0,
  compat INTEGER NOT NULL DEFAULT 0,
  window_end_at TEXT,
  principal_kind TEXT NOT NULL DEFAULT 'owner',
  principal_id TEXT NOT NULL DEFAULT 'owner',
  workspace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  UNIQUE(principal_kind, principal_id, workspace_id, channel, tag)
);
CREATE INDEX IF NOT EXISTS pending_due ON pending(due_at);
CREATE TABLE IF NOT EXISTS failed_pending(
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  tag TEXT NOT NULL,
  created_at TEXT NOT NULL,
  failed_at TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  error TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0,
  principal_kind TEXT NOT NULL DEFAULT 'owner',
  principal_id TEXT NOT NULL DEFAULT 'owner',
  workspace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS idem_aliases(
  idem_key TEXT NOT NULL,
  message_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  principal_kind TEXT NOT NULL DEFAULT 'owner',
  principal_id TEXT NOT NULL DEFAULT 'owner',
  workspace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(idem_key, principal_kind, principal_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS suppressed_idem(
  idem_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  result TEXT NOT NULL,
  principal_kind TEXT NOT NULL DEFAULT 'owner',
  principal_id TEXT NOT NULL DEFAULT 'owner',
  workspace_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(idem_key, principal_kind, principal_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""
MESSAGE_COLUMNS = {"idem_key": "TEXT", "tag": "TEXT", "normalized": "INTEGER NOT NULL DEFAULT 0",
                   "truncated": "INTEGER NOT NULL DEFAULT 0"}
TAG_INDEX = "CREATE INDEX IF NOT EXISTS messages_tag ON messages(channel, tag, created_at)"
# idem_key holds sha256(Idempotency-Key header), never the raw header. Added in schema version 2.
IDEM_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS messages_idem ON messages(principal_kind, principal_id, workspace_id, idem_key)"
SCHEMA_VERSION = 6  # 6: principal/workspace provenance and scoped pending uniqueness


@dataclass(frozen=True)
class MessageRow:
    rowid: int
    id: str
    created_at: str
    channel: str
    type: str
    speech: str | None
    key: str
    bytes: int
    text_chars: int | None
    tag: str | None = None
    normalized: bool = False
    truncated: bool = False
    principal_kind: str = "owner"
    principal_id: str = "owner"
    workspace_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class PendingRow:
    """A text message held for coalescing: written when `due_at` passes, with the latest text sent under its tag."""

    id: str
    channel: str
    tag: str
    created_at: str
    due_at: str
    text: str
    speech: str
    voice: str | None
    normalized: bool
    truncated: bool
    merged: int
    attempts: int
    compat: bool = False
    window_end_at: str | None = None
    principal_kind: str = "owner"
    principal_id: str = "owner"
    workspace_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class FailedRow:
    """A coalesced message whose write failed every retry. The text is gone; only this record is kept."""

    id: str
    channel: str
    tag: str
    created_at: str
    failed_at: str
    attempts: int
    error: str
    principal_kind: str = "owner"
    principal_id: str = "owner"
    workspace_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class BeaconRow:
    key: str
    channel: str
    phase: str
    device: str
    app_version: str
    at: str
    last_modified: str | None
    clock_skew_ms: int | None
    error: str | None
    received_at: str


class TooManyDevices(Exception):
    """A beacon from yet another device for a key that already has the most devices the relay accepts."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.executescript(SCHEMA)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(messages)")}
            for name, decl in MESSAGE_COLUMNS.items():  # a version 1 or 2 database
                if name not in columns:
                    self._db.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")
            # A version 3 or 4 database: a held row of unknown origin is treated as a compat send (the stricter alarm
            # check at flush), and its window ends at its due time.
            pending_columns = {r["name"] for r in self._db.execute("PRAGMA table_info(pending)")}
            for name, decl in {"compat": "INTEGER NOT NULL DEFAULT 1", "window_end_at": "TEXT"}.items():
                if name not in pending_columns:
                    self._db.execute(f"ALTER TABLE pending ADD COLUMN {name} {decl}")
            self._db.execute("UPDATE pending SET window_end_at=due_at WHERE window_end_at IS NULL")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                provenance_columns = {"principal_kind": "TEXT NOT NULL DEFAULT 'owner'",
                                      "principal_id": "TEXT NOT NULL DEFAULT 'owner'",
                                      "workspace_id": "TEXT NOT NULL DEFAULT ''",
                                      "agent_id": "TEXT NOT NULL DEFAULT ''"}
                for table in ("messages", "pending", "failed_pending", "idem_aliases", "suppressed_idem"):
                    columns = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                    for name, decl in provenance_columns.items():
                        if name not in columns:
                            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                unique_indexes = [r for r in self._db.execute("PRAGMA index_list(pending)") if r["unique"]]
                legacy_unique = any([c["name"] for c in self._db.execute(f"PRAGMA index_info('{r['name']}')")]
                                    == ["channel", "tag"] for r in unique_indexes)
                if legacy_unique:
                    pending_schema = SCHEMA.split("CREATE TABLE IF NOT EXISTS pending(", 1)[1].split(";", 1)[0]
                    self._db.execute("CREATE TABLE pending_v6(" + pending_schema)
                    cols = ("id,channel,tag,created_at,due_at,text,speech,voice,normalized,truncated,merged,"
                            "attempts,compat,window_end_at,principal_kind,principal_id,workspace_id,agent_id")
                    self._db.execute(f"INSERT INTO pending_v6({cols}) SELECT {cols} FROM pending")
                    self._db.execute("DROP TABLE pending")
                    self._db.execute("ALTER TABLE pending_v6 RENAME TO pending")
                    self._db.execute("CREATE INDEX pending_due ON pending(due_at)")
                for table, payload in (("idem_aliases", "message_id"), ("suppressed_idem", "result")):
                    primary = [r["name"] for r in sorted(self._db.execute(f"PRAGMA table_info({table})"),
                                                         key=lambda r: r["pk"]) if r["pk"]]
                    if primary == ["idem_key"]:
                        definition = SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table}(", 1)[1].split(";", 1)[0]
                        self._db.execute(f"CREATE TABLE {table}_v6(" + definition)
                        cols = f"idem_key,{payload},created_at,principal_kind,principal_id,workspace_id,agent_id"
                        self._db.execute(f"INSERT INTO {table}_v6({cols}) SELECT {cols} FROM {table}")
                        self._db.execute(f"DROP TABLE {table}")
                        self._db.execute(f"ALTER TABLE {table}_v6 RENAME TO {table}")
                self._db.execute("DROP INDEX IF EXISTS messages_idem")
                self._db.execute(IDEM_INDEX)
                self._db.execute(TAG_INDEX)
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # messages -----------------------------------------------------------------------------------------------

    def add_message(self, *, id: str, created_at: str, channel: str, type: str, speech: str | None, key: str,
                    bytes: int, text_chars: int | None, idem_key: str | None = None, tag: str | None = None,
                    normalized: bool = False, truncated: bool = False, principal: Principal = OWNER) -> None:
        with self._lock:
            if idem_key is not None:  # an expired use of the same key gives way to this one
                self._db.execute("UPDATE messages SET idem_key=NULL WHERE idem_key=?"
                                 " AND principal_kind=? AND principal_id=? AND workspace_id=?",
                                 (idem_key, *provenance(principal)))
            self._db.execute(
                "INSERT INTO messages(id, created_at, channel, type, speech, key, bytes, text_chars, idem_key, tag,"
                " normalized, truncated, principal_kind, principal_id, workspace_id, agent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, created_at, channel, type, speech, key, bytes, text_chars, idem_key, tag, int(normalized),
                 int(truncated), *provenance(principal), principal.agent_id or ""),
            )

    def get_by_idem(self, idem_key: str, not_before: str, *, principal: Principal = OWNER) -> MessageRow | None:
        """The message first sent with this Idempotency-Key hash, if it was created at or after `not_before`."""
        with self._lock:
            row = self._db.execute("SELECT rowid, * FROM messages WHERE idem_key=? AND created_at >= ?"
                                   " AND principal_kind=? AND principal_id=? AND workspace_id=?",
                                   (idem_key, not_before, *provenance(principal))).fetchone()
        return self._message(row)

    def owner_idempotency_exists(self, digest: str) -> bool:
        """Detect a retained ambiguous owner key without returning owner message data."""
        with self._lock:
            for table in ("messages", "idem_aliases", "suppressed_idem"):
                row = self._db.execute(
                    f"SELECT 1 FROM {table} WHERE idem_key=?"
                    " AND principal_kind=? AND principal_id=? AND workspace_id=? LIMIT 1",
                    (digest, *provenance(OWNER)),
                ).fetchone()
                if row is not None:
                    return True
        return False

    def _message(self, row: sqlite3.Row | None) -> MessageRow | None:
        if row is None:
            return None
        return MessageRow(row["rowid"], row["id"], row["created_at"], row["channel"], row["type"], row["speech"],
                          row["key"], row["bytes"], row["text_chars"], row["tag"], bool(row["normalized"]),
                          bool(row["truncated"]), row["principal_kind"], row["principal_id"], row["workspace_id"], row["agent_id"])

    def get_message(self, message_id: str, *, principal: Principal | None = None,
                    allowed_channels: list[str] | None = None) -> MessageRow | None:
        where, args = [], []
        if message_id != "latest":
            where.append("id=?")
            args.append(message_id)
        else:
            where.append("channel != 'replies'")
        if principal is not None:
            where.append("principal_kind=? AND principal_id=? AND workspace_id=?")
            args.extend(provenance(principal))
        if allowed_channels is not None:
            if not allowed_channels:
                return None
            where.append("channel IN (" + ",".join("?" for _ in allowed_channels) + ")")
            args.extend(allowed_channels)
        sql = "SELECT rowid, * FROM messages" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return self._message(row)

    def list_messages(self, *, channel: str | None, limit: int, since_created: str | None = None,
                      since_rowid: int | None = None, principal: Principal | None = None,
                      allowed_channels: list[str] | None = None) -> list[MessageRow]:
        """Newest first; with a `since` bound, oldest first after that bound (exclusive)."""
        where, args = ["channel != 'replies'"], []
        if principal is not None:
            where.append("principal_kind=? AND principal_id=? AND workspace_id=?")
            args.extend(provenance(principal))
        if allowed_channels is not None:
            if not allowed_channels:
                return []
            where.append("channel IN (" + ",".join("?" for _ in allowed_channels) + ")")
            args.extend(allowed_channels)
        if channel:
            where.append("channel=?")
            args.append(channel)
        if since_created is not None:
            if since_rowid is not None:
                where.append("(created_at > ? OR (created_at = ? AND rowid > ?))")
                args += [since_created, since_created, since_rowid]
            else:
                where.append("created_at > ?")
                args.append(since_created)
            order = "created_at ASC, rowid ASC"
        else:
            order = "created_at DESC, rowid DESC"
        sql = "SELECT rowid, * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._message(r) for r in rows]

    def last_message_at(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(created_at) AS t FROM messages").fetchone()
        return row["t"] if row else None

    def latest_tagged(self, channel: str, tag: str, not_before: str, *, principal: Principal = OWNER) -> MessageRow | None:
        """The newest message written to `channel` under `tag` at or after `not_before` (the cooldown check)."""
        with self._lock:
            row = self._db.execute(
                "SELECT rowid, * FROM messages WHERE channel=? AND tag=? AND created_at >= ?"
                " AND principal_kind=? AND principal_id=? AND workspace_id=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1", (channel, tag, not_before, *provenance(principal))).fetchone()
        return self._message(row)

    # The watchdog. Correlation is by key: only a beacon for a message's own key reports on it, so a receipt for
    # another message (or for a key the relay never stored) never covers one that got none.
    _UNREPORTED = ("FROM messages m WHERE m.created_at > ? AND m.created_at <= ? AND NOT EXISTS ("
                   "SELECT 1 FROM beacons b WHERE b.key = m.key AND b.phase IN ({marks}))")

    def undetected_message(self, after: str, before: str) -> MessageRow | None:
        """The oldest message created in (after, before] that no device has reported on (no playback beacon for
        its own key)."""
        marks = ",".join("?" * len(PLAYBACK_PHASES))
        with self._lock:
            row = self._db.execute(
                f"SELECT m.rowid, m.* {self._UNREPORTED.format(marks=marks)} ORDER BY m.created_at ASC, m.rowid ASC "
                "LIMIT 1", (after, before, *PLAYBACK_PHASES)).fetchone()
        return self._message(row)

    def undetected_count(self, after: str, before: str) -> int:
        marks = ",".join("?" * len(PLAYBACK_PHASES))
        with self._lock:
            (n,) = self._db.execute(f"SELECT COUNT(*) {self._UNREPORTED.format(marks=marks)}",
                                    (after, before, *PLAYBACK_PHASES)).fetchone()
        return n

    def reported_since(self, created_from: str, received_from: str) -> bool:
        """A playback beacon received at or after `received_from` for a message created at or after `created_from`
        (an announced watchdog incident clears on it: the phone is reporting again)."""
        marks = ",".join("?" * len(PLAYBACK_PHASES))
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM beacons b JOIN messages m ON m.key = b.key WHERE m.created_at >= ? AND "
                f"b.received_at >= ? AND b.phase IN ({marks}) LIMIT 1",
                (created_from, received_from, *PLAYBACK_PHASES)).fetchone()
        return row is not None

    # coalescing (pending writes) ------------------------------------------------------------------------------

    @staticmethod
    def _pending(row: sqlite3.Row | None) -> PendingRow | None:
        if row is None:
            return None
        return PendingRow(row["id"], row["channel"], row["tag"], row["created_at"], row["due_at"], row["text"],
                          row["speech"], row["voice"], bool(row["normalized"]), bool(row["truncated"]), row["merged"],
                          row["attempts"], bool(row["compat"]), row["window_end_at"], row["principal_kind"],
                          row["principal_id"], row["workspace_id"], row["agent_id"])

    def get_pending(self, channel: str, tag: str, *, principal: Principal = OWNER) -> PendingRow | None:
        with self._lock:
            return self._pending(self._db.execute(
                "SELECT * FROM pending WHERE channel=? AND tag=? AND principal_kind=? AND principal_id=? AND workspace_id=?",
                (channel, tag, *provenance(principal))).fetchone())

    def ambiguous_legacy_pending(self, channel: str, tag: str) -> bool:
        """A pre-scope compat hold may be missing the alias of a request interrupted by a crash."""
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM pending WHERE channel=? AND tag=?"
                " AND principal_kind=? AND principal_id=? AND workspace_id=? LIMIT 1",
                (channel, tag, *provenance(OWNER)),
            ).fetchone() is not None

    def get_pending_by_id(self, message_id: str) -> PendingRow | None:
        with self._lock:
            return self._pending(self._db.execute("SELECT * FROM pending WHERE id=?", (message_id,)).fetchone())

    def _save_pending(self, sql: str, values: tuple, *, idem_key: str | None, message_id: str,
                      alias_at: str | None, principal: Principal) -> None:
        if idem_key and alias_at is None:
            raise ValueError("alias timestamp required")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(sql, values)
                if idem_key:
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError("pending row disappeared")
                    assert alias_at is not None
                    self._insert_alias(idem_key, message_id, alias_at, principal=principal)
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def add_pending(self, row: PendingRow, *, idem_key: str | None = None) -> None:
        self._save_pending(
            "INSERT INTO pending(id, channel, tag, created_at, due_at, text, speech, voice, normalized, truncated,"
            " merged, attempts, compat, window_end_at, principal_kind, principal_id, workspace_id, agent_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.id, row.channel, row.tag, row.created_at, row.due_at, row.text, row.speech, row.voice,
             int(row.normalized), int(row.truncated), row.merged, row.attempts, int(row.compat),
             row.window_end_at or row.due_at, row.principal_kind, row.principal_id, row.workspace_id, row.agent_id),
            idem_key=idem_key, message_id=row.id, alias_at=row.created_at, principal=principal_for(row))

    def update_pending(self, message_id: str, *, text: str, speech: str, voice: str | None, normalized: bool,
                       truncated: bool, compat: bool = False, idem_key: str | None = None,
                       alias_at: str | None = None, principal: Principal = OWNER) -> None:
        """Latest text wins; retain compat provenance if any merged send used the compat alarm gate."""
        self._save_pending(
            "UPDATE pending SET text=?, speech=?, voice=?, normalized=?, truncated=?, compat=MAX(compat, ?), "
            "merged=merged+1 WHERE id=?",
            (text, speech, voice, int(normalized), int(truncated), int(compat), message_id),
            idem_key=idem_key, message_id=message_id, alias_at=alias_at, principal=principal)

    def retry_pending(self, message_id: str, attempts: int, due_at: str) -> None:
        with self._lock:
            self._db.execute("UPDATE pending SET attempts=?, due_at=? WHERE id=?", (attempts, due_at, message_id))

    def delete_pending(self, message_id: str) -> None:
        """Delete a held message and checkpoint, so no copy of its text lingers in the write-ahead log."""
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE id=?", (message_id,))
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass  # busy (another reader): the next checkpoint gets it

    def fail_pending(self, row: PendingRow, failed_at: str, error: str) -> None:
        """Give up on a held message: keep a record without its text (visible in its receipt and in status)."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO failed_pending(id, channel, tag, created_at, failed_at, attempts, error,"
                " principal_kind, principal_id, workspace_id, agent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row.id, row.channel, row.tag, row.created_at, failed_at, row.attempts, error,
                 row.principal_kind, row.principal_id, row.workspace_id, row.agent_id))
            self._db.execute("DELETE FROM pending WHERE id=?", (row.id,))
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass

    @staticmethod
    def _failed(row: sqlite3.Row | None) -> FailedRow | None:
        if row is None:
            return None
        return FailedRow(row["id"], row["channel"], row["tag"], row["created_at"], row["failed_at"], row["attempts"],
                         row["error"], row["principal_kind"], row["principal_id"], row["workspace_id"], row["agent_id"])

    def get_failed(self, message_id: str) -> FailedRow | None:
        with self._lock:
            return self._failed(self._db.execute("SELECT * FROM failed_pending WHERE id=?", (message_id,)).fetchone())

    def failed_count(self) -> int:
        with self._lock:
            (n,) = self._db.execute("SELECT COUNT(*) FROM failed_pending").fetchone()
        return n

    def unnotified_failed(self) -> tuple[FailedRow | None, int]:
        """The oldest failed coalesced message nobody was told about, and how many there are."""
        with self._lock:
            row = self._db.execute("SELECT * FROM failed_pending WHERE notified=0 ORDER BY failed_at ASC LIMIT 1").fetchone()
            (n,) = self._db.execute("SELECT COUNT(*) FROM failed_pending WHERE notified=0").fetchone()
        return self._failed(row), n

    def mark_failed_notified(self) -> None:
        with self._lock:
            self._db.execute("UPDATE failed_pending SET notified=1 WHERE notified=0")

    def due_pending(self, now: str, limit: int = 50) -> list[PendingRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM pending WHERE due_at <= ? ORDER BY due_at ASC LIMIT ?",
                                    (now, limit)).fetchall()
        return [self._pending(r) for r in rows]

    def next_pending_at(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(due_at) AS t FROM pending").fetchone()
        return row["t"] if row else None

    def pending_count(self) -> int:
        with self._lock:
            (n,) = self._db.execute("SELECT COUNT(*) FROM pending").fetchone()
        return n

    def save_suppressed(self, digest: str, at: str, result: dict, *, principal: Principal = OWNER) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO suppressed_idem"
                             "(idem_key,created_at,result,principal_kind,principal_id,workspace_id,agent_id) VALUES(?,?,?,?,?,?,?)",
                             (digest, at, json.dumps(result), *provenance(principal), principal.agent_id or ""))

    def get_suppressed(self, digest: str, cutoff: str, *, principal: Principal = OWNER) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT result FROM suppressed_idem WHERE idem_key=? AND created_at>=?"
                                   " AND principal_kind=? AND principal_id=? AND workspace_id=?",
                                   (digest, cutoff, *provenance(principal))).fetchone()
        return json.loads(row["result"]) if row else None

    def _insert_alias(self, idem_key: str, message_id: str, created_at: str, *, principal: Principal) -> None:
        self._db.execute("INSERT OR REPLACE INTO idem_aliases"
                         "(idem_key,message_id,created_at,principal_kind,principal_id,workspace_id,agent_id) VALUES(?,?,?,?,?,?,?)",
                         (idem_key, message_id, created_at, *provenance(principal), principal.agent_id or ""))

    def add_alias(self, idem_key: str, message_id: str, created_at: str, *, principal: Principal = OWNER) -> None:
        with self._lock:
            self._insert_alias(idem_key, message_id, created_at, principal=principal)

    def get_alias(self, idem_key: str, not_before: str, *, principal: Principal = OWNER) -> str | None:
        """The id of the message an Idempotency-Key hash was merged into by coalescing, if recent enough."""
        with self._lock:
            row = self._db.execute("SELECT message_id FROM idem_aliases WHERE idem_key=? AND created_at >= ?"
                                   " AND principal_kind=? AND principal_id=? AND workspace_id=?",
                                   (idem_key, not_before, *provenance(principal))).fetchone()
        return row["message_id"] if row else None

    # meta (small persisted state, e.g. the watchdog's) -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, **values: str | None) -> None:
        with self._lock:
            for k, v in values.items():
                if v is None:
                    self._db.execute("DELETE FROM meta WHERE k=?", (k,))
                else:
                    self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, v))

    # beacons ------------------------------------------------------------------------------------------------

    def add_beacon(self, beacon: dict, received_at: str, max_devices_per_key: int | None = None) -> bool:
        """Insert one validated beacon. False when it is a duplicate (a retried POST): still a success.

        Raises TooManyDevices when the key already has `max_devices_per_key` other devices: real phones on one
        bucket are a handful, so more is a forged flood from a leaked beacon URL.
        """
        key = beacon.get("key", "")
        with self._lock:
            if max_devices_per_key is not None and key:
                known = self._db.execute("SELECT 1 FROM beacons WHERE key=? AND device=? LIMIT 1",
                                         (key, beacon["device"])).fetchone()
                if known is None:
                    (count,) = self._db.execute("SELECT COUNT(DISTINCT device) FROM beacons WHERE key=?",
                                                (key,)).fetchone()
                    if count >= max_devices_per_key:
                        raise TooManyDevices(max_devices_per_key)
            cur = self._db.execute(
                "INSERT OR IGNORE INTO beacons(key, channel, phase, device, app_version, at, last_modified, clock_skew_ms,"
                " error, received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, beacon["channel"], beacon["phase"], beacon["device"], beacon["app_version"],
                 beacon["at"], beacon.get("last_modified"), beacon.get("clock_skew_ms"), beacon.get("error"), received_at),
            )
            return cur.rowcount == 1

    @staticmethod
    def _beacon(row: sqlite3.Row) -> BeaconRow:
        return BeaconRow(row["key"], row["channel"], row["phase"], row["device"], row["app_version"], row["at"],
                         row["last_modified"], row["clock_skew_ms"], row["error"], row["received_at"])

    def beacons_for(self, keys: list[str]) -> dict[str, list[BeaconRow]]:
        out: dict[str, list[BeaconRow]] = {k: [] for k in keys}
        if not keys:
            return out
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for row in self._db.execute(f"SELECT * FROM beacons WHERE key IN ({marks}) ORDER BY id", chunk):
                    out[row["key"]].append(self._beacon(row))
        return out

    def any_beacon(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM beacons LIMIT 1").fetchone() is not None

    def last_beacon_at(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(received_at) AS t FROM beacons").fetchone()
        return row["t"] if row else None

    def devices(self, limit: int = 20) -> list[BeaconRow]:
        """The most recently received beacon of each device, newest first, at most `limit` devices."""
        with self._lock:
            rows = self._db.execute(
                "SELECT b.* FROM beacons b JOIN (SELECT device, MAX(id) AS id FROM beacons GROUP BY device) m ON b.id = m.id"
                " ORDER BY b.id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._beacon(r) for r in rows]

    # retention ----------------------------------------------------------------------------------------------

    def prune(self, retention_days: int, now: datetime | None = None) -> tuple[int, int]:
        cutoff = iso_ms((now or now_utc()) - timedelta(days=retention_days))
        with self._lock:
            m = self._db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,)).rowcount
            b = self._db.execute("DELETE FROM beacons WHERE received_at < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM suppressed_idem WHERE created_at < ?", (cutoff,))
            # An old coalesced request remains in flight while its pending row exists.
            self._db.execute("DELETE FROM idem_aliases WHERE created_at < ? AND NOT EXISTS ("
                             "SELECT 1 FROM pending p WHERE p.id=idem_aliases.message_id"
                             " AND p.principal_kind=idem_aliases.principal_kind"
                             " AND p.principal_id=idem_aliases.principal_id"
                             " AND p.workspace_id=idem_aliases.workspace_id)", (cutoff,))
            self._db.execute("DELETE FROM failed_pending WHERE failed_at < ?", (cutoff,))
        return m, b
