"""Optional server-side TTS: any OpenAI-compatible `POST {base}/audio/speech` (Kokoro-FastAPI, LocalAI, ...), or
ElevenLabs (`POST {base}/v1/text-to-speech/{voice_id}`), chosen with EARSHOT_RELAY_TTS_PROVIDER.

The result gets the same 700 ms lead-in the operator toolkit adds (Bluetooth sinks swallow the first moment) and is
re-encoded to Ogg/Opus with ffmpeg. A failure is an error for the caller: the relay never silently falls back to a
device `.say`, because the caller asked for server speech.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import TtsSettings
from .keys import ApiError

MIN_AUDIO_BYTES = 200  # the operator toolkit's floor for a real synthesis result
# ElevenLabs output_format for each EARSHOT_RELAY_TTS_FORMAT (the audio is re-encoded by the lead-in step anyway).
ELEVENLABS_FORMATS = {"mp3": "mp3_44100_128", "opus": "opus_48000_64"}
CONTENT_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg"}


@dataclass(frozen=True)
class SynthResult:
    data: bytes
    ext: str
    content_type: str


class TtsClient:
    def __init__(self, settings: TtsSettings, max_bytes: int, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.max_bytes = max_bytes
        self._transport = transport

    def _request_body(self, text: str, voice: str | None) -> dict:
        if self.settings.provider == "elevenlabs":
            return {"text": text, "model_id": self.settings.model}
        body = {"model": self.settings.model, "input": text, "response_format": self.settings.response_format}
        chosen = voice or self.settings.voice
        if chosen:
            body["voice"] = chosen
        return body

    def _endpoint(self, voice: str | None) -> tuple[str, dict[str, str]]:
        """The request URL and headers for the configured provider. The API key goes only in a header."""
        key = self.settings.api_key
        if self.settings.provider == "elevenlabs":
            # A dot is encoded too: a voice of "." or ".." must stay one path segment, not rewrite the request path.
            voice_id = quote(voice or self.settings.voice or "", safe="").replace(".", "%2E")
            fmt = ELEVENLABS_FORMATS[self.settings.response_format]
            headers = {"Content-Type": "application/json", "Accept": "audio/*"}
            if key:
                headers["xi-api-key"] = key
            return f"{self.settings.url}/v1/text-to-speech/{voice_id}?output_format={fmt}", headers
        headers = {"Content-Type": "application/json", "Accept": "audio/*"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return f"{self.settings.url}/audio/speech", headers

    async def synthesize(self, text: str, voice: str | None) -> bytes:
        url, headers = self._endpoint(voice)
        timeout = httpx.Timeout(self.settings.timeout_s)
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport, follow_redirects=False) as client:
                async with asyncio.timeout(self.settings.timeout_s):
                    async with client.stream("POST", url, json=self._request_body(text, voice), headers=headers) as resp:
                        if not 200 <= resp.status_code < 300:
                            raise ApiError(502, "tts_failed", f"the TTS server answered HTTP {resp.status_code}")
                        chunks, total = [], 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise ApiError(502, "tts_failed", "the TTS result is larger than EARSHOT_RELAY_MAX_AUDIO_BYTES")
                            chunks.append(chunk)
        except (httpx.TimeoutException, TimeoutError):
            raise ApiError(504, "tts_timeout", f"the TTS server did not answer within {self.settings.timeout_s:g} s") from None
        except httpx.HTTPError as exc:
            raise ApiError(502, "tts_failed", f"the TTS server could not be reached ({type(exc).__name__})") from None
        data = b"".join(chunks)
        if len(data) < MIN_AUDIO_BYTES:
            raise ApiError(502, "tts_failed", f"the TTS server returned {len(data)} bytes, not audio")
        return data

    async def render(self, text: str, voice: str | None) -> SynthResult:
        raw = await self.synthesize(text, voice)
        fmt = self.settings.response_format
        if self.settings.leadin_ms <= 0:
            return SynthResult(raw, fmt, CONTENT_TYPES[fmt])
        return SynthResult(await add_leadin(raw, fmt, self.settings.leadin_ms), "opus", "audio/ogg")


async def add_leadin(audio: bytes, input_ext: str, ms: int, timeout_s: float = 120) -> bytes:
    """`ffmpeg -i in -af adelay=<ms>:all=1 -c:a libopus -b:a 48k out.opus`, the operator toolkit's exact recipe."""
    with tempfile.TemporaryDirectory(prefix="earshot-relay-tts-") as d:
        src = Path(d) / f"in.{input_ext}"
        dest = Path(d) / "out.opus"
        src.write_bytes(audio)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
                "-af", f"adelay={ms}:all=1", "-c:a", "libopus", "-b:a", "48k", str(dest),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ApiError(502, "tts_failed", "ffmpeg is not installed; it is needed for the lead-in") from None
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ApiError(502, "tts_failed", "ffmpeg timed out adding the lead-in") from None
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < MIN_AUDIO_BYTES:
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1][:200] if detail else f"exit {proc.returncode}"
            raise ApiError(502, "tts_failed", f"ffmpeg could not process the TTS audio: {reason}")
        return dest.read_bytes()

