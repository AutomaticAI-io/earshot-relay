"""Settings from the environment. Every variable also accepts a `<NAME>_FILE` variant holding a path to read.

Startup validation fails fast with messages that name variables, never their values.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .hygiene import MAX_WORDS_LIMIT
from .keys import CHANNEL_RE, VOICE_RE

MIN_TOKEN_LEN = 32
BEACON_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
API_TOKEN_RE = re.compile(r"^[\x21-\x7e]{32,512}$")
RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(s|sec|second|m|min|minute|h|hr|hour|d|day)s?\s*$", re.IGNORECASE)
RATE_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
              "d": 86400, "day": 86400}
REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$")
# Words that only appear in a value copied unchanged from .env.example or the docs. A real random token
# (`earshot-relay token`) never contains them, so a value that does was never filled in.
PLACEHOLDER_RE = re.compile(r"replace|changeme|change-me|your-token|placeholder", re.IGNORECASE)
SECRET_VARS = ("EARSHOT_RELAY_TOKEN", "EARSHOT_RELAY_BEACON_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
               "EARSHOT_RELAY_TTS_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY", "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN")

TTS_PROVIDERS = ("openai", "elevenlabs", "gemini")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
ELEVENLABS_URL = "https://api.elevenlabs.io"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
NORMALIZE_MODES = ("off", "basic")
WATCHDOG_FORMATS = ("auto", "json", "text")
WATCHDOG_MIN_S, WATCHDOG_MAX_S = 10, 86_400

# Producer-compat ingress (compat.py): the push APIs a producer may already speak.
COMPAT_PROTOCOLS = ("ntfy", "gotify", "pushover", "join")
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")  # ntfy's own topic grammar
# Pushover app tokens and user keys are 30 letters and digits, and client libraries check that before sending.
PUSHOVER_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{30}$")
# The example token and user key printed in Pushover's API documentation: copied, never generated.
PUSHOVER_DOC_EXAMPLES = ("azGDORePK8gMaC0QOYAMyEEuzJnyUi", "uQiRzpo4DXghDmr9QzzfQu27cmVRsG")
# On the compat listener ntfy topics sit at the root, so these paths are not topics.
RESERVED_ROOT_TOPICS = ("healthz", "v1", "compat", "mcp")


class ConfigError(Exception):
    """A configuration problem. The message names variables only, never a secret value."""


def env(name: str, environ: dict[str, str] | None = None) -> str | None:
    """`NAME`, else the contents of the file named by `NAME_FILE` (trailing newline stripped). Empty means unset."""
    e = os.environ if environ is None else environ
    direct = e.get(name)
    path = e.get(f"{name}_FILE")
    if direct and path:
        raise ConfigError(f"set {name} or {name}_FILE, not both")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{name}_FILE cannot be read ({exc.strerror or type(exc).__name__})") from None
        return value or None
    return direct.strip() if direct and direct.strip() else None


@dataclass(frozen=True)
class RateLimit:
    count: int
    per_seconds: int

    def describe(self) -> str:
        unit = {1: "second", 60: "minute", 3600: "hour", 86400: "day"}.get(self.per_seconds, f"{self.per_seconds}s")
        return f"{self.count}/{unit}"


def parse_rate(name: str, raw: str | None, default: str) -> RateLimit | None:
    value = (raw or default).strip()
    if value.lower() in ("off", "0", "none", "disabled"):
        return None
    m = RATE_RE.match(value)
    if not m or int(m.group(1)) < 1:
        raise ConfigError(f"{name} must look like 60/min, 6/hour or off")
    return RateLimit(int(m.group(1)), RATE_UNITS[m.group(2).lower()])


def _int(name: str, raw: str | None, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def default_data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(base) / "earshot-relay")


@dataclass(frozen=True)
class TtsSettings:
    provider: str
    url: str
    model: str
    voice: str | None
    api_key: str | None = field(repr=False)
    response_format: str
    timeout_s: float
    leadin_ms: int


@dataclass(frozen=True)
class Settings:
    s3_endpoint: str
    bucket: str
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    token: str = field(repr=False)
    beacon_token: str | None = field(repr=False)
    channels: tuple[str, ...] = ("phone",)
    default_channel: str = "phone"
    public_url: str | None = None
    data_dir: str = "/data"
    host: str = "127.0.0.1"
    port: int = 8080
    beacon_port: int | None = None
    retention_days: int = 7
    receipt_wait_min: int = 60
    max_audio_bytes: int = 26_214_400
    rate_limit: RateLimit | None = RateLimit(60, 60)
    alarm_rate_limit: RateLimit | None = RateLimit(6, 3600)
    beacon_rate_limit: RateLimit | None = RateLimit(600, 60)
    default_speech: str = "device"
    tts: TtsSettings | None = None
    allow_http_s3: bool = False
    compat: tuple[str, ...] = ()
    compat_alarm: bool = False
    compat_port: int | None = None
    compat_ntfy_topics: tuple[tuple[str, str], ...] = ()
    compat_pushover_token: str | None = field(default=None, repr=False)
    normalize: str = "off"
    max_words: int | None = None
    channel_hygiene: tuple[tuple[str, str | None, int | None], ...] = ()
    watchdog_s: int | None = None
    watchdog_url: str | None = field(default=None, repr=False)  # may carry a topic or token: never logged or shown
    watchdog_cooldown_s: int = 1800
    watchdog_format: str = "auto"

    @property
    def normalize_default(self) -> bool:
        return self.normalize == "basic"

    def hygiene_for(self, channel: str) -> tuple[bool, int | None]:
        mode, words = self.normalize, self.max_words
        for name, override_mode, override_words in self.channel_hygiene:
            if name == channel:
                mode = override_mode if override_mode is not None else mode
                words = override_words if override_words is not None else words
                break
        return mode == "basic", words

    @property
    def watchdog_host(self) -> str | None:
        return urlsplit(self.watchdog_url).netloc.rpartition("@")[2] if self.watchdog_url else None

    @property
    def endpoint_host(self) -> str:
        return urlsplit(self.s3_endpoint).netloc

    @property
    def alarm_allowed(self) -> bool:
        return "alarm" in self.channels

    @property
    def ntfy_topics(self) -> dict[str, str]:
        return dict(self.compat_ntfy_topics)

    @property
    def compat_alarm_allowed(self) -> bool:
        """A compat sender's top priority may reach the alarm channel only when both switches are on."""
        return self.compat_alarm and self.alarm_allowed

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "relay.db")


def _require(name: str, environ: dict[str, str] | None) -> str:
    value = env(name, environ)
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def load_settings(environ: dict[str, str] | None = None, *, check_ffmpeg: bool = True) -> Settings:
    """Read and validate everything. Raises ConfigError with every problem found, one per line."""
    problems: list[str] = []

    def take(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConfigError as exc:
            problems.append(str(exc))
            return None

    e = environ
    allow_http = _flag(take(env, "EARSHOT_RELAY_ALLOW_HTTP_S3", e))
    endpoint = take(_require, "EARSHOT_S3_ENDPOINT", e)
    if endpoint:
        endpoint = endpoint.rstrip("/")
        parts = urlsplit(endpoint)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            problems.append("EARSHOT_S3_ENDPOINT must be an https:// URL (path-style endpoint, no bucket)")
        elif parts.scheme == "http" and not allow_http:
            problems.append("EARSHOT_S3_ENDPOINT must use https (EARSHOT_RELAY_ALLOW_HTTP_S3=1 is for disposable tests only)")
        elif parts.username or parts.password or parts.query or parts.fragment:
            problems.append("EARSHOT_S3_ENDPOINT must not carry credentials, a query or a fragment")
    bucket = take(_require, "EARSHOT_BUCKET", e)
    if bucket and ("/" in bucket or not BUCKET_RE.fullmatch(bucket)):
        problems.append("EARSHOT_BUCKET must be a single bucket name (no '/')")
    region = take(_require, "EARSHOT_REGION", e)
    if region and not REGION_RE.fullmatch(region):
        problems.append("EARSHOT_REGION must be a signing region such as us-east-1 or auto")
    key_id = take(_require, "AWS_ACCESS_KEY_ID", e)
    secret = take(_require, "AWS_SECRET_ACCESS_KEY", e)
    for var in SECRET_VARS:
        value = take(env, var, e)
        if value and PLACEHOLDER_RE.search(value):
            hint = " (generate one with: earshot-relay token)" if var.endswith("TOKEN") else ""
            problems.append(f"{var} still holds a placeholder from .env.example; set a real value{hint}")

    token = take(_require, "EARSHOT_RELAY_TOKEN", e)
    if token and not API_TOKEN_RE.fullmatch(token):
        problems.append(f"EARSHOT_RELAY_TOKEN must be at least {MIN_TOKEN_LEN} printable characters without spaces "
                        "(generate one with: earshot-relay token)")
    beacon_token = take(env, "EARSHOT_RELAY_BEACON_TOKEN", e)
    if beacon_token:
        if not BEACON_TOKEN_RE.fullmatch(beacon_token):
            problems.append(f"EARSHOT_RELAY_BEACON_TOKEN must be at least {MIN_TOKEN_LEN} URL-safe characters "
                            "(A-Z a-z 0-9 . _ ~ -; generate one with: earshot-relay token)")
        elif token and beacon_token == token:
            problems.append("EARSHOT_RELAY_BEACON_TOKEN must differ from EARSHOT_RELAY_TOKEN")

    raw_channels = take(env, "EARSHOT_RELAY_CHANNELS", e) or "phone"
    channels: list[str] = []
    for name in re.split(r"[\s,;]+", raw_channels.strip()):
        if not name:
            continue
        if not CHANNEL_RE.fullmatch(name):
            problems.append(f"EARSHOT_RELAY_CHANNELS has an invalid channel name {name!r} "
                            "(lowercase letters, digits, - and _, 1-32 chars, starting with a letter or digit)")
        elif name not in channels:
            channels.append(name)
    if not channels and not problems:
        problems.append("EARSHOT_RELAY_CHANNELS lists no channel")
    # The default channel is what every send without an explicit channel uses (CLI say, MCP say, hooks), so it can
    # never be the loud alarm channel: alarm is reachable only by naming it.
    quiet = [c for c in channels if c != "alarm"]
    default_channel = take(env, "EARSHOT_RELAY_DEFAULT_CHANNEL", e)
    if default_channel == "alarm":
        problems.append("EARSHOT_RELAY_DEFAULT_CHANNEL cannot be alarm (alarm is loud; send to it only by naming it)")
    elif default_channel is None:
        default_channel = "phone" if "phone" in quiet else (quiet[0] if quiet else None)
        if channels and default_channel is None:
            problems.append("EARSHOT_RELAY_CHANNELS needs at least one channel besides alarm (the default channel)")
    elif channels and default_channel not in channels:
        problems.append("EARSHOT_RELAY_DEFAULT_CHANNEL must be one of EARSHOT_RELAY_CHANNELS")

    public_url = take(env, "EARSHOT_RELAY_PUBLIC_URL", e)
    if public_url:
        public_url = public_url.rstrip("/")
        pu = urlsplit(public_url)
        if pu.scheme != "https" or not pu.netloc or pu.query or pu.fragment or pu.username:
            problems.append("EARSHOT_RELAY_PUBLIC_URL must be an https:// URL without query or fragment "
                            "(the app refuses a plain-http beacon endpoint)")

    data_dir = take(env, "EARSHOT_RELAY_DATA_DIR", e) or default_data_dir()
    host = take(env, "EARSHOT_RELAY_HOST", e) or "127.0.0.1"
    port = take(_int, "EARSHOT_RELAY_PORT", take(env, "EARSHOT_RELAY_PORT", e), 8080, 1, 65535)
    raw_beacon_port = take(env, "EARSHOT_RELAY_BEACON_PORT", e)
    beacon_port = take(_int, "EARSHOT_RELAY_BEACON_PORT", raw_beacon_port, 0, 1, 65535) if raw_beacon_port else None
    if beacon_port is not None and beacon_port == port:
        problems.append("EARSHOT_RELAY_BEACON_PORT must differ from EARSHOT_RELAY_PORT")
    if beacon_port is not None and not beacon_token:
        problems.append("EARSHOT_RELAY_BEACON_PORT is set but EARSHOT_RELAY_BEACON_TOKEN is not")
    retention = take(_int, "EARSHOT_RELAY_RETENTION_DAYS", take(env, "EARSHOT_RELAY_RETENTION_DAYS", e), 7, 1, 3650)
    wait_min = take(_int, "EARSHOT_RELAY_RECEIPT_WAIT_MIN", take(env, "EARSHOT_RELAY_RECEIPT_WAIT_MIN", e), 60, 1, 10080)
    max_audio = take(_int, "EARSHOT_RELAY_MAX_AUDIO_BYTES", take(env, "EARSHOT_RELAY_MAX_AUDIO_BYTES", e),
                     26_214_400, 1024, 50 * 1024 * 1024)
    rate = take(parse_rate, "EARSHOT_RELAY_RATE_LIMIT", take(env, "EARSHOT_RELAY_RATE_LIMIT", e), "60/min")
    alarm_rate = take(parse_rate, "EARSHOT_RELAY_ALARM_RATE_LIMIT", take(env, "EARSHOT_RELAY_ALARM_RATE_LIMIT", e),
                      "6/hour")
    beacon_rate = take(parse_rate, "EARSHOT_RELAY_BEACON_RATE_LIMIT", take(env, "EARSHOT_RELAY_BEACON_RATE_LIMIT", e),
                       "600/min")

    default_speech = (take(env, "EARSHOT_RELAY_DEFAULT_SPEECH", e) or "device").lower()
    if default_speech not in ("device", "server"):
        problems.append("EARSHOT_RELAY_DEFAULT_SPEECH must be device or server")

    tts = _tts_settings(e, take, problems, check_ffmpeg=check_ffmpeg)
    if default_speech == "server" and tts is None and not problems:
        problems.append("EARSHOT_RELAY_DEFAULT_SPEECH=server needs EARSHOT_RELAY_TTS_URL "
                        "(or EARSHOT_RELAY_TTS_PROVIDER=elevenlabs)")

    hygiene = _hygiene_settings(e, take, problems, channels=channels)
    watchdog = _watchdog_settings(e, take, problems, beacon_token=beacon_token)
    compat = _compat_settings(e, take, problems, channels=channels, port=port, beacon_port=beacon_port)

    if problems:
        raise ConfigError("\n".join(dict.fromkeys(problems)))
    return Settings(
        s3_endpoint=endpoint, bucket=bucket, region=region, access_key_id=key_id, secret_access_key=secret,
        token=token, beacon_token=beacon_token, channels=tuple(channels), default_channel=default_channel,
        public_url=public_url, data_dir=data_dir, host=host, port=port, beacon_port=beacon_port,
        retention_days=retention, receipt_wait_min=wait_min, max_audio_bytes=max_audio, rate_limit=rate,
        alarm_rate_limit=alarm_rate, beacon_rate_limit=beacon_rate, default_speech=default_speech, tts=tts,
        allow_http_s3=allow_http, **compat, **hygiene, **watchdog,
    )


def _compat_settings(e, take, problems: list[str], *, channels: list[str], port, beacon_port) -> dict:
    """EARSHOT_RELAY_COMPAT and friends. Everything is off unless EARSHOT_RELAY_COMPAT names a protocol."""
    raw = (take(env, "EARSHOT_RELAY_COMPAT", e) or "").strip().lower()
    names = [n for n in re.split(r"[\s,;]+", raw) if n] if raw not in ("", "off", "none", "0") else []
    if names == ["all"]:
        names = list(COMPAT_PROTOCOLS)
    enabled: list[str] = []
    for n in names:
        if n not in COMPAT_PROTOCOLS:
            problems.append(f"EARSHOT_RELAY_COMPAT has an unknown protocol {n!r} (use any of: {', '.join(COMPAT_PROTOCOLS)}, or all)")
        elif n not in enabled:
            enabled.append(n)
    alarm = _flag(take(env, "EARSHOT_RELAY_COMPAT_ALARM", e))
    raw_port = take(env, "EARSHOT_RELAY_COMPAT_PORT", e)
    compat_port = take(_int, "EARSHOT_RELAY_COMPAT_PORT", raw_port, 0, 1, 65535) if raw_port else None
    raw_topics = take(env, "EARSHOT_RELAY_COMPAT_NTFY_TOPICS", e)
    pushover_token = take(env, "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN", e)

    if not enabled:
        for var, value in (("EARSHOT_RELAY_COMPAT_PORT", raw_port), ("EARSHOT_RELAY_COMPAT_ALARM", alarm),
                           ("EARSHOT_RELAY_COMPAT_NTFY_TOPICS", raw_topics),
                           ("EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN", pushover_token)):
            if value:
                problems.append(f"{var} is set but EARSHOT_RELAY_COMPAT names no protocol")
        return {}
    if compat_port is not None and compat_port in (port, beacon_port):
        problems.append("EARSHOT_RELAY_COMPAT_PORT must differ from EARSHOT_RELAY_PORT and EARSHOT_RELAY_BEACON_PORT")

    topics: dict[str, str] = {}
    if "ntfy" in enabled:
        if raw_topics:
            for entry in re.split(r"[\s,;]+", raw_topics.strip()):
                if not entry:
                    continue
                topic, _, chan = entry.partition("=")
                topic, chan = topic.strip(), (chan.strip() or topic.strip())
                if not NTFY_TOPIC_RE.fullmatch(topic):
                    problems.append(f"EARSHOT_RELAY_COMPAT_NTFY_TOPICS has an invalid topic {topic!r} "
                                    "(letters, digits, - and _, 1-64 chars, as ntfy allows)")
                elif chan not in channels:
                    problems.append(f"EARSHOT_RELAY_COMPAT_NTFY_TOPICS maps topic {topic!r} to {chan!r}, "
                                    "which is not in EARSHOT_RELAY_CHANNELS")
                elif chan == "alarm" and not alarm:
                    problems.append(f"EARSHOT_RELAY_COMPAT_NTFY_TOPICS maps topic {topic!r} to alarm, which also "
                                    "needs EARSHOT_RELAY_COMPAT_ALARM=1")
                elif topic in topics and topics[topic] != chan:
                    problems.append(f"EARSHOT_RELAY_COMPAT_NTFY_TOPICS maps topic {topic!r} twice")
                else:
                    topics[topic] = chan
        else:  # default: every quiet channel is a topic of the same name; alarm never is
            topics = {c: c for c in channels if c != "alarm"}
        for topic in topics:
            if compat_port is not None and (topic in RESERVED_ROOT_TOPICS or (topic == "message" and "gotify" in enabled)):
                problems.append(f"ntfy topic {topic!r} collides with a path on EARSHOT_RELAY_COMPAT_PORT; rename it")
    elif raw_topics:
        problems.append("EARSHOT_RELAY_COMPAT_NTFY_TOPICS is set but EARSHOT_RELAY_COMPAT does not include ntfy")

    if pushover_token:
        if "pushover" not in enabled:
            problems.append("EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN is set but EARSHOT_RELAY_COMPAT does not include pushover")
        elif not PUSHOVER_TOKEN_RE.fullmatch(pushover_token):
            problems.append("EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN must be exactly 30 letters and digits, the shape "
                            "Pushover clients check (generate one with: earshot-relay token --pushover)")
        elif pushover_token in PUSHOVER_DOC_EXAMPLES:
            problems.append("EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN is the example from Pushover's documentation; "
                            "generate one with: earshot-relay token --pushover")
    return {"compat": tuple(enabled), "compat_alarm": alarm, "compat_port": compat_port,
            "compat_ntfy_topics": tuple(topics.items()), "compat_pushover_token": pushover_token}


def _tts_settings(e, take, problems: list[str], *, check_ffmpeg: bool) -> TtsSettings | None:
    """The optional server TTS: an OpenAI-compatible endpoint (EARSHOT_RELAY_TTS_URL) or ElevenLabs."""
    provider = (take(env, "EARSHOT_RELAY_TTS_PROVIDER", e) or "openai").lower()
    if provider not in TTS_PROVIDERS:
        problems.append(f"EARSHOT_RELAY_TTS_PROVIDER must be one of: {', '.join(TTS_PROVIDERS)}")
        return None
    tts_url = take(env, "EARSHOT_RELAY_TTS_URL", e)
    if provider == "openai" and not tts_url:
        for orphan in ("EARSHOT_RELAY_TTS_MODEL", "EARSHOT_RELAY_TTS_VOICE", "EARSHOT_RELAY_TTS_API_KEY"):
            if take(env, orphan, e):
                problems.append(f"{orphan} is set but EARSHOT_RELAY_TTS_URL is not")
        return None
    elevenlabs = provider == "elevenlabs"
    gemini = provider == "gemini"
    if not tts_url and (elevenlabs or gemini):
        tts_url = ELEVENLABS_URL if elevenlabs else GEMINI_URL
    tts_url = tts_url.rstrip("/")
    tu = urlsplit(tts_url)
    if tu.scheme not in ("http", "https") or not tu.netloc or tu.query or tu.fragment:
        problems.append("EARSHOT_RELAY_TTS_URL must be an http(s) base URL" + ("" if elevenlabs else " ending in /v1"))
    elif tu.username or tu.password:
        problems.append("EARSHOT_RELAY_TTS_URL must not carry credentials (use EARSHOT_RELAY_TTS_API_KEY)")
    model = take(env, "EARSHOT_RELAY_TTS_MODEL", e)
    if elevenlabs:
        model = model or ELEVENLABS_DEFAULT_MODEL
    elif gemini:
        model = model or GEMINI_DEFAULT_MODEL
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model):
            problems.append("EARSHOT_RELAY_TTS_MODEL must be a Gemini model id, not a path")
    elif not model:
        problems.append("EARSHOT_RELAY_TTS_MODEL is required when EARSHOT_RELAY_TTS_URL is set")
    voice = take(env, "EARSHOT_RELAY_TTS_VOICE", e)
    if not voice:
        problems.append("EARSHOT_RELAY_TTS_VOICE is required when " + (
            "EARSHOT_RELAY_TTS_PROVIDER=elevenlabs (an ElevenLabs voice id; a request can still override it)" if elevenlabs
            else "EARSHOT_RELAY_TTS_URL is set (the OpenAI speech API requires a voice; a request can still override it)"))
    elif not VOICE_RE.fullmatch(voice):
        problems.append("EARSHOT_RELAY_TTS_VOICE must match ^[A-Za-z0-9_+().: -]{1,100}$")
    if gemini:
        api_key = take(env, "GEMINI_API_KEY", e) or take(env, "EARSHOT_RELAY_TTS_API_KEY", e)
        if not api_key:
            problems.append("GEMINI_API_KEY is required when EARSHOT_RELAY_TTS_PROVIDER=gemini")
        fmt = (take(env, "EARSHOT_RELAY_TTS_FORMAT", e) or "pcm").lower()
        if fmt != "pcm":
            problems.append("EARSHOT_RELAY_TTS_FORMAT must be pcm for Gemini (converted to Ogg/Opus)")
    elif elevenlabs:
        # The ElevenLabs key comes from the environment (or a *_FILE secret), under its usual name or the relay's.
        api_key = take(env, "ELEVENLABS_API_KEY", e) or take(env, "EARSHOT_RELAY_TTS_API_KEY", e)
        if not api_key:
            problems.append("ELEVENLABS_API_KEY is required when EARSHOT_RELAY_TTS_PROVIDER=elevenlabs "
                            "(environment or ELEVENLABS_API_KEY_FILE; never put it in a file in the repo)")
        fmt = (take(env, "EARSHOT_RELAY_TTS_FORMAT", e) or "mp3").lower()
        if fmt not in ("mp3", "opus"):
            problems.append("EARSHOT_RELAY_TTS_FORMAT must be mp3 or opus for ElevenLabs")
    else:
        api_key = take(env, "EARSHOT_RELAY_TTS_API_KEY", e)
        fmt = (take(env, "EARSHOT_RELAY_TTS_FORMAT", e) or "mp3").lower()
        if fmt not in ("mp3", "wav"):
            problems.append("EARSHOT_RELAY_TTS_FORMAT must be mp3 or wav")
    timeout = take(_int, "EARSHOT_RELAY_TTS_TIMEOUT_S", take(env, "EARSHOT_RELAY_TTS_TIMEOUT_S", e), 60, 1, 600)
    leadin = take(_int, "EARSHOT_RELAY_TTS_LEADIN_MS", take(env, "EARSHOT_RELAY_TTS_LEADIN_MS", e), 700, 0, 10000)
    if check_ffmpeg and (leadin or gemini) and shutil.which("ffmpeg") is None:
        problems.append("ffmpeg is not installed; required for Gemini PCM conversion and nonzero EARSHOT_RELAY_TTS_LEADIN_MS")
    if not model:
        return None
    return TtsSettings(provider=provider, url=tts_url, model=model, voice=voice, api_key=api_key, response_format=fmt,
                       timeout_s=float(timeout or 60), leadin_ms=leadin or 0)


def _hygiene_settings(e, take, problems: list[str], *, channels: list[str]) -> dict:
    """EARSHOT_RELAY_NORMALIZE and EARSHOT_RELAY_MAX_WORDS."""
    mode = (take(env, "EARSHOT_RELAY_NORMALIZE", e) or "off").lower()
    if mode not in NORMALIZE_MODES:
        problems.append(f"EARSHOT_RELAY_NORMALIZE must be one of: {', '.join(NORMALIZE_MODES)}")
        mode = "off"
    raw_words = take(env, "EARSHOT_RELAY_MAX_WORDS", e)
    max_words = take(_int, "EARSHOT_RELAY_MAX_WORDS", raw_words, 0, 1, MAX_WORDS_LIMIT) if raw_words else None
    raw_channels = take(env, "EARSHOT_RELAY_CHANNEL_HYGIENE", e)
    overrides = []
    if raw_channels:
        try:
            mapping = json.loads(raw_channels)
            if not isinstance(mapping, dict):
                raise ValueError
            for channel, options in mapping.items():
                if channel not in channels or not isinstance(options, dict) or set(options) - {"normalize", "max_words"}:
                    raise ValueError
                override_mode, words = options.get("normalize"), options.get("max_words")
                if override_mode is not None and override_mode not in NORMALIZE_MODES:
                    raise ValueError
                if words is not None and (type(words) is not int or not 1 <= words <= MAX_WORDS_LIMIT):
                    raise ValueError
                overrides.append((channel, override_mode, words))
        except (ValueError, TypeError, RecursionError):
            problems.append("EARSHOT_RELAY_CHANNEL_HYGIENE must be a JSON object of allowed channels with "
                            "normalize=off|basic and/or max_words=1..1000")
    return {"normalize": mode, "max_words": max_words, "channel_hygiene": tuple(overrides)}


def _watchdog_settings(e, take, problems: list[str], *, beacon_token: str | None) -> dict:
    """The delivery watchdog: off unless EARSHOT_RELAY_WATCHDOG_S is set, and then it needs receipts and a URL."""
    raw_s = take(env, "EARSHOT_RELAY_WATCHDOG_S", e)
    raw_url = take(env, "EARSHOT_RELAY_WATCHDOG_URL", e)
    raw_cooldown = take(env, "EARSHOT_RELAY_WATCHDOG_COOLDOWN_S", e)
    raw_format = take(env, "EARSHOT_RELAY_WATCHDOG_FORMAT", e)
    if raw_s and raw_s.lower() in ("off", "0", "none", "disabled"):
        return {}
    if not raw_s and not raw_url:
        if raw_cooldown:
            problems.append("EARSHOT_RELAY_WATCHDOG_COOLDOWN_S is set but EARSHOT_RELAY_WATCHDOG_S is not")
        if raw_format:
            problems.append("EARSHOT_RELAY_WATCHDOG_FORMAT is set but EARSHOT_RELAY_WATCHDOG_S is not")
        return {}
    seconds = take(_int, "EARSHOT_RELAY_WATCHDOG_S", raw_s, 120, WATCHDOG_MIN_S, WATCHDOG_MAX_S)
    cooldown = take(_int, "EARSHOT_RELAY_WATCHDOG_COOLDOWN_S", raw_cooldown, 1800, 60, 7 * 86400)
    fmt = (raw_format or "auto").lower()
    if fmt not in WATCHDOG_FORMATS:
        problems.append(f"EARSHOT_RELAY_WATCHDOG_FORMAT must be one of: {', '.join(WATCHDOG_FORMATS)}")
    if not raw_url:
        problems.append("EARSHOT_RELAY_WATCHDOG_URL is required when EARSHOT_RELAY_WATCHDOG_S is set")
    else:
        wu = urlsplit(raw_url)
        if wu.scheme not in ("http", "https") or not wu.netloc or wu.fragment:
            problems.append("EARSHOT_RELAY_WATCHDOG_URL must be an http(s) URL without a fragment")
    return {"watchdog_s": seconds, "watchdog_url": raw_url, "watchdog_cooldown_s": cooldown or 1800,
            "watchdog_format": fmt if fmt in WATCHDOG_FORMATS else "auto"}
