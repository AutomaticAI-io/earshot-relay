"""Producer-compat ingress: ntfy, Gotify, Pushover and Join requests in, one ordinary relay send out.

A producer that already speaks one of these push APIs switches to Earshot by changing a URL. Each protocol is off
unless EARSHOT_RELAY_COMPAT names it. Every compat send goes through Relay.send_text or Relay.send_audio, so it gets
the same validation, rate limits, Idempotency-Key handling, storage path and receipts as POST /v1/messages.

Every compat route answers with:

- the protocol's own success body and HTTP 200 (the clients treat anything else as a failure and retry). Like the
  native 202, it means stored in the bucket, not played;
- `X-Earshot-Message-Id` and `Location: /v1/messages/{id}`, so the receipt is one GET away;
- `X-Earshot-Ignored`: the request fields Earshot cannot honour (attachments, actions, click URLs, sounds,
  markdown...), named, never dropped silently;
- `X-Earshot-Downgraded: alarm` when the sender asked for its top priority and compat alarm is off or its rate limit
  is used up (the message is stored as a normal item, never refused for it);
- `X-Earshot-Promoted: alarm` when the top priority was stored on the alarm channel;
- `X-Earshot-Status: coalescing|suppressed` when tag/coalesce/cooldown held or dropped the send (the protocol body
  still says 200, which then means accepted, not written), and `X-Earshot-Normalized` / `X-Earshot-Truncated`.

None of these protocols can carry Earshot's hygiene fields, so they ride as `X-Earshot-Tag`, `X-Earshot-Coalesce-Window-S`,
`X-Earshot-Cooldown-S` and `X-Earshot-Normalize` headers (`X-Earshot-Coalesce-S` and `earshot_coalesce_s`, the 1.1.0 names, still work), or the query parameters `earshot_tag`, `earshot_coalesce_window_s`,
`earshot_cooldown_s` and `earshot_normalize` (for producers that can set a URL but not a header). The server-wide
EARSHOT_RELAY_NORMALIZE default applies to every compat send without any of them.

Errors come back in each protocol's own error shape, never truncated text.
"""

from __future__ import annotations

import base64
import binascii
import email.header
import hmac
import json
import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .beacons import parse_rfc3339
from .config import NTFY_TOPIC_RE
from .keys import AUDIO_EXTENSIONS, MAX_TEXT_UTF8_BYTES, MEDIA_TYPE_EXTENSIONS, ApiError
from .service import Relay

log = logging.getLogger("earshot_relay")

PREFIXES = {"ntfy": "/compat/ntfy", "gotify": "/compat/gotify", "pushover": "/compat/pushover", "join": "/compat/join"}
MAX_FORM_BYTES = 256 * 1024
PUSHOVER_MAX_BYTES = 5 * 1024 * 1024 + 64 * 1024  # Pushover's 5 MB attachment limit plus form overhead
IDEMPOTENCY_KEY_RE = re.compile(r"^[\x20-\x7e]{1,255}$")
TERMINAL_PUNCT = ".!?:;…。！？"
FIELD_NAME_RE = re.compile(r"[^A-Za-z0-9_.:-]")
REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Request Entity Too Large",
           415: "Unsupported Media Type", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway",
           504: "Gateway Timeout"}


@dataclass
class Incoming:
    """One compat request, reduced to what Earshot can do with it."""

    title: str = ""
    message: str = ""
    high: bool = False  # the protocol's top priority (ntfy 5, Gotify >= 8, Pushover 2)
    channel: str | None = None  # None: the relay's default channel
    ignored: list[str] = field(default_factory=list)
    audio: tuple[IO[bytes], int, str] | None = None  # (file, size, extension) for an audio attachment

    def ignore(self, *names: str) -> None:
        for n in names:
            clean = FIELD_NAME_RE.sub("", str(n))[:40] or "?"
            if clean not in self.ignored and len(self.ignored) < 20:
                self.ignored.append(clean)

    @property
    def text(self) -> str:
        return spoken(self.title, self.message)


@dataclass
class Delivered:
    result: dict
    headers: dict[str, str]


def spoken(title: str | None, message: str | None) -> str:
    """Title and message as one spoken sentence: "Title. Message." A title already ending in punctuation keeps it;
    a title the message repeats is said once."""
    title, message = (title or "").strip(), (message or "").strip()
    if not title:
        return message
    if not message:
        return title
    if message.casefold().startswith(title.casefold()) and not message[len(title):len(title) + 1].isalnum():
        return message  # "Garage" + "Garage door left open" is said once
    sep = " " if title[-1] in TERMINAL_PUNCT else ". "
    return f"{title}{sep}{message}"


def header_text(value: str) -> str:
    """A header as the producer meant it: UTF-8 bytes (Starlette hands them over as latin-1) or RFC 2047 words."""
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    if "=?" in value:
        try:
            value = str(email.header.make_header(email.header.decode_header(value)))
        except (ValueError, LookupError, UnicodeDecodeError, binascii.Error):
            pass
    return value.strip()


# ---- auth ------------------------------------------------------------------------------------------------------


def token_matches(relay: Relay, presented: str | None, *, pushover: bool = False) -> bool:
    """Constant-time check against the relay token, the send-only compat token (any compat route: it opens nothing
    else) and, on the Pushover route only, the Pushover-shaped token."""
    expected = [relay.settings.token]
    if relay.settings.compat_token:
        expected.append(relay.settings.compat_token)
    if pushover and relay.settings.compat_pushover_token:
        expected.append(relay.settings.compat_pushover_token)
    got = (presented or "").encode("utf-8", "replace")
    ok = False
    for e in expected:
        ok = hmac.compare_digest(got, e.encode()) or ok
    return ok and bool(presented)


def authorization_secret(value: str | None) -> str | None:
    """The secret in an Authorization header: the Bearer token, or the Basic password (any username)."""
    if not value:
        return None
    parts = value.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, rest = parts[0].lower(), parts[1].strip()
    if scheme == "bearer":
        return rest
    if scheme == "basic":
        try:
            decoded = base64.b64decode(rest + "=" * (-len(rest) % 4), validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        return decoded.partition(":")[2] or None
    return None


def ntfy_query_auth(value: str) -> str | None:
    """ntfy's ?auth= parameter: the whole Authorization header value, base64 (URL alphabet, padding optional)."""
    raw = value.strip().rstrip("=")
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
    return None


# ---- request bodies ---------------------------------------------------------------------------------------------


def _media(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


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


async def spool_capped(request: Request, cap: int) -> tuple[IO[bytes], int]:
    """The body in a temporary file (memory up to 1 MiB), for a body that may be an audio attachment."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise ApiError(413, "too_large", f"request body is over {cap} bytes")
    f = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > cap:
                raise ApiError(413, "too_large", f"request body is over {cap} bytes")
            f.write(chunk)
    except BaseException:
        f.close()
        raise
    f.seek(0)
    return f, total


async def _form_from_bytes(request: Request, raw: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return await Request(request.scope, receive).form(max_files=8, max_fields=128)


async def read_fields(request: Request, cap: int) -> tuple[dict[str, Any], list[str]]:
    """A JSON object, a urlencoded form or a multipart form, as {name: value} (first value wins) plus the names of
    file parts. Producers are loose about Content-Type, so a body that looks like JSON is read as JSON."""
    raw = await read_capped(request, cap)
    media = _media(request)
    if not raw.strip():
        return {}, []
    if media == "multipart/form-data":
        try:
            form = await _form_from_bytes(request, raw)
        except Exception:  # noqa: BLE001 - python-multipart raises several types for a broken body
            raise ApiError(400, "bad_request", "the multipart body could not be parsed") from None
        try:
            fields: dict[str, Any] = {}
            files: list[str] = []
            for k, v in form.multi_items():
                if isinstance(v, UploadFile):
                    files.append(k)
                else:
                    fields.setdefault(k, v)
            return fields, files
        finally:
            await form.close()
    if media == "application/json" or media.endswith("+json") or (
            media != "application/x-www-form-urlencoded" and raw.lstrip()[:1] == b"{"):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "bad_json", "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "body must be a JSON object")
        return data, []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, "bad_request", "form body is not UTF-8") from None
    fields = {}
    for k, v in parse_qsl(text, keep_blank_values=True):
        fields.setdefault(k, v)
    return fields, []


def _str_field(data: dict, name: str, *, required: bool = False) -> str:
    value = data.get(name)
    if value is None:
        if required:
            raise ApiError(400, "invalid_field", f"{name} is required", name)
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_field", f"{name} must be a string", name)
    return value


def _int_field(data: dict, name: str) -> int | None:
    value = data.get(name)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ApiError(400, "invalid_priority", f"{name} must be a whole number", name)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,4}\s*", value):
        return int(value)
    raise ApiError(400, "invalid_priority", f"{name} must be a whole number", name)


# ---- delivery (shared by every protocol) --------------------------------------------------------------------------

HYGIENE_NAMES = ("tag", "coalesce_window_s", "coalesce_s", "cooldown_s", "normalize")  # coalesce_s: the 1.1.0 name
FLAG_WORDS = {"1": True, "true": True, "yes": True, "on": True, "0": False, "false": False, "no": False, "off": False}


def hygiene_params(request: Request) -> dict[str, Any]:
    """Earshot's own send options from X-Earshot-* headers or earshot_* query parameters (header wins)."""
    out: dict[str, Any] = {}
    for name in HYGIENE_NAMES:
        raw = request.headers.get("x-earshot-" + name.replace("_", "-"))
        if raw is None:
            raw = request.query_params.get("earshot_" + name)
        if raw is None or not raw.strip():
            continue
        raw = raw.strip()
        if name == "tag":
            out[name] = raw
        elif name == "normalize":
            if raw.lower() not in FLAG_WORDS:
                raise ApiError(400, "invalid_field", "earshot normalize must be true or false", name)
            out[name] = FLAG_WORDS[raw.lower()]
        else:
            try:
                out[name] = float(raw)
            except ValueError:
                raise ApiError(400, "invalid_field", f"earshot {name} must be a number of seconds", name) from None
    return out


async def deliver(relay: Relay, request: Request, incoming: Incoming, protocol: str) -> Delivered:
    idem_header = request.headers.get("idempotency-key")
    if idem_header is not None and not IDEMPOTENCY_KEY_RE.fullmatch(idem_header):
        raise ApiError(400, "invalid_field", "Idempotency-Key must be 1-255 printable ASCII characters",
                       "Idempotency-Key")
    headers: dict[str, str] = {}
    channel = incoming.channel
    if incoming.high and channel != "alarm":
        if not relay.settings.compat_alarm_allowed:
            headers["X-Earshot-Downgraded"] = "alarm"
        elif relay.alarm_rate_limited():
            # Over the alarm budget: a flapping monitor must not lose its alert. Store it as a normal item.
            headers["X-Earshot-Downgraded"] = "alarm"
        else:
            channel = "alarm"
            headers["X-Earshot-Promoted"] = "alarm"
    text = incoming.text if incoming.audio is None else ""
    if incoming.audio is None and not text:
        raise ApiError(400, "empty_text", "there is no title or message to speak", "message")
    hygiene = hygiene_params(request)
    if incoming.audio is not None and hygiene:
        incoming.ignore(*(f"earshot_{n}" for n in hygiene))  # audio is never normalized, tagged or coalesced
        hygiene = {}
    async with relay.idempotency(idem_header) as idem:
        if idem.replay is not None:
            result = idem.replay
            headers["Idempotent-Replayed"] = "true"
        elif incoming.audio is not None:
            fileobj, size, ext = incoming.audio
            result = await relay.send_audio(fileobj=fileobj, size=size, ext=ext, channel=channel, idem_key=idem.digest)
        else:
            try:
                result = await relay.send_text(text=text, channel=channel, idem_key=idem.digest, compat=True, **hygiene)
            except ApiError as exc:
                if exc.field == "text":
                    exc.field = "message"
                raise
    headers["X-Earshot-Message-Id"] = result["id"]
    headers["Location"] = result["receipt_url"]
    if result["status"] != "stored":
        headers["X-Earshot-Status"] = result["status"]
    if result.get("normalized"):
        headers["X-Earshot-Normalized"] = "true"
    if result.get("truncated"):
        headers["X-Earshot-Truncated"] = "true"
    if incoming.ignored:
        headers["X-Earshot-Ignored"] = ", ".join(incoming.ignored)
    request.state.message_id = result["id"]
    request.state.compat = protocol
    if incoming.ignored:
        request.state.ignored = list(incoming.ignored)
    if "X-Earshot-Downgraded" in headers:
        request.state.downgraded = "alarm"
    return Delivered(result, headers)


def _epoch(created_at: str) -> int:
    ts = parse_rfc3339(created_at)
    return int(ts.timestamp()) if ts else int(time.time())


def _retry_headers(exc: ApiError) -> dict[str, str] | None:
    return {"Retry-After": str(exc.retry_after)} if exc.retry_after else None


def _client_status(exc: ApiError) -> int:
    # These protocols have no 422; an invalid field is a 400 to every one of their clients.
    return 400 if exc.status == 422 else exc.status


# ---- ntfy ---------------------------------------------------------------------------------------------------------

NTFY_PRIORITIES = {"1": 1, "min": 1, "2": 2, "low": 2, "3": 3, "default": 3, "4": 4, "high": 4, "5": 5, "max": 5,
                   "urgent": 5}
# Header and query names, as ntfy reads them (server/util.go readParam: headers first, then the query string).
NTFY_PARAMS = {
    "message": ("x-message", "message", "m"),
    "title": ("x-title", "title", "t"),
    "priority": ("x-priority", "priority", "prio", "p"),
    "tags": ("x-tags", "tags", "tag", "ta"),
    "click": ("x-click", "click"),
    "icon": ("x-icon", "icon"),
    "attach": ("x-attach", "attach", "a"),
    "filename": ("x-filename", "filename", "file", "f"),
    "actions": ("x-actions", "actions", "action"),
    "markdown": ("x-markdown", "markdown", "md"),
    "email": ("x-email", "x-e-mail", "email", "e-mail", "mail", "e"),
    "call": ("x-call", "call"),
    "delay": ("x-delay", "delay", "x-at", "at", "x-in", "in"),
    "cache": ("x-cache", "cache"),
    "firebase": ("x-firebase", "firebase"),
    "unifiedpush": ("x-unifiedpush", "unifiedpush", "up"),
    "template": ("x-template", "template", "tpl"),
    "sequence_id": ("x-sequence-id", "sequence-id", "sid"),
    "poll_id": ("x-poll-id", "poll-id"),
}
NTFY_IGNORABLE = ("tags", "click", "icon", "attach", "actions", "markdown", "email", "call", "cache", "firebase",
                  "unifiedpush", "template", "sequence_id", "poll_id")
NTFY_JSON_FIELDS = {"topic", "message", "title", "priority"}
# RFC 9218 `Priority: u=3, i`, which Cloudflare adds in front of a server; ntfy ignores it too.
HTTP_PRIORITY_RE = re.compile(r"^u=\d(\s*,\s*(i|\d))?$")
NTFY_CODES = {400: 40000, 401: 40101, 403: 40301, 404: 40401, 413: 41301, 415: 41501, 429: 42901, 500: 50001,
              502: 50201, 504: 50401}
NTFY_LINKS = {401: "https://ntfy.sh/docs/publish/#authentication", 429: "https://ntfy.sh/docs/publish/#limitations"}


def ntfy_error(exc: ApiError) -> JSONResponse:
    status = 413 if exc.code == "text_too_long" else _client_status(exc)
    code = {"invalid_priority": 40007, "invalid_topic": 40009}.get(exc.code, NTFY_CODES.get(status, status * 100))
    body: dict[str, Any] = {"code": code, "http": status, "error": exc.message}
    if status in NTFY_LINKS:
        body["link"] = NTFY_LINKS[status]
    return JSONResponse(body, status_code=status, headers=_retry_headers(exc))


class NtfyParams:
    """ntfy's parameter lookup: any of a field's header names, then any of its query names."""

    def __init__(self, request: Request, json_body: dict | None = None):
        self.request = request
        self.json = json_body or {}

    def get(self, name: str) -> str:
        if name in self.json and self.json[name] not in (None, "", False, [], {}):
            value = self.json[name]
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return ",".join(value)
            return json.dumps(value)
        names = NTFY_PARAMS[name]
        for n in names:
            raw = self.request.headers.get(n)
            if raw is None:
                continue
            value = header_text(raw)
            if n == "priority" and HTTP_PRIORITY_RE.match(value):
                continue
            if value:
                return value
        for n in names:
            value = self.request.query_params.get(n)
            if value and value.strip():
                return value.strip()
        return ""


def ntfy_priority(raw: object) -> int:
    if raw in (None, "", 0):
        return 3
    if isinstance(raw, bool):
        raise ApiError(400, "invalid_priority", "invalid priority parameter")
    if isinstance(raw, int):
        if 1 <= raw <= 5:
            return raw
        raise ApiError(400, "invalid_priority", "invalid priority parameter")
    value = NTFY_PRIORITIES.get(str(raw).strip().lower())
    if value is None:
        raise ApiError(400, "invalid_priority", "invalid priority parameter")
    return value


def ntfy_authorized(relay: Relay, request: Request) -> bool:
    header = request.headers.get("authorization")
    if not header:
        q = request.query_params.get("auth") or request.query_params.get("authorization")
        header = ntfy_query_auth(q) if q else None
    return token_matches(relay, authorization_secret(header))


def _ntfy_common(params: NtfyParams, incoming: Incoming) -> int:
    if params.get("delay"):
        raise ApiError(400, "delay_unsupported", "Earshot cannot schedule a message (delay/at/in); send it when it "
                                                 "should play")
    priority = ntfy_priority(params.get("priority"))
    incoming.high = priority == 5
    for name in NTFY_IGNORABLE:
        value = params.get(name)
        if name in ("cache", "firebase", "unifiedpush") and value.lower() in ("yes", "true", "1"):
            continue  # the default; nothing is being asked for
        if name == "markdown" and value.lower() not in ("yes", "true", "1"):
            continue
        if value:
            incoming.ignore(name)
    if _media(params.request) == "text/markdown":
        incoming.ignore("markdown")
    return priority


def ntfy_success(result: dict, topic: str, title: str, message: str, priority: int, tags: str) -> dict:
    created = _epoch(result["created_at"])
    body: dict[str, Any] = {"id": result["id"], "time": created, "expires": created + 12 * 3600, "event": "message",
                            "topic": topic, "message": message}
    if title:
        body["title"] = title
    if priority != 3:
        body["priority"] = priority
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if tag_list:
        body["tags"] = tag_list
    return body


def _audio_ext(filename: str, media: str) -> str | None:
    name = filename.rsplit("/", 1)[-1]
    if "." in name.lstrip("."):
        ext = name.rsplit(".", 1)[1].lower()
        return ext if ext in AUDIO_EXTENSIONS else None
    return MEDIA_TYPE_EXTENSIONS.get(media) if not filename else None


def _ntfy_routes(app: FastAPI, relay: Relay, prefix: str, include_in_schema: bool) -> None:
    topics = relay.settings.ntfy_topics

    def channel_for(topic: object) -> str:
        if not isinstance(topic, str) or not NTFY_TOPIC_RE.fullmatch(topic):
            raise ApiError(400, "invalid_topic", "invalid request: topic invalid")
        if topic not in topics:
            raise ApiError(404, "not_found", "topic not found (it is not in EARSHOT_RELAY_COMPAT_NTFY_TOPICS)")
        return topics[topic]

    async def publish_json(request: Request):
        try:
            if not ntfy_authorized(relay, request):
                raise ApiError(401, "unauthorized", "unauthorized")
            raw = await read_capped(request, MAX_FORM_BYTES)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise ApiError(400, "bad_json", "invalid request: request body must be message JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "invalid request: request body must be message JSON")
            topic = data.get("topic")
            incoming = Incoming(channel=channel_for(topic))
            for key in data:
                if key not in NTFY_JSON_FIELDS and key not in NTFY_PARAMS and data[key] not in (None, "", False, [], {}):
                    incoming.ignore(key)
            for name in ("message", "title"):
                if data.get(name) is not None and not isinstance(data[name], str):
                    raise ApiError(400, "bad_json", f"invalid request: {name} must be a string")
            # As ntfy does: a JSON field wins, else the same field as a header or query parameter.
            params = NtfyParams(request, data)
            incoming.message = params.get("message")
            incoming.title = params.get("title")
            priority = _ntfy_common(params, incoming)
            if params.get("filename"):
                incoming.ignore("filename")
            delivered = await deliver(relay, request, incoming, "ntfy")
            body = ntfy_success(delivered.result, topic, incoming.title, incoming.message, priority, params.get("tags"))
            return JSONResponse(body, headers=delivered.headers)
        except ApiError as exc:
            return ntfy_error(exc)

    async def publish_topic(request: Request, topic: str):
        return await _publish(request, topic, None)

    async def publish_action(request: Request, topic: str, action: str):
        return await _publish(request, topic, action)

    async def _publish(request: Request, topic: str, action: str | None):
        body_file: IO[bytes] | None = None
        try:
            if action is not None and action not in ("publish", "send", "trigger"):
                raise ApiError(404, "not_found", "page not found")
            if not ntfy_authorized(relay, request):
                raise ApiError(401, "unauthorized", "unauthorized")
            incoming = Incoming(channel=channel_for(topic))
            params = NtfyParams(request)
            priority = _ntfy_common(params, incoming)
            incoming.title = params.get("title")
            param_message = params.get("message")
            filename = params.get("filename")
            cap = max(relay.settings.max_audio_bytes, MAX_TEXT_UTF8_BYTES)
            body_file, size = await spool_capped(request, cap)
            media = _media(request)
            ext = _audio_ext(filename, media) if size else None
            message = param_message
            if ext is not None:
                incoming.audio = (body_file, size, ext)
                for name, value in (("title", incoming.title), ("message", param_message)):
                    if value:
                        incoming.ignore(name)
                message = f"You received a file: {filename or 'audio.' + ext}"
            elif size and (filename or param_message):
                incoming.ignore("attachment")  # ntfy reads the body as an attachment here; Earshot cannot speak it
            elif size:
                if size > MAX_TEXT_UTF8_BYTES:
                    raise ApiError(413, "text_too_long", f"message is over {MAX_TEXT_UTF8_BYTES} bytes")
                try:
                    message = body_file.read().decode("utf-8").strip()
                except UnicodeDecodeError:
                    raise ApiError(400, "bad_request", "the body is not UTF-8 text; to send audio, add a Filename "
                                                       "header ending in " + "/".join("." + e for e in AUDIO_EXTENSIONS)
                                   ) from None
            if filename and ext is None:
                incoming.ignore("filename")
            incoming.message = message if ext is None else ""
            delivered = await deliver(relay, request, incoming, "ntfy")
            body = ntfy_success(delivered.result, topic, incoming.title, message, priority, params.get("tags"))
            return JSONResponse(body, headers=delivered.headers)
        except ApiError as exc:
            return ntfy_error(exc)
        finally:
            if body_file is not None:
                body_file.close()

    doc = _doc("ntfy", include_in_schema)
    base_paths = [prefix or "/"] + ([prefix + "/"] if prefix else [])
    for i, path in enumerate(base_paths):
        _add(app, path, publish_json, ["POST", "PUT"], "ntfy_publish_json" + ("" if i == 0 else "_slash"), doc(
            "ntfy: publish as JSON (Apprise ntfy://, Uptime Kuma, Healthchecks)",
            "ntfy's JSON publish: `{\"topic\", \"message\", \"title\", \"priority\"}` POSTed to the server base. "
            "The topic must be listed in EARSHOT_RELAY_COMPAT_NTFY_TOPICS (unknown topic: 404). Answers ntfy's "
            "message JSON with 200, which means stored in the bucket, not played.",
            NTFY_JSON_SCHEMA, visible=i == 0))
    _add(app, prefix + "/{topic}", publish_topic, ["POST", "PUT"], "ntfy_publish_topic", doc(
        "ntfy: publish to a topic (curl -d, the ntfy CLI)",
        "The body is the message; Title/X-Title/t, Priority/X-Priority/p (1-5, min/low/default/high/max/urgent) and "
        "the other ntfy headers or query parameters as ntfy reads them. A body with a Filename ending in an audio "
        "extension is sent as audio. Answers ntfy's message JSON with 200: stored, not played.",
        NTFY_TEXT_SCHEMA))
    _add(app, prefix + "/{topic}/{action}", publish_action, ["GET", "POST", "PUT"], "ntfy_publish_action", doc(
        "ntfy: publish via /publish, /send or /trigger (GET webhooks)",
        "ntfy's webhook form: `GET .../{topic}/publish?message=...&title=...&auth=...`.", None))


# ---- Gotify -------------------------------------------------------------------------------------------------------

GOTIFY_FIELDS = {"title", "message", "priority", "extras"}
# Server-assigned fields the gotify CLI (go-api-client) serializes with zero values on every send; Gotify ignores them.
GOTIFY_READ_ONLY = {"id", "appid", "date"}


def gotify_error(exc: ApiError) -> JSONResponse:
    status = _client_status(exc)
    return JSONResponse({"error": REASONS.get(status, "Error"), "errorCode": status, "errorDescription": exc.message},
                        status_code=status, headers=_retry_headers(exc))


def gotify_extras(extras: object, incoming: Incoming) -> None:
    if not isinstance(extras, dict):
        if extras not in (None, ""):
            incoming.ignore("extras")
        return
    for ns, value in extras.items():
        if ns == "client::display" and isinstance(value, dict) and \
                str(value.get("contentType", "")).lower() == "text/markdown":
            incoming.ignore("markdown")
        elif ns == "client::notification" and isinstance(value, dict):
            if value.get("click"):
                incoming.ignore("click")
            if value.get("bigImageUrl"):
                incoming.ignore("image")
        elif ns == "android::action":
            incoming.ignore("actions")
        elif ns != "client::display":
            incoming.ignore("extras")


def _gotify_routes(app: FastAPI, relay: Relay, prefix: str, include_in_schema: bool) -> None:
    async def create_message(request: Request):
        try:
            secret = (request.headers.get("x-gotify-key") or request.query_params.get("token")
                      or authorization_secret(request.headers.get("authorization")))
            if not token_matches(relay, secret):
                raise ApiError(401, "unauthorized",
                               "you need to provide a valid access token or user credentials to access this api")
            data, files = await read_fields(request, MAX_FORM_BYTES)
            incoming = Incoming()
            incoming.message = _str_field(data, "message", required=True)
            if not incoming.message.strip():
                raise ApiError(400, "invalid_field", "Field 'message' is required", "message")
            incoming.title = _str_field(data, "title")
            priority = _int_field(data, "priority")
            incoming.high = priority is not None and priority >= 8
            gotify_extras(data.get("extras"), incoming)
            for key in list(data) + files:
                if key not in GOTIFY_FIELDS and key not in GOTIFY_READ_ONLY and key != "token":
                    incoming.ignore(key)
            delivered = await deliver(relay, request, incoming, "gotify")
            result = delivered.result
            row = relay.store.get_message(result["id"])
            body = {"id": row.rowid if row else 0, "appid": 1, "message": incoming.message, "title": incoming.title,
                    "priority": priority or 0, "date": result["created_at"],
                    "extras": {"earshot::message": {"id": result["id"], "receipt_url": result["receipt_url"]}}}
            return JSONResponse(body, headers=delivered.headers)
        except ApiError as exc:
            return gotify_error(exc)

    _add(app, prefix + "/message", create_message, ["POST"], "gotify_create_message", _doc("gotify", include_in_schema)(
        "Gotify: create a message (Apprise gotify://, gotify CLI, Uptime Kuma, Healthchecks)",
        "Gotify's POST /message: JSON or form `{title, message, priority, extras}`; the token in X-Gotify-Key, "
        "?token= or Authorization: Bearer. Priority 8 and above is Gotify's top band. Answers Gotify's message JSON "
        "with 200 (a numeric id, plus extras.earshot::message.id for the receipt): stored, not played.",
        GOTIFY_SCHEMA))


# ---- Pushover -----------------------------------------------------------------------------------------------------

PUSHOVER_FIELDS = {"token", "user", "message", "title", "priority"}


def pushover_error(exc: ApiError) -> JSONResponse:
    status = _client_status(exc)
    body: dict[str, Any] = {"status": 0, "request": "", "errors": [exc.message]}
    if exc.field in ("token", "user", "message", "title", "priority"):
        body[exc.field] = "invalid"
    return JSONResponse(body, status_code=status, headers=_retry_headers(exc))


def pushover_limit_headers(relay: Relay) -> dict[str, str]:
    """Pushover clients read these on every success (gregdel/pushover fails without them): the relay's own window."""
    w = relay.limiter.global_window
    now = int(time.time())
    if w is None:
        return {"X-Limit-App-Limit": "10000", "X-Limit-App-Remaining": "10000", "X-Limit-App-Reset": str(now + 86400)}
    return {"X-Limit-App-Limit": str(w.limit.count), "X-Limit-App-Remaining": str(w.remaining()),
            "X-Limit-App-Reset": str(now + w.limit.per_seconds)}


def _pushover_routes(app: FastAPI, relay: Relay, prefix: str, include_in_schema: bool) -> None:
    async def messages(request: Request):
        try:
            data, files = await read_fields(request, PUSHOVER_MAX_BYTES)
            token = data.get("token")
            if not isinstance(token, str) or not token_matches(relay, token, pushover=True):
                raise ApiError(401, "unauthorized", "application token is invalid", "token")
            incoming = Incoming()
            incoming.message = _str_field(data, "message", required=True)
            if not incoming.message.strip():
                raise ApiError(400, "invalid_field", "message cannot be blank", "message")
            incoming.title = _str_field(data, "title")
            priority = _int_field(data, "priority")
            if priority is not None and not -2 <= priority <= 2:
                raise ApiError(400, "invalid_priority", "priority is invalid", "priority")
            incoming.high = priority == 2
            for key in list(data) + files:
                value = data.get(key, "file")
                if key not in PUSHOVER_FIELDS and value not in (None, "", "0", 0):
                    incoming.ignore(key)
            delivered = await deliver(relay, request, incoming, "pushover")
            headers = {**delivered.headers, **pushover_limit_headers(relay)}
            return JSONResponse({"status": 1, "request": delivered.result["id"]}, headers=headers)
        except ApiError as exc:
            return pushover_error(exc)

    _add(app, prefix + "/1/messages.json", messages, ["POST"], "pushover_messages", _doc("pushover", include_in_schema)(
        "Pushover: send a message (scripts and clients with a configurable API URL)",
        "Pushover's POST /1/messages.json as a urlencoded form, multipart form or JSON: token (the relay token or "
        "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN), user (accepted, not checked), message, title, priority (-2..2; 2 is the "
        "top). Answers `{\"status\": 1, \"request\": \"<message id>\"}` with 200 and X-Limit-App-* headers: stored, "
        "not played.", PUSHOVER_SCHEMA))


# ---- Join ---------------------------------------------------------------------------------------------------------

JOIN_SPOKEN = {"text", "title", "say"}


def join_error(exc: ApiError) -> JSONResponse:
    status = _client_status(exc)
    return JSONResponse({"success": False, "userAuthError": status == 401, "errorMessage": exc.message},
                        status_code=status, headers=_retry_headers(exc))


def _join_routes(app: FastAPI, relay: Relay, prefix: str, include_in_schema: bool) -> None:
    async def send_push(request: Request):
        try:
            data: dict[str, Any] = {}
            for k, v in request.query_params.multi_items():
                data.setdefault(k, v)
            if request.method == "POST":
                body, files = await read_fields(request, MAX_FORM_BYTES)
                data.update({k: v for k, v in body.items()})
                data.update({f: "file" for f in files})
            apikey = data.get("apikey")
            if not isinstance(apikey, str) or not token_matches(relay, apikey):
                raise ApiError(401, "unauthorized", "User Not Authenticated", "apikey")
            incoming = Incoming()
            say, text, title = (_str_field(data, n) for n in ("say", "text", "title"))
            if say.strip():
                incoming.message = say
                for name, value in (("text", text), ("title", title)):
                    if value.strip():
                        incoming.ignore(name)
            else:
                incoming.message, incoming.title = text, title
            for key, value in data.items():
                if key not in JOIN_SPOKEN and key != "apikey" and not key.startswith("earshot_") and value not in (None, ""):
                    incoming.ignore(key)
            delivered = await deliver(relay, request, incoming, "join")
            return JSONResponse({"success": True, "userAuthError": False, "messageId": delivered.result["id"]},
                                headers=delivered.headers)
        except ApiError as exc:
            return join_error(exc)

    _add(app, prefix + "/_ah/api/messaging/v1/sendPush", send_push, ["GET", "POST"], "join_send_push",
         _doc("join", include_in_schema)(
             "Join: sendPush (Tasker HTTP Request, scripts)",
             "Join's sendPush with query parameters, a form or JSON: apikey (the relay token), text, title, say "
             "(spoken instead of title and text when present); deviceId and the rest are ignored and named. Answers "
             "`{\"success\": true}` with 200: stored, not played.",
             JOIN_SCHEMA))


# ---- installation -------------------------------------------------------------------------------------------------

INSTALLERS: dict[str, Callable[[FastAPI, Relay, str, bool], None]] = {
    "gotify": _gotify_routes,  # before ntfy, so the root listener's /message is Gotify's and not an ntfy topic
    "pushover": _pushover_routes,
    "join": _join_routes,
    "ntfy": _ntfy_routes,
}


def install(app: FastAPI, relay: Relay, protocols: tuple[str, ...] | list[str], *, root: bool,
            include_in_schema: bool = True) -> None:
    """Add the compat routes: under /compat/<protocol> on the main listener, or at the root (root=True) on the
    compat listener, where clients that only take a host and port (Apprise ntfy://) can reach them."""
    for name, installer in INSTALLERS.items():
        if name in protocols:
            installer(app, relay, "" if root else PREFIXES[name], include_in_schema)


def exempt_prefixes(protocols: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Paths the main listener's bearer middleware leaves to the compat routes, which take their protocol's auth."""
    return tuple(PREFIXES[p] for p in protocols if p in PREFIXES)


# ---- OpenAPI ------------------------------------------------------------------------------------------------------

HEADERS_DOC = {
    "X-Earshot-Message-Id": {"description": "The relay message id (GET /v1/messages/{id} reads its receipt)",
                             "schema": {"type": "string"}},
    "Location": {"description": "/v1/messages/{id}", "schema": {"type": "string"}},
    "X-Earshot-Ignored": {"description": "Comma-separated request fields Earshot did not honour (attachments, "
                                         "actions, click, sound, markdown, ...)", "schema": {"type": "string"}},
    "X-Earshot-Status": {"description": "coalescing (held until the window closes) or suppressed (dropped by "
                                        "cooldown): the 200 means accepted, not written", "schema": {"type": "string"}},
    "X-Earshot-Normalized": {"description": "'true' when the normalizer changed the text", "schema": {"type": "string"}},
    "X-Earshot-Truncated": {"description": "'true' when the text was cut at EARSHOT_RELAY_MAX_WORDS",
                            "schema": {"type": "string"}},
    "X-Earshot-Downgraded": {"description": "'alarm' when the top priority was stored as a normal item because "
                                            "EARSHOT_RELAY_COMPAT_ALARM is off, alarm is not in "
                                            "EARSHOT_RELAY_CHANNELS, or the alarm rate limit is used up",
                             "schema": {"type": "string"}},
    "X-Earshot-Promoted": {"description": "'alarm' when the top priority was stored on the alarm channel "
                                          "(EARSHOT_RELAY_COMPAT_ALARM=1)", "schema": {"type": "string"}},
}
NTFY_JSON_SCHEMA = {"application/json": {"schema": {"type": "object", "required": ["topic"], "properties": {
    "topic": {"type": "string"}, "message": {"type": "string"}, "title": {"type": "string"},
    "priority": {"type": "integer", "minimum": 1, "maximum": 5}}}}}
NTFY_TEXT_SCHEMA = {"text/plain": {"schema": {"type": "string", "description": "The message"}},
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary",
                                                            "description": "Audio, with a Filename header"}}}
GOTIFY_SCHEMA = {"application/json": {"schema": {"type": "object", "required": ["message"], "properties": {
    "message": {"type": "string"}, "title": {"type": "string"}, "priority": {"type": "integer"},
    "extras": {"type": "object"}}}}}
PUSHOVER_SCHEMA = {"application/x-www-form-urlencoded": {"schema": {"type": "object", "required": ["token", "message"],
                                                                     "properties": {
    "token": {"type": "string"}, "user": {"type": "string"}, "message": {"type": "string"},
    "title": {"type": "string"}, "priority": {"type": "integer", "minimum": -2, "maximum": 2}}}}}
JOIN_SCHEMA = {"application/x-www-form-urlencoded": {"schema": {"type": "object", "required": ["apikey"], "properties": {
    "apikey": {"type": "string"}, "text": {"type": "string"}, "title": {"type": "string"},
    "say": {"type": "string"}}}}}


def _add(app: FastAPI, path: str, endpoint, methods: list[str], op: str, doc: dict) -> None:
    """One route per method, so every OpenAPI operation id is unique."""
    for method in methods:
        app.add_api_route(path, endpoint, methods=[method], operation_id=f"compat_{op}_{method.lower()}", **doc)


def _doc(protocol: str, include_in_schema: bool):
    def make(summary: str, description: str, body: dict | None, visible: bool = True) -> dict:
        extra: dict[str, Any] = {}
        if body is not None:
            extra["requestBody"] = {"required": False, "content": body}
        return {
            "tags": ["compat"],
            "summary": summary,
            "description": description + " Off unless EARSHOT_RELAY_COMPAT lists " + protocol + ".",
            "include_in_schema": include_in_schema and visible,
            "responses": {200: {"description": "Stored in the bucket (the protocol's success body)",
                                "headers": HEADERS_DOC}},
            "openapi_extra": extra or None,
        }
    return make
