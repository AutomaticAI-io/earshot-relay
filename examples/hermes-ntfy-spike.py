#!/usr/bin/env python3
"""Isolated Hermes outbound ntfy + one-shot cron transport smoke (no Android playback).

Run from a relay checkout after `uv sync --frozen` with an installed `hermes` CLI.
Moto, relay, Hermes state and generated credentials remain inside an active-profile
scratch directory. This intentionally does NOT create a gateway, recurring clock,
phone beacon, device credentials or any production endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

import boto3
from moto.server import ThreadedMotoServer  # type: ignore[reportMissingImports]  # installed by uv sync --frozen


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str, token: str) -> dict:
    with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=10) as response:
        return json.load(response)


def run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int = 75,
        secrets_for_redaction: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, env=env, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    stdout, stderr = result.stdout.strip(), result.stderr.strip()
    for value in secrets_for_redaction:
        stdout, stderr = stdout.replace(value, "[REDACTED]"), stderr.replace(value, "[REDACTED]")
    print(json.dumps({"argv": argv, "exit": result.returncode, "stdout": stdout,
                      "stderr": stderr}), flush=True)
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    scratch = Path(os.environ["TMPDIR"]).resolve()
    # Never accidentally use /tmp, another profile, or a checkout as a runtime.
    if not str(scratch).startswith("/home/delorenj/.hermes/profiles/earshot-pm/") or not str(scratch).endswith("/cache/scratch"):
        raise SystemExit("Set TMPDIR to the active Hermes profile's cache/scratch directory")
    if shutil.which("hermes") is None:
        raise SystemExit("Install Hermes CLI first")
    relay_exe = root / ".venv/bin/earshot-relay"
    if not relay_exe.exists():
        raise SystemExit("Run uv sync --frozen in this relay checkout first")
    with tempfile.TemporaryDirectory(prefix="ears56-", dir=scratch) as private:
        work = Path(private)
        home = work / "hermes"
        home.mkdir(mode=0o700)
        (home / "scripts").mkdir(mode=0o700)
        tokens = {name: secrets.token_urlsafe(32) for name in ("writer_id", "writer_secret", "main", "compat", "beacon")}
        s3_port, relay_port, compat_port = free_port(), free_port(), free_port()
        if len({s3_port, relay_port, compat_port}) < 3:
            raise SystemExit("Port collision: retry")
        s3_url = f"http://127.0.0.1:{s3_port}"
        main_url = f"http://127.0.0.1:{relay_port}"
        compat_url = f"http://127.0.0.1:{compat_port}"
        moto = ThreadedMotoServer(ip_address="127.0.0.1", port=s3_port, verbose=False)
        relay: subprocess.Popen[str] | None = None
        job_id: str | None = None
        env: dict[str, str] = {}
        moto.start()
        try:
            admin = boto3.client("s3", endpoint_url=s3_url, region_name="us-east-1",
                                 aws_access_key_id="fixture-admin", aws_secret_access_key="fixture-admin")
            bucket = "ears56-private-fixture"
            admin.create_bucket(Bucket=bucket)
            relay_env = {"HOME": private, "TMPDIR": private, "PATH": os.environ["PATH"],
                         "AWS_EC2_METADATA_DISABLED": "true", "AWS_ACCESS_KEY_ID": tokens["writer_id"],
                         "AWS_SECRET_ACCESS_KEY": tokens["writer_secret"],
                         "EARSHOT_S3_ENDPOINT": s3_url, "EARSHOT_BUCKET": bucket, "EARSHOT_REGION": "us-east-1",
                         "EARSHOT_RELAY_ALLOW_HTTP_S3": "1", "EARSHOT_RELAY_HOST": "127.0.0.1",
                         "EARSHOT_RELAY_PORT": str(relay_port), "EARSHOT_RELAY_DATA_DIR": str(work / "relay-data"),
                         "EARSHOT_RELAY_TOKEN": tokens["main"], "EARSHOT_RELAY_BEACON_TOKEN": tokens["beacon"],
                         "EARSHOT_RELAY_COMPAT": "ntfy", "EARSHOT_RELAY_COMPAT_PORT": str(compat_port),
                         "EARSHOT_RELAY_COMPAT_NTFY_TOPICS": "phone=phone",
                         "EARSHOT_RELAY_COMPAT_TOKEN": tokens["compat"]}
            with (work / "relay.log").open("w") as logfile:
                relay = subprocess.Popen([str(relay_exe), "serve"], cwd=root, env=relay_env,
                                         stdout=logfile, stderr=subprocess.STDOUT, text=True)
                for _ in range(100):
                    try:
                        with urlopen(main_url + "/healthz", timeout=1) as response:
                            if response.status == 200:
                                break
                    except URLError:
                        if relay.poll() is not None:
                            raise RuntimeError("fixture relay exited before health check")
                        time.sleep(0.1)
                else:
                    raise RuntimeError("fixture relay readiness timed out")
                (home / ".env").write_text(f"NTFY_TOKEN={tokens['compat']}\nNTFY_SERVER_URL={compat_url}\n")
                (home / ".env").chmod(0o600)
                (home / "config.yaml").write_text(
                    "platforms:\n  ntfy:\n    enabled: true\n    extra:\n"
                    f'      server: "{compat_url}"\n      token: "${{NTFY_TOKEN}}"\n')
                (home / "scripts/ears56-once.py").write_text(
                    'from datetime import datetime, timezone\n'
                    'print("EARS56-cron-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))\n')
                env = {"PATH": os.environ["PATH"], "HOME": private, "HERMES_HOME": str(home),
                       "TMPDIR": private, "TMP": private, "TEMP": private,
                       "XDG_CONFIG_HOME": str(work / "config"), "XDG_DATA_HOME": str(work / "data"),
                       "XDG_CACHE_HOME": str(work / "cache"), "NO_PROXY": "localhost,127.0.0.1",
                       "no_proxy": "localhost,127.0.0.1", "AWS_EC2_METADATA_DISABLED": "true",
                       "HERMES_QUIET": "1"}
                def hermes(argv: list[str], timeout: int = 75) -> subprocess.CompletedProcess[str]:
                    return run(argv, env, work, timeout=timeout,
                               secrets_for_redaction=tuple(tokens.values()))
                print(json.dumps({"hermes_home": "throwaway", "ntfy_topic": "unset",
                                  "relay": main_url, "compat": compat_url, "bucket": bucket,
                                  "subscribe_http": _status(compat_url + "/phone/json"),
                                  "compat_native_http": _status(compat_url + "/v1/status"),
                                  "native_send_token_http": _status(main_url + "/v1/status", tokens["compat"]),
                                  "mcp_send_token_http": _status(main_url + "/mcp", tokens["compat"])}))
                nonce = "EARS56-manual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                manual = hermes(["hermes", "send", "--to", "ntfy:phone", nonce, "--json"])
                if manual.returncode:
                    raise RuntimeError("manual Hermes send failed")
                manual_id = json.loads(manual.stdout)["message_id"]
                once = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat(timespec="seconds")
                created = hermes(["hermes", "cron", "create", once, "EARS56 no-agent one shot",
                                  "--script", "ears56-once.py", "--no-agent", "--deliver", "ntfy:phone",
                                  "--no-wrap-response", "--name", "EARS56 isolated one shot"])
                if created.returncode:
                    raise RuntimeError("one-shot creation failed")
                job_id = created.stdout.split("Created job: ", 1)[1].splitlines()[0].strip()
                executed = hermes(["hermes", "cron", "run", job_id], timeout=150)
                history = hermes(["hermes", "cron", "runs", job_id])
                if executed.returncode or history.returncode or "completed" not in history.stdout:
                    raise RuntimeError("scheduler execution not completed")
                objects = admin.list_objects_v2(Bucket=bucket).get("Contents", [])
                for obj in objects:
                    key = obj["Key"]
                    body = admin.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
                    msg_id = "msg_" + key.rsplit("/", 1)[-1].split(".", 1)[0]
                    receipt = get_json(main_url + "/v1/messages/" + msg_id + "?wait=2", tokens["main"])
                    print(json.dumps({"id": msg_id, "key": key,
                                      "nonce": body if body.startswith("EARS56-") else "redacted",
                                      "status": receipt["status"], "devices": receipt["devices"],
                                      "hint": receipt.get("hint")}), flush=True)
                if len(objects) != 2 or not any(key["Key"].endswith(manual_id[4:] + ".say") for key in objects):
                    raise RuntimeError("expected two correlated private objects")
                return 0
        finally:
            if job_id is not None:
                run(["hermes", "cron", "remove", job_id], env, work,
                    secrets_for_redaction=tuple(tokens.values()))
                run(["hermes", "cron", "list", "--all"], env, work,
                    secrets_for_redaction=tuple(tokens.values()))
            if relay is not None:
                relay.terminate()
                try:
                    relay.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait()
            moto.stop()
            print(json.dumps({"cleanup": "fixture listeners stopped; throwaway home, credentials, bucket and DB removed on exit"}))


def _status(url: str, token: str | None = None) -> int:
    from urllib.error import HTTPError
    try:
        with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"} if token else {}), timeout=5) as r:
            return r.status
    except HTTPError as exc:
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
