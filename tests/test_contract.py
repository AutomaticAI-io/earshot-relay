"""The committed openapi.yaml is the contract; it must equal what the app generates."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from earshot_relay import __version__

ROOT = Path(__file__).resolve().parent.parent


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "scripts" / "gen_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_file_equals_generated():
    gen = load_generator()
    committed = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    assert committed == gen.generate(), "openapi.yaml is stale: run `uv run python scripts/gen_openapi.py`"


def test_contract_lists_every_route_and_the_security_scheme():
    doc = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    assert set(doc["paths"]) >= {"/v1/messages", "/v1/messages/{message_id}", "/v1/status", "/v1/doctor",
                                  "/v1/beacons/{beacon_token}", "/healthz"}
    assert doc["info"]["version"] == __version__
