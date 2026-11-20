"""MCP over streamable HTTP (mounted at /mcp) and over stdio (`earshot-relay mcp`)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app

from .conftest import BUCKET, TOKEN

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}}


def rpc(client, method, params=None, id_=2, headers=None):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", headers={**MCP_HEADERS, **(headers or {"Authorization": f"Bearer {TOKEN}"})}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def call(client, name, arguments):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments})["result"]


@pytest.fixture
def mcp_client(relay):
    with TestClient(create_app(relay), base_url="http://relay.example.com") as c:
        yield c


def test_initialize(mcp_client):
    res = rpc(mcp_client, "initialize", INIT["params"], id_=1)["result"]
    assert res["serverInfo"]["name"] == "earshot"
    assert res["serverInfo"]["version"] == "1.0.0"
    assert "not proof that a person heard it" in res["instructions"]
    assert "Never put secrets" in res["instructions"]


def test_mcp_requires_the_bearer_token(mcp_client):
    r = mcp_client.post("/mcp", headers=MCP_HEADERS, json=INIT)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = mcp_client.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer " + "x" * 40}, json=INIT)
    assert r.status_code == 401


def test_tools_list_without_alarm(mcp_client):
    tools = {t["name"]: t for t in rpc(mcp_client, "tools/list")["result"]["tools"]}
    assert set(tools) == {"say", "receipt", "recent", "status"}
    assert tools["say"]["inputSchema"]["required"] == ["text"]
    assert tools["receipt"]["annotations"]["readOnlyHint"] is True
    assert "not that a person heard it" in tools["receipt"]["description"]
    assert "outputSchema" in tools["say"]


def test_alarm_tool_only_when_allowed(make_relay):
    relay = make_relay(EARSHOT_RELAY_CHANNELS="phone,alarm")
    with TestClient(create_app(relay)) as c:
        tools = {t["name"]: t for t in rpc(c, "tools/list")["result"]["tools"]}
        assert "alarm" in tools
        desc = tools["alarm"]["description"]
        assert "LOUD" in desc and "phone call" in desc and "other party may hear it" in desc
        res = call(c, "alarm", {"text": "Prod is down"})
        assert res["isError"] is False
        assert res["structuredContent"]["channel"] == "alarm"


def test_say_and_receipt(mcp_client, s3_admin):
    res = call(mcp_client, "say", {"text": "Tests are green"})
    assert res["isError"] is False, res
    sent = res["structuredContent"]
    assert sent["status"] == "stored" and sent["channel"] == "phone" and sent["id"].startswith("msg_")
    key = f"phone/{sent['id'][4:]}.say"
    assert s3_admin.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"Tests are green"
    rec = call(mcp_client, "receipt", {"id": sent["id"]})["structuredContent"]
    assert rec["status"] == "stored" and "no beacon" in rec["hint"]
    assert "not proof" in rec["note"]
    recent = call(mcp_client, "recent", {"limit": 5})["structuredContent"]
    assert recent["items"][0]["id"] == sent["id"]
    status = call(mcp_client, "status", {})["structuredContent"]
    assert status["channels"] == ["phone"]
    assert "beacon_url" not in status["receipts"] and "beacon_path" not in status["receipts"]


def test_say_refuses_alarm(mcp_client):
    res = call(mcp_client, "say", {"text": "wake up", "channel": "alarm"})
    assert res["isError"] is True
    assert "alarm" in res["content"][0]["text"]


def test_tool_errors_carry_the_relay_code(mcp_client):
    res = call(mcp_client, "say", {"text": "a" * 4001})
    assert res["isError"] is True and "text_too_long" in res["content"][0]["text"]
    res = call(mcp_client, "receipt", {"id": "msg_" + "0" * 32})
    assert res["isError"] is True and "not_found" in res["content"][0]["text"]


# ---- stdio ------------------------------------------------------------------------------------------------------


def test_stdio_smoke(live_relay):
    env = {**os.environ, "EARSHOT_API_URL": live_relay, "EARSHOT_API_TOKEN": TOKEN}
    proc = subprocess.Popen([sys.executable, "-m", "earshot_relay", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()

    def request(msg, expect=True):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if expect:
            return json.loads(lines.get(timeout=20))

    try:
        init = request(INIT)
        assert init["result"]["serverInfo"]["name"] == "earshot"
        request({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        tools = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in tools["result"]["tools"]} == {"say", "receipt", "recent", "status"}
        said = request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "say", "arguments": {"text": "stdio works"}}})
        assert said["result"]["isError"] is False, said
        assert said["result"]["structuredContent"]["id"].startswith("msg_")
    finally:
        proc.stdin.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
