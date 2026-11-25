"""Service-free checks for workspace control and protected credential issuance."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import load_settings, ConfigError
from earshot_relay.control import Control, ControlError
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer
from earshot_relay.store import Store
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


def test_issuance_auth_and_revocation(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    token_file = directory / "agent-token"
    ident = control.issue(TOKEN, "agent1", ["normal"], alarm=False, receipts=True, expires_at=None, output=token_file)
    token = token_file.read_text().strip()
    assert token_file.stat().st_mode & 0o777 == 0o600
    principal = control.authenticate(token)
    assert principal is not None and principal.id == ident
    assert control.permits(principal, "phone", receipt=True)
    assert not control.permits(principal, "alarm")
    assert token.encode() not in (directory / "control.db").read_bytes()
    control.revoke(TOKEN, ident)
    assert control.authenticate(token) is None


def test_init_refuses_foreign_binding_and_refs(tmp_path):
    directory, settings, contract = rig(tmp_path)
    contract["devices"][0]["workspace_id"] = "other"
    with pytest.raises(ControlError):
        Control.initialize(directory / "control.db", settings, contract, TOKEN)
    assert not (directory / "control.db").exists()
    contract["devices"][0]["workspace_id"] = "ws"
    contract["workspace"]["bucket"] = "other"
    with pytest.raises(ControlError):
        Control.initialize(directory / "control.db", settings, contract, TOKEN)


def test_existing_output_is_not_overwritten(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "exists"
    target.write_text("keep")
    with pytest.raises(ControlError):
        control.issue(TOKEN, "agent1", ["normal"], alarm=False, receipts=True, expires_at=None, output=target)
    assert target.read_text() == "keep"
    with sqlite3.connect(directory / "control.db") as db:
        assert db.execute("select count(*) from principals").fetchone()[0] == 0


def test_reserved_replies_rejected_at_config(tmp_path):
    env = base_env("http://127.0.0.1:9", str(tmp_path), EARSHOT_RELAY_CHANNELS="phone,replies")
    with pytest.raises(ConfigError):
        load_settings(env, check_ffmpeg=False)


def test_scoped_requests_fail_closed_until_integration(tmp_path):
    directory, settings, contract = rig(tmp_path)
    control = Control.initialize(directory / "control.db", settings, contract, TOKEN)
    target = directory / "agent-token"
    control.issue(TOKEN, "agent1", ["normal"], alarm=False, receipts=True, expires_at=None, output=target)
    token = target.read_text().strip()
    store = Store(str(tmp_path / "relay.db"))
    try:
        relay = Relay(settings, store, S3Writer(settings))
        with TestClient(create_app(relay)) as client:
            response = client.post("/v1/messages", headers={"Authorization": "Bearer " + token}, json={"text": "x"})
            assert response.status_code == 403
            assert client.get("/v1/status", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200
    finally:
        store.close()
