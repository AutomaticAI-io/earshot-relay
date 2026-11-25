"""Isolated workspace contract and authorization checks."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from dataclasses import replace

import pytest

from earshot_relay.config import load_settings, ConfigError
from earshot_relay.control import Control, ControlError, Principal, canonical_endpoint, digest_token, parse_contract, validate_contract
from .conftest import TOKEN, base_env


def rig(tmp_path):
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    env = base_env("http://127.0.0.1:9", str(tmp_path / "index"), EARSHOT_RELAY_CHANNELS="phone,alarm",
                   EARSHOT_RELAY_CONTROL_DB=str(directory / "control.db"))
    settings = load_settings(env, check_ffmpeg=False)
    contract = {"version": "control.v1", "workspace": {"id": "ws", "endpoint": settings.s3_endpoint,
        "bucket": settings.bucket, "region": settings.region}, "channels": [
            {"id": "normal", "workspace_id": "ws", "name": "phone"},
            {"id": "urgent", "workspace_id": "ws", "name": "alarm"}],
        "agents": [{"id": "agent1", "workspace_id": "ws"}],
        "devices": [{"id": "device1", "workspace_id": "ws"}]}
    return directory, settings, contract


def issue(control, target, *, channels=("normal",), alarm=False, receipts=True, expires_at=None):
    channel_ids = list(channels) if isinstance(channels, (list, tuple)) else channels
    return control.issue(TOKEN, "agent1", channel_ids, alarm=alarm, receipts=receipts, expires_at=expires_at, output=target)


def test_issuance_auth_and_individual_revocation(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target, other = directory / "token", directory / "other"
    ident = issue(control, target)
    other_id = issue(control, other, receipts=False)
    token, other_token = target.read_text().strip(), other.read_text().strip()
    assert target.stat().st_mode & 0o777 == 0o600
    principal, second = control.authenticate(token), control.authenticate(other_token)
    assert principal == Principal("scoped", ident, "ws", "agent1")
    assert second.id == other_id and other_id != ident
    assert control.permits(principal, "phone", receipt=True)
    assert not control.permits(principal, "alarm")
    assert not control.permits(second, "phone", receipt=True)
    assert not control.permits(Principal("scoped", ident, "ws", "wrong"), "phone")
    assert token.encode() not in (directory / "control.db").read_bytes()
    with sqlite3.connect(directory / "control.db") as db:
        assert db.execute("SELECT token_digest FROM principals WHERE id=?", (ident,)).fetchone()[0] == digest_token(token)
    control.revoke(TOKEN, ident)
    assert control.authenticate(token) is None and not control.permits(principal, "phone")
    assert control.permits(second, "phone")


@pytest.mark.parametrize("section,key,value", [
    (None, None, []), (None, "version", None), (None, "channels", {}), (None, "agents", None),
    ("workspace", "id", []), ("workspace", "endpoint", []), ("workspace", "endpoint", "http://["),
    ("workspace", "bucket", "a/b"), ("workspace", "region", {}),
    ("channels", "workspace_id", []), ("channels", "name", {}), ("agents", "id", []),
    ("devices", "workspace_id", "other"),
])
def test_malformed_contracts_return_safe_errors(tmp_path, section, key, value):
    _, settings, contract = rig(tmp_path)
    if key is None:
        contract = value
    elif section is None:
        contract[key] = value
    elif section == "workspace":
        contract[section][key] = value
    else:
        contract[section][0][key] = value
    with pytest.raises(ControlError) as exc:
        validate_contract(contract, settings)
    assert "secret-marker" not in str(exc.value)


def test_closed_objects_references_and_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "schemas/control-v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    _, settings, original = rig(tmp_path)
    jsonschema.validate(original, schema)
    for path in (("root",), ("workspace",), ("channels", 0), ("agents", 0), ("devices", 0)):
        contract = copy.deepcopy(original)
        target = contract if path == ("root",) else contract[path[0]]
        if len(path) == 2:
            target = target[path[1]]
        target["secret-marker-field"] = "secret-marker-value"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(contract, schema)
        with pytest.raises(ControlError) as exc:
            validate_contract(contract, settings)
        assert "secret-marker" not in str(exc.value)
    for collection in ("channels", "agents", "devices"):
        contract = copy.deepcopy(original)
        contract[collection].append(copy.deepcopy(contract[collection][0]))
        with pytest.raises(ControlError):
            validate_contract(contract, settings)
    for text in ('{"version":"control.v1","version":"control.v1"}', '{"secret-marker":NaN}'):
        with pytest.raises(ControlError, match="invalid control contract"):
            parse_contract(text)
    for section, key in (("workspace", "id"), ("workspace", "bucket"), ("channels", "name")):
        contract = copy.deepcopy(original)
        obj = contract[section][0] if section == "channels" else contract[section]
        obj[key] += "\n"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(contract, schema)
        with pytest.raises(ControlError):
            validate_contract(contract, settings)


@pytest.mark.parametrize("endpoint", [None, [], "https://[", "https://user:secret@example.com", "https://host:99999",
    "https://host:0", "https://host/?q=secret", "https://host/#secret", "https://host/../x", "https://host\\bad",
    "https://host/%2fsecret", " https://host", "https://host\n"])
def test_invalid_endpoints_safe(endpoint):
    with pytest.raises(ControlError) as exc:
        canonical_endpoint(endpoint)
    assert "secret" not in str(exc.value)


def test_binding_is_canonical_immutable_and_matches_writer(tmp_path):
    directory, settings, contract = rig(tmp_path)
    contract["workspace"]["endpoint"] = "HTTP://127.0.0.1:9/"
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    assert control.contract["workspace"]["endpoint"] == "http://127.0.0.1:9"
    with sqlite3.connect(directory / "control.db") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE binding SET contract='{}'")
    for override in ({"bucket": "other"}, {"s3_endpoint": "http://127.0.0.1:8"}, {"region": "other"}):
        with pytest.raises(ControlError):
            Control(directory / "control.db", replace(settings, **override))
    assert canonical_endpoint("https://EXAMPLE.com:443/") == "https://example.com"


def test_owner_rotation_without_credential_binding(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "before"
    issue(control, target)
    rotated = replace(settings, token="z" * 40)
    control = Control(directory / "control.db", rotated)
    assert control.authenticate(target.read_text().strip())
    with pytest.raises(ControlError):
        control.revoke(TOKEN, "principal_unknown")
    control.issue(rotated.token, "agent1", ["normal"], alarm=False, receipts=False, expires_at=None,
                  output=directory / "after")
    with sqlite3.connect(directory / "control.db") as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(binding)")] == ["singleton", "contract"]


def test_live_binding_principal_expiry_and_corruption_fail_closed(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "token"
    ident = issue(control, target)
    token = target.read_text().strip()
    p = control.authenticate(token)
    for field, value in (("expires_at", "2001-01-01T00:00:00Z"), ("expires_at", "bad"), ("revoked", 2),
                         ("agent_id", "missing"), ("workspace_id", "other"), ("receipts", 2)):
        with sqlite3.connect(directory / "control.db") as db:
            db.execute(f"UPDATE principals SET {field}=? WHERE id=?", (value, ident))
        assert control.authenticate(token) is None
        assert not control.permits(p, "phone")
        with sqlite3.connect(directory / "control.db") as db:
            db.execute("UPDATE principals SET expires_at=NULL,revoked=0,agent_id='agent1',workspace_id='ws',receipts=1")
    with sqlite3.connect(directory / "control.db") as db:
        db.execute("DROP TRIGGER binding_no_update")
        db.execute("UPDATE binding SET contract='{}'")
    assert control.authenticate(token) is None and not control.permits(p, "phone")
    with pytest.raises(ControlError):
        issue(control, directory / "another")


def test_permissions_symlinks_and_sidecars_refuse(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "token"
    issue(control, target)
    token = target.read_text().strip()
    principal = control.authenticate(token)
    dbpath = directory / "control.db"
    dbpath.chmod(0o644)
    assert control.authenticate(token) is None and not control.permits(principal, "phone")
    dbpath.chmod(0o600)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = directory / ("control.db" + suffix)
        sidecar.symlink_to(target)
        assert control.authenticate(token) is None and not control.permits(principal, "phone")
        sidecar.unlink()
        sidecar.write_text("x")
        sidecar.chmod(0o644)
        assert control.authenticate(token) is None
        sidecar.unlink()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ControlError):
        Control(link / "control.db", settings)
    directory.chmod(0o750)
    assert control.authenticate(token) is None
    directory.chmod(0o700)
    os.link(dbpath, directory / "hardlink")
    assert control.authenticate(token) is None
    (directory / "hardlink").unlink()
    with pytest.raises(ControlError):
        Control(link / ".." / "control" / "control.db", settings)


def test_unknown_grants_output_conflicts_and_cleanup(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    existing = directory / "existing"
    existing.write_text("keep")
    for channels in (("missing",), ("normal", "normal"), ([],)):
        with pytest.raises(ControlError):
            issue(control, directory / "new", channels=channels)
    with pytest.raises(ControlError):
        issue(control, existing)
    assert existing.read_text() == "keep"
    with pytest.raises(OSError):
        control.issue(TOKEN, "agent1", ["normal"], alarm=False, receipts=True, expires_at=None,
                      output=directory / "new", publish_id=lambda _: (_ for _ in ()).throw(OSError("stdout")))
    assert not (directory / "new").exists()
    with sqlite3.connect(directory / "control.db") as db:
        assert db.execute("select count(*) from principals").fetchone()[0] == 0


def test_reserved_replies_rejected_at_config(tmp_path):
    env = base_env("http://127.0.0.1:9", str(tmp_path), EARSHOT_RELAY_CHANNELS="phone,replies")
    with pytest.raises(ConfigError):
        load_settings(env, check_ffmpeg=False)


def test_revoke_waits_for_immediate_put_guard(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    tokenfile = directory / "token"
    ident = issue(control, tokenfile)
    principal = control.authenticate(tokenfile.read_text().strip())
    db = control.connect()
    assert not control.permits(principal, "phone", db=db)
    db.execute("BEGIN IMMEDIATE")
    assert control.permits(principal, "phone", db=db)
    started, finished = threading.Event(), threading.Event()
    errors = []

    def revoke():
        started.set()
        try:
            control.revoke(TOKEN, ident)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=revoke)
    thread.start()
    assert started.wait(1)
    assert not finished.wait(0.1)
    assert control.permits(principal, "phone", db=db)
    db.commit()
    db.close()
    thread.join(5)
    assert finished.is_set() and not errors
    assert not control.permits(principal, "phone")


def test_sidecar_modes_under_permissive_umask_and_wal(tmp_path):
    directory, settings, contract = rig(tmp_path)
    old_mask = os.umask(0)
    try:
        control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
        with control.connect() as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO principals VALUES('principal_test','ws','agent1',?,NULL,0,1,0)", ("a" * 64,))
            for suffix in ("-wal", "-shm"):
                assert (directory / ("control.db" + suffix)).stat().st_mode & 0o777 == 0o600
            db.rollback()
        issue(control, directory / "token")
        assert (directory / "token").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_mask)


def test_output_fsync_failure_rolls_back_and_removes_file(tmp_path, monkeypatch):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    def failed(_):
        raise OSError("private-output-fixture")
    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError):
        issue(control, directory / "token")
    assert not (directory / "token").exists()
    with sqlite3.connect(directory / "control.db") as db:
        assert db.execute("SELECT count(*) FROM principals").fetchone()[0] == 0


def test_ambiguous_commit_retains_durable_credential(tmp_path, monkeypatch):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    connect = control.connect

    class CommitError:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def commit(self):
            self.db.commit()
            raise sqlite3.OperationalError("simulated post-commit failure")

    monkeypatch.setattr(control, "connect", lambda: CommitError(connect()))
    ids = []
    with pytest.raises(sqlite3.OperationalError):
        control.issue(TOKEN, "agent1", ["normal"], alarm=False, receipts=False, expires_at=None,
                      output=directory / "token", publish_id=ids.append)
    monkeypatch.setattr(control, "connect", connect)
    principal = control.authenticate((directory / "token").read_text().strip())
    assert principal is not None and principal.id == ids[0]
    control.revoke(TOKEN, ids[0])
    assert control.authenticate((directory / "token").read_text().strip()) is None


def test_unrecognized_external_db_transaction_cannot_authorize(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "token"
    issue(control, target)
    principal = control.authenticate(target.read_text().strip())
    other_dir = directory / "other"
    other_dir.mkdir(mode=0o700)
    other = Control.initialize(other_dir / "control.db", settings, contract, TOKEN)
    with other.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert not control.permits(principal, "phone", db=db)


def test_corrupt_database_refuses_runtime_authentication(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "token"
    issue(control, target)
    token = target.read_text().strip()
    principal = control.authenticate(token)
    (directory / "control.db").write_bytes(b"not a database")
    assert control.authenticate(token) is None
    assert not control.permits(principal, "phone")
    with pytest.raises(ControlError):
        Control(directory / "control.db", settings)
