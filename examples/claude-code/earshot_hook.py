#!/usr/bin/env python3
"""Claude Code hook: speak a short line on your phone through earshot-relay. Standard library only.

  Notification -> "Claude Code needs attention in <project>"
  Stop         -> the first sentence or two of the assistant's last message (about 300 characters at most)

WARNING: the Stop hook sends a slice of what the agent wrote to your bucket. Use a private bucket and a
private channel, and leave the Stop hook out if the agent works with anything you would not say aloud.

Configuration (environment): EARSHOT_API_URL (default http://127.0.0.1:8080), EARSHOT_API_TOKEN or
EARSHOT_API_TOKEN_FILE, optional EARSHOT_HOOK_CHANNEL. This script always exits 0: a hook that fails must
never block or slow the agent (exit code 2 on Stop would keep the agent working).
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


def last_assistant_text(transcript_path: str) -> str:
    """Fallback when the payload has no last_assistant_message: read the transcript (JSONL)."""
    found = ""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if entry.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    parts = [content]
                else:
                    parts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
                text = " ".join(p for p in parts if p).strip()
                if text:
                    found = text
    except OSError:
        return ""
    return found


def project_name(payload: dict) -> str:
    return os.path.basename((payload.get("cwd") or "").rstrip("/")) or "your project"


def build_line(payload: dict) -> str | None:
    event = payload.get("hook_event_name")
    if event == "Notification":
        return f"Claude Code needs attention in {project_name(payload)}."
    if event == "Stop":
        if payload.get("stop_hook_active"):
            return None  # a Stop hook already kept the agent going; do not speak twice
        text = payload.get("last_assistant_message") or last_assistant_text(payload.get("transcript_path") or "")
        return gist(text) or f"Claude Code finished in {project_name(payload)}."
    return None


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
        payload = json.load(sys.stdin)
        line = build_line(payload) if isinstance(payload, dict) else None
        if line:
            send(line)
    except Exception:  # noqa: BLE001 - never fail the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
