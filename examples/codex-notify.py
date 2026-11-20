#!/usr/bin/env python3
"""Codex CLI `notify` program: speak a short gist when Codex finishes a turn. Standard library only.

  ~/.codex/config.toml:   notify = ["python3", "/path/to/codex-notify.py"]

Codex passes one JSON argument: {"type": "agent-turn-complete", "last-assistant-message": "...", "cwd": ...}.
WARNING: this sends a slice of the agent's text to your bucket. Use a private bucket. Always exits 0.
Environment: EARSHOT_API_URL (default http://127.0.0.1:8080), EARSHOT_API_TOKEN or EARSHOT_API_TOKEN_FILE,
optional EARSHOT_HOOK_CHANNEL. Self-contained on purpose: copy this one file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

MAX_CHARS = 300
TIMEOUT_S = 5


def read_token(environ=os.environ) -> str | None:
    token = environ.get("EARSHOT_API_TOKEN")
    path = environ.get("EARSHOT_API_TOKEN_FILE")
    if not token and path:
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            return None
    return token or None


def strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # fenced code is noise when spoken
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>)\s+", "", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def gist(text: str, limit: int = MAX_CHARS) -> str:
    """The first one or two sentences, at most `limit` characters."""
    plain = strip_markdown(text)
    sentences = re.findall(r".+?(?:[.!?](?=\s|$)|$)", plain)
    out = ""
    for sentence in sentences[:2]:
        candidate = (out + " " + sentence.strip()).strip()
        if len(candidate) > limit:
            break
        out = candidate
    if not out:  # the first sentence alone is too long: cut on a word boundary
        out = plain[:limit].rsplit(" ", 1)[0] if len(plain) > limit else plain
    return out.strip()


def send(line: str, environ=os.environ) -> None:
    token = read_token(environ)
    if not token:
        return
    base = (environ.get("EARSHOT_API_URL") or "http://127.0.0.1:8080").rstrip("/")
    body = {"text": line}
    if environ.get("EARSHOT_HOOK_CHANNEL"):
        body["channel"] = environ["EARSHOT_HOOK_CHANNEL"]
    request = urllib.request.Request(
        base + "/v1/messages", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S):
        pass


def main() -> int:
    try:
        event = json.loads(sys.argv[-1])
        if event.get("type") != "agent-turn-complete":
            return 0
        project = os.path.basename((event.get("cwd") or "").rstrip("/")) or "your project"
        send(gist(event.get("last-assistant-message") or "") or f"Codex finished in {project}.")
    except Exception:  # noqa: BLE001 - never fail the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
