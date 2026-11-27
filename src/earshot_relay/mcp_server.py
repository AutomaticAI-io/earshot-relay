"""The MCP server: say, alarm (only when the relay allows it), receipt, recent and status.

Both transports use the same REST API: the `/mcp` route calls the app in-process (httpx ASGITransport), and the
stdio server (`earshot-relay mcp`) calls a relay over HTTP with EARSHOT_API_URL and EARSHOT_API_TOKEN. There is
deliberately no file or audio tool: a prompt-injected agent must not be able to read local files into the bucket.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import RECEIPT_WORDING, __version__
from .auth import current_token
from .beacons import receipt_is_final
from .keys import is_valid_channel


class _CapabilitiesNotFound(ToolError):
    pass


def _legacy_capabilities(data: dict) -> dict:
    # The 1.1.2 status schema is owner-only; never return its beacon or bucket details. 1.1.3 ships the
    # same schema, so exactly these versions are accepted; anything else fails closed.
    from .api import StatusResponse

    try:
        status = StatusResponse.model_validate(data, strict=True)
    except ValidationError:
        raise ToolError("the relay returned an unexpected legacy status") from None
    channels = status.channels
    if (status.relay.version not in {"1.1.2", "1.1.3"} or not channels or len(set(channels)) != len(channels)
            or not all(is_valid_channel(c) for c in channels) or status.default_channel not in channels):
        raise ToolError("the relay returned an unexpected legacy status")
    return {"channels": channels, "default_channel": status.default_channel}

INSTRUCTIONS = (
    "Earshot reads text aloud on the user's Android phone: the relay stores a message in the user's own bucket and the "
    "Earshot app, when it is listening, plays it. A successful send means stored, not heard. Keep spoken text short and plain: one or two sentences, no markdown, code, URLs or long "
    "lists. Never put secrets, passwords, tokens or private data in spoken text: it is stored in the bucket and spoken "
    "out loud. Use alarm (when present) only for urgency the user asked for. " + RECEIPT_WORDING
)

SAY_DESCRIPTION = (
    "Send a short message to be spoken aloud on the user's phone through Earshot. The text is stored in the user's "
    "bucket; if the Earshot app is listening it should speak it within seconds (Android text-to-speech by default, at "
    "the phone's media volume; held while the phone is on a call). A successful call means stored, not played. Keep "
    "it to one or two plain sentences. Returns the message id; call receipt with it to learn whether the phone played "
    "it. Not for the alarm channel: urgent alerts use the alarm tool, which exists only when the user enabled it. "
    "Hygiene options: normalize=true strips emoji, markdown and URLs before storing (links become the word 'link'). "
    "tag with coalesce_window_s holds the write that many seconds and writes only the latest text sent under that tag, so a "
    "burst of updates is spoken once (status 'coalescing' until it is written; the id is the one the message will have). "
    "tag with cooldown_s drops a repeat of the tag inside the cooldown (status 'suppressed', nothing written). Use "
    "them for progress updates and flapping alerts, not for one-off messages."
)

ALARM_DESCRIPTION = (
    "LOUD urgent alert on the user's phone. The app plays a siren, then the message on the alarm stream at high "
    "volume. It usually gets through Do Not Disturb (not 'Total silence') and plays during a phone call, where the "
    "other party may hear it, unless the user turned off 'Alarms can interrupt calls'. It can still be silenced by "
    "Total-silence Do Not Disturb, an Android refusal, a stopped service, a powered-off phone, battery restrictions or "
    "no network, so never tell the user it is certain to wake them. It is not an emergency or life-safety service. "
    "Use it only for urgency the user asked to be interrupted for (a production outage, a failed deploy they are "
    "waiting on). Never for routine status: use say. Rate limited by the relay."
)

RECEIPT_DESCRIPTION = (
    "Did the phone play it? Returns the device-playback receipt of a message (id from say or alarm, or 'latest'): "
    "coalescing (held, not yet written), write_failed (the held message could not be written: nothing is stored, send it again), stored, detected, started, finished, skipped (the user skipped it), failed, missed (not played: it went stale, the "
    "bucket changed or audio was unavailable) or unconfirmed (no device report yet). wait_seconds (0-60) waits for a final status; "
    "`final` says whether waiting longer could still change it. A finished receipt means the phone finished playing "
    "it, not that a person heard it."
)

SUMMARIES = {
    "coalescing": "Held for coalescing: nothing is in the bucket yet; it is written when the window closes.",
    "write_failed": "The relay could not write this held message to the bucket after retries: nothing is stored and "
                    "nothing will play. Send it again.",
    "stored": "Stored in the bucket; no device has reported it yet.",
    "detected": "The phone saw the message and queued it.",
    "started": "The phone started playing it.",
    "finished": "The phone finished playing it (device playback; not proof a person heard it).",
    "skipped": "The phone started it but the user skipped it.",
    "failed": "The phone could not play it.",
    "missed": "The phone did not play it: it went stale (too old, or held too long for a call), audio was "
              "unavailable, or the bucket changed. See the hop error.",
    "unconfirmed": "No device has reported this message within the wait window; it may still report later.",
}


class Backend:
    """REST client used by the tools. Errors become ToolError with the relay's error code and message."""

    def __init__(self, client: httpx.AsyncClient, *, caller_context: bool = False):
        self.client = client
        self.caller_context = caller_context

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        if self.caller_context:
            token = current_token.get()
            if not token:
                raise ToolError("unauthorized: caller credential is unavailable")
            kwargs["headers"] = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self.client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ToolError("the relay did not answer in time") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"the relay could not be reached ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            if method == "GET" and path == "/v1/capabilities" and resp.status_code == 404:
                raise _CapabilitiesNotFound("capabilities endpoint is unavailable")
            err = (data or {}).get("error", {}) if isinstance(data, dict) else {}
            code = err.get("code", f"http_{resp.status_code}")
            message = err.get("message", "request refused")
            raise ToolError(f"{code}: {message}")
        if not isinstance(data, dict):
            raise ToolError("the relay returned an unexpected response")
        return data

    async def send(self, body: dict) -> dict:
        return await self._call("POST", "/v1/messages", json={k: v for k, v in body.items() if v is not None})

    async def receipt(self, message_id: str, wait: int) -> dict:
        if message_id != "latest" and not re.fullmatch(r"msg_[0-9a-f]{32}", message_id):
            raise ToolError("not_found: no message with that id")
        params = {"wait": wait} if wait else None
        return await self._call("GET", f"/v1/messages/{message_id}", params=params, timeout=wait + 15)

    async def recent(self, limit: int, channel: str | None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if channel:
            params["channel"] = channel
        return await self._call("GET", "/v1/messages", params=params)

    async def status(self) -> dict:
        return await self._call("GET", "/v1/status")

    async def capabilities(self) -> dict:
        try:
            return await self._call("GET", "/v1/capabilities")
        except _CapabilitiesNotFound:
            return _legacy_capabilities(await self.status())


def _sent(result: dict) -> dict[str, Any]:
    status = result["status"]
    out: dict[str, Any] = {
        "id": result["id"],
        "status": status,
        "channel": result["channel"],
        "speech": result.get("speech"),
    }
    for name in ("tag", "write_at", "cooldown_until"):
        if result.get(name):
            out[name] = result[name]
    if result.get("normalized"):
        out["normalized"] = True
    if result.get("truncated"):
        out["truncated"] = True
    if status == "coalescing":
        out["next"] = (f"held until {result.get('write_at')} and then written with the latest text sent under tag "
                       f"'{result.get('tag')}'; nothing is in the bucket yet. Call receipt with id {result['id']} "
                       "afterwards to see whether the phone played it")
    elif status == "suppressed":
        out["next"] = (f"nothing was written: tag '{result.get('tag')}' is in its cooldown until "
                       f"{result.get('cooldown_until')}. The id is the earlier message's; its receipt shows what the "
                       "phone did with that one")
    else:
        out["next"] = f"call receipt with id {result['id']} (wait_seconds up to 60) to see whether the phone played it"
    return out


class CallerMCPServer(MCPServer):
    def __init__(self, backend: Backend, **kwargs):
        self.backend = backend
        super().__init__(**kwargs)

    async def _handle_call_tool(self, ctx, params):
        if self._tool_manager.get_tool(params.name) is None:
            return CallToolResult(content=[TextContent(type="text", text="unknown tool")], is_error=True)
        return await super()._handle_call_tool(ctx, params)

    async def call_tool(self, name, arguments, context=None):
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError("unknown tool")
        allowed = set(tool.parameters.get("properties", {}))
        if not isinstance(arguments, dict) or set(arguments) - allowed:
            raise ToolError("invalid tool arguments")
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError):
                raise ToolError("tool execution failed") from None
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError("invalid tool arguments") from None
            raise
        except Exception:
            raise ToolError("tool execution failed") from None

    async def list_tools(self):
        tools = await super().list_tools()
        if not any(tool.name == "alarm" for tool in tools):
            return tools
        try:
            allowed = "alarm" in (await self.backend.capabilities()).get("channels", [])
        except ToolError:
            allowed = False
        return [tool for tool in tools if tool.name != "alarm" or allowed]


def build_mcp(backend: Backend, *, alarm_allowed: bool) -> MCPServer:
    mcp = CallerMCPServer(backend, name="earshot", title="Earshot", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING",
                          website_url="https://store.automaticai.io/earshot/")

    @mcp.tool(description=SAY_DESCRIPTION,
              annotations=ToolAnnotations(title="Say on phone", readOnlyHint=False, destructiveHint=False,
                                          idempotentHint=False, openWorldHint=True))
    async def say(text: str, channel: str | None = None, speech: str | None = None, voice: str | None = None,
                   normalize: bool | None = None, tag: str | None = None, coalesce_window_s: float | None = None,
                   cooldown_s: float | None = None, coalesce_s: float | None = None) -> dict[str, Any]:
        """text: what to speak. channel: optional, default the relay's default. speech: device or server. voice: server
        only. normalize: strip emoji, markdown and URLs. tag + coalesce_window_s: write only the latest text sent under the tag
        within that many seconds (coalesce_s is the 1.1.0 name of coalesce_window_s). tag + cooldown_s: drop a repeat of the tag inside that many seconds."""
        if channel == "alarm" or (channel is None and (await backend.capabilities()).get("default_channel") == "alarm"):
            hint = "use the alarm tool" if alarm_allowed else "alarm is not enabled on this relay"
            raise ToolError(f"say does not send to the alarm channel; {hint}")
        return _sent(await backend.send({"text": text, "channel": channel, "speech": speech, "voice": voice,
                                         "normalize": normalize, "tag": tag, "coalesce_window_s": coalesce_window_s,
                                         "cooldown_s": cooldown_s, "coalesce_s": coalesce_s}))

    if alarm_allowed:
        @mcp.tool(description=ALARM_DESCRIPTION,
                  annotations=ToolAnnotations(title="LOUD alarm on phone", readOnlyHint=False, destructiveHint=False,
                                              idempotentHint=False, openWorldHint=True))
        async def alarm(text: str) -> dict[str, Any]:
            """text: the short urgent message to speak after the siren."""
            return _sent(await backend.send({"text": text, "channel": "alarm"}))

    @mcp.tool(description=RECEIPT_DESCRIPTION,
              annotations=ToolAnnotations(title="Playback receipt", readOnlyHint=True, openWorldHint=False))
    async def receipt(id: str = "latest", wait_seconds: int = 0) -> dict[str, Any]:
        """id: message id or 'latest'. wait_seconds: 0-60."""
        wait = max(0, min(60, int(wait_seconds)))
        r = await backend.receipt(id, wait)
        return {
            "id": r["id"],
            "status": r["status"],
            "final": receipt_is_final(r),
            "summary": SUMMARIES.get(r["status"], r["status"]),
            "channel": r["channel"],
            "created_at": r["created_at"],
            "status_at": r.get("status_at"),
            "hops": [{"phase": h["phase"], "device": h["device"], "at_server": h["at_server"],
                      "since_upload_ms": h["since_upload_ms"], "error": h["error"]} for h in r.get("hops", [])],
            "hint": r.get("hint"),
            "note": RECEIPT_WORDING,
        }

    @mcp.tool(description="List recent messages sent through this relay with their playback status (newest first).",
              annotations=ToolAnnotations(title="Recent messages", readOnlyHint=True, openWorldHint=False))
    async def recent(limit: int = 10, channel: str | None = None) -> dict[str, Any]:
        """limit: 1-50. channel: optional filter."""
        data = await backend.recent(max(1, min(50, int(limit))), channel)
        return {"items": [{"id": i["id"], "type": i["type"], "channel": i["channel"], "created_at": i["created_at"],
                           "status": i["status"]} for i in data["items"]], "note": RECEIPT_WORDING}

    @mcp.tool(description="Relay status: sendable channels, speech modes, and the last beacon from each phone.",
              annotations=ToolAnnotations(title="Relay status", readOnlyHint=True, openWorldHint=False))
    async def status() -> dict[str, Any]:
        s = await backend.status()
        receipts = dict(s["receipts"])
        receipts.pop("beacon_url", None)  # contains the beacon token; an agent has no use for it
        receipts.pop("beacon_path", None)
        return {"relay": s["relay"], "channels": s["channels"], "default_channel": s.get("default_channel"),
                "speech": s["speech"], "receipts": receipts}

    return mcp


def build_http_mcp(app, relay) -> tuple[MCPServer, list]:
    """The `/mcp` route forwards the authenticated request's credential in-process."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://earshot-relay.internal",
        timeout=httpx.Timeout(120),
    )
    mcp = build_mcp(Backend(client, caller_context=True), alarm_allowed=relay.settings.alarm_allowed)
    # Bearer auth on every request already blocks DNS-rebinding style access, and the Host header is whatever the
    # user's tunnel or proxy sends, so the SDK's localhost-only Host check is off.
    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return mcp, list(starlette_app.routes)


def run_stdio(api_url: str, token: str) -> None:
    """`earshot-relay mcp`: a stdio MCP server backed by a relay's HTTP API."""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"earshot-relay-mcp/{__version__}"}
    alarm_allowed = False
    try:
        with httpx.Client(base_url=api_url, headers=headers, timeout=10) as c:
            resp = c.get("/v1/capabilities")
            if resp.status_code == 404:
                legacy = c.get("/v1/status")
                if legacy.status_code == 200:
                    alarm_allowed = "alarm" in _legacy_capabilities(legacy.json())["channels"]
            elif resp.status_code == 200:
                data = resp.json()
                channels = data.get("channels") if isinstance(data, dict) else None
                alarm_allowed = isinstance(channels, list) and "alarm" in channels
    except (httpx.HTTPError, ValueError, TypeError, ToolError):
        pass  # tools still register; each call reports the connection problem
    client = httpx.AsyncClient(base_url=api_url, headers=headers, timeout=httpx.Timeout(120))
    mcp = build_mcp(Backend(client), alarm_allowed=alarm_allowed)
    mcp.run(transport="stdio")
