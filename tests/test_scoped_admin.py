"""Exercise the real local admin CLI with dummy credentials and no network."""
from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import sqlite3
import subprocess
import sys
import termios
import time
from datetime import datetime, timedelta, timezone

import pytest

from earshot_relay.control import Control
from .conftest import TOKEN, base_env
from .test_control import rig


def cli(settings, *args, owner=TOKEN, env_extra=None, stdout=subprocess.PIPE):
    env = base_env(settings.s3_endpoint, settings.data_dir, EARSHOT_RELAY_CHANNELS="phone,alarm",
                   EARSHOT_RELAY_CONTROL_DB=settings.control_db)
    env.update({k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "PYTHONPATH", "AWS_EC2_METADATA_DISABLED")
                if k in os.environ})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", "from earshot_relay.cli import main; raise SystemExit(main())",
                           "admin", *map(str, args), "--owner-fd", "0"],
                          input=owner + "\n", text=True, stdout=stdout, stderr=subprocess.PIPE, env=env, timeout=15)


def setup(tmp_path):
    directory, settings, contract = rig(tmp_path)
    contract_path = directory / "contract.json"
    contract_path.write_text(json.dumps(contract))
    result = cli(settings, "init", "--contract", contract_path)
    assert result.returncode == 0 and result.stdout == ""
    return directory, settings, contract_path


def test_cli_owner_no_echo_terminal(tmp_path):
    directory, settings, _ = setup(tmp_path)
    env = base_env(settings.s3_endpoint, settings.data_dir, EARSHOT_RELAY_CHANNELS="phone,alarm",
                   EARSHOT_RELAY_CONTROL_DB=settings.control_db)
    env.update({k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "PYTHONPATH", "AWS_EC2_METADATA_DISABLED")
                if k in os.environ})
    master, slave = pty.openpty()

    def tty_child():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([sys.executable, "-c", "from earshot_relay.cli import main; raise SystemExit(main())",
                             "admin", "create", "--agent", "agent1", "--channel", "normal", "--output",
                             str(directory / "tty-token")], stdin=slave, stdout=slave, stderr=slave, env=env,
                            preexec_fn=tty_child)
    os.close(slave)
    received = bytearray()
    try:
        deadline = time.monotonic() + 10
        while b"Owner credential:" not in received and time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                received.extend(os.read(master, 4096))
        assert b"Owner credential:" in received
        os.write(master, (TOKEN + "\n").encode())
        proc.wait(timeout=10)
        while select.select([master], [], [], 0.1)[0]:
            try:
                received.extend(os.read(master, 4096))
            except OSError:
                break
        assert proc.returncode == 0 and TOKEN.encode() not in received
        assert b"principal_" in received
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


def test_cli_owner_issue_revoke_expiry_and_rotation(tmp_path):
    directory, settings, _ = setup(tmp_path)
    tokenfile, secondfile = directory / "first", directory / "second"
    create = ["create", "--agent", "agent1", "--channel", "normal", "--output"]
    rejected = cli(settings, *create, tokenfile, owner="wrong-owner-fixture")
    assert rejected.returncode == 1 and rejected.stdout == "" and not tokenfile.exists()
    result = cli(settings, *create, tokenfile, "--receipts")
    assert result.returncode == 0 and result.stdout.startswith("principal_")
    ident = result.stdout.strip()
    token = tokenfile.read_text().strip()
    assert token not in result.stdout + result.stderr
    assert tokenfile.stat().st_mode & 0o777 == 0o600
    second = cli(settings, *create, secondfile, "--pushover")
    assert second.returncode == 0 and len(secondfile.read_text().strip()) == 30
    control = Control(settings.control_db, settings)
    assert control.permits(control.authenticate(token), "phone", receipt=True)
    assert cli(settings, "revoke", ident, owner="wrong").returncode == 1
    assert control.authenticate(token)
    revoked = cli(settings, "revoke", ident)
    assert revoked.returncode == 0 and revoked.stdout == ""
    assert control.authenticate(token) is None
    assert control.authenticate(secondfile.read_text().strip())
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    bad_expiry = cli(settings, *create, directory / "past", "--expires-at", past)
    assert bad_expiry.returncode == 1 and not (directory / "past").exists()
    from dataclasses import replace
    rotated = replace(settings, token="z" * 40)
    new_file = directory / "new"
    after = cli(rotated, *create, new_file, owner=rotated.token,
                env_extra={"EARSHOT_RELAY_TOKEN": rotated.token})
    assert after.returncode == 0 and Control(settings.control_db, rotated).authenticate(new_file.read_text().strip())
    assert cli(rotated, "revoke", second.stdout.strip(), owner=TOKEN,
               env_extra={"EARSHOT_RELAY_TOKEN": rotated.token}).returncode == 1


@pytest.mark.parametrize("args", [
    ("--agent", "missing", "--channel", "normal"),
    ("--agent", "agent1", "--channel", "missing"),
    ("--agent", "agent1", "--channel", "urgent"),
    ("--agent", "agent1", "--channel", "normal", "--alarm"),
    ("--agent", "agent1", "--channel", "normal", "--expires-at", "secret-marker"),
])
def test_cli_rejects_unknown_refs_and_bad_expiry(tmp_path, args):
    directory, settings, _ = setup(tmp_path)
    result = cli(settings, "create", *args, "--output", directory / "token")
    assert result.returncode == 1 and result.stdout == ""
    assert "secret-marker" not in result.stderr
    assert not (directory / "token").exists()
    with sqlite3.connect(settings.control_db) as db:
        assert db.execute("select count(*) from principals").fetchone()[0] == 0


@pytest.mark.parametrize("text", [
    '{"version":"control.v1", "password-secret-field":"secret-marker-value"}',
    '{"version":"control.v1", "version":"secret-marker-value"}',
    '{"secret-marker-field": [NaN]}',
    '{"version":',
])
def test_cli_malformed_input_never_reflects_secret(tmp_path, text):
    directory, settings, _ = rig(tmp_path)
    path = directory / "contract"
    path.write_text(text)
    result = cli(settings, "init", "--contract", path)
    assert result.returncode == 1 and result.stdout == ""
    assert "secret-marker" not in result.stderr and "password-secret-field" not in result.stderr
    assert not os.path.exists(settings.control_db)


def test_cli_parse_failure_does_not_reflect_unknown_secret_argument(tmp_path):
    directory, settings, _ = setup(tmp_path)
    result = cli(settings, "create", "--agent", "agent1", "--channel", "normal", "--output",
                 directory / "token", "--secret-marker-field=secret-marker-value")
    assert result.returncode == 2
    assert "secret-marker" not in result.stderr + result.stdout
    assert not (directory / "token").exists()
    result = cli(settings, "secret-marker-command")
    assert result.returncode == 2 and "secret-marker" not in result.stderr + result.stdout


def test_cli_config_errors_never_reflect_values(tmp_path):
    directory, settings, _ = rig(tmp_path)
    result = cli(settings, "revoke", "principal_x", env_extra={"EARSHOT_RELAY_COMPAT": "secret-marker-value"})
    assert result.returncode == 1 and "secret-marker" not in result.stderr


def test_cli_stdout_failure_rolls_back_credential(tmp_path):
    directory, settings, _ = setup(tmp_path)
    tokenfile = directory / "token"
    with open("/dev/full", "w") as full:
        result = cli(settings, "create", "--agent", "agent1", "--channel", "normal", "--output", tokenfile,
                     stdout=full)
    assert result.returncode != 0 and not tokenfile.exists()
    with sqlite3.connect(settings.control_db) as db:
        assert db.execute("select count(*) from principals").fetchone()[0] == 0


def test_crash_after_durable_output_before_commit_leaves_token_inert(tmp_path):
    directory, settings, _ = setup(tmp_path)
    tokenfile = directory / "crash-token"
    env = base_env(settings.s3_endpoint, settings.data_dir, EARSHOT_RELAY_CHANNELS="phone,alarm",
                   EARSHOT_RELAY_CONTROL_DB=settings.control_db)
    env.update({k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "PYTHONPATH", "AWS_EC2_METADATA_DISABLED")
                if k in os.environ})
    code = ("import os, sys; from earshot_relay.config import load_settings; "
            "from earshot_relay.control import Control; s=load_settings(check_ffmpeg=False); "
            "Control(s.control_db,s).issue(s.token,'agent1',['normal'],alarm=False,receipts=False,"
            "expires_at=None,output=sys.argv[1],publish_id=lambda _: os._exit(42))")
    result = subprocess.run([sys.executable, "-c", code, str(tokenfile)], env=env, timeout=15)
    assert result.returncode == 42
    assert tokenfile.exists() and tokenfile.stat().st_mode & 0o777 == 0o600
    control = Control(settings.control_db, settings)
    assert control.authenticate(tokenfile.read_text().strip()) is None
    with sqlite3.connect(settings.control_db) as db:
        assert db.execute("select count(*) from principals").fetchone()[0] == 0
