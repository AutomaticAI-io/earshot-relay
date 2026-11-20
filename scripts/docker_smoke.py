"""Docker smoke test (spec section 15.9): the image, end to end, against a throwaway moto S3 server.

Builds the image, runs it with host networking next to an in-process moto server, sends one text message with curl,
replays the golden beta beacons, and requires the receipt to read `finished`, also after a container restart (the
SQLite index lives on the volume). Also checks the healthcheck, the non-root user and `earshot-relay doctor`.

    uv run python scripts/docker_smoke.py               # build earshot-relay:smoke, then test it
    uv run python scripts/docker_smoke.py --no-build --image earshot-relay:local

Linux only (host networking). Everything it starts is removed at the end, pass or fail.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from moto.server import ThreadedMotoServer

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "beta-0.1.11-receipts.jsonl"
BUCKET = "earshot-smoke"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*cmd: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict | None]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def wait_for(fn, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")


def ok(what: str) -> None:
    print(f"ok   {what}", flush=True)


def require(cond: bool, what: str, detail: object = "") -> None:
    if not cond:
        raise SystemExit(f"FAIL: {what} {detail}")
    ok(what)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="earshot-relay:smoke")
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()

    if not args.no_build:
        print(f"building {args.image} ...", flush=True)
        sh("docker", "build", "-q", "-t", args.image, str(ROOT))
        ok("image built")

    moto_port, relay_port, compat_port = free_port(), free_port(), free_port()
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=moto_port, verbose=False)
    moto.start()
    endpoint = f"http://127.0.0.1:{moto_port}"
    admin = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="admin",
                         aws_secret_access_key="admin")
    admin.create_bucket(Bucket=BUCKET)

    token, beacon_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    name = f"earshot-relay-smoke-{secrets.token_hex(4)}"
    volume = f"{name}-data"
    tmp = tempfile.TemporaryDirectory(prefix="earshot-relay-smoke-")
    env_file = Path(tmp.name) / "relay.env"
    env_file.write_text("\n".join([
        f"EARSHOT_S3_ENDPOINT={endpoint}", f"EARSHOT_BUCKET={BUCKET}", "EARSHOT_REGION=us-east-1",
        "AWS_ACCESS_KEY_ID=smoke-writer", "AWS_SECRET_ACCESS_KEY=smoke-writer-secret",
        f"EARSHOT_RELAY_TOKEN={token}", f"EARSHOT_RELAY_BEACON_TOKEN={beacon_token}",
        "EARSHOT_RELAY_ALLOW_HTTP_S3=1", "EARSHOT_RELAY_HOST=127.0.0.1", f"EARSHOT_RELAY_PORT={relay_port}",
        "EARSHOT_RELAY_PUBLIC_URL=https://relay.example.com",
        "EARSHOT_RELAY_COMPAT=ntfy,gotify", f"EARSHOT_RELAY_COMPAT_PORT={compat_port}",
    ]) + "\n")
    os.chmod(env_file, 0o600)
    base = f"http://127.0.0.1:{relay_port}"
    try:
        sh("docker", "run", "-d", "--name", name, "--network", "host", "--env-file", str(env_file),
           "-v", f"{volume}:/data", args.image)
        wait_for(lambda: http("GET", f"{base}/healthz")[0] == 200, "/healthz", 60)
        require(http("GET", f"{base}/healthz") == (200, {"ok": True}), "/healthz answers {\"ok\": true}")
        require(sh("docker", "exec", name, "id", "-u").stdout.strip() == "10001", "runs as uid 10001")
        require(sh("docker", "exec", name, "ffmpeg", "-version").returncode == 0, "ffmpeg present")

        sent = sh("curl", "-fsS", "-X", "POST", f"{base}/v1/messages", "-H", f"Authorization: Bearer {token}",
                  "-H", "Content-Type: application/json", "-d", '{"text": "Smoke test. Build finished."}',
                  "-w", "\n%{http_code}")
        body, code = sent.stdout.rsplit("\n", 1)
        msg = json.loads(body)
        require(code == "202" and msg["status"] == "stored", "curl send returns 202 stored", sent.stdout)
        obj = admin.get_object(Bucket=BUCKET, Key=msg["key"])
        require(obj["Body"].read() == b"Smoke test. Build finished." and
                obj["ContentType"] == "text/plain; charset=utf-8", f"object {msg['key']} written as a .say")

        golden = [json.loads(line)["body"] for line in GOLDEN.read_text().splitlines() if line]
        say = [b for b in golden if b["key"].endswith(".say")]
        for b in say:
            status, _ = http("POST", f"{base}/v1/beacons/{beacon_token}", body={**b, "key": msg["key"]})
            require(status == 200, f"replayed {b['phase']} beacon accepted")

        status, receipt = http("GET", f"{base}/v1/messages/{msg['id']}?wait=5", token)
        require(status == 200 and receipt["status"] == "finished", "receipt reads finished", receipt)
        require([h["phase"] for h in receipt["hops"]] == ["detected", "started", "finished"]
                and receipt["hops"][0]["since_upload_ms"] == 2838, "hops and since_upload_ms match the golden run")

        # producer compat: an ntfy publish at the compat root and a Gotify message under the main-port prefix
        sent = sh("curl", "-fsS", "-u", f"x:{token}", "-H", "Title: Smoke", "-d", "compat ntfy ok",
                  f"http://127.0.0.1:{compat_port}/phone", "-w", "\n%{http_code}")
        body, code = sent.stdout.rsplit("\n", 1)
        cmsg = json.loads(body)
        require(code == "200" and cmsg["event"] == "message", "ntfy publish at the compat root returns 200", sent.stdout)
        status, receipt = http("GET", f"{base}/v1/messages/{cmsg['id']}", token)
        require(status == 200 and receipt["status"] == "stored", "the ntfy send has a stored receipt", receipt)
        require(admin.get_object(Bucket=BUCKET, Key=receipt["key"])["Body"].read() == b"Smoke. compat ntfy ok",
                "ntfy send stored as 'Title. Message.'")
        status, gmsg = http("POST", f"{base}/compat/gotify/message?token={token}", body={"title": "G", "message": "ok"})
        require(status == 200 and isinstance(gmsg["id"], int), "Gotify message under /compat/gotify returns 200", gmsg)

        doctor = sh("docker", "exec", name, "earshot-relay", "doctor", check=False)
        require(doctor.returncode == 0, "earshot-relay doctor exits 0 inside the container", doctor.stdout + doctor.stderr)

        wait_for(lambda: sh("docker", "inspect", "-f", "{{.State.Health.Status}}", name).stdout.strip() == "healthy",
                 "container healthcheck", 90)
        ok("container healthcheck reports healthy")

        sh("docker", "restart", name)
        wait_for(lambda: http("GET", f"{base}/healthz")[0] == 200, "/healthz after restart", 60)
        status, receipt = http("GET", f"{base}/v1/messages/{msg['id']}", token)
        require(status == 200 and receipt["status"] == "finished", "receipt still finished after a restart (volume)")
        print("PASS docker smoke", flush=True)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"FAIL: {' '.join(exc.cmd[:3])} exited {exc.returncode}: {exc.stderr or exc.stdout}", file=sys.stderr)
        logs = sh("docker", "logs", "--tail", "50", name, check=False)
        print(logs.stdout + logs.stderr, file=sys.stderr)
        return 1
    except SystemExit:
        logs = sh("docker", "logs", "--tail", "50", name, check=False)
        print(logs.stdout + logs.stderr, file=sys.stderr)
        raise
    finally:
        sh("docker", "rm", "-f", name, check=False)
        sh("docker", "volume", "rm", "-f", volume, check=False)
        moto.stop()
        tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
