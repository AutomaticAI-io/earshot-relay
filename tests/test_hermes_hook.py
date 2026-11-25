"""EARS-48: hook policy and detached relay invocation, with no bucket/network access."""
import json
import os
import pathlib
import subprocess
import time

HOOK = pathlib.Path(__file__).resolve().parents[1] / "examples/hermes/earshot-say.sh"


def test_hermes_hook_allowlist_and_profile_credentials(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "earshot.env").write_text("EARSHOT_API_URL=http://127.0.0.1:8080\nEARSHOT_API_TOKEN=test-token\n")
    (home / "earshot.env").chmod(0o600)
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
    env = {k: v for k, v in os.environ.items() if not k.startswith("EARSHOT_")}
    env.update(HERMES_HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               CAPTURE=str(capture))

    def run(platform, text="A short reply"):
        return subprocess.run([str(HOOK)], input=json.dumps({"extra": {"platform": platform,
                                                                              "assistant_response": text}}),
                              text=True, env=env, capture_output=True, timeout=5)

    for platform in ("cron", "subagent", "desktop", "unknown", ""):
        assert run(platform).returncode == 0
        assert not capture.exists()

    for platform in ("cli", "tui", "ntfy"):
        assert run(platform).returncode == 0
        deadline = time.monotonic() + 4
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert capture.exists(), platform
        assert capture.read_text().splitlines() == ["http://127.0.0.1:8080", "test-token",
                                                     "say --normalize --tag hermes -", "A short reply"]
        capture.unlink()

    env["EARSHOT_HERMES_PLATFORMS"] = "ntfy cron"
    assert run("cli").returncode == 0
    assert run("cron").returncode == 0
    assert not capture.exists()
    assert run("ntfy", "").returncode == 0
    assert not capture.exists()
    assert run("ntfy", "x" * 1700).returncode == 0
    deadline = time.monotonic() + 4
    while not capture.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(capture.read_text().splitlines()[-1]) == 1500


def test_hermes_hook_fails_closed_without_profile_file(tmp_path):
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    result = subprocess.run([str(HOOK)], input='{"extra":{"platform":"ntfy","assistant_response":"test"}}',
                            text=True, env=env, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
