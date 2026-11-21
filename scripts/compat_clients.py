"""Real-client interop check for the producer-compat ingress (docs/compat.md).

Starts a throwaway moto bucket and an in-process relay with every compat protocol on, drives real clients at it, and
requires each one to leave the expected object in the bucket and a receipt the relay can read back:

    uv run python scripts/compat_clients.py                    # every client found on this machine
    uv run python scripts/compat_clients.py --write-fixtures   # also re-record tests/fixtures/compat/*.json
    uv run python scripts/compat_clients.py --kuma             # also Uptime Kuma's own providers (docker, ~30 s)

Clients: curl; Apprise (`uvx apprise`) ntfy://, ntfys:// and gotify://; the official ntfy CLI (docker image
binwiederhier/ntfy); the gotify CLI (`--gotify-cli PATH`, a release binary from github.com/gotify/cli);
gregdel/pushover (needs `go`); Uptime Kuma 1.23's ntfy and Gotify providers (`--kuma`, docker image
louislam/uptime-kuma:1, plus `npm` for socket.io-client). A client that is missing is reported as SKIP, never as a pass.

The tokens are the test suite's dummy values; nothing here touches a real bucket or a real push service.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import uvicorn
from moto.server import ThreadedMotoServer

from earshot_relay.api import create_app, create_compat_app
from earshot_relay.config import load_settings
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer
from earshot_relay.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "compat"
BUCKET = "earshot-compat"
TOKEN = "t" * 40  # the test suite's TOKEN
PUSHOVER_TOKEN = "p" * 30
PUSHOVER_USER = "u" * 30
DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    """ASGI wrapper that keeps every request (and the answer) so a run can be saved as a test fixture."""

    def __init__(self, app, listener: str, sink: list, lock: threading.Lock):
        self.app, self.listener, self.sink, self.lock = app, listener, sink, lock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        status = {"code": 0, "headers": []}
        out = bytearray()

        async def rec_receive():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def rec_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                status["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                out.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, rec_receive, rec_send)
        finally:
            entry = {
                "listener": self.listener,
                "method": scope["method"],
                "path": scope["path"],
                "query": scope.get("query_string", b"").decode("latin-1"),
                "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in scope["headers"]
                            if k.decode("latin-1").lower() not in DROP_HEADERS],
                "body_b64": base64.b64encode(bytes(body)).decode(),
                "status": status["code"],
                "response_headers": {k: v for k, v in status["headers"] if k.lower().startswith("x-earshot")},
                "response": out.decode("utf-8", "replace"),
            }
            with self.lock:
                self.sink.append(entry)


def serve(app, port: int, certs: tuple[str, str] | None = None) -> uvicorn.Server:
    kwargs = {"ssl_certfile": certs[0], "ssl_keyfile": certs[1]} if certs else {}
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                                           lifespan="off", **kwargs))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit(f"FAIL: listener on {port} did not start")
    return server


def self_signed(tmp: Path) -> tuple[str, str]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return str(cert), str(key)


def tiny_wav(path: Path) -> bytes:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path.read_bytes()


@dataclass
class Case:
    name: str
    argv: list[str] | None  # None: a custom runner
    expect: str | bytes | None  # spoken text (or audio bytes) the bucket must hold; None: must NOT be delivered
    fixture: str | None = None
    channel: str = "phone"
    note: str = ""
    runner: object = None
    needs: str | None = None  # an executable that must exist, else SKIP
    env: dict = field(default_factory=dict)


@dataclass
class Outcome:
    case: Case
    status: str  # PASS / FAIL / SKIP / EXPECTED-FAIL
    detail: str
    records: list


class Harness:
    def __init__(self, args):
        self.args = args
        self.records: list[dict] = []
        self.lock = threading.Lock()
        self.tmp = Path(tempfile.mkdtemp(prefix="earshot-compat-"))
        self.moto = ThreadedMotoServer(ip_address="127.0.0.1", port=free_port(), verbose=False)
        self.moto.start()
        host, port = self.moto.get_host_and_port()
        self.endpoint = f"http://{host}:{port}"
        self.s3 = boto3.client("s3", endpoint_url=self.endpoint, region_name="us-east-1", aws_access_key_id="admin",
                               aws_secret_access_key="admin")
        self.s3.create_bucket(Bucket=BUCKET)
        self.main_port, self.compat_port, self.tls_port = free_port(), free_port(), free_port()
        settings = load_settings({
            "EARSHOT_S3_ENDPOINT": self.endpoint, "EARSHOT_BUCKET": BUCKET, "EARSHOT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "compat-writer", "AWS_SECRET_ACCESS_KEY": "compat-writer-secret",
            "EARSHOT_RELAY_TOKEN": TOKEN, "EARSHOT_RELAY_ALLOW_HTTP_S3": "1",
            "EARSHOT_RELAY_DATA_DIR": str(self.tmp / "data"), "EARSHOT_RELAY_CHANNELS": "phone,alarm",
            "EARSHOT_RELAY_RATE_LIMIT": "off", "EARSHOT_RELAY_ALARM_RATE_LIMIT": "off",
            "EARSHOT_RELAY_COMPAT": "all", "EARSHOT_RELAY_COMPAT_PORT": str(self.compat_port),
            "EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN": PUSHOVER_TOKEN,
        }, check_ffmpeg=False)
        self.store = Store(settings.db_path)
        self.relay = Relay(settings, self.store, S3Writer(settings))
        certs = self_signed(self.tmp)
        self.servers = [
            serve(Recorder(create_app(self.relay, mcp=False), "main", self.records, self.lock), self.main_port),
            serve(Recorder(create_compat_app(self.relay), "compat", self.records, self.lock), self.compat_port),
            serve(Recorder(create_compat_app(self.relay), "compat", self.records, self.lock), self.tls_port, certs),
        ]
        self.main = f"http://127.0.0.1:{self.main_port}"
        self.root = f"127.0.0.1:{self.compat_port}"
        self.wav = tiny_wav(self.tmp / "ding.wav")

    def close(self):
        for s in self.servers:
            s.should_exit = True
        self.store.close()
        self.moto.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def message_ids(self) -> list[str]:
        return [r.id for r in self.store.list_messages(channel=None, limit=200)]

    def run(self, case: Case) -> Outcome:
        if case.needs and not shutil.which(case.needs) and not Path(case.needs).exists():
            return Outcome(case, "SKIP", f"{case.needs} not found", [])
        before_ids, before_rec = set(self.message_ids()), len(self.records)
        if case.runner is not None:
            code, out = case.runner(self)
        else:
            proc = subprocess.run(case.argv, capture_output=True, text=True, timeout=180,
                                  env={**os.environ, **case.env})
            code, out = proc.returncode, (proc.stdout + proc.stderr)
        time.sleep(0.2)
        recs = self.records[before_rec:]
        new = [m for m in self.message_ids() if m not in before_ids]
        tail = " | ".join(line.strip() for line in out.strip().splitlines()[-3:])[:400]
        if case.expect is None:
            ok = not new
            return Outcome(case, "EXPECTED-FAIL" if ok else "FAIL",
                           f"exit {code}; nothing delivered, as expected. {case.note} client said: {tail}", recs)
        if code != 0 or len(new) != 1:
            return Outcome(case, "FAIL", f"exit {code}, {len(new)} new messages; client said: {tail}", recs)
        msg = self.store.get_message(new[0])
        body = self.s3.get_object(Bucket=BUCKET, Key=msg.key)["Body"].read()
        want = case.expect if isinstance(case.expect, bytes) else case.expect.encode()
        receipt = self.relay.receipt(msg.id)
        if body != want or msg.channel != case.channel or receipt["status"] != "stored":
            return Outcome(case, "FAIL", f"object {msg.key} holds {body[:80]!r} on {msg.channel}, receipt "
                                         f"{receipt['status']}", recs)
        shown = body.decode() if not isinstance(case.expect, bytes) else f"<{len(body)} bytes of audio>"
        hdr = next((r["response_headers"] for r in recs if r["response_headers"]), {})
        extra = "".join(f"; {k}: {v}" for k, v in hdr.items() if k != "x-earshot-message-id")
        return Outcome(case, "PASS", f"exit 0; {msg.key} = {shown!r}; GET /v1/messages/{msg.id} -> "
                                     f"{receipt['status']}{extra}", recs)


# ---- custom runners ------------------------------------------------------------------------------------------------

GO_PUSHOVER = textwrap.dedent("""\
    package main

    import (
    \t"fmt"
    \t"os"

    \t"github.com/gregdel/pushover"
    )

    func main() {
    \tpushover.APIEndpoint = os.Getenv("PUSHOVER_ENDPOINT")
    \tapp := pushover.New(os.Getenv("PUSHOVER_TOKEN"))
    \tuser := pushover.NewRecipient(os.Getenv("PUSHOVER_USER"))
    \tmsg := pushover.NewMessageWithTitle("Front door opened", "Security")
    \tresp, err := app.SendMessage(msg, user)
    \tif err != nil {
    \t\tfmt.Println("error:", err)
    \t\tos.Exit(1)
    \t}
    \tfmt.Println(resp)
    }
    """)


def run_go_pushover(h: Harness) -> tuple[int, str]:
    work = h.tmp / "pushover-go"
    work.mkdir(exist_ok=True)
    (work / "main.go").write_text(GO_PUSHOVER)
    env = {**os.environ, "GOFLAGS": "-mod=mod", "PUSHOVER_ENDPOINT": f"{h.main}/compat/pushover/1",
           "PUSHOVER_TOKEN": PUSHOVER_TOKEN, "PUSHOVER_USER": PUSHOVER_USER}
    steps = [["go", "mod", "init", "earshot.test/pushover"], ["go", "get", "github.com/gregdel/pushover@v1.3.1"],
             ["go", "run", "."]]
    out = ""
    for argv in steps:
        p = subprocess.run(argv, cwd=work, capture_output=True, text=True, env=env, timeout=300)
        out += p.stdout + p.stderr
        if p.returncode:
            return p.returncode, out
    return 0, out


KUMA_JS = textwrap.dedent("""\
    const { io } = require("socket.io-client");
    const [url, notification] = [process.argv[2], JSON.parse(process.argv[3])];
    const s = io(url, { transports: ["websocket"], reconnection: false });
    const call = (ev, ...a) => new Promise((ok) => s.emit(ev, ...a, ok));
    s.on("connect", async () => {
      await call("setup", "earshot", "earshot-compat-1");
      const login = await call("login", { username: "earshot", password: "earshot-compat-1", token: "" });
      if (!login.ok) { console.log("login failed", JSON.stringify(login)); process.exit(1); }
      const r = await call("testNotification", notification);
      console.log(JSON.stringify(r)); process.exit(r.ok ? 0 : 1);
    });
    s.on("connect_error", (e) => { console.log("connect_error", e.message); process.exit(2); });
    """)


class Kuma:
    def __init__(self, h: Harness):
        self.h, self.port, self.name = h, free_port(), f"earshot-compat-kuma-{os.getpid()}"
        self.dir = h.tmp / "kuma"
        self.dir.mkdir(exist_ok=True)
        self.ready = False

    def start(self) -> str | None:
        if not shutil.which("docker") or not shutil.which("npm"):
            return "docker or npm not found"
        (self.dir / "kuma.js").write_text(KUMA_JS)
        p = subprocess.run(["npm", "install", "--silent", "--no-audit", "--no-fund", "socket.io-client@4.8.1"],
                           cwd=self.dir, capture_output=True, text=True, timeout=300)
        if p.returncode:
            return "npm install socket.io-client failed: " + p.stderr[-200:]
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "--network", "host",
                        "-e", f"UPTIME_KUMA_PORT={self.port}", "louislam/uptime-kuma:1"],
                       check=True, capture_output=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    self.ready = True
                    time.sleep(3)
                    return None
            except OSError:
                time.sleep(1)
        return "Uptime Kuma did not start"

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def runner(self, notification: dict):
        def run(_h: Harness) -> tuple[int, str]:
            if not self.ready:
                return 1, "Uptime Kuma is not running"
            p = subprocess.run(["node", "kuma.js", f"http://127.0.0.1:{self.port}", json.dumps(notification)],
                               cwd=self.dir, capture_output=True, text=True, timeout=120)
            return p.returncode, p.stdout + p.stderr
        return run


# ---- the cases -----------------------------------------------------------------------------------------------------


def cases(h: Harness, args, kuma: Kuma | None) -> list[Case]:
    m, root, tls = h.main, h.root, f"127.0.0.1:{h.tls_port}"
    apprise = ["uvx", "--quiet", "apprise@1.9.5"] if args.apprise_pin else ["uvx", "--quiet", "apprise"]
    ntfy = ["docker", "run", "--rm", "--network", "host", "-v", f"{h.tmp}:/work:ro", "binwiederhier/ntfy"]
    out = [
        Case("curl: ntfy topic publish, Bearer", ["curl", "-fsS", "-d", "washer done", "-H", f"Authorization: Bearer {TOKEN}",
             f"{m}/compat/ntfy/phone"], "washer done", "curl_ntfy_bearer", needs="curl"),
        Case("curl: ntfy headers Title/Priority/Tags, Basic (any user)", ["curl", "-fsS", "-u", f"earshot:{TOKEN}",
             "-H", "Title: Laundry", "-H", "Priority: urgent", "-H", "Tags: tada", "-d", "Washer done",
             f"{m}/compat/ntfy/phone"], "Laundry. Washer done", "curl_ntfy_headers", needs="curl"),
        Case("curl: ntfy GET /publish with ?auth=", ["curl", "-fsS", "-G", f"{m}/compat/ntfy/phone/publish",
             "--data-urlencode", "message=Garage door left open", "--data-urlencode", "title=Garage",
             "--data-urlencode", "auth=" + base64.urlsafe_b64encode(f"Bearer {TOKEN}".encode()).decode().rstrip("=")],
             "Garage door left open", "curl_ntfy_get_auth", needs="curl",
             note="the title is said once when the message starts with it"),
        Case("curl: ntfy JSON publish at the prefix base", ["curl", "-fsS", "-H", f"Authorization: Bearer {TOKEN}",
             "-d", json.dumps({"topic": "phone", "title": "CI", "message": "Build 512 passed", "priority": 4,
                               "click": "https://ci.example.com/512"}), f"{m}/compat/ntfy"],
             "CI. Build 512 passed", "curl_ntfy_json", needs="curl"),
        Case("apprise ntfy:// (Basic) at the compat listener root", apprise + ["-vv", "-t", "Laundry", "-b", "washer done",
             f"ntfy://earshot:{TOKEN}@{root}/phone"], "Laundry. washer done", "apprise_ntfy_basic", needs="uvx"),
        Case("apprise ntfys:// (token) over TLS at the root", apprise + ["-vv", "-b", "washer done",
             f"ntfys://{TOKEN}@{tls}/phone?auth=token&verify=no&priority=max"], "washer done",
             "apprise_ntfys_token", needs="uvx"),
        Case("apprise ntfy:// pointed at the /compat/ntfy prefix (not usable)", apprise + ["-vv", "-b", "washer done",
             f"ntfy://earshot:{TOKEN}@127.0.0.1:{h.main_port}/compat/ntfy/phone"], None,
             note="Apprise posts to the server root and reads compat, ntfy and phone as three topics.", needs="uvx"),
        Case("ntfy CLI publish --token to the /compat/ntfy prefix", ntfy + ["publish", "--token", TOKEN, "--title",
             "Backup", "--priority", "high", f"{m}/compat/ntfy/phone", "Nightly backup finished"],
             "Backup. Nightly backup finished", "ntfy_cli_prefix_token", needs="docker"),
        Case("ntfy CLI publish -u user:token to the compat root", ntfy + ["publish", "-u", f"earshot:{TOKEN}",
             f"http://{root}/phone", "Dryer done"], "Dryer done", "ntfy_cli_root_basic", needs="docker"),
        Case("ntfy CLI publish --file ding.wav (audio attachment)", ntfy + ["publish", "--token", TOKEN, "--file",
             "/work/ding.wav", f"{m}/compat/ntfy/phone"], None, "ntfy_cli_file_audio",
             note="Compat is text only since 1.1.1: an attachment-only upload is a 400 and plays nothing.",
             needs="docker"),
        Case("apprise gotify:// with a path prefix (main listener)", apprise + ["-vv", "-t", "Backup", "-b", "done",
             f"gotify://127.0.0.1:{h.main_port}/compat/gotify/{TOKEN}?priority=high"], "Backup. done",
             "apprise_gotify_prefix", needs="uvx"),
        Case("apprise gotify:// at the compat listener root", apprise + ["-vv", "-b", "Server rebooted",
             f"gotify://{root}/{TOKEN}"], "Server rebooted", "apprise_gotify_root", needs="uvx"),
        Case("curl: Pushover form (--form-string, as Pushover's FAQ)", ["curl", "-fsS", "--form-string",
             f"token={TOKEN}", "--form-string", f"user={PUSHOVER_USER}", "--form-string", "message=Front door opened",
             "--form-string", "title=Security", "--form-string", "priority=2", "--form-string", "sound=siren",
             f"{m}/compat/pushover/1/messages.json"], "Security. Front door opened", "curl_pushover_form",
             needs="curl"),
        Case("gregdel/pushover (Go) with APIEndpoint overridden, 30-char token", None, "Security. Front door opened",
             "go_pushover", runner=run_go_pushover, needs="go"),
        Case("curl: Join sendPush GET (as Tasker HTTP Request)", ["curl", "-fsS", "-G",
             f"{m}/compat/join/_ah/api/messaging/v1/sendPush", "--data-urlencode", f"apikey={TOKEN}",
             "--data-urlencode", "title=Tasker", "--data-urlencode", "text=Battery low",
             "--data-urlencode", "say=Phone battery is low", "--data-urlencode", "deviceId=group.all"],
             "Phone battery is low", "curl_join_get", needs="curl"),
    ]
    if args.gotify_cli:
        out.append(Case("gotify CLI push --url <prefix>", [args.gotify_cli, "push", "--url", f"{m}/compat/gotify",
                        "--token", TOKEN, "-t", "Deploy", "-p", "5", "Release 1.1 is live"], "Deploy. Release 1.1 is live",
                        "gotify_cli_prefix", needs=args.gotify_cli))
    if kuma is not None:
        out += [
            Case("Uptime Kuma 1.23 ntfy provider (Test), server URL = /compat/ntfy", None,
                 "phone [Uptime-Kuma]. Earshot Testing", "kuma_ntfy",
                 runner=kuma.runner({"name": "Earshot", "type": "ntfy", "ntfyserverurl": f"{m}/compat/ntfy",
                                     "ntfytopic": "phone", "ntfyPriority": 5, "ntfyAuthenticationMethod": "accessToken",
                                     "ntfyaccesstoken": TOKEN})),
            Case("Uptime Kuma 1.23 Gotify provider (Test), server URL = /compat/gotify", None,
                 "Uptime-Kuma. Earshot Testing", "kuma_gotify",
                 runner=kuma.runner({"name": "Earshot", "type": "gotify", "gotifyserverurl": f"{m}/compat/gotify",
                                     "gotifyapplicationToken": TOKEN, "gotifyPriority": 8})),
        ]
    return out


def save_fixture(case: Case, outcome: Outcome, versions: dict) -> None:
    compat = [r for r in outcome.records if r["status"] == 200 and r["response_headers"].get("x-earshot-message-id")]
    if not compat or case.fixture is None:
        return
    r = compat[-1]
    expect = case.expect
    doc = {
        "client": case.name,
        "versions": versions,
        "listener": r["listener"],
        "request": {k: r[k] for k in ("method", "path", "query", "headers", "body_b64")},
        "response_status": r["status"],
        "response": json.loads(r["response"]),
        "response_headers": {k: v for k, v in r["response_headers"].items() if k != "x-earshot-message-id"},
        "expect_channel": case.channel,
        "expect_text": expect if isinstance(expect, str) else None,
        "expect_audio_b64": base64.b64encode(expect).decode() if isinstance(expect, bytes) else None,
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{case.fixture}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


def versions(gotify_cli: str | None) -> dict:
    def grep_line(argv, needle):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            return next((ln.strip()[:80] for ln in (p.stdout + p.stderr).splitlines() if ln.startswith(needle)), None)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def first_line(argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            return (p.stdout or p.stderr).strip().splitlines()[0][:80]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            return None
    return {k: v for k, v in {
        "curl": first_line(["curl", "--version"]),
        "apprise": first_line(["uvx", "--quiet", "apprise", "--version"]),
        "ntfy": grep_line(["docker", "run", "--rm", "binwiederhier/ntfy", "--help"], "ntfy "),
        "go": first_line(["go", "version"]),
        "gotify-cli": grep_line([gotify_cli, "--version"], "Version") if gotify_cli else None,
    }.items() if v}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write-fixtures", action="store_true")
    ap.add_argument("--kuma", action="store_true", help="also run Uptime Kuma 1.23 in docker and press its Test button")
    ap.add_argument("--gotify-cli", default=None, metavar="PATH", help="a gotify CLI binary (github.com/gotify/cli)")
    ap.add_argument("--apprise-pin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--only", default=None, help="run only cases whose name contains this")
    args = ap.parse_args()
    h = Harness(args)
    kuma = Kuma(h) if args.kuma else None
    results: list[Outcome] = []
    try:
        if kuma is not None:
            why = kuma.start()
            if why:
                print(f"SKIP Uptime Kuma: {why}")
                kuma = None
        vers = versions(args.gotify_cli)
        print("clients:", json.dumps(vers))
        for case in cases(h, args, kuma):
            if args.only and args.only not in case.name:
                continue
            outcome = h.run(case)
            results.append(outcome)
            print(f"{outcome.status:<13} {case.name}: {outcome.detail}", flush=True)
            if args.write_fixtures and outcome.status == "PASS":
                save_fixture(case, outcome, vers)
    finally:
        if kuma is not None:
            kuma.stop()
        h.close()
    failed = [o for o in results if o.status == "FAIL"]
    print(f"{len(results) - len(failed)} ok / {len(failed)} failed "
          f"({sum(o.status == 'SKIP' for o in results)} skipped)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
