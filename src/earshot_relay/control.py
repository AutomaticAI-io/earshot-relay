"""Versioned workspace contract and the local principal database.

The control database is an application authorization boundary, not provider IAM.
A local user able to modify the database or the relay process is trusted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ControlError(Exception):
    """A safe, non-secret control-plane error."""


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ControlError("invalid control contract")


def _id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ControlError("invalid control identifier")
    return value


def canonical_endpoint(endpoint):
    p = urlsplit(endpoint)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ControlError("invalid workspace binding")
    try:
        port = p.port
    except ValueError:
        raise ControlError("invalid workspace binding") from None
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = (p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)
    return f"{p.scheme.lower()}://{host}{':' + str(port) if port and not default else ''}{p.path.rstrip('/')}"


def validate_contract(contract, settings):
    """Validate all closed objects and references, then compare the actual writer binding."""
    _object(contract, ("version", "workspace", "channels", "agents", "devices"))
    if contract["version"] != "control.v1":
        raise ControlError("unsupported control version")
    workspace = contract["workspace"]
    _object(workspace, ("id", "endpoint", "bucket", "region"))
    workspace_id = _id(workspace["id"])
    if not all(isinstance(workspace[k], str) and workspace[k] for k in ("endpoint", "bucket", "region")):
        raise ControlError("invalid workspace binding")
    if (canonical_endpoint(workspace["endpoint"]) != canonical_endpoint(settings.s3_endpoint)
            or workspace["bucket"] != settings.bucket or workspace["region"] != settings.region):
        raise ControlError("workspace binding does not match relay writer")
    for collection, extra in (("channels", "name"), ("agents", None), ("devices", None)):
        rows = contract[collection]
        if not isinstance(rows, list):
            raise ControlError("invalid control contract")
        ids, names = set(), set()
        for row in rows:
            _object(row, ("id", "workspace_id", extra) if extra else ("id", "workspace_id"))
            ident = _id(row["id"])
            if ident in ids or row["workspace_id"] != workspace_id:
                raise ControlError("duplicate or foreign control reference")
            ids.add(ident)
            if extra:
                name = row["name"]
                if not isinstance(name, str) or not CHANNEL_RE.fullmatch(name) or name == "replies" or name in names:
                    raise ControlError("invalid or reserved channel")
                names.add(name)
                if name not in settings.channels:
                    raise ControlError("control channel is not configured")
    return workspace_id


def digest_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_directory(path: Path):
    try:
        info = path.lstat()
    except OSError:
        raise ControlError("control directory is unavailable") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ControlError("control directory must be owned by the relay user and mode 0700")


def _safe_file(path: Path):
    _safe_directory(path.parent)
    try:
        info = path.lstat()
    except OSError:
        raise ControlError("control database is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ControlError("control database must be owned by the relay user and mode 0600")


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str
    workspace_id: str | None = None
    agent_id: str | None = None


OWNER = Principal("owner", "owner")


class Control:
    SCHEMA = """
        CREATE TABLE binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), contract TEXT NOT NULL,
                              owner_digest TEXT NOT NULL);
        CREATE TABLE principals (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                                 token_digest TEXT NOT NULL UNIQUE, expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                                 receipts INTEGER NOT NULL, alarm INTEGER NOT NULL);
        CREATE TABLE grants (principal_id TEXT NOT NULL REFERENCES principals(id), channel_id TEXT NOT NULL,
                             PRIMARY KEY(principal_id, channel_id));
    """

    def __init__(self, path, settings):
        self.path = Path(path)
        self.settings = settings
        _safe_directory(self.path.parent)
        _safe_file(self.path)
        with closing(self.connect()) as db:
            row = db.execute("SELECT contract, owner_digest FROM binding WHERE singleton=1").fetchone()
            if row is None:
                raise ControlError("control binding is missing")
            try:
                self.contract = json.loads(row["contract"])
                self.workspace_id = validate_contract(self.contract, settings)
            except (ValueError, TypeError):
                raise ControlError("invalid control contract") from None
            if not hmac.compare_digest(row["owner_digest"], digest_token(settings.token)):
                raise ControlError("owner credential does not match control binding")

    @classmethod
    def initialize(cls, path, settings, contract, owner_token):
        validate_contract(contract, settings)
        if not hmac.compare_digest(owner_token.encode(), settings.token.encode()):
            raise ControlError("owner authentication failed")
        path = Path(path)
        _safe_directory(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise ControlError("cannot create control database") from None
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            try:
                db.executescript(cls.SCHEMA)
                db.execute("INSERT INTO binding VALUES(1,?,?)",
                           (json.dumps(contract, sort_keys=True, separators=(",", ":")), digest_token(owner_token)))
                db.commit()
            finally:
                db.close()
            return cls(path, settings)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def connect(self):
        _safe_directory(self.path.parent)
        _safe_file(self.path)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def verify_owner(self, owner_token):
        with closing(self.connect()) as db:
            expected = db.execute("SELECT owner_digest FROM binding WHERE singleton=1").fetchone()[0]
        if not hmac.compare_digest(expected, digest_token(owner_token)):
            raise ControlError("owner authentication failed")

    def authenticate(self, token):
        if not token or len(token) > 512:
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM principals WHERE token_digest=?", (digest_token(token),)).fetchone()
            if not row or not self._active(row):
                return None
            return Principal("scoped", row["id"], row["workspace_id"], row["agent_id"])

    @staticmethod
    def _active(row):
        if row["revoked"]:
            return False
        if row["expires_at"]:
            try:
                expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                return expires.tzinfo is not None and datetime.now(timezone.utc) < expires
            except ValueError:
                return False
        return True

    def permits(self, principal, channel, *, receipt=False, db=None):
        if principal.kind != "scoped" or principal.workspace_id != self.workspace_id or channel == "replies":
            return False
        close = db is None
        db = db or self.connect()
        try:
            row = db.execute("SELECT * FROM principals WHERE id=? AND workspace_id=?",
                             (principal.id, self.workspace_id)).fetchone()
            if row is None or not self._active(row) or (receipt and not row["receipts"]):
                return False
            if channel == "alarm" and not row["alarm"]:
                return False
            channel_id = next((c["id"] for c in self.contract["channels"] if c["name"] == channel), None)
            return channel_id is not None and db.execute(
                "SELECT 1 FROM grants WHERE principal_id=? AND channel_id=?", (principal.id, channel_id)).fetchone() is not None
        finally:
            if close:
                db.close()

    def issue(self, owner_token, agent_id, channel_ids, *, alarm, receipts, expires_at, output, pushover=False):
        self.verify_owner(owner_token)
        _id(agent_id)
        if agent_id not in {x["id"] for x in self.contract["agents"]}:
            raise ControlError("unknown agent")
        known = {x["id"]: x["name"] for x in self.contract["channels"]}
        if not channel_ids or len(set(channel_ids)) != len(channel_ids) or any(x not in known for x in channel_ids):
            raise ControlError("invalid channel grants")
        if alarm != any(known[x] == "alarm" for x in channel_ids):
            raise ControlError("alarm requires both the alarm channel and explicit alarm grant")
        if expires_at:
            try:
                t = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if t.tzinfo is None or t <= datetime.now(timezone.utc):
                    raise ValueError
            except ValueError:
                raise ControlError("invalid expiry") from None
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        token = "".join(secrets.choice(alphabet) for _ in range(30)) if pushover else secrets.token_urlsafe(32)
        principal_id = "principal_" + secrets.token_hex(16)
        output = Path(output)
        _safe_directory(output.parent)
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise ControlError("cannot create token output") from None
        db = None
        try:
            db = self.connect()
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO principals(id,workspace_id,agent_id,token_digest,expires_at,receipts,alarm) "
                       "VALUES(?,?,?,?,?,?,?)", (principal_id, self.workspace_id, agent_id, digest_token(token), expires_at,
                                               int(receipts), int(alarm)))
            db.executemany("INSERT INTO grants VALUES(?,?)", [(principal_id, c) for c in channel_ids])
            with os.fdopen(fd, "w", encoding="ascii") as f:
                fd = -1
                f.write(token + "\n")
                f.flush()
                os.fsync(f.fileno())
            db.commit()
            return principal_id
        except BaseException:
            if db is not None:
                db.rollback()
            if fd != -1:
                os.close(fd)
            output.unlink(missing_ok=True)
            raise
        finally:
            if db is not None:
                db.close()

    def revoke(self, owner_token, principal_id):
        self.verify_owner(owner_token)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE principals SET revoked=1 WHERE id=? AND workspace_id=?",
                                 (principal_id, self.workspace_id)).rowcount
            if not changed:
                db.rollback()
                raise ControlError("principal not found")
            db.commit()
