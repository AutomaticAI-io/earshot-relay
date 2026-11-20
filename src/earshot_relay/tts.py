"""Optional server-side TTS through any OpenAI-compatible `POST {base}/audio/speech` (Kokoro-FastAPI, LocalAI, ...).

The result gets the same 700 ms lead-in the operator toolkit adds (Bluetooth sinks swallow the first moment) and is
re-encoded to Ogg/Opus with ffmpeg. A failure is an error for the caller: the relay never silently falls back to a
device `.say`, because the caller asked for server speech.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import TtsSettings
from .keys import ApiError

MIN_AUDIO_BYTES = 200  # the operator toolkit's floor for a real synthesis result


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
        body = {"model": self.settings.model, "input": text, "response_format": self.settings.response_format}
        chosen = voice or self.settings.voice
        if chosen:
            body["voice"] = chosen
        return body

    async def synthesize(self, text: str, voice: str | None) -> bytes:
        headers = {"Content-Type": "application/json", "Accept": "audio/*"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        url = f"{self.settings.url}/audio/speech"
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
            return SynthResult(raw, fmt, "audio/mpeg" if fmt == "mp3" else "audio/wav")
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

