"""Local application authorization, not provider IAM.

The control directory and relay configuration belong to a trusted OS user. A user
who can change either can change authorization; owner authentication is not an
independent defense against that user. Crashes can leave an inert token file or
an incomplete database, but cannot activate a token before its file is durable.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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

ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CHANNEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
BUCKET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,254}")
REGION_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
EXPIRY_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


class ControlError(Exception):
    """A safe, non-secret control-plane error."""


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ControlError("invalid control contract")


def _id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ControlError("invalid control identifier")
    return value


def parse_contract(text):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError

    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError):
        raise ControlError("invalid control contract") from None


def canonical_endpoint(endpoint):
    if (not isinstance(endpoint, str) or not endpoint or not endpoint.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
            or any(c in endpoint for c in "\\?#")):
        raise ControlError("invalid workspace binding")
    try:
        p = urlsplit(endpoint)
        port = p.port
        host = p.hostname
        if (p.scheme not in ("http", "https") or not host or p.username is not None
                or p.password is not None or p.netloc.endswith(":") or port == 0
                or not re.fullmatch(r"/[A-Za-z0-9._~/-]*|", p.path)
                or any(segment in (".", "..") for segment in p.path.split("/"))
                or "//" in p.path):
            raise ValueError
        if ":" in host:
            if not re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?", p.netloc):
                raise ValueError
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        elif (not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", p.netloc)
              or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host.lower())):
            raise ValueError
        else:
            host = host.lower()
        default = (p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)
        return f"{p.scheme}://{host}{':' + str(port) if port and not default else ''}{p.path.rstrip('/')}"
    except (ValueError, TypeError):
        raise ControlError("invalid workspace binding") from None


def validate_contract(contract, settings):
    """Check closed objects, references, and the configured writer binding."""
    _object(contract, ("version", "workspace", "channels", "agents", "devices"))
    if not isinstance(contract["version"], str) or contract["version"] != "control.v1":
        raise ControlError("unsupported control version")
    workspace = contract["workspace"]
    _object(workspace, ("id", "endpoint", "bucket", "region"))
    workspace_id = _id(workspace["id"])
    bucket, region = workspace["bucket"], workspace["region"]
    if (not isinstance(bucket, str) or not BUCKET_RE.fullmatch(bucket)
            or not isinstance(region, str) or not REGION_RE.fullmatch(region)):
        raise ControlError("invalid workspace binding")
    if (canonical_endpoint(workspace["endpoint"]) != canonical_endpoint(settings.s3_endpoint)
            or bucket != settings.bucket or region != settings.region):
        raise ControlError("workspace binding does not match relay writer")
    for collection, extra in (("channels", "name"), ("agents", None), ("devices", None)):
        rows = contract[collection]
        if not isinstance(rows, list):
            raise ControlError("invalid control contract")
        ids, names = set(), set()
        for row in rows:
            _object(row, ("id", "workspace_id", extra) if extra else ("id", "workspace_id"))
            ident = _id(row["id"])
            _id(row["workspace_id"])
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
    if ".." in path.parts:
        raise ControlError("unsafe control directory")
    path = Path(os.path.abspath(path))
    private = False
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except OSError:
            raise ControlError("control directory is unavailable") from None
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or mode & 0o7000:
            raise ControlError("unsafe control directory")
        if not private and mode & 0o022:
            raise ControlError("unsafe control directory")
        if info.st_uid == os.geteuid() and not mode & 0o077:
            private = True
    if info.st_uid != os.geteuid() or mode != 0o700:
        raise ControlError("control directory must be owned by the relay user and mode 0700")


def _check_file(path, *, required):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not required:
            return
        raise ControlError("control database is unavailable") from None
    except OSError:
        raise ControlError("control database is unavailable") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ControlError("unsafe control database file")


def _safe_file(path: Path):
    _safe_directory(path.parent)
    _check_file(path, required=True)
    for suffix in ("-journal", "-wal", "-shm"):
        _check_file(Path(str(path) + suffix), required=False)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _expiry(value):
    if not isinstance(value, str) or not EXPIRY_RE.fullmatch(value):
        raise ValueError
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str
    workspace_id: str | None = None
    agent_id: str | None = None


OWNER = Principal("owner", "owner")


class Control:
    SCHEMA = """
        CREATE TABLE binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), contract TEXT NOT NULL);
        CREATE TRIGGER binding_no_update BEFORE UPDATE ON binding BEGIN SELECT RAISE(ABORT,'immutable binding'); END;
        CREATE TRIGGER binding_no_delete BEFORE DELETE ON binding BEGIN SELECT RAISE(ABORT,'immutable binding'); END;
        CREATE TABLE principals (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                                 token_digest TEXT NOT NULL UNIQUE, expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                                 receipts INTEGER NOT NULL, alarm INTEGER NOT NULL);
        CREATE TABLE grants (principal_id TEXT NOT NULL REFERENCES principals(id), channel_id TEXT NOT NULL,
                             PRIMARY KEY(principal_id, channel_id));
    """

    def __init__(self, path, settings):
        self.path = Path(path)
        self.settings = settings
        try:
            with closing(self.connect()) as db:
                rows = db.execute("SELECT contract FROM binding").fetchall()
                if len(rows) != 1 or not isinstance(rows[0][0], str):
                    raise ControlError("control binding is missing")
                self._binding = rows[0][0]
                self.contract = parse_contract(self._binding)
                self.workspace_id = validate_contract(self.contract, settings)
        except sqlite3.Error:
            raise ControlError("control database is unavailable") from None

    @classmethod
    def initialize(cls, path, settings, contract, owner_token):
        validate_contract(contract, settings)
        cls._check_owner(settings, owner_token)
        normalized = json.loads(json.dumps(contract))
        normalized["workspace"]["endpoint"] = canonical_endpoint(normalized["workspace"]["endpoint"])
        binding = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        path = Path(path)
        _safe_directory(path.parent)
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.lexists(str(path) + suffix):
                raise ControlError("cannot create control database")
        fd = -1
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            fd = -1
        except OSError:
            if fd != -1:
                os.close(fd)
                path.unlink(missing_ok=True)
            raise ControlError("cannot create control database") from None
        try:
            db = sqlite3.connect(path, isolation_level=None)
            try:
                db.executescript("BEGIN IMMEDIATE;" + cls.SCHEMA)
                db.execute("INSERT INTO binding VALUES(1,?)", (binding,))
                db.commit()
                _safe_file(path)
            finally:
                db.close()
            _sync_directory(path.parent)
            return cls(path, settings)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def connect(self):
        _safe_file(self.path)
        try:
            db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=5,
                                 isolation_level=None, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            _safe_file(self.path)
            return db
        except (sqlite3.Error, OSError, ControlError):
            if 'db' in locals():
                db.close()
            raise ControlError("control database is unavailable") from None

    def _live_contract(self, db):
        _safe_file(self.path)
        try:
            conn = db.execute("PRAGMA database_list").fetchall()
            main = [row[2] for row in conn if row[1] == "main"]
            if len(main) != 1 or not os.path.samefile(main[0], self.path):
                raise ControlError("control database changed")
        except (OSError, ValueError):
            raise ControlError("control database changed") from None
        rows = db.execute("SELECT contract FROM binding").fetchall()
        if len(rows) != 1 or rows[0][0] != self._binding:
            raise ControlError("control binding changed")
        contract = parse_contract(rows[0][0])
        if validate_contract(contract, self.settings) != self.workspace_id:
            raise ControlError("control binding changed")
        return contract

    @staticmethod
    def _check_owner(settings, owner_token):
        try:
            good = (isinstance(owner_token, str) and isinstance(settings.token, str)
                    and hmac.compare_digest(owner_token.encode("utf-8"), settings.token.encode("utf-8")))
        except UnicodeError:
            good = False
        if not good:
            raise ControlError("owner authentication failed")

    def verify_owner(self, owner_token):
        self._check_owner(self.settings, owner_token)
        with closing(self.connect()) as db:
            self._live_contract(db)

    @staticmethod
    def _active(row):
        if row["revoked"] != 0 or row["receipts"] not in (0, 1) or row["alarm"] not in (0, 1):
            return False
        if row["expires_at"] is not None:
            try:
                return datetime.now(timezone.utc) < _expiry(row["expires_at"])
            except (ValueError, TypeError, OverflowError):
                return False
        return True

    def _valid_principal(self, row, contract):
        return (row is not None and self._active(row) and row["workspace_id"] == self.workspace_id
                and isinstance(row["id"], str) and ID_RE.fullmatch(row["id"]) is not None
                and row["agent_id"] in {x["id"] for x in contract["agents"]})

    def authenticate(self, token):
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN")
                contract = self._live_contract(db)
                row = db.execute("SELECT * FROM principals WHERE token_digest=?", (digest_token(token),)).fetchone()
                if not self._valid_principal(row, contract):
                    return None
                return Principal("scoped", row["id"], row["workspace_id"], row["agent_id"])
        except (ControlError, sqlite3.Error, OSError, ValueError, TypeError, UnicodeError, IndexError, KeyError):
            return None

    def permits(self, principal, channel, *, receipt=False, db=None):
        if (not isinstance(principal, Principal) or principal.kind != "scoped"
                or principal.workspace_id != self.workspace_id or not isinstance(channel, str) or channel == "replies"):
            return False
        close = db is None
        try:
            if close:
                db = self.connect()
                db.execute("BEGIN")
            elif not db.in_transaction:
                return False
            contract = self._live_contract(db)
            row = db.execute("SELECT * FROM principals WHERE id=? AND workspace_id=?",
                             (principal.id, self.workspace_id)).fetchone()
            if (not self._valid_principal(row, contract) or row["agent_id"] != principal.agent_id
                    or (receipt and row["receipts"] != 1) or (channel == "alarm" and row["alarm"] != 1)):
                return False
            channel_id = next((c["id"] for c in contract["channels"] if c["name"] == channel), None)
            return channel_id is not None and db.execute(
                "SELECT 1 FROM grants WHERE principal_id=? AND channel_id=?", (principal.id, channel_id)).fetchone() is not None
        except (ControlError, sqlite3.Error, OSError, ValueError, TypeError, IndexError, KeyError):
            return False
        finally:
            if close and db is not None:
                db.close()

    def issue(self, owner_token, agent_id, channel_ids, *, alarm, receipts, expires_at, output, pushover=False,
              publish_id=None):
        self.verify_owner(owner_token)
        _id(agent_id)
        if (not isinstance(channel_ids, (list, tuple)) or not channel_ids
                or any(not isinstance(c, str) or not ID_RE.fullmatch(c) for c in channel_ids)
                or len(set(channel_ids)) != len(channel_ids)):
            raise ControlError("invalid channel grants")
        if type(alarm) is not bool or type(receipts) is not bool or type(pushover) is not bool:
            raise ControlError("invalid principal settings")
        if expires_at is not None:
            try:
                if _expiry(expires_at) <= datetime.now(timezone.utc):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ControlError("invalid expiry") from None
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        token = "".join(secrets.choice(alphabet) for _ in range(30)) if pushover else secrets.token_urlsafe(32)
        principal_id = "principal_" + secrets.token_hex(16)
        output = Path(output)
        _safe_directory(output.parent)
        db = None
        fd = -1
        created = False
        committing = False
        try:
            db = self.connect()
            db.execute("BEGIN IMMEDIATE")
            contract = self._live_contract(db)
            if agent_id not in {x["id"] for x in contract["agents"]}:
                raise ControlError("unknown agent")
            known = {x["id"]: x["name"] for x in contract["channels"]}
            if any(c not in known for c in channel_ids):
                raise ControlError("invalid channel grants")
            if alarm != any(known[c] == "alarm" for c in channel_ids):
                raise ControlError("alarm requires both the alarm channel and explicit alarm grant")
            try:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise ControlError("cannot create token output") from None
            created = True
            os.fchmod(fd, 0o600)
            db.execute("INSERT INTO principals(id,workspace_id,agent_id,token_digest,expires_at,receipts,alarm) "
                       "VALUES(?,?,?,?,?,?,?)", (principal_id, self.workspace_id, agent_id, digest_token(token), expires_at,
                                               int(receipts), int(alarm)))
            db.executemany("INSERT INTO grants VALUES(?,?)", [(principal_id, c) for c in channel_ids])
            _safe_file(self.path)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                fd = -1
                f.write(token + "\n")
                f.flush()
                os.fsync(f.fileno())
            _sync_directory(output.parent)
            if publish_id is not None:
                publish_id(principal_id)
            committing = True
            db.commit()
            return principal_id
        except BaseException:
            try:
                if db is not None and db.in_transaction:
                    db.rollback()
            finally:
                if fd != -1:
                    os.close(fd)
                # A failed commit can be ambiguous; retain its durable output for reconciliation.
                if created and not committing:
                    output.unlink(missing_ok=True)
            raise
        finally:
            if db is not None:
                db.close()

    def revoke(self, owner_token, principal_id):
        self.verify_owner(owner_token)
        _id(principal_id)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._live_contract(db)
            changed = db.execute("UPDATE principals SET revoked=1 WHERE id=? AND workspace_id=?",
                                 (principal_id, self.workspace_id)).rowcount
            if not changed:
                db.rollback()
                raise ControlError("principal not found")
            _safe_file(self.path)
            db.commit()
