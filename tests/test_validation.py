"""Unit vectors shared with the app: channel grammar, .say text limits, key shape, audio types, voice, config."""

from __future__ import annotations

import re

import pytest

from earshot_relay.config import ConfigError, load_settings, parse_rate
from earshot_relay.keys import (
    ApiError,
    audio_extension,
    check_voice,
    clean_text,
    content_type_for,
    is_valid_channel,
    new_key,
    utf16_units,
)

from .conftest import base_env

# The app's own vectors (MiscCoreTest.channelRuleMatchesTheCliAndProxy, BetaBucketTest).
GOOD_CHANNELS = ["phone", "car", "a", "0", "emu-test", "a_b-c9", "x" * 32, "alarm"]
BAD_CHANNELS = ["Car", "car.1", "-car", "_car", "", "x" * 33, "a b", "a/b", "a.b", "phone/", None, 5]


@pytest.mark.parametrize("name", GOOD_CHANNELS)
def test_channel_grammar_accepts(name):
    assert is_valid_channel(name)


@pytest.mark.parametrize("name", BAD_CHANNELS)
def test_channel_grammar_refuses(name):
    assert not is_valid_channel(name)


EMOJI = "\U0001F600"  # one code point, two UTF-16 code units (Kotlin String.length counts 2)


def test_utf16_counts_like_kotlin():
    assert utf16_units("abc") == 3
    assert utf16_units(EMOJI) == 2
    assert utf16_units("é") == 1


@pytest.mark.parametrize(
    "text, ok",
    [
        ("a" * 3999, True),
        ("a" * 4000, True),
        ("a" * 4001, False),
        ("a" * 3998 + EMOJI, True),  # 4000 units exactly, emoji at the edge
        ("a" * 3999 + EMOJI, False),  # 4001 units: the emoji straddles the limit
        ("a" * 3997 + EMOJI, True),  # 3999 units
        (EMOJI * 2000, True),  # 4000 units, 2000 code points
        (EMOJI * 2001, False),
    ],
)
def test_text_length_boundaries(text, ok):
    if ok:
        assert clean_text(text) == text
    else:
        with pytest.raises(ApiError) as e:
            clean_text(text)
        assert (e.value.status, e.value.code, e.value.field) == (422, "text_too_long", "text")


def test_text_is_trimmed_and_empty_refused():
    assert clean_text("  Build finished \n") == "Build finished"
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ApiError) as e:
            clean_text(blank)
        assert e.value.code == "empty_text"


def test_trim_happens_before_the_length_check():
    assert clean_text("  " + "a" * 4000 + "  ") == "a" * 4000


def test_utf8_cap_is_a_second_check(monkeypatch):
    import earshot_relay.keys as keys

    monkeypatch.setattr(keys, "MAX_TEXT_UTF16_UNITS", 10**6)
    big = "é" * 32769  # 65538 UTF-8 bytes, under the patched unit limit
    with pytest.raises(ApiError) as e:
        keys.clean_text(big)
    assert e.value.code == "text_too_long"
    assert keys.clean_text("é" * 32768) == "é" * 32768  # exactly 65536 bytes


def test_text_type_and_surrogates():
    with pytest.raises(ApiError) as e:
        clean_text(42)
    assert e.value.code == "invalid_field"
    with pytest.raises(ApiError) as e:
        clean_text("bad \ud800 surrogate")
    assert e.value.code == "invalid_text"


def test_key_shape_is_opaque():
    k = new_key("phone", "say")
    assert re.fullmatch(r"phone/[0-9a-f]{32}\.say", k.key)
    assert k.message_id == "msg_" + k.key.split("/")[1].split(".")[0]
    assert new_key("phone", "say").key != k.key
    with pytest.raises(ValueError):
        new_key("Bad", "say")


@pytest.mark.parametrize(
    "filename, media, ext",
    [
        ("clip.mp3", None, "mp3"),
        ("CLIP.OGG", None, "ogg"),
        ("dir/voice.opus", None, "opus"),
        ("x.m4a", "application/octet-stream", "m4a"),
        ("noext", "audio/wav", "wav"),
        ("noext", "audio/x-wav; charset=binary", "wav"),
        (None, "audio/mpeg", "mp3"),
        ("", "audio/webm", "webm"),
        (".mp3", "audio/flac", "flac"),  # a dot-file has no extension, like the app's AudioKeys
    ],
)
def test_audio_extension(filename, media, ext):
    assert audio_extension(filename, media) == ext


@pytest.mark.parametrize("filename, media", [("notes.txt", None), ("message.say", None), ("x.exe", "audio/mpeg"),
                                             ("noext", "application/octet-stream"), (None, None)])
def test_audio_extension_refused(filename, media):
    with pytest.raises(ApiError) as e:
        audio_extension(filename, media)
    assert (e.value.status, e.value.code) == (415, "unsupported_media_type")


def test_content_types_match_the_spec():
    assert content_type_for("say") == "text/plain; charset=utf-8"
    assert content_type_for("ogg") == content_type_for("opus") == "audio/ogg"
    assert [content_type_for(e) for e in ("mp3", "wav", "m4a", "aac", "flac", "webm")] == [
        "audio/mpeg", "audio/wav", "audio/mp4", "audio/aac", "audio/flac", "audio/webm"]


def test_voice_grammar():
    assert check_voice(None) is None
    assert check_voice("af_bella") == "af_bella"
    assert check_voice("en-US: Jenny (neural)+v2.1") == "en-US: Jenny (neural)+v2.1"
    for bad in ("", "x" * 101, "a/b", "a;b", "voice\n", 7):
        with pytest.raises(ApiError) as e:
            check_voice(bad)
        assert e.value.code == "invalid_voice"


# ---- config ----------------------------------------------------------------------------------------------------


def env(tmp_path, **extra):
    return base_env("https://s3.example.com", str(tmp_path), **extra)


def test_config_ok_defaults(tmp_path):
    s = load_settings(env(tmp_path, EARSHOT_RELAY_PUBLIC_URL=None), check_ffmpeg=False)
    assert s.channels == ("phone",)
    assert s.default_channel == "phone"
    assert not s.alarm_allowed
    assert s.rate_limit.describe() == "60/minute"
    assert s.alarm_rate_limit.describe() == "6/hour"
    assert s.max_audio_bytes == 26_214_400 and s.receipt_wait_min == 60 and s.retention_days == 7
    assert s.host == "127.0.0.1" and s.port == 8080
    assert "writer-secret-value" not in repr(s) and "t" * 40 not in repr(s)


@pytest.mark.parametrize(
    "extra, needle",
    [
        ({"EARSHOT_RELAY_TOKEN": "short"}, "EARSHOT_RELAY_TOKEN must be at least 32"),
        ({"EARSHOT_RELAY_BEACON_TOKEN": "t" * 40}, "must differ"),
        ({"EARSHOT_RELAY_BEACON_TOKEN": "x" * 31}, "EARSHOT_RELAY_BEACON_TOKEN must be at least 32"),
        ({"EARSHOT_RELAY_CHANNELS": "phone,Bad"}, "invalid channel name"),
        ({"EARSHOT_RELAY_DEFAULT_CHANNEL": "car"}, "EARSHOT_RELAY_DEFAULT_CHANNEL must be one of"),
        ({"AWS_SECRET_ACCESS_KEY": None}, "AWS_SECRET_ACCESS_KEY is required"),
        ({"EARSHOT_S3_ENDPOINT": "http://s3.example.com", "EARSHOT_RELAY_ALLOW_HTTP_S3": None}, "must use https"),
        ({"EARSHOT_BUCKET": "a/b"}, "single bucket name"),
        ({"EARSHOT_RELAY_TTS_URL": "http://kokoro:8880/v1"}, "EARSHOT_RELAY_TTS_MODEL is required"),
        ({"EARSHOT_RELAY_DEFAULT_SPEECH": "server"}, "needs EARSHOT_RELAY_TTS_URL"),
        ({"EARSHOT_RELAY_RATE_LIMIT": "lots"}, "EARSHOT_RELAY_RATE_LIMIT must look like"),
        ({"EARSHOT_RELAY_PUBLIC_URL": "http://relay.example.com"}, "EARSHOT_RELAY_PUBLIC_URL must be an https"),
        ({"EARSHOT_RELAY_BEACON_PORT": "8080"}, "must differ from EARSHOT_RELAY_PORT"),
    ],
)
def test_config_refuses(tmp_path, extra, needle):
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, **extra), check_ffmpeg=False)
    assert needle in str(e.value)
    for secret in ("writer-secret-value", "t" * 40, "b" * 40):
        assert secret not in str(e.value)


def test_config_reports_every_problem_at_once(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, EARSHOT_BUCKET=None, EARSHOT_RELAY_TOKEN="short"), check_ffmpeg=False)
    assert "EARSHOT_BUCKET is required" in str(e.value) and "EARSHOT_RELAY_TOKEN must" in str(e.value)


def test_config_ffmpeg_missing_with_leadin(tmp_path, monkeypatch):
    monkeypatch.setattr("earshot_relay.config.shutil.which", lambda _name: None)
    extra = {"EARSHOT_RELAY_TTS_URL": "http://kokoro:8880/v1", "EARSHOT_RELAY_TTS_MODEL": "kokoro",
             "EARSHOT_RELAY_TTS_VOICE": "af_heart"}
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, **extra))
    assert "ffmpeg is not installed" in str(e.value)
    s = load_settings(env(tmp_path, EARSHOT_RELAY_TTS_LEADIN_MS="0", **extra))
    assert s.tts.leadin_ms == 0


def test_config_file_variant(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("f" * 40 + "\n")
    s = load_settings(env(tmp_path, EARSHOT_RELAY_TOKEN=None, EARSHOT_RELAY_TOKEN_FILE=str(secret)), check_ffmpeg=False)
    assert s.token == "f" * 40
    with pytest.raises(ConfigError) as e:
        load_settings(env(tmp_path, EARSHOT_RELAY_TOKEN_FILE=str(secret)), check_ffmpeg=False)
    assert "not both" in str(e.value)


def test_alarm_must_be_listed(tmp_path):
    assert not load_settings(env(tmp_path), check_ffmpeg=False).alarm_allowed
    assert load_settings(env(tmp_path, EARSHOT_RELAY_CHANNELS="phone, alarm"), check_ffmpeg=False).alarm_allowed


def test_parse_rate():
    assert parse_rate("X", "10/s", "60/min").per_seconds == 1
    assert parse_rate("X", None, "6/hour").count == 6
    assert parse_rate("X", "off", "60/min") is None
    with pytest.raises(ConfigError):
        parse_rate("X", "0/min", "60/min")
