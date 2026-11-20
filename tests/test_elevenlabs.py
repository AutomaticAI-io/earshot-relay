"""The ElevenLabs adapter against a stub server only: no real key, no network. Request shape, auth header, formats,
failures, and the key never reaching an error, a status page or a log."""

from __future__ import annotations

import json
import logging

import pytest
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import ConfigError, load_settings

from .conftest import BUCKET, base_env
from .test_tts import StubTts, count_tts_objects, needs_ffmpeg

KEY = "xi-stub-key-not-real-0123456789"
VOICE = "VoiceIdStub0123456789"


@pytest.fixture
def stub():
    s = StubTts()
    yield s
    s.close()


def eleven_env(stub: StubTts, **extra) -> dict:
    env = {"EARSHOT_RELAY_TTS_PROVIDER": "elevenlabs", "EARSHOT_RELAY_TTS_URL": stub.url[: -len("/v1")],
           "ELEVENLABS_API_KEY": KEY, "EARSHOT_RELAY_TTS_VOICE": VOICE, "EARSHOT_RELAY_TTS_LEADIN_MS": "0",
           "EARSHOT_RELAY_CHANNELS": "tts", "EARSHOT_RELAY_DEFAULT_CHANNEL": "tts"}
    env.update(extra)
    return env


def test_request_shape_and_stored_object(make_relay, stub, auth, s3_admin):
    relay = make_relay(**eleven_env(stub))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": " Build is green ", "speech": "server"})
        assert r.status_code == 202, r.text
        body = r.json()
    assert body["speech"] == "server" and body["key"].endswith(".mp3")
    req = stub.requests[0]
    assert req["path"] == f"/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128"
    assert req["json"] == {"text": "Build is green", "model_id": "eleven_multilingual_v2"}
    assert req["headers"]["xi-api-key"] == KEY and "authorization" not in req["headers"]
    obj = s3_admin.get_object(Bucket=BUCKET, Key=body["key"])
    assert obj["Body"].read() == stub.audio and obj["ContentType"] == "audio/mpeg"


def test_model_id_voice_override_and_opus_format(make_relay, stub, auth):
    relay = make_relay(**eleven_env(stub, EARSHOT_RELAY_TTS_MODEL="eleven_flash_v2_5", EARSHOT_RELAY_TTS_FORMAT="opus"))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hi", "speech": "server", "voice": "Other_Voice9"})
        assert r.status_code == 202, r.text
    req = stub.requests[0]
    assert req["path"] == "/v1/text-to-speech/Other_Voice9?output_format=opus_48000_64"
    assert req["json"]["model_id"] == "eleven_flash_v2_5"
    assert r.json()["key"].endswith(".opus")


@needs_ffmpeg
def test_leadin_path_still_produces_ogg_opus(make_relay, stub, auth, s3_admin):
    relay = make_relay(**eleven_env(stub, EARSHOT_RELAY_TTS_LEADIN_MS="700"))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hello", "speech": "server"})
        assert r.status_code == 202, r.text
    assert r.json()["key"].endswith(".opus")
    assert s3_admin.get_object(Bucket=BUCKET, Key=r.json()["key"])["Body"].read()[:4] == b"OggS"


@pytest.mark.parametrize("mode, status, code", [("error", 502, "tts_failed"), ("tiny", 502, "tts_failed")])
def test_failures_never_fall_back_and_never_leak_the_key(make_relay, stub, auth, s3_admin, caplog, mode, status, code):
    caplog.set_level(logging.DEBUG)
    stub.mode = mode
    before = count_tts_objects(s3_admin)
    relay = make_relay(**eleven_env(stub))
    with TestClient(create_app(relay)) as c:
        r = c.post("/v1/messages", headers=auth, json={"text": "hi", "speech": "server"})
        assert r.status_code == status and r.json()["error"]["code"] == code
        shown = r.text + json.dumps(c.get("/v1/status", headers=auth).json()) + caplog.text + repr(relay.settings)
    assert KEY not in shown
    assert count_tts_objects(s3_admin) == before


def test_status_names_the_provider_not_the_key(make_relay, stub, auth):
    relay = make_relay(**eleven_env(stub))
    with TestClient(create_app(relay)) as c:
        s = c.get("/v1/status", headers=auth)
    assert s.json()["speech"]["provider"] == "elevenlabs" and KEY not in s.text


def test_key_may_come_from_a_file_and_the_relay_variable(moto_endpoint, tmp_path):
    secret = tmp_path / "key"
    secret.write_text(KEY + "\n")
    env = base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_TTS_PROVIDER="elevenlabs",
                   EARSHOT_RELAY_TTS_VOICE=VOICE, ELEVENLABS_API_KEY_FILE=str(secret), EARSHOT_RELAY_TTS_LEADIN_MS="0")
    s = load_settings(env, check_ffmpeg=False)
    assert s.tts.api_key == KEY and s.tts.url == "https://api.elevenlabs.io" and s.tts.model == "eleven_multilingual_v2"
    env.pop("ELEVENLABS_API_KEY_FILE")
    env["EARSHOT_RELAY_TTS_API_KEY"] = KEY
    assert load_settings(env, check_ffmpeg=False).tts.api_key == KEY


@pytest.mark.parametrize("extra, expect", [
    ({"ELEVENLABS_API_KEY": None}, "ELEVENLABS_API_KEY is required"),
    ({"EARSHOT_RELAY_TTS_VOICE": None}, "EARSHOT_RELAY_TTS_VOICE is required"),
    ({"EARSHOT_RELAY_TTS_FORMAT": "wav"}, "mp3 or opus for ElevenLabs"),
    ({"EARSHOT_RELAY_TTS_PROVIDER": "polly"}, "PROVIDER must be one of"),
    ({"EARSHOT_RELAY_TTS_URL": "https://user:pw@api.elevenlabs.io"}, "must not carry credentials"),
])
def test_config_errors_never_echo_values(moto_endpoint, tmp_path, extra, expect):
    env = base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_TTS_PROVIDER="elevenlabs", ELEVENLABS_API_KEY=KEY,
                   EARSHOT_RELAY_TTS_VOICE=VOICE, EARSHOT_RELAY_TTS_LEADIN_MS="0")
    env.update(extra)
    env = {k: v for k, v in env.items() if v is not None}
    with pytest.raises(ConfigError, match=expect) as err:
        load_settings(env, check_ffmpeg=False)
    assert KEY not in str(err.value) and "pw" not in str(err.value).replace("PROVIDER", "")


def test_openai_provider_is_unchanged_by_default(moto_endpoint, tmp_path):
    s = load_settings(base_env(moto_endpoint, str(tmp_path), EARSHOT_RELAY_TTS_URL="http://kokoro:8880/v1",
                               EARSHOT_RELAY_TTS_MODEL="kokoro", EARSHOT_RELAY_TTS_VOICE="af_bella",
                               EARSHOT_RELAY_TTS_LEADIN_MS="0"), check_ffmpeg=False)
    assert s.tts.provider == "openai" and s.tts.response_format == "mp3"
