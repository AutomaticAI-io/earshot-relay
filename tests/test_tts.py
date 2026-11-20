"""Server TTS through a stub OpenAI-compatible server: request shape, auth, failures, timeout, ffmpeg lead-in."""

from __future__ import annotations

import io
import json
import math
import shutil
import socket
import struct
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app

from .conftest import BUCKET

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def tone_wav(seconds: float = 1.0, rate: int = 24000, hz: float = 440.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / rate)))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)
    return buf.getvalue()


class StubTts:
    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "ok"
        self.audio = tone_wav()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                stub.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "json": json.loads(body)})
                if stub.mode == "slow":
                    time.sleep(3)
                if stub.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                payload = b"tiny" if stub.mode == "tiny" else stub.audio
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stub():
    s = StubTts()
    yield s
    s.close()


def tts_env(url: str, **extra) -> dict:
    env = {"EARSHOT_RELAY_TTS_URL": url, "EARSHOT_RELAY_TTS_MODEL": "kokoro", "EARSHOT_RELAY_TTS_VOICE": "af_bella",
           "EARSHOT_RELAY_TTS_API_KEY": "tts-secret-key", "EARSHOT_RELAY_TTS_FORMAT": "wav",
           "EARSHOT_RELAY_CHANNELS": "tts", "EARSHOT_RELAY_DEFAULT_CHANNEL": "tts"}
    env.update(extra)
    return env


def count_tts_objects(s3_admin) -> int:
    return s3_admin.list_objects_v2(Bucket=BUCKET, Prefix="tts/").get("KeyCount", 0)


def leading_silence_seconds(ogg: bytes) -> float:
    """Decode with ffmpeg to 48 kHz mono s16 and return the time of the first sample above 2% full scale."""
    pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
                          "-ar", "48000", "pipe:1"], input=ogg, capture_output=True, check=True).stdout
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    threshold = 0.02 * 32768
    for i, s in enumerate(samples):
        if abs(s) > threshold:
            return i / 48000
    return len(samples) / 48000


@needs_ffmpeg
def test_server_speech_request_shape_and_leadin(make_relay, stub, auth, s3_admin):
    relay = make_relay(**tts_env(stub.url))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": " Deploy finished ", "speech": "server"})
        assert r.status_code == 202, r.text
        body = r.json()
    assert body["speech"] == "server" and body["type"] == "text"
    assert body["key"].startswith("tts/") and body["key"].endswith(".opus")
    req = stub.requests[0]
    assert req["path"] == "/v1/audio/speech"
    assert req["json"] == {"model": "kokoro", "input": "Deploy finished", "voice": "af_bella", "response_format": "wav"}
    assert req["headers"]["authorization"] == "Bearer tts-secret-key"
    assert req["headers"]["content-type"] == "application/json"
    obj = s3_admin.get_object(Bucket=BUCKET, Key=body["key"])
    assert obj["ContentType"] == "audio/ogg"
    data = obj["Body"].read()
    assert data[:4] == b"OggS"
    assert leading_silence_seconds(data) >= 0.69


def test_voice_override_and_no_leadin(make_relay, stub, auth, s3_admin):
    relay = make_relay(**tts_env(stub.url, EARSHOT_RELAY_TTS_LEADIN_MS="0", EARSHOT_RELAY_TTS_API_KEY=None,
                                 EARSHOT_RELAY_DEFAULT_SPEECH="server"))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hi there", "voice": "am_adam"})
        assert r.status_code == 202, r.text
    req = stub.requests[0]
    assert req["json"]["voice"] == "am_adam"
    assert "authorization" not in req["headers"]
    obj = s3_admin.get_object(Bucket=BUCKET, Key=r.json()["key"])
    assert r.json()["key"].endswith(".wav") and obj["ContentType"] == "audio/wav"
    assert obj["Body"].read() == stub.audio  # unchanged at lead-in 0


def test_device_speech_still_default_with_tts_configured(make_relay, stub, auth):
    relay = make_relay(**tts_env(stub.url, EARSHOT_RELAY_TTS_LEADIN_MS="0"))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hi"})
    assert r.json()["speech"] == "device" and r.json()["key"].endswith(".say")
    assert stub.requests == []


@pytest.mark.parametrize("mode, status, code", [("error", 502, "tts_failed"), ("tiny", 502, "tts_failed"),
                                                ("slow", 504, "tts_timeout")])
def test_tts_failures_never_fall_back(make_relay, stub, auth, s3_admin, mode, status, code):
    stub.mode = mode
    relay = make_relay(**tts_env(stub.url, EARSHOT_RELAY_TTS_TIMEOUT_S="1", EARSHOT_RELAY_TTS_LEADIN_MS="0"))
    before = count_tts_objects(s3_admin)
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hello", "speech": "server"})
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code
        assert "tts-secret-key" not in r.text
        assert c.get("/v1/messages", headers=auth).json()["items"] == []
    assert count_tts_objects(s3_admin) == before  # nothing written: no silent .say fallback


def test_tts_unreachable(make_relay, auth):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    relay = make_relay(**tts_env(f"http://127.0.0.1:{port}/v1", EARSHOT_RELAY_TTS_LEADIN_MS="0"))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hello", "speech": "server"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "tts_failed"


@needs_ffmpeg
def test_ffmpeg_failure_is_tts_failed(make_relay, stub, auth):
    stub.audio = b"\x00" * 1000  # not decodable audio
    relay = make_relay(**tts_env(stub.url))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hello", "speech": "server"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "tts_failed"
    assert "ffmpeg" in r.json()["error"]["message"]
