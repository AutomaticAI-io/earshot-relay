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
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
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
    # Each CLI gets its own process group: a timeout must not leave its Hermes
    # startup helpers writing into the soon-to-be-deleted throwaway home.
    child = subprocess.Popen(argv, env=env, cwd=cwd, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_group(child)
        raise
    finally:
        _stop_group(child)
    result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    stdout, stderr = stdout.strip(), stderr.strip()
    for value in secrets_for_redaction:
        stdout, stderr = stdout.replace(value, "[REDACTED]"), stderr.replace(value, "[REDACTED]")
    print(json.dumps({"argv": argv, "exit": result.returncode, "stdout": stdout,
                      "stderr": stderr}), flush=True)
    return result


def _stop_group(child: subprocess.Popen[str]) -> None:
    # This group contains only the child we launched and processes it spawned.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)
    # Reaped direct child may have exited while a helper still owns the group.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _response(url: str, token: str | None = None) -> tuple[int, dict]:
    request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def _require_response(url: str, expected: int, token: str | None = None,
                      *, error_code: str | int | None = None) -> dict:
    status, body = _response(url, token)
    if status != expected:
        raise RuntimeError(f"expected HTTP {expected} from {url}, got HTTP {status}")
    if error_code is not None and body.get("error") != error_code:
        if not isinstance(body.get("error"), dict) or body["error"].get("code") != error_code:
            if body.get("code") != error_code:
                raise RuntimeError(f"unexpected refusal body for {url}: {body!r}")
    return body


def _remove_runtime(work: Path) -> None:
    # Other processes may finish writes shortly after their CLI exits. Retry
    # boundedly, then fail visibly rather than report a false cleanup success.
    for attempt in range(12):
        try:
            shutil.rmtree(work)
        except FileNotFoundError:
            pass
        except OSError:
            if attempt == 11:
                raise
            time.sleep(0.25)
            continue
        if not work.exists():
            return
        time.sleep(0.25)
    raise RuntimeError(f"private runtime still exists: {work}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    scratch = Path(os.environ["TMPDIR"]).resolve()
    profile = Path(os.environ.get("HERMES_HOME", "")).resolve()
    # The caller explicitly selects their active profile; never a global home
    # or an arbitrary TMPDIR. The throwaway home is created *under* its scratch.
    if profile.name == "" or profile.parent.name != "profiles" or profile.parent.parent.name != ".hermes" or scratch != profile / "cache/scratch":
        raise SystemExit("Set HERMES_HOME to the active profile and TMPDIR to its cache/scratch")
    if shutil.which("hermes") is None:
        raise SystemExit("Install Hermes CLI first")
    relay_exe = root / ".venv/bin/earshot-relay"
    if not relay_exe.exists():
        raise SystemExit("Run uv sync --frozen in this relay checkout first")
    work = Path(tempfile.mkdtemp(prefix="ears56-", dir=scratch))
    job_id: str | None = None
    job_removed = False
    try:
        home = work / "hermes"
        home.mkdir(mode=0o700)
        private = str(work)
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
                (home / "scripts/ears56-once.py").write_text("# overwritten with a unique, precommitted cron nonce before job creation\n")
                env = {"PATH": os.environ["PATH"], "HOME": private, "HERMES_HOME": str(home),
                       "TMPDIR": private, "TMP": private, "TEMP": private,
                       "XDG_CONFIG_HOME": str(work / "config"), "XDG_DATA_HOME": str(work / "data"),
                       "XDG_CACHE_HOME": str(work / "cache"), "NO_PROXY": "localhost,127.0.0.1",
                       "no_proxy": "localhost,127.0.0.1", "AWS_EC2_METADATA_DISABLED": "true",
                       "HERMES_QUIET": "1"}
                def hermes(argv: list[str], timeout: int = 75) -> subprocess.CompletedProcess[str]:
                    return run(argv, env, work, timeout=timeout,
                               secrets_for_redaction=tuple(tokens.values()))
                checks = {"subscribe_http": _status(compat_url + "/phone/json"),
                          "compat_native_http": _status(compat_url + "/v1/status"),
                          "native_send_token_http": _status(main_url + "/v1/status", tokens["compat"]),
                          "mcp_send_token_http": _status(main_url + "/mcp", tokens["compat"])}
                print(json.dumps({"hermes_home": "throwaway", "ntfy_topic": "unset",
                                  "relay": main_url, "compat": compat_url, "bucket": bucket, **checks}))
                expected_checks = {"subscribe_http": 404, "compat_native_http": 404,
                                   "native_send_token_http": 401, "mcp_send_token_http": 401}
                if checks != expected_checks:
                    raise RuntimeError(f"unexpected security/subscription status: {checks}")
                for url, code in ((compat_url + "/phone/json", 404),
                                  (compat_url + "/v1/status", 404)):
                    _require_response(url, code, error_code=40401)
                for url in (main_url + "/v1/status", main_url + "/mcp"):
                    _require_response(url, 401, tokens["compat"], error_code="unauthorized")
                status = _require_response(main_url + "/v1/status", 200, tokens["main"])
                if status.get("compat", {}).get("ntfy_topics") != {"phone": "phone"} or not status["compat"].get("send_token"):
                    raise RuntimeError("compat phone mapping/send token not active")
                nonce = "EARS56-manual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                manual = hermes(["hermes", "send", "--to", "ntfy:phone", nonce, "--json"])
                if manual.returncode:
                    raise RuntimeError("manual Hermes send failed")
                response = json.loads(manual.stdout)
                manual_id = response.get("message_id")
                if response.get("success") is not True or response.get("platform") != "ntfy" or response.get("chat_id") != "phone" or not isinstance(manual_id, str) or not manual_id.startswith("msg_"):
                    raise RuntimeError(f"unexpected Hermes send result: {response!r}")
                _require_response(main_url + "/v1/messages/" + manual_id, 401,
                                  tokens["compat"], error_code="unauthorized")
                manual_receipt = _require_response(main_url + "/v1/messages/" + manual_id,
                                                   200, tokens["main"])
                if manual_receipt.get("id") != manual_id:
                    raise RuntimeError("main test token did not read matching manual receipt")
                cron_nonce = "EARS56-cron-" + secrets.token_hex(12)
                (home / "scripts/ears56-once.py").write_text(f"print({cron_nonce!r})\n")
                once = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat(timespec="seconds")
                created = hermes(["hermes", "cron", "create", once, "EARS56 no-agent one shot",
                                  "--script", "ears56-once.py", "--no-agent", "--deliver", "ntfy:phone",
                                  "--no-wrap-response", "--name", "EARS56 isolated one shot"])
                if created.returncode:
                    raise RuntimeError("one-shot creation failed")
                job_id = created.stdout.split("Created job: ", 1)[1].splitlines()[0].strip()
                executed = hermes(["hermes", "cron", "run", job_id], timeout=150)
                history = hermes(["hermes", "cron", "runs", job_id])
                if executed.returncode or "Ran now: succeeded." not in executed.stdout or history.returncode or not any("completed" in line and f"job={job_id}" in line and "source=direct" in line for line in history.stdout.splitlines()):
                    raise RuntimeError("scheduler execution not completed for this one-shot job")
                objects = admin.list_objects_v2(Bucket=bucket).get("Contents", [])
                expected = {manual_id: nonce, "cron": cron_nonce}
                if len(objects) != 2:
                    raise RuntimeError(f"expected exactly two private objects, got {len(objects)}")
                observed: set[str] = set()
                for obj in objects:
                    key = obj["Key"]
                    body = admin.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
                    msg_id = "msg_" + key.rsplit("/", 1)[-1].split(".", 1)[0]
                    if key != f"phone/{msg_id[4:]}.say":
                        raise RuntimeError(f"unexpected bucket key: {key}")
                    if msg_id == manual_id:
                        label = "manual"
                        if body != expected[manual_id]:
                            raise RuntimeError("manual message body differs from sent nonce")
                    else:
                        label = "cron"
                        if body != expected["cron"] or msg_id == manual_id:
                            raise RuntimeError("cron body or identity differs from precommitted nonce")
                    if label in observed:
                        raise RuntimeError(f"duplicate {label} object")
                    observed.add(label)
                    receipt = get_json(main_url + "/v1/messages/" + msg_id + "?wait=2", tokens["main"])
                    if receipt.get("id") != msg_id or receipt.get("key") != key or receipt.get("channel") != "phone" or receipt.get("status") != "stored" or receipt.get("devices") != []:
                        raise RuntimeError(f"unexpected {label} receipt identity/state: {receipt!r}")
                    print(json.dumps({"id": msg_id, "key": key, "nonce": body,
                                      "status": receipt["status"], "devices": receipt["devices"],
                                      "hint": receipt.get("hint")}), flush=True)
                if observed != {"manual", "cron"}:
                    raise RuntimeError(f"missing correlated objects: {observed}")
                return 0
        finally:
            try:
                if job_id is not None:
                    removed = run(["hermes", "cron", "remove", job_id], env, work,
                                  secrets_for_redaction=tuple(tokens.values()))
                    if removed.returncode:
                        raise RuntimeError(f"one-shot job removal failed: {job_id}")
                    listed = run(["hermes", "cron", "list", "--all"], env, work,
                                 secrets_for_redaction=tuple(tokens.values()))
                    if listed.returncode or "No scheduled jobs" not in listed.stdout:
                        raise RuntimeError("one-shot job still present after removal")
                    job_removed = True
            finally:
                try:
                    if relay is not None:
                        relay.terminate()
                        try:
                            relay.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            relay.kill()
                            relay.wait(timeout=5)
                finally:
                    moto.stop()
    finally:
        _remove_runtime(work)
        if work.exists():
            raise RuntimeError(f"cleanup incomplete: {work}")
        print(json.dumps({"cleanup": "complete" if job_id is None or job_removed else "partial: job removal failed; throwaway home erased",
                          "job_removed": job_removed if job_id is not None else "not-created",
                          "runtime_absent": True}), flush=True)


def _status(url: str, token: str | None = None) -> int:
    from urllib.error import HTTPError
    try:
        with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"} if token else {}), timeout=5) as r:
            return r.status
    except HTTPError as exc:
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
