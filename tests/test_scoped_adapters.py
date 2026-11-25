"""Scoped ingress tests with an in-memory writer and a private local control database."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

import earshot_relay.mcp_server as mcp_module

from earshot_relay.api import create_app, create_compat_app
from earshot_relay.config import load_settings
from earshot_relay.control import Control
from earshot_relay.mcp_server import Backend, build_mcp
from mcp.server.mcpserver.exceptions import ToolError
from earshot_relay.keys import is_valid_channel, new_key
from earshot_relay.service import Relay
from earshot_relay.store import Store
from .conftest import TOKEN, base_env


class Writer:
    def __init__(self):
        self.objects = {}

    def put(self, key, body, content_type):
        self.objects[key] = body if isinstance(body, bytes) else body.read()


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    env = base_env("http://127.0.0.1:9", str(tmp_path / "data"),
                   EARSHOT_RELAY_CHANNELS="phone,kitchen,alarm", EARSHOT_RELAY_COMPAT="all",
                   EARSHOT_RELAY_COMPAT_ALARM="1", EARSHOT_RELAY_COMPAT_PORT="8082",
                   EARSHOT_RELAY_COMPAT_TOKEN="c" * 40, EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN="p" * 30,
                   EARSHOT_RELAY_RATE_LIMIT="off", EARSHOT_RELAY_ALARM_RATE_LIMIT="off",
                   EARSHOT_RELAY_CONTROL_DB=str(private / "control.db"))
    settings = load_settings(env, check_ffmpeg=False)
    contract = {"version": "control.v1", "workspace": {"id": "ws", "endpoint": settings.s3_endpoint,
                "bucket": settings.bucket, "region": settings.region},
                "channels": [{"id": name, "name": name, "workspace_id": "ws"}
                             for name in ("phone", "kitchen", "alarm")],
                "agents": [{"id": "agent", "workspace_id": "ws"}], "devices": []}
    control = Control.initialize(private / "control.db", settings, contract, TOKEN)
    tokens, ids = {}, {}
    def issue(name, channels, receipts=True, alarm=False):
        target = private / name
        ids[name] = control.issue(TOKEN, "agent", channels, alarm=alarm, receipts=receipts,
                                  expires_at=None, output=target)
        tokens[name] = target.read_text().strip()
    issue("phone", ["phone"])
    issue("other", ["phone"])
    issue("kitchen", ["kitchen"])
    issue("alarm", ["phone", "alarm"], alarm=True)
    issue("alarm_only", ["alarm"], alarm=True)
    issue("no_receipts", ["phone"], receipts=False)
    issue("revoked", ["phone"])
    issue("expired", ["phone"])
    control.revoke(TOKEN, ids["revoked"])
    with control.connect() as db:
        db.execute("UPDATE principals SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (ids["expired"],))
    store = Store(str(tmp_path / "index.db"))
    relay = Relay(settings, store, Writer())
    with ExitStack() as stack:
        main = stack.enter_context(TestClient(create_app(relay), base_url="http://relay.test"))
        root = stack.enter_context(TestClient(create_compat_app(relay), base_url="http://relay.test"))
        yield main, root, relay, tokens
    store.close()


def bearer(token):
    return {"Authorization": "Bearer " + token}


def post(client, token, **data):
    return client.post("/v1/messages", headers=bearer(token), json={"text": "hello", **data})


def test_native_scope_receipts_replay_and_workspace(rig):
    main, _, _, t = rig
    assert main.post("/v1/messages", json={"text": "hello"}).status_code == 401
    for token in ("unknown", t["revoked"], t["expired"], "c" * 40, "p" * 30):
        assert post(main, token).status_code == 401
    assert post(main, t["phone"], channel="kitchen").status_code == 403
    assert post(main, t["phone"], channel="alarm").status_code == 403
    assert post(main, t["phone"], channel="replies").status_code == 403
    assert post(main, TOKEN, channel="replies").status_code == 403
    assert post(main, t["phone"], channel="Phone").status_code == 422
    assert post(main, t["phone"], workspace="foreign").status_code == 403
    assert post(main, t["phone"], workspace="ws").status_code == 202
    duplicated = main.post("/v1/messages", headers={**bearer(t["phone"]), "Content-Type": "application/json"},
                           content='{"text":"x","workspace":"foreign","workspace":"ws"}')
    assert duplicated.status_code == 400
    assert main.post("/v1/messages?workspace=foreign", headers=bearer(t["phone"]), json={"text": "x"}).status_code == 403
    assert main.post("/v1/messages", headers={**bearer(t["phone"]), "X-Earshot-Workspace": "ws"}, json={"text": "x"}).status_code == 202
    a = main.post("/v1/messages", headers={**bearer(t["phone"]), "Idempotency-Key": "shared"}, json={"text": "x"})
    b = main.post("/v1/messages", headers={**bearer(t["other"]), "Idempotency-Key": "shared"}, json={"text": "x"})
    assert a.status_code == b.status_code == 202 and a.json()["id"] != b.json()["id"]
    retry = main.post("/v1/messages", headers={**bearer(t["phone"]), "Idempotency-Key": "shared"}, json={"text": "x"})
    assert retry.json()["id"] == a.json()["id"] and retry.headers["idempotent-replayed"] == "true"
    bad = main.post("/v1/messages", headers={**bearer(t["phone"]), "Idempotency-Key": "shared"}, json={"text": "x", "channel": "kitchen"})
    assert bad.status_code == 403
    for token, expected in ((t["phone"], 200), (t["other"], 404), (t["no_receipts"], 403), (TOKEN, 200)):
        assert main.get("/v1/messages/" + a.json()["id"], headers=bearer(token)).status_code == expected
    receipt_wait = main.get("/v1/messages/" + a.json()["id"], headers=bearer(t["phone"]), params={"wait": 1})
    assert receipt_wait.status_code == 200 and receipt_wait.json()["id"] == a.json()["id"]
    assert main.get("/v1/messages/latest", headers=bearer(t["phone"])).json()["id"] == a.json()["id"]
    assert main.get("/v1/messages", headers=bearer(t["phone"]), params={"since": b.json()["id"]}).status_code == 404
    recent = main.get("/v1/messages", headers=bearer(t["phone"])).json()["items"]
    assert a.json()["id"] in {x["id"] for x in recent} and b.json()["id"] not in {x["id"] for x in recent}
    assert main.get("/v1/status", headers=bearer(t["phone"])).status_code == 403
    assert main.post("/v1/doctor", headers=bearer(t["phone"])).status_code == 403
    assert main.get("/v1/messages", headers=bearer(t["no_receipts"])).status_code == 403
    for name, token in (("compat", "c" * 40), ("pushover", "p" * 30)):
        protocol = "pushover" if name == "pushover" else "gotify"
        response = compat_send(main, "/compat/" + protocol, protocol, token)
        assert response.status_code == 200
        message_id = response.headers["x-earshot-message-id"]
        assert main.get("/v1/messages/" + message_id, headers=bearer(token)).status_code == 401
        assert main.post("/mcp", headers={**MCP_HEADERS, **bearer(token)}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        assert main.get("/v1/messages/" + message_id, headers=bearer(t["phone"])).status_code == 404
    assert main.get("/v1/capabilities", headers=bearer(t["phone"])).json() == {"channels": ["phone"], "default_channel": "phone"}
    assert main.get("/v1/capabilities", headers=bearer(t["kitchen"])).json() == {"channels": ["kitchen"], "default_channel": None}
    assert main.get("/v1/capabilities", headers=bearer(t["phone"])).headers["cache-control"] == "no-store"
    assert post(main, t["kitchen"]).status_code == 403
    assert post(main, t["kitchen"], channel="kitchen").status_code == 202
    owner_headers = {**bearer(TOKEN), "Idempotency-Key": "owner-workspace"}
    assert main.post("/v1/messages", headers=owner_headers, json={"text": "owner"}).status_code == 202
    assert main.post("/v1/messages", headers=owner_headers, json={"text": "x", "workspace": "foreign"}).status_code == 403
    assert main.get("/v1/capabilities", headers=bearer(t["alarm"])).json()["channels"] == ["phone", "alarm"]


def test_owner_replay_rechecks_channel_before_json_and_audio(rig, monkeypatch):
    main, _, relay, _ = rig
    syntheses = []
    class TTS:
        async def synthesize(self, *args, **kwargs):
            syntheses.append((args, kwargs))
            raise AssertionError("replay must not synthesize")
    relay.tts = TTS()
    headers = {**bearer(TOKEN), "Idempotency-Key": "owner-channel"}
    first = main.post("/v1/messages", headers=headers, json={"text": "first", "channel": "phone"})
    assert first.status_code == 202
    before = dict(relay.writer.objects)
    for channel, status in (("replies", 403), ("Phone", 422), ("missing", 403), ("", 422)):
        response = main.post("/v1/messages", headers=headers, json={"text": "ignored", "channel": channel, "speech": "server"})
        assert response.status_code == status
        response = main.post("/v1/messages", headers=headers, data={"channel": channel},
                             files={"file": ("sound.wav", b"RIFF", "audio/wav")})
        assert response.status_code == status
        assert relay.writer.objects == before
    ambiguous = main.post("/v1/messages", headers=headers,
                          files=[("channel", (None, "phone")), ("channel", (None, "replies")),
                                 ("file", ("sound.wav", b"RIFF", "audio/wav"))])
    assert ambiguous.status_code == 403 and relay.writer.objects == before
    for payload in ({"text": "retry", "channel": "phone"}, {"text": "retry"}):
        retry = main.post("/v1/messages", headers=headers, json=payload)
        assert retry.status_code == 202 and retry.json()["id"] == first.json()["id"]
        assert retry.headers["idempotent-replayed"] == "true"
    retry = main.post("/v1/messages", headers=headers, data={"channel": "phone"},
                      files={"file": ("sound.wav", b"RIFF", "audio/wav")})
    assert retry.status_code == 202 and retry.json()["id"] == first.json()["id"]
    monkeypatch.setattr(relay, "settings", replace(relay.settings, default_channel="replies"))
    assert main.post("/v1/messages", headers=headers, json={"text": "retry"}).status_code == 403
    assert main.post("/v1/messages", headers=headers, files={"file": ("sound.wav", b"RIFF", "audio/wav")}).status_code == 403
    assert relay.writer.objects == before and not syntheses


@pytest.mark.parametrize("root", [False, True])
def test_ntfy_topic_json_workspace_cannot_be_spoken(rig, root):
    main, compat, relay, tokens = rig
    client, prefix = (compat, "") if root else (main, "/compat/ntfy")
    before = dict(relay.writer.objects)
    syntheses = []
    class TTS:
        async def synthesize(self, *args, **kwargs):
            syntheses.append((args, kwargs))
            raise AssertionError("workspace rejection must not synthesize")
    relay.tts = TTS()
    for suffix in ("/phone", "/phone/publish"):
        for selector in ("workspace", "workspace_id"):
            response = client.post(prefix + suffix, headers=bearer(tokens["phone"]),
                                   json={selector: "foreign", "message": "fixture"})
            assert response.status_code == 403
            assert relay.writer.objects == before
        response = client.post(prefix + suffix, headers=bearer(tokens["phone"]),
                               json={"workspace": "ws", "message": "fixture"})
        assert response.status_code == 400
        assert relay.writer.objects == before
        for encoding, media in (("utf-8-sig", "application/json"), ("utf-16", "application/json"),
                                ("utf-8-sig", "text/plain")):
            payload = json.dumps({"workspace": "foreign", "message": "fixture"}).encode(encoding)
            response = client.post(prefix + suffix, headers={**bearer(tokens["phone"]), "Content-Type": media},
                                   content=payload)
            assert response.status_code == 403 and relay.writer.objects == before
        duplicated = '{"workspace":"foreign","workspace":"ws","message":"fixture"}'
        for media in ("application/json", "text/plain"):
            response = client.post(prefix + suffix, headers={**bearer(tokens["phone"]), "Content-Type": media},
                                   content=duplicated)
            assert response.status_code == 400
            assert relay.writer.objects == before
        assert not syntheses
        response = client.post(prefix + suffix, headers=bearer(tokens["phone"]),
                               content="plain ntfy message")
        assert response.status_code == 200
        assert next(reversed(relay.writer.objects.values())) == b"plain ntfy message"
        before = dict(relay.writer.objects)


def test_scoped_adapters_never_fall_back_without_core(rig, monkeypatch):
    main, root, relay, tokens = rig
    monkeypatch.setattr(relay, "check_replay", None)
    assert post(main, tokens["phone"]).status_code == 503
    assert compat_send(root, "", "gotify", tokens["phone"]).status_code == 503
    assert main.get("/v1/messages/latest", headers=bearer(tokens["phone"])).status_code == 503


def test_audio_and_redaction(rig, caplog):
    main, _, _, t = rig
    h = bearer(t["phone"])
    assert main.post("/v1/messages", headers=h, data={"channel": "phone"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")}).status_code == 202
    assert main.post("/v1/messages", headers=h, data={"channel": "kitchen"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")}).status_code == 403
    first = main.post("/v1/messages", headers={**h, "Idempotency-Key": "audio"}, data={"channel": "phone"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")})
    replay = main.post("/v1/messages", headers={**h, "Idempotency-Key": "audio"}, data={"channel": "phone"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")})
    assert first.json()["id"] == replay.json()["id"] and replay.headers["idempotent-replayed"] == "true"
    assert main.post("/v1/messages", headers={**h, "Idempotency-Key": "audio"}, data={"channel": "kitchen"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")}).status_code == 403
    assert main.post("/v1/messages", headers=h, data={"workspace_id": "foreign"}, files={"file": ("ok.wav", b"RIFF", "audio/wav")}).status_code == 403
    for response in (main.post("/v1/messages", headers=h, json={"text": "x", t["phone"]: 1}),
                     main.post("/v1/messages", headers=h, files={t["phone"]: ("x", b"x")}),
                     main.post("/v1/messages", headers=h, files={"file": ("x." + t["phone"], b"x")})):
        assert t["phone"] not in response.text
    assert t["phone"] not in caplog.text
    assert not is_valid_channel("replies")
    with pytest.raises(ValueError):
        new_key("replies", "say")


def compat_send(client, prefix, protocol, token, high=False, topic="phone", workspace=None):
    query = {"workspace": workspace} if workspace else {}
    if protocol == "ntfy":
        return client.post(prefix + "/" + topic, headers={**bearer(token), "Priority": "5" if high else "3"}, params=query, content="hi")
    if protocol == "gotify":
        return client.post(prefix + "/message", headers={"X-Gotify-Key": token}, params=query, json={"message": "hi", "priority": 8 if high else 1})
    if protocol == "pushover":
        return client.post(prefix + "/1/messages.json", params=query, data={"token": token, "message": "hi", "priority": 2 if high else 0})
    return client.get(prefix + "/_ah/api/messaging/v1/sendPush", params={**query, "apikey": token, "text": "hi"})


@pytest.mark.parametrize("protocol", ["ntfy", "gotify", "pushover", "join"])
@pytest.mark.parametrize("root", [False, True])
def test_compat_scopes(rig, protocol, root):
    main, compat, relay, t = rig
    client = compat if root else main
    prefix = "" if root else "/compat/" + protocol
    assert compat_send(client, prefix, protocol, t["phone"]).status_code == 200
    assert compat_send(client, prefix, protocol, t["kitchen"]).status_code == 403
    assert compat_send(client, prefix, protocol, t["phone"], workspace="foreign").status_code == 403
    if protocol == "ntfy":
        assert client.post(prefix or "/", headers=bearer(t["phone"]), json={"topic": "phone", "message": "hi", "workspace": "foreign"}).status_code == 403
    elif protocol == "gotify":
        assert client.post(prefix + "/message", headers={"X-Gotify-Key": t["phone"]}, json={"message": "hi", "workspace": "foreign"}).status_code == 403
    elif protocol == "pushover":
        assert client.post(prefix + "/1/messages.json", data={"token": t["phone"], "message": "hi", "workspace": "foreign"}).status_code == 403
    else:
        path = prefix + "/_ah/api/messaging/v1/sendPush"
        assert client.post(path, data={"apikey": t["phone"], "text": "hi", "workspace": "foreign"}).status_code == 403
        assert client.post(path, params={"workspace": "foreign"}, data={"apikey": t["phone"], "text": "hi", "workspace": "ws"}).status_code == 403
    for name in ("revoked", "expired"):
        assert compat_send(client, prefix, protocol, t[name]).status_code == 401
    assert compat_send(client, prefix, protocol, "c" * 40).status_code == 200
    if protocol != "join":
        downgraded = compat_send(client, prefix, protocol, t["phone"], high=True)
        assert downgraded.status_code == 200 and downgraded.headers["x-earshot-downgraded"] == "alarm"
        promoted = compat_send(client, prefix, protocol, t["alarm"], high=True)
        assert promoted.status_code == 200 and promoted.headers["x-earshot-promoted"] == "alarm"
        assert compat_send(client, prefix, protocol, t["alarm_only"], high=True).status_code == 200
        limited = relay.alarm_rate_limited
        relay.alarm_rate_limited = lambda: True
        try:
            assert compat_send(client, prefix, protocol, t["alarm_only"], high=True).status_code == 403
        finally:
            relay.alarm_rate_limited = limited
    if protocol == "ntfy":
        assert compat_send(client, prefix, protocol, t["phone"], topic="kitchen").status_code == 403
        basic = "Basic " + base64.b64encode(("user:" + t["phone"]).encode()).decode()
        assert client.post(prefix + "/phone", headers={"Authorization": basic}, content="hi").status_code == 200
        query = base64.urlsafe_b64encode(("Bearer " + t["phone"]).encode()).decode()
        assert client.get(prefix + "/phone/publish", params={"auth": query, "message": "hi"}).status_code == 200
        assert client.post(prefix + "/phone", headers={**bearer(t["phone"]), "Filename": "ok.wav"}, content=b"RIFF").status_code == 200
        assert client.post(prefix + "/kitchen", headers={**bearer(t["phone"]), "Filename": "ok.wav"}, content=b"RIFF").status_code == 403
        def retry(token):
            return client.post(prefix + "/phone", headers={**bearer(token), "Idempotency-Key": "compat-key"}, content="hi")
        first, replay, foreign = retry(t["phone"]), retry(t["phone"]), retry(t["other"])
        assert first.headers["x-earshot-message-id"] == replay.headers["x-earshot-message-id"]
        assert replay.headers["idempotent-replayed"] == "true"
        assert foreign.headers["x-earshot-message-id"] != first.headers["x-earshot-message-id"]


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

def rpc(client, token, method, params):
    r = client.post("/mcp", headers={**MCP_HEADERS, **bearer(token)}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_mcp_capabilities_legacy_status_fallback(rig):
    _, _, relay, _ = rig
    legacy = relay.status()
    calls = []
    def handler(request):
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/capabilities":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=legacy)
    async def probe():
        async with httpx.AsyncClient(base_url="http://fixture.invalid", headers=bearer(TOKEN),
                                     transport=httpx.MockTransport(handler)) as client:
            assert await Backend(client).capabilities() == {"channels": ["phone", "kitchen", "alarm"],
                                                            "default_channel": "phone"}
    asyncio.run(probe())
    assert calls == [("/v1/capabilities", "Bearer " + TOKEN), ("/v1/status", "Bearer " + TOKEN)]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_mcp_capabilities_denial_never_probes_legacy_status(status):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"error": {"code": "denied", "message": "request refused"}})
    async def probe():
        async with httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ToolError):
                await Backend(client).capabilities()
    asyncio.run(probe())
    assert calls == ["/v1/capabilities"]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_mcp_legacy_status_requires_authenticated_owner_response(status):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/v1/capabilities" else httpx.Response(status)
    async def probe():
        async with httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ToolError):
                await Backend(client).capabilities()
    asyncio.run(probe())
    assert calls == ["/v1/capabilities", "/v1/status"]


@pytest.mark.parametrize("legacy", [
    {}, {"channels": ["alarm"], "default_channel": "alarm"},
    {"channels": ["phone", "alarm"], "default_channel": "phone", "relay": {"version": "1.1.2"}},
    {"relay": {"version": "99.0.0"}, "channels": ["phone", "alarm"], "default_channel": "phone"},
])
def test_mcp_capabilities_rejects_unknown_legacy_status_schema(legacy):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/v1/capabilities" else httpx.Response(200, json=legacy)
    async def probe():
        async with httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ToolError):
                await Backend(client).capabilities()
    asyncio.run(probe())
    assert calls == ["/v1/capabilities", "/v1/status"]


def test_mcp_capabilities_valid_schema_still_rejects_unknown_version_and_invalid_channels(rig):
    _, _, relay, _ = rig
    for edit in (
        lambda value: value["relay"].update(version="2.0.0"),
        lambda value: value.update(channels=["phone", "phone"]),
        lambda value: value.update(channels=["phone", "replies"]),
        lambda value: value.update(default_channel="alarm-not-enabled"),
    ):
        status = relay.status()
        edit(status)
        def handler(request):
            return httpx.Response(404) if request.url.path == "/v1/capabilities" else httpx.Response(200, json=status)
        async def probe():
            async with httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(ToolError):
                    await Backend(client).capabilities()
        asyncio.run(probe())


def test_mcp_caller_identity_and_concurrency(rig, caplog):
    caplog.set_level("INFO", logger="mcp.server.mcpserver.server")
    main, _, _, t = rig
    assert "alarm" not in {x["name"] for x in rpc(main, t["phone"], "tools/list", {})["tools"]}
    assert "alarm" in {x["name"] for x in rpc(main, t["alarm"], "tools/list", {})["tools"]}
    def send(token, channel):
        return rpc(main, token, "tools/call", {"name": "say", "arguments": {"text": "hi", "channel": channel}})
    jobs = [(t["phone"], "phone", False), (t["phone"], "kitchen", True),
            (t["kitchen"], "kitchen", False), (t["kitchen"], "phone", True)] * 4
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: send(item[0], item[1]), jobs))
    assert [x["isError"] for x in results] == [j[2] for j in jobs]
    assert rpc(main, t["phone"], "tools/call", {"name": "status", "arguments": {}})["isError"]
    written = rpc(main, t["phone"], "tools/call", {"name": "say", "arguments": {"text": "own"}})["structuredContent"]["id"]
    assert not rpc(main, t["phone"], "tools/call", {"name": "receipt", "arguments": {"id": written}})["isError"]
    assert rpc(main, t["other"], "tools/call", {"name": "receipt", "arguments": {"id": written}})["isError"]
    assert rpc(main, TOKEN, "tools/call", {"name": "receipt", "arguments": {"id": "../status?"}})["isError"]
    assert rpc(main, t["no_receipts"], "tools/call", {"name": "recent", "arguments": {}})["isError"]
    assert rpc(main, t["kitchen"], "tools/call", {"name": "say", "arguments": {"text": "default denied"}})["isError"]
    foreign = main.post("/mcp", headers={**MCP_HEADERS, **bearer(t["phone"])}, json={"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": "say", "arguments": {"text": "x", "workspace": "foreign"}}})
    assert foreign.status_code == 403
    same = main.post("/mcp", headers={**MCP_HEADERS, **bearer(t["phone"])}, json={"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": "say", "arguments": {"text": "x", "workspace": "ws"}}})
    assert same.status_code == 200 and same.json()["result"]["isError"]
    assert rpc(main, t["phone"], "tools/call", {"name": "alarm", "arguments": {"text": "x"}})["isError"]
    malformed = rpc(main, t["phone"], "tools/call", {"name": "recent", "arguments": {"limit": t["phone"]}})
    assert malformed["isError"] and t["phone"] not in json.dumps(malformed)
    malformed = rpc(main, t["phone"], "tools/call", {"name": t["phone"], "arguments": {}})
    assert malformed["isError"] and t["phone"] not in json.dumps(malformed)
    async def crash(*args, **kwargs):
        raise RuntimeError(t["phone"])

    backend = Backend(httpx.AsyncClient())
    backend.send = crash
    mcp = build_mcp(backend, alarm_allowed=False)

    async def probe():
        try:
            with pytest.raises(ToolError) as exc:
                await mcp.call_tool("say", {"text": "x", "channel": "phone"})
            assert "tool execution failed" in str(exc.value)
            assert t["phone"] not in str(exc.value)
        finally:
            await backend.client.aclose()

    asyncio.run(probe())
    assert t["phone"] not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 500])
def test_stdio_alarm_discovery_never_falls_back_on_denial(rig, monkeypatch, status):
    _, _, relay, _ = rig
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, path):
            calls.append(path)
            if path == "/v1/status":
                return httpx.Response(200, json=relay.status())
            return httpx.Response(status)
    class Server:
        def run(self, **kwargs):
            pass
    seen = []
    def build(backend, *, alarm_allowed):
        seen.append(alarm_allowed)
        asyncio.run(backend.client.aclose())
        return Server()
    monkeypatch.setattr(mcp_module.httpx, "Client", Client)
    monkeypatch.setattr(mcp_module, "build_mcp", build)
    mcp_module.run_stdio("http://fixture.invalid", TOKEN)
    assert calls == ["/v1/capabilities"] and seen == [False]


def test_documented_admin_fd_and_exclusive_output(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    owner = private / "owner"
    owner.write_text(TOKEN + "\n")
    owner.chmod(0o600)
    contract = {"version": "control.v1", "workspace": {"id": "home", "endpoint": "http://127.0.0.1:9",
                "bucket": "earshot-test", "region": "us-east-1"},
                "channels": [{"id": "phone", "name": "phone", "workspace_id": "home"}],
                "agents": [{"id": "assistant", "workspace_id": "home"}], "devices": []}
    (private / "contract.json").write_text(json.dumps(contract))
    env = {**os.environ, **base_env("http://127.0.0.1:9", str(tmp_path / "data")),
           "PRIVATE": str(private), "OWNER_FILE": str(owner), "PYTHON": sys.executable,
           "EARSHOT_RELAY_CONTROL_DB": str(private / "control.db"), "EARSHOT_RELAY_TOKEN_FILE": str(owner)}
    del env["EARSHOT_RELAY_TOKEN"]
    def command(text):
        return subprocess.run(["/bin/sh", "-c", 'exec "$PYTHON" -m earshot_relay admin ' + text],
                              env=env, capture_output=True, text=True, timeout=15)
    init = command('init --contract "$PRIVATE/contract.json" --owner-fd 3 3<"$OWNER_FILE"')
    assert init.returncode == 0 and TOKEN not in init.stdout + init.stderr
    create_cmd = ('create --agent assistant --channel phone --receipts '
                  '--output "$PRIVATE/assistant.token" --owner-fd 3 3<"$OWNER_FILE"')
    created = command(create_cmd)
    assert created.returncode == 0 and created.stdout.startswith("principal_")
    tokenfile = private / "assistant.token"
    token = tokenfile.read_text().strip()
    assert tokenfile.stat().st_mode & 0o777 == 0o600
    assert token not in created.stdout + created.stderr and TOKEN not in created.stdout + created.stderr
    control = Control(private / "control.db", load_settings(env, check_ffmpeg=False))
    assert control.permits(control.authenticate(token), "phone", receipt=True)
    assert command(create_cmd).returncode != 0 and tokenfile.read_text().strip() == token
    env["PRINCIPAL_ID"] = created.stdout.strip()
    revoked = command('revoke "$PRINCIPAL_ID" --owner-fd 3 3<"$OWNER_FILE"')
    assert revoked.returncode == 0 and TOKEN not in revoked.stdout + revoked.stderr
    assert control.authenticate(token) is None


def test_stdio_owner_legacy_relay_capabilities(tmp_path):
    from starlette.responses import JSONResponse

    settings = load_settings(base_env("http://127.0.0.1:9", str(tmp_path),
                                      EARSHOT_RELAY_CHANNELS="phone,alarm",
                                      EARSHOT_RELAY_RATE_LIMIT="off", EARSHOT_RELAY_ALARM_RATE_LIMIT="off"),
                             check_ffmpeg=False)
    store = Store(str(tmp_path / "index.db"))
    relay = Relay(settings, store, Writer())
    app = create_app(relay)
    requests = []

    @app.middleware("http")
    async def legacy_only(request, call_next):
        if request.url.path in ("/v1/capabilities", "/v1/status"):
            requests.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/capabilities":
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        env = {**os.environ, "EARSHOT_API_URL": f"http://127.0.0.1:{port}", "EARSHOT_API_TOKEN": TOKEN}
        proc = subprocess.Popen([sys.executable, "-m", "earshot_relay", "mcp"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
        reader.start()
        def request(method, params=None):
            msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(lines.get(timeout=15))["result"]
        try:
            request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "legacy-test", "version": "1"}})
            assert "alarm" in {x["name"] for x in request("tools/list")["tools"]}
            assert not request("tools/call", {"name": "status", "arguments": {}})["isError"]
            default = request("tools/call", {"name": "say", "arguments": {"text": "normal fixture"}})
            alarm = request("tools/call", {"name": "alarm", "arguments": {"text": "urgent fixture"}})
            assert not default["isError"] and default["structuredContent"]["channel"] == "phone"
            assert not alarm["isError"] and alarm["structuredContent"]["channel"] == "alarm"
            assert len(relay.writer.objects) == 2
            assert ("/v1/capabilities", "Bearer " + TOKEN) in requests
            assert ("/v1/status", "Bearer " + TOKEN) in requests
            server_default = relay.settings
            relay.settings = replace(server_default, default_channel="alarm")
            refused = request("tools/call", {"name": "say", "arguments": {"text": "not urgent"}})
            assert refused["isError"] and len(relay.writer.objects) == 2
            relay.settings = server_default
        finally:
            proc.stdin.close()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
            reader.join(5)
            proc.stdout.close()
            proc.stderr.close()
    finally:
        server.should_exit = True
        thread.join(10)
        store.close()
        assert not thread.is_alive()


def test_stdio_scoped_capability_and_identity(rig):
    _, _, relay, tokens = rig
    api_relay = create_app(relay)
    server = uvicorn.Server(uvicorn.Config(api_relay, host="127.0.0.1", port=0,
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        for token, alarm in ((tokens["phone"], False), (tokens["alarm"], True)):
            env = {**os.environ, "EARSHOT_API_URL": f"http://127.0.0.1:{port}", "EARSHOT_API_TOKEN": token}
            proc = subprocess.Popen([sys.executable, "-m", "earshot_relay", "mcp"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
            reader.start()
            def request(method, params=None):
                msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                return json.loads(lines.get(timeout=15))["result"]
            try:
                request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
                tools = {x["name"] for x in request("tools/list")["tools"]}
                assert ("alarm" in tools) == alarm
                allowed = request("tools/call", {"name": "say", "arguments": {"text": "hello", "channel": "phone"}})
                denied = request("tools/call", {"name": "say", "arguments": {"text": "hello", "channel": "kitchen"}})
                assert not allowed["isError"] and denied["isError"]
                assert request("tools/call", {"name": "status", "arguments": {}})["isError"]
            finally:
                proc.stdin.close()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
                reader.join(5)
                proc.stdout.close()
                proc.stderr.close()
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
