"""SQLite index in the data dir (WAL). Holds message metadata and beacons only: never message text, never tokens."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .beacons import iso_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  type TEXT NOT NULL,
  speech TEXT,
  key TEXT UNIQUE NOT NULL,
  bytes INTEGER NOT NULL,
  text_chars INTEGER
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
"""


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
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA user_version=1")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # messages -----------------------------------------------------------------------------------------------

    def add_message(self, *, id: str, created_at: str, channel: str, type: str, speech: str | None, key: str,
                    bytes: int, text_chars: int | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(id, created_at, channel, type, speech, key, bytes, text_chars) VALUES(?,?,?,?,?,?,?,?)",
                (id, created_at, channel, type, speech, key, bytes, text_chars),
            )

    def _message(self, row: sqlite3.Row | None) -> MessageRow | None:
        if row is None:
            return None
        return MessageRow(row["rowid"], row["id"], row["created_at"], row["channel"], row["type"], row["speech"],
                          row["key"], row["bytes"], row["text_chars"])

    def get_message(self, message_id: str) -> MessageRow | None:
        with self._lock:
            if message_id == "latest":
                row = self._db.execute("SELECT rowid, * FROM messages ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            else:
                row = self._db.execute("SELECT rowid, * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._message(row)

    def list_messages(self, *, channel: str | None, limit: int, since_created: str | None = None,
                      since_rowid: int | None = None) -> list[MessageRow]:
        """Newest first; with a `since` bound, oldest first after that bound (exclusive)."""
        where, args = [], []
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

    # beacons ------------------------------------------------------------------------------------------------

    def add_beacon(self, beacon: dict, received_at: str) -> bool:
        """Insert one validated beacon. False when it is a duplicate (a retried POST): still a success."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO beacons(key, channel, phase, device, app_version, at, last_modified, clock_skew_ms,"
                " error, received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (beacon.get("key", ""), beacon["channel"], beacon["phase"], beacon["device"], beacon["app_version"],
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

    def devices(self) -> list[BeaconRow]:
        """The most recently received beacon of each device."""
        with self._lock:
            rows = self._db.execute(
                "SELECT b.* FROM beacons b JOIN (SELECT device, MAX(id) AS id FROM beacons GROUP BY device) m ON b.id = m.id"
                " ORDER BY b.received_at DESC"
            ).fetchall()
        return [self._beacon(r) for r in rows]

    # retention ----------------------------------------------------------------------------------------------

    def prune(self, retention_days: int, now: datetime | None = None) -> tuple[int, int]:
        cutoff = iso_ms((now or now_utc()) - timedelta(days=retention_days))
        with self._lock:
            m = self._db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,)).rowcount
            b = self._db.execute("DELETE FROM beacons WHERE received_at < ?", (cutoff,)).rowcount
        return m, b
