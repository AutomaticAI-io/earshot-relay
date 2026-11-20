"""Validation and object keys, identical to what the Earshot app accepts.

Every rule here mirrors the app: the channel grammar is the app's `Channels` rule, the audio extensions are the
app's `AudioKeys.extensions`, and the text limits come from the app's `.say` handling (it fetches at most 64 KB and
silently truncates to 4000 UTF-16 code units, so the relay refuses instead of letting text be cut off).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
VOICE_RE = re.compile(r"^[A-Za-z0-9_+().: -]{1,100}$")

MAX_TEXT_UTF16_UNITS = 4000
MAX_TEXT_UTF8_BYTES = 65536

AUDIO_EXTENSIONS = ("ogg", "opus", "mp3", "m4a", "aac", "wav", "flac", "webm")

CONTENT_TYPES = {
    "say": "text/plain; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "webm": "audio/webm",
}

# Upload Content-Type -> extension, used only when the uploaded file name has no extension.
MEDIA_TYPE_EXTENSIONS = {
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/vnd.wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/webm": "webm",
}


class ApiError(Exception):
    """A refused request: the HTTP status, the stable snake_case error code, a message and the field at fault."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.retry_after = retry_after


def is_valid_channel(name: object) -> bool:
    return isinstance(name, str) and CHANNEL_RE.fullmatch(name) is not None


def utf16_units(text: str) -> int:
    """Length as Kotlin's String.length counts it: UTF-16 code units, so an emoji outside the BMP counts 2."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clean_text(raw: object) -> str:
    """Trim outer whitespace and enforce the app's limits. Returns the text that will be written as UTF-8."""
    if not isinstance(raw, str):
        raise ApiError(422, "invalid_field", "text must be a string", "text")
    text = raw.strip()
    if not text:
        raise ApiError(422, "empty_text", "text is empty after trimming whitespace", "text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(422, "invalid_text", "text contains an unpaired surrogate", "text") from None
    units = utf16_units(text)
    if units > MAX_TEXT_UTF16_UNITS:
        raise ApiError(
            422,
            "text_too_long",
            f"text is {units} UTF-16 code units; the limit is {MAX_TEXT_UTF16_UNITS} (the app would cut it off)",
            "text",
        )
    if len(encoded) > MAX_TEXT_UTF8_BYTES:
        raise ApiError(422, "text_too_long", f"text is over {MAX_TEXT_UTF8_BYTES} UTF-8 bytes", "text")
    return text


def check_voice(voice: object) -> str | None:
    if voice is None:
        return None
    if not isinstance(voice, str) or VOICE_RE.fullmatch(voice) is None:
        raise ApiError(422, "invalid_voice", "voice must match ^[A-Za-z0-9_+().: -]{1,100}$", "voice")
    return voice


def audio_extension(filename: str | None, media_type: str | None) -> str:
    """Extension from the upload's file name, else from its Content-Type. 415 when neither is a playable audio type."""
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name.lstrip("."):
        ext = name.rsplit(".", 1)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            return ext
        hint = " (send text as JSON instead)" if ext in ("say", "txt") else ""
        raise ApiError(
            415,
            "unsupported_media_type",
            f".{ext} is not a playable audio type; use one of {', '.join('.' + e for e in AUDIO_EXTENSIONS)}{hint}",
            "file",
        )
    base = (media_type or "").split(";", 1)[0].strip().lower()
    ext = MEDIA_TYPE_EXTENSIONS.get(base)
    if ext is None:
        raise ApiError(
            415,
            "unsupported_media_type",
            "the file has no extension and its Content-Type is not a known audio type",
            "file",
        )
    return ext


def content_type_for(ext: str) -> str:
    return CONTENT_TYPES[ext]


@dataclass(frozen=True)
class NewKey:
    message_id: str
    key: str


def new_key(channel: str, ext: str) -> NewKey:
    """Opaque key `<channel>/<32 hex>.<ext>`: no text ever leaks into an object name. Id is `msg_<same hex>`."""
    if not is_valid_channel(channel):
        raise ValueError("invalid channel")
    hex_id = uuid.uuid4().hex
    return NewKey(message_id=f"msg_{hex_id}", key=f"{channel}/{hex_id}.{ext}")
