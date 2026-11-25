"""EARS-48 hook policy against synthetic profiles and supervised descendants."""
import http.server
import json
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time
from typing import cast

import pytest

HOOK = pathlib.Path(__file__).resolve().parents[1] / "examples/hermes/earshot-say.sh"
WATCH_SECONDS = 1.4  # Longer than the independently reported one-second cron mutant.

# Each invocation has its own subreaper, so teardown cannot kill another pytest child.
# The final acceptance run additionally puts every process in a private PID/network namespace.
SUPERVISOR = r'''import ctypes, json, os, pathlib, signal, subprocess, sys, time
hook, capture_name, interval = sys.argv[1], sys.argv[2], float(sys.argv[3])
capture = pathlib.Path(capture_name)
pending = pathlib.Path(capture_name + ".pending")
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "cannot enable fixture subreaper")
proc = None
result = None
try:
    payload = sys.stdin.read()
    proc = subprocess.Popen([hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate(payload, timeout=5)
    seen = False
    deadline = time.monotonic() + interval
    while time.monotonic() < deadline:
        seen = seen or capture.exists() or pending.exists()
        time.sleep(0.01)
    seen = seen or capture.exists() or pending.exists()
    result = {"exit": proc.returncode, "stderr": stderr, "seen": seen}
finally:
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait()
    children = pathlib.Path(f"/proc/self/task/{os.getpid()}/children")
    deadline = time.monotonic() + 3
    while True:
        for pid in map(int, children.read_text().split()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("fixture descendant did not terminate")
        time.sleep(0.01)
if result is not None:
    print(json.dumps(result))
'''


@pytest.fixture
def hook_rig(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "capture"
    cli = bin_dir / "earshot-relay"
    cli.write_text(
        '#!/bin/sh\n'
        '{ printf \'%s\\n\' "$EARSHOT_API_URL" "$EARSHOT_API_TOKEN" "$*"; cat; } '
        '> "$CAPTURE.pending"\nmv "$CAPTURE.pending" "$CAPTURE"\n'
    )
    cli.chmod(0o700)
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(SUPERVISOR)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EARSHOT_") and k not in ("BASH_ENV", "ENV", "PYTHONHOME")}
    env.update(HOME=str(home), HERMES_HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               CAPTURE=str(capture), SUPERVISOR=str(supervisor))
    return home, capture, env


def configure(home, url: str | None = "http://127.0.0.1:12345", token: str | None = "profile-token", extra=""):
    lines = []
    if url is not None:
        lines.append("EARSHOT_API_URL=" + shlex.quote(url))
    if token is not None:
        lines.append("EARSHOT_API_TOKEN=" + shlex.quote(token))
    (home / "earshot.env").write_text("\n".join(lines) + "\n" + extra)
    (home / "earshot.env").chmod(0o600)


def run(env, platform="cli", text="A short reply"):
    result = subprocess.run([sys.executable, env["SUPERVISOR"], str(HOOK), env["CAPTURE"], str(WATCH_SECONDS)],
                            input=json.dumps({"extra": {"platform": platform, "assistant_response": text}}),
                            text=True, env=env, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["exit"] == 0, outcome["stderr"]
    return outcome


def assert_quiet(outcome, capture):
    assert not outcome["seen"], "forbidden detached send occurred during bounded observation"
    assert not capture.exists() and not capture.with_suffix(".pending").exists()


def receive(capture):
    assert capture.exists(), "expected detached send was not captured during observation"
    return capture.read_text().splitlines()


@pytest.mark.parametrize("platform", ["cron", "subagent", "desktop", "unknown", ""])
def test_hermes_hook_forbidden_surfaces_stay_quiet(hook_rig, platform):
    home, capture, env = hook_rig
    configure(home)
    # Force even the protected exclusions into the configured allowlist.
    env["EARSHOT_HERMES_PLATFORMS"] = "cli ntfy cron subagent"
    assert_quiet(run(env, platform), capture)


@pytest.mark.parametrize("platform", ["cli", "tui", "ntfy"])
def test_hermes_hook_uses_profile_pair_for_allowed_surface(hook_rig, platform):
    home, capture, env = hook_rig
    configure(home)
    env.update(EARSHOT_API_URL="http://inherited.invalid", EARSHOT_API_TOKEN="inherited-token",
               EARSHOT_API_URL_FILE="/nonexistent", EARSHOT_API_TOKEN_FILE="/nonexistent",
               EARSHOT_RELAY_TOKEN="inherited-relay-token", EARSHOT_RELAY_PORT="9000")
    run(env, platform)
    assert receive(capture) == ["http://127.0.0.1:12345", "profile-token",
                                "say --normalize --tag hermes -", "A short reply"]


@pytest.mark.parametrize("url,token,extra", [
    (None, "profile-token", ""),
    ("http://127.0.0.1:12345", None, ""),
    (None, None, ""),
    ("", "profile-token", ""),
    ("   ", "profile-token", ""),
    ("/", "profile-token", ""),
    ("///", "profile-token", ""),
    ("http://", "profile-token", ""),
    (" http://127.0.0.1:12345 ", "profile-token", ""),
    ("http://127.0.0.1:12345", "", ""),
    ("http://127.0.0.1:12345", "  \t  ", ""),
    ("http://127.0.0.1:12345", "profile-token", "EARSHOT_API_URL_FILE=/nonexistent\n"),
    ("http://127.0.0.1:12345", "profile-token", "EARSHOT_API_TOKEN_FILE=/nonexistent\n"),
    ("http://127.0.0.1:12345", "profile-token", "EARSHOT_RELAY_TOKEN=other-token\n"),
    ("http://127.0.0.1:12345", "profile-token", "EARSHOT_RELAY_PORT=8080\n"),
])
def test_hermes_hook_rejects_incomplete_or_ambiguous_profile(hook_rig, url, token, extra):
    home, capture, env = hook_rig
    configure(home, url, token, extra)
    env.update(EARSHOT_API_URL="http://inherited.invalid", EARSHOT_API_TOKEN="inherited-token",
               EARSHOT_API_URL_FILE="/nonexistent", EARSHOT_API_TOKEN_FILE="/nonexistent",
               EARSHOT_RELAY_TOKEN="inherited-relay-token", EARSHOT_RELAY_PORT="9000")
    assert_quiet(run(env), capture)


def test_hermes_hook_empty_reply_and_missing_profile_file_are_quiet(hook_rig):
    home, capture, env = hook_rig
    configure(home)
    assert_quiet(run(env, platform="ntfy", text=""), capture)
    (home / "earshot.env").unlink()
    assert_quiet(run(env, platform="ntfy"), capture)


def test_hermes_hook_caps_reply_and_respects_narrow_allowlist(hook_rig):
    home, capture, env = hook_rig
    configure(home)
    env["EARSHOT_HERMES_PLATFORMS"] = "ntfy cron"
    assert_quiet(run(env, platform="cli"), capture)
    run(env, platform="ntfy", text="x" * 1700)
    assert receive(capture)[-1] == "x" * 1500


def test_hermes_hook_non_http_profile_is_not_sent(hook_rig):
    home, capture, env = hook_rig
    configure(home, "file:///private/path")
    assert_quiet(run(env), capture)


def test_real_cli_wire_never_falls_back_to_localhost_in_private_namespace(hook_rig):
    """The explicit server receives real CLI POSTs; port 8080 is a private canary."""
    if os.environ.get("EARSHOT_TEST_PRIVATE_NETNS") != "1":
        pytest.skip("run under the EARS-48 private-network namespace harness")
    try:
        net_fd = int(os.environ["EARSHOT_TEST_NETNS_FD"])
        pid_fd = int(os.environ["EARSHOT_TEST_PIDNS_FD"])
        for fd, current in ((net_fd, "/proc/self/ns/net"), (pid_fd, "/proc/self/ns/pid")):
            actual, expected = os.stat(current), os.fstat(fd)
            assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
        assert os.stat("/proc/self/ns/user").st_ino != 0xEFFFFFFD, "private user namespace required"
        assert os.stat("/proc/self/ns/net").st_ino != 0xEFFFFFFF, "private network namespace required"
        assert os.stat("/proc/self/ns/pid").st_ino != 0xEFFFFFFC, "private PID namespace required"
    except (KeyError, OSError, ValueError):
        pytest.fail("private namespace descriptor guard failed")
    home, capture, env = hook_rig
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((cast(http.server.HTTPServer, self.server).server_port, self.path,
                             self.headers.get("Authorization"), body["text"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id":"fixture-only","status":"stored"}')

        def log_message(self, format, *args):
            pass

    servers = [http.server.HTTPServer(("127.0.0.1", 8080), Handler),
               http.server.HTTPServer(("127.0.0.1", 0), Handler)]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        # This wrapper launches the actual candidate relay CLI, not a stub HTTP client.
        cli = pathlib.Path(env["PATH"].split(os.pathsep)[0]) / "earshot-relay"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m earshot_relay "$@"\n')
        cli.chmod(0o700)
        env["PYTHONPATH"] = str(HOOK.parents[2] / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        explicit = servers[1].server_port
        env.update(EARSHOT_API_URL="http://127.0.0.1:8080", EARSHOT_API_TOKEN="inherited-token")
        configure(home, f"http://127.0.0.1:{explicit}", "profile-token")
        run(env, text="private wire probe")
        assert requests == [(explicit, "/v1/messages", "Bearer profile-token", "private wire probe")]
        before = list(requests)
        for url, token in (("   ", "profile-token"), ("/", "profile-token"),
                           ("///", "profile-token"), ("http://", "profile-token"),
                           ("", "profile-token"), (None, "profile-token"),
                           (f"http://127.0.0.1:{explicit}", None)):
            configure(home, url, token)
            run(env, text="must not be sent")
            assert requests == before, "invalid profile wrote to a real CLI endpoint"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
