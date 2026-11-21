"""Synchronous HTTP client for the CLI. Reads EARSHOT_API_URL and EARSHOT_API_TOKEN (or EARSHOT_API_TOKEN_FILE)."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .config import ConfigError, env

DEFAULT_API_URL = "http://127.0.0.1:8080"


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def client_settings(environ: dict[str, str] | None = None) -> tuple[str, str]:
    """(api_url, token). The token falls back to EARSHOT_RELAY_TOKEN so `docker exec ... earshot-relay say` works."""
    e = os.environ if environ is None else environ
    url = (env("EARSHOT_API_URL", e) or "").rstrip("/") or f"http://127.0.0.1:{env('EARSHOT_RELAY_PORT', e) or '8080'}"
    token = env("EARSHOT_API_TOKEN", e) or env("EARSHOT_RELAY_TOKEN", e)
    if not token:
        raise ConfigError("set EARSHOT_API_TOKEN (or EARSHOT_API_TOKEN_FILE) to the relay's EARSHOT_RELAY_TOKEN")
    return url, token


class RelayClient:
    def __init__(self, api_url: str, token: str, timeout: float = 30):
        self.http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": f"earshot-relay-cli/{__version__}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "RelayClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _handle(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            code = err.get("code") or f"http_{resp.status_code}"
            message = err.get("message") or resp.reason_phrase
            raise ClientError(f"{code}: {message}", resp.status_code, code)
        if not isinstance(data, dict):
            raise ClientError(f"unexpected response (HTTP {resp.status_code})", resp.status_code)
        return data

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            resp = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ClientError("the relay did not answer in time") from None
        except httpx.HTTPError as exc:
            raise ClientError(f"cannot reach the relay at {self.http.base_url} ({type(exc).__name__})") from None
        return self._handle(resp)

    @staticmethod
    def _idem(key: str | None) -> dict[str, str] | None:
        return {"Idempotency-Key": key} if key else None

    def say(self, text: str, channel: str | None = None, speech: str | None = None, voice: str | None = None,
            idempotency_key: str | None = None, normalize: bool | None = None, tag: str | None = None,
            coalesce_window_s: float | None = None, cooldown_s: float | None = None,
            coalesce_s: float | None = None) -> dict:
        body = {"text": text, "channel": channel, "speech": speech, "voice": voice, "normalize": normalize, "tag": tag,
                "coalesce_window_s": coalesce_window_s, "cooldown_s": cooldown_s, "coalesce_s": coalesce_s}
        return self._request("POST", "/v1/messages", json={k: v for k, v in body.items() if v is not None},
                             headers=self._idem(idempotency_key))

    def drop(self, path: str, channel: str | None = None, idempotency_key: str | None = None) -> dict:
        p = Path(path)
        data = {"channel": channel} if channel is not None else None
        media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as fh:
            return self._request("POST", "/v1/messages", files={"file": (p.name, fh, media)}, data=data, timeout=120,
                                 headers=self._idem(idempotency_key))

    def receipt(self, message_id: str, wait: int = 0) -> dict:
        params = {"wait": wait} if wait else None
        return self._request("GET", f"/v1/messages/{message_id}", params=params, timeout=wait + 30)

    def recent(self, limit: int = 20, channel: str | None = None, since: str | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if channel:
            params["channel"] = channel
        if since:
            params["since"] = since
        return self._request("GET", "/v1/messages", params=params)

    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    def doctor(self) -> dict:
        return self._request("POST", "/v1/doctor", timeout=60)
