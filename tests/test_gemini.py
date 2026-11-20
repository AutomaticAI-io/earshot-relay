"""Provider contract fixtures only: no paid provider requests or phone playback claims."""
import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import ConfigError, load_settings
from earshot_relay.tts import TtsClient

from .conftest import BUCKET, base_env
from .test_tts import leading_silence_seconds, needs_ffmpeg, tone_wav

KEY = "gemini-fixture-secret-not-a-real-key"


def reply(pcm=None, mime="audio/L16;codec=pcm;rate=24000"):
    if pcm is None:
        with wave.open(io.BytesIO(tone_wav()), "rb") as source:
            pcm = source.readframes(source.getnframes())
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(pcm).decode()}}]}}]}


def env(**extra):
    return {"EARSHOT_RELAY_TTS_PROVIDER": "gemini", "GEMINI_API_KEY": KEY,
            "EARSHOT_RELAY_TTS_VOICE": "Kore", **extra}


@needs_ffmpeg
def test_gemini_pcm_request_and_decoded_audio(make_relay, auth, s3_admin):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=reply())

    relay = make_relay(tts_transport=httpx.MockTransport(handle), **env())
    with TestClient(create_app(relay)) as client:
        response = client.post("/v1/messages", headers=auth,
                               json={"text": "read this exactly", "speech": "server", "voice": "Puck"})
        assert response.status_code == 202, response.text
        assert client.get("/v1/status", headers=auth).json()["speech"]["provider"] == "gemini"
    req = requests[0]
    assert str(req.url) == ("https://generativelanguage.googleapis.com/v1beta/models/"
                            "gemini-2.5-flash-preview-tts:generateContent")
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
    assert "authorization" not in req.headers
    assert json.loads(req.content) == {
        "contents": [{"parts": [{"text": "read this exactly"}]}], "generationConfig": {
            "responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": "Puck"}}}}}
    obj = s3_admin.get_object(Bucket=BUCKET, Key=response.json()["key"])
    assert response.json()["key"].endswith(".opus") and obj["ContentType"] == "audio/ogg"
    assert leading_silence_seconds(obj["Body"].read()) >= 0.700


@pytest.mark.parametrize("bad", [
    {}, [], {"error": {"message": KEY}}, {"candidates": []},
    {"candidates": [{"finishReason": "SAFETY"}]}, reply(b"a" * 201), reply(b"a" * 100),
    reply(mime="audio/L16;rate=48000"), reply(mime="audio/mp3"),
    {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000", "data": "!bad!"}}]}}]},
])
def test_malformed_provider_reply_writes_nothing_and_is_safe(make_relay, auth, bad, caplog):
    relay = make_relay(tts_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad)), **env())
    client = TestClient(create_app(relay))
    result = client.post("/v1/messages", headers=auth, json={"text": "private-input", "speech": "server"})
    assert result.status_code == 502, result.text
    assert result.json()["error"]["code"] == "tts_failed"
    assert relay.store.get_message("latest") is None and relay.last_write_ok_at is None
    assert KEY not in result.text + caplog.text + repr(relay.settings)
    assert "private-input" not in result.text + caplog.text


@pytest.mark.parametrize("kind", ["http", "timeout", "oversize", "decoded_oversize", "json"])
def test_provider_errors_and_bounds(make_relay, auth, kind):
    def handle(request):
        if kind == "http":
            return httpx.Response(403, text=KEY)
        if kind == "timeout":
            raise httpx.ReadTimeout(KEY)
        if kind == "json":
            return httpx.Response(200, text="[" * 5000)
        if kind == "decoded_oversize":
            return httpx.Response(200, json=reply(b"a" * 1026))
        return httpx.Response(200, content=b"a" * 70000)
    relay = make_relay(tts_transport=httpx.MockTransport(handle), **env(EARSHOT_RELAY_MAX_AUDIO_BYTES="1024"))
    result = TestClient(create_app(relay)).post("/v1/messages", headers=auth,
                                               json={"text": "hello", "speech": "server"})
    assert result.status_code == (504 if kind == "timeout" else 502)
    assert KEY not in result.text and relay.last_write_ok_at is None


@needs_ffmpeg
@pytest.mark.parametrize("provider", ["openai", "elevenlabs", "gemini"])
def test_each_adapter_has_at_least_700_ms_decoded_leadin(make_relay, provider):
    config = {"EARSHOT_RELAY_TTS_PROVIDER": provider, "EARSHOT_RELAY_TTS_VOICE": "Kore",
              "EARSHOT_RELAY_TTS_API_KEY": KEY}
    if provider == "openai":
        config.update(EARSHOT_RELAY_TTS_URL="https://voice.invalid/v1", EARSHOT_RELAY_TTS_MODEL="fixture",
                      EARSHOT_RELAY_TTS_FORMAT="wav")
    def handle(_):
        return httpx.Response(200, json=reply()) if provider == "gemini" else httpx.Response(200, content=tone_wav())
    relay = make_relay(**config)
    client = TtsClient(relay.settings.tts, relay.settings.max_audio_bytes, httpx.MockTransport(handle))
    result = asyncio.run(client.render("fixture tone, not synthesis", None))
    silence = leading_silence_seconds(result.data)
    assert 0.700 <= silence <= 0.750, (provider, silence)


def test_gemini_key_file_and_config(moto_endpoint, tmp_path):
    key_file = tmp_path / "secret"
    key_file.write_text(KEY)
    settings = load_settings(base_env(moto_endpoint, str(tmp_path), **env(GEMINI_API_KEY=None,
                                      GEMINI_API_KEY_FILE=str(key_file))), check_ffmpeg=False)
    assert settings.tts.api_key == KEY and settings.tts.response_format == "pcm"
    for extra, match in [({"GEMINI_API_KEY": None}, "GEMINI_API_KEY is required"),
                         ({"EARSHOT_RELAY_TTS_MODEL": "../bad"}, "model id"),
                         ({"EARSHOT_RELAY_TTS_FORMAT": "mp3"}, "must be pcm")]:
        with pytest.raises(ConfigError, match=match):
            load_settings(base_env(moto_endpoint, str(tmp_path), **env(**extra)), check_ffmpeg=False)
