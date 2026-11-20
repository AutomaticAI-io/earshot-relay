"""The HTTP API (FastAPI). Auth, error schema, request parsing, the MCP mount and the optional beacon listener."""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import RECEIPT_WORDING, __version__
from .beacons import MAX_BODY_BYTES as BEACON_MAX_BYTES
from .beacons import validate_beacon
from .keys import AUDIO_EXTENSIONS, ApiError, audio_extension
from .service import Relay

log = logging.getLogger("earshot_relay")

MAX_JSON_BYTES = 256 * 1024
MULTIPART_OVERHEAD = 64 * 1024
BEACON_PATH_RE = re.compile(r"^/v1/beacons/[^/]+$")
TEXT_FIELDS = {"text", "channel", "speech", "voice"}

RECEIPT_STATUS_DOC = (
    "stored | detected | started | finished | skipped | failed | missed | unconfirmed. "
    "A receipt records what the phone did (device playback); it is never proof that a person heard the message."
)

# ---- schemas (the OpenAPI contract) ----------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(description="Stable snake_case error code")
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class TextMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", title="TextMessageRequest")
    text: str = Field(description="Spoken text. Outer whitespace is trimmed; 1 to 4000 UTF-16 code units "
                                  "(an emoji outside the BMP counts 2), at most 64 KiB UTF-8.")
    channel: str | None = Field(None, description="Channel (first key segment). Default: the relay's default channel. "
                                                  "Must be in EARSHOT_RELAY_CHANNELS.")
    speech: Literal["device", "server"] | None = Field(
        None, description="device (default): write a .say that the phone speaks with Android TTS. "
                          "server: synthesize on the relay's OpenAI-compatible TTS and upload audio.")
    voice: str | None = Field(None, description="Server TTS voice (speech=server only).")


class AudioMessageRequest(BaseModel):
    model_config = ConfigDict(title="AudioMessageRequest")
    file: bytes = Field(json_schema_extra={"format": "binary"},
                        description=f"Audio file: {', '.join('.' + e for e in AUDIO_EXTENSIONS)}. The extension comes "
                                    "from the file name, else from the part's Content-Type.")
    channel: str | None = Field(None, description="Channel. Default: the relay's default channel.")


class MessageAccepted(BaseModel):
    id: str = Field(description="Message id, msg_<32 hex>")
    type: Literal["text", "audio"]
    speech: Literal["device", "server"] | None = Field(description="null for audio")
    channel: str
    status: Literal["stored"] = Field(description="Stored in the bucket; delivery to the phone is asynchronous")
    created_at: str
    key: str | None = Field(description="Object key <channel>/<32 hex>.<ext> (opaque; never derived from the text)")
    receipt_url: str


class HopModel(BaseModel):
    phase: Literal["detected", "started", "finished", "failed", "missed"]
    device: str
    at: str = Field(description="Phone clock, as the phone reported it")
    at_server: str = Field(description="at minus clock_skew_ms: the hop on the S3 server clock")
    received_at: str = Field(description="Relay clock when the beacon arrived")
    clock_skew_ms: int | None = Field(description="Phone clock minus S3 server clock, as the phone estimated it")
    since_upload_ms: int | None = Field(description="at_server minus the object's LastModified")
    error: str | None = Field(description="Device error text; 'skipped' on a finished hop means the user skipped it")


class DeviceStateModel(BaseModel):
    device: str
    status: Literal["detected", "started", "finished", "skipped", "failed", "missed"]
    app_version: str


StatusLiteral = Literal["stored", "detected", "started", "finished", "skipped", "failed", "missed", "unconfirmed"]


class ReceiptSummary(BaseModel):
    id: str
    type: Literal["text", "audio"]
    speech: Literal["device", "server"] | None
    channel: str
    key: str | None
    created_at: str
    status: StatusLiteral = Field(description=RECEIPT_STATUS_DOC)
    status_at: str | None
    object_last_modified: str | None = Field(description="The object's LastModified as the phone reported it")
    devices: list[DeviceStateModel]
    hint: str | None


class Receipt(ReceiptSummary):
    model_config = ConfigDict(title="Receipt")
    hops: list[HopModel] = Field(description="One hop per (device, phase), earliest kept, ordered by server time")


class MessageList(BaseModel):
    items: list[ReceiptSummary]
    next_since: str | None = Field(description="Pass as since= to fetch messages newer than this page")


class StatusRelay(BaseModel):
    version: str


class StatusBucket(BaseModel):
    endpoint_host: str
    name: str
    last_write_ok_at: str | None


class StatusSpeech(BaseModel):
    default: Literal["device", "server"]
    server_configured: bool


class StatusDevice(BaseModel):
    device: str
    last_seen: str = Field(description="Relay clock when this device's latest beacon arrived")
    app_version: str
    last_phase: str
    clock_skew_ms: int | None


class StatusReceipts(BaseModel):
    configured: bool
    beacon_url: str | None = Field(description="Paste into the app's 'Optional HTTPS beacon endpoint'. Contains the "
                                               "beacon token: shown only here, never logged. null when "
                                               "EARSHOT_RELAY_PUBLIC_URL is unset.")
    beacon_path: str | None = Field(description="The beacon path (with token) to append to your public https origin")
    separate_beacon_port: int | None
    last_beacon_at: str | None
    devices: list[StatusDevice] = Field(description="Last beacon per device. Buyer builds send no heartbeat, so this "
                                                    "is not liveness.")
    note: str


class StatusLimits(BaseModel):
    rate_limit: str | None
    alarm_rate_limit: str | None
    max_audio_bytes: int
    receipt_wait_min: int
    retention_days: int


class StatusResponse(BaseModel):
    relay: StatusRelay
    bucket: StatusBucket
    channels: list[str]
    default_channel: str
    speech: StatusSpeech
    receipts: StatusReceipts
    limits: StatusLimits


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class DoctorWrite(BaseModel):
    ok: bool
    key: str
    error: str | None


class DoctorResponse(BaseModel):
    ok: bool = Field(description="The inert test write succeeded")
    relay: StatusRelay
    write: DoctorWrite
    checks: list[DoctorCheck]
    beacon_url: str | None
    beacon_path: str | None


class Health(BaseModel):
    ok: bool


class BeaconRequest(BaseModel):
    model_config = ConfigDict(title="Beacon")
    channel: str
    key: str | None = Field(None, description="Required for every phase except heartbeat")
    phase: Literal["detected", "started", "finished", "failed", "missed", "heartbeat"]
    at: str = Field(description="RFC 3339, phone clock")
    last_modified: str | None = Field(None, description="RFC 3339 object LastModified; required except for heartbeat")
    device: str
    app_version: str
    clock_skew_ms: int | None = None
    error: str | None = None


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {c: {"model": ErrorResponse} for c in codes}


# ---- middleware ------------------------------------------------------------------------------------------------


def error_json(status: int, code: str, message: str, field: str | None = None,
               headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if field:
        body["field"] = field
    return JSONResponse({"error": body}, status_code=status, headers=headers)


class BearerAuth:
    """Every route except /healthz and the beacon route needs `Authorization: Bearer <EARSHOT_RELAY_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/healthz" or BEACON_PATH_RE.match(path):
            return await self.app(scope, receive, send)
        presented = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                presented = value.strip()
                break
        if not hmac.compare_digest(presented, self.token):
            scope.setdefault("state", {})["auth_failed"] = True
            resp = error_json(401, "unauthorized", "missing or invalid bearer token",
                              headers={"WWW-Authenticate": 'Bearer realm="earshot-relay"'})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


class AccessLog:
    """One JSON line per request: method, path template, status, ms, message id. Never a raw path, body or token."""

    STATIC = {"/healthz", "/mcp", "/openapi.json"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None)
            if path is None:
                raw = scope.get("path", "")
                path = raw if raw in self.STATIC else ("<unauthorized>" if scope.get("state", {}).get("auth_failed")
                                                       else "<unmatched>")
            entry = {"event": "http", "method": scope.get("method"), "path": path, "status": status["code"],
                     "ms": round((time.perf_counter() - start) * 1000, 1)}
            message_id = scope.get("state", {}).get("message_id")
            if message_id:
                entry["message_id"] = message_id
            log.info(json.dumps(entry, separators=(",", ":")))


# ---- request helpers -------------------------------------------------------------------------------------------


class _BodyTooLarge(Exception):
    pass


async def read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise ApiError(413, "too_large", f"request body is over {cap} bytes")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise ApiError(413, "too_large", f"request body is over {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return error_json(exc.status, exc.code, exc.message, exc.field, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError):
        errs = exc.errors()
        first = errs[0] if errs else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("query", "path", "body", "header")]
        field = loc[-1] if loc else None
        return error_json(422, "invalid_field", str(first.get("msg", "invalid request"))[:200], field)

    codes = {400: "bad_request", 401: "unauthorized", 404: "not_found", 405: "method_not_allowed", 413: "too_large"}

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        code = codes.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else (
            "not found" if exc.status_code == 404 else "request refused")
        return error_json(exc.status_code, code, str(message)[:200], headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _crash(_request: Request, exc: Exception):
        log.error(json.dumps({"event": "crash", "error": type(exc).__name__}))
        return error_json(500, "internal_error", "the relay hit an unexpected error")


# ---- routes ----------------------------------------------------------------------------------------------------


def _beacon_route(app: FastAPI, relay: Relay) -> None:
    @app.post(
        "/v1/beacons/{beacon_token}",
        tags=["relay"],
        summary="Receive one phone beacon (the app's 'Optional HTTPS beacon endpoint')",
        description=(
            "The Earshot app POSTs one JSON beacon per playback phase. The token in the path is "
            "EARSHOT_RELAY_BEACON_TOKEN (the app sends no auth header). A wrong token is 404. Valid beacons are "
            "stored, including keys the relay did not issue; a repeated beacon is a no-op 200. "
            "Beacons record device playback, never that a person heard anything."
        ),
        status_code=200,
        response_class=Response,
        responses={200: {"description": "Stored (empty body)"}, **_errors(400, 404, 413)},
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": BeaconRequest.model_json_schema()}}}},
    )
    async def post_beacon(request: Request, beacon_token: str):
        expected = relay.settings.beacon_token
        if not expected or not hmac.compare_digest(beacon_token.encode(), expected.encode()):
            raise ApiError(404, "not_found", "not found")
        raw = await read_capped(request, BEACON_MAX_BYTES)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "body is not JSON") from None
        clean, why = validate_beacon(data)
        if clean is None:
            raise ApiError(400, "invalid_beacon", why or "invalid beacon")
        relay.ingest_beacon(clean)
        return Response(status_code=200)


def _health_route(app: FastAPI) -> None:
    @app.get("/healthz", tags=["core"], response_model=Health, summary="Liveness (no auth)")
    async def healthz():
        return {"ok": True}


def _api_routes(app: FastAPI, relay: Relay) -> None:
    @app.post(
        "/v1/messages",
        status_code=202,
        response_model=MessageAccepted,
        tags=["core"],
        summary="Send text or audio to the phone",
        description=(
            "JSON body: text (written as a .say the phone speaks, or synthesized on the relay with speech=server). "
            "multipart/form-data: an audio file. The relay finishes PutObject (and TTS) before it answers, so 202 "
            "means stored in the bucket; delivery to the phone is asynchronous. Location points at the receipt."
        ),
        responses={
            202: {"headers": {"Location": {"description": "/v1/messages/{id}", "schema": {"type": "string"}}}},
            **_errors(400, 401, 403, 413, 415, 422, 429, 502, 504),
        },
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": TextMessageRequest.model_json_schema()},
            "multipart/form-data": {"schema": AudioMessageRequest.model_json_schema()},
        }}},
    )
    async def post_message(request: Request, response: Response):
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media == "application/json" or media.endswith("+json"):
            raw = await read_capped(request, MAX_JSON_BYTES)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "bad_json", "body is not valid JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "body must be a JSON object")
            unknown = sorted(set(data) - TEXT_FIELDS)
            if unknown:
                raise ApiError(422, "unknown_field", f"unknown field '{unknown[0]}' (allowed: text, channel, speech, voice)",
                               unknown[0])
            if "text" not in data:
                raise ApiError(422, "invalid_field", "text is required", "text")
            for name in ("channel", "speech", "voice"):
                if data.get(name) is not None and not isinstance(data[name], str):
                    raise ApiError(422, "invalid_field", f"{name} must be a string", name)
            result = await relay.send_text(text=data["text"], channel=data.get("channel"), speech=data.get("speech"),
                                           voice=data.get("voice"))
        elif media == "multipart/form-data":
            result = await _send_multipart(request, relay)
        else:
            raise ApiError(415, "unsupported_media_type",
                           "send application/json (text) or multipart/form-data (an audio file named 'file')")
        request.state.message_id = result["id"]
        response.headers["Location"] = result["receipt_url"]
        return result

    @app.get(
        "/v1/messages/{message_id}",
        response_model=Receipt,
        tags=["core"],
        summary="The receipt: did the phone play it?",
        description=(
            "message_id may be 'latest'. wait=1..60 long-polls until the status is finished, skipped, failed or missed, "
            "or the time is up. " + RECEIPT_STATUS_DOC + " A message with no beacon after EARSHOT_RELAY_RECEIPT_WAIT_MIN "
            "reads unconfirmed, which can still change if a beacon the phone parked while offline arrives."
        ),
        responses=_errors(401, 404, 422),
    )
    async def get_message(request: Request, message_id: str,
                          wait: int = Query(0, ge=0, le=60, description="Seconds to long-poll for a terminal status")):
        receipt = await relay.wait_receipt(message_id, wait) if wait else relay.receipt(message_id)
        request.state.message_id = receipt["id"]
        return receipt

    @app.get(
        "/v1/messages",
        response_model=MessageList,
        tags=["core"],
        summary="Recent messages from the relay's index (newest first; oldest first after since)",
        responses=_errors(401, 404, 422),
    )
    async def list_messages(
        channel: str | None = Query(None, description="Only this channel"),
        limit: int = Query(20, ge=1, le=200),
        since: str | None = Query(None, description="RFC 3339 time or message id (exclusive). Switches to oldest first."),
    ):
        return relay.list_messages(channel=channel, limit=limit, since=since)

    @app.get("/v1/status", response_model=StatusResponse, tags=["core"], summary="Relay, bucket, channels and receipts",
             responses=_errors(401))
    async def get_status():
        return relay.status()

    @app.post(
        "/v1/doctor",
        response_model=DoctorResponse,
        tags=["relay"],
        summary="Write one inert .txt to the default channel and report configuration sanity",
        description="The phone never plays a lone .txt, so this proves the writer key without making a sound.",
        responses=_errors(401),
    )
    async def post_doctor():
        return await relay.doctor()


async def _send_multipart(request: Request, relay: Relay) -> dict:
    cap = relay.settings.max_audio_bytes + MULTIPART_OVERHEAD
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise ApiError(413, "too_large", f"the upload is over EARSHOT_RELAY_MAX_AUDIO_BYTES ({relay.settings.max_audio_bytes})",
                       "file")
    total = 0

    async def capped_receive():
        nonlocal total
        message = await request.receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > cap:
                raise _BodyTooLarge()
        return message

    limited = Request(request.scope, receive=capped_receive)
    try:
        form = await limited.form(max_files=1, max_fields=4)
    except _BodyTooLarge:
        raise ApiError(413, "too_large", f"the upload is over EARSHOT_RELAY_MAX_AUDIO_BYTES ({relay.settings.max_audio_bytes})",
                       "file") from None
    try:
        unknown = sorted(set(form.keys()) - {"file", "channel"})
        if unknown:
            raise ApiError(422, "unknown_field", f"unknown form field '{unknown[0]}' (allowed: file, channel)", unknown[0])
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, "missing_file", "multipart field 'file' (an audio file) is required", "file")
        channel = form.get("channel")
        if channel is not None and not isinstance(channel, str):
            raise ApiError(422, "invalid_field", "channel must be a text field", "channel")
        ext = audio_extension(upload.filename, upload.content_type)
        size = upload.size if upload.size is not None else _measure(upload)
        return await relay.send_audio(fileobj=upload.file, size=size, ext=ext, channel=channel or None)
    finally:
        await form.close()


def _measure(upload: UploadFile) -> int:
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


# ---- app factories ---------------------------------------------------------------------------------------------


def _base_app(title: str, lifespan=None, with_openapi: bool = True) -> FastAPI:
    return FastAPI(
        title=title,
        version=__version__,
        description=(
            "Send text or audio to the Earshot Android app through your own S3-compatible bucket, and read back "
            "what the phone did. " + RECEIPT_WORDING
        ),
        license_info={"name": "MIT", "identifier": "MIT"},
        openapi_tags=[
            {"name": "core", "description": "Messages and status: the portable part of the API."},
            {"name": "relay", "description": "Relay-only: the phone's beacon endpoint and doctor."},
        ],
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if with_openapi else None,
        lifespan=lifespan,
    )


def create_app(relay: Relay, *, serve_beacons: bool | None = None, mcp: bool = True) -> FastAPI:
    """The main app. `serve_beacons` defaults to True unless a separate beacon port is configured."""
    from .mcp_server import build_http_mcp

    if serve_beacons is None:
        serve_beacons = relay.settings.beacon_port is None
    mcp_server = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_prune_loop, relay)
            if mcp_server is not None:
                async with mcp_server.session_manager.run():
                    yield
            else:
                yield
            tg.cancel_scope.cancel()

    app = _base_app("earshot-relay", lifespan)
    _install_error_handlers(app)
    _health_route(app)
    _api_routes(app, relay)
    if serve_beacons:
        _beacon_route(app, relay)
    if mcp:
        mcp_server, mcp_routes = build_http_mcp(app, relay)
        app.router.routes.extend(mcp_routes)
    app.add_middleware(BearerAuth, token=relay.settings.token)
    app.add_middleware(AccessLog)
    app.state.relay = relay
    return app


def create_beacon_app(relay: Relay) -> FastAPI:
    """The separate beacon listener (EARSHOT_RELAY_BEACON_PORT): only /v1/beacons/{token} and /healthz."""
    app = _base_app("earshot-relay beacons", with_openapi=False)
    _install_error_handlers(app)
    _health_route(app)
    _beacon_route(app, relay)
    app.add_middleware(AccessLog)
    return app


def openapi_document(relay: Relay) -> dict:
    return create_app(relay, serve_beacons=True, mcp=False).openapi()


async def _prune_loop(relay: Relay) -> None:
    while True:
        try:
            m, b = relay.store.prune(relay.settings.retention_days, relay.clock())
            if m or b:
                log.info(json.dumps({"event": "prune", "messages": m, "beacons": b}))
        except Exception as exc:  # noqa: BLE001 - pruning must never take the relay down
            log.warning(json.dumps({"event": "prune_failed", "error": type(exc).__name__}))
        await anyio.sleep(24 * 3600)
