"""`earshot-relay`: serve the relay, or talk to one (say, alarm, drop, receipt, ls, status, doctor, mcp)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import secrets
import signal
import sys
import time

from . import RECEIPT_WORDING, __version__
from .beacons import receipt_is_final
from .config import ConfigError

EXIT_OK, EXIT_ERROR, EXIT_NOT_PLAYED, EXIT_TIMEOUT = 0, 1, 3, 4
UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
ALARM_HELP = (
    "LOUD: a siren, then the text on the phone's alarm stream at high volume (the alarm channel must be enabled on "
    "the relay). It plays during a call unless the app's 'Alarms can interrupt calls' is off, and the other party may "
    "hear it. Total-silence Do Not Disturb, an Android refusal, a stopped service, a powered-off phone or no network "
    "can still silence it. Not an emergency or life-safety service."
)


def safe(value: object) -> str:
    """Text from beacons reaches the terminal: show control characters as \\xNN instead of letting them act."""
    return UNPRINTABLE_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))


# ---- logging ---------------------------------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
                   "level": record.levelname.lower()}
        msg = record.getMessage()
        parsed = None
        if msg.startswith("{"):
            try:
                parsed = json.loads(msg)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            payload.update(parsed)
        else:
            payload.update({"logger": record.name, "msg": msg})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "mcp", "botocore", "boto3", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---- serve -----------------------------------------------------------------------------------------------------


def cmd_serve(_args) -> int:
    import uvicorn

    from .api import create_app, create_beacon_app, create_compat_app
    from .config import load_settings
    from .service import Relay
    from .storage import S3Writer
    from .store import Store
    from .tts import TtsClient

    setup_logging()
    log = logging.getLogger("earshot_relay")
    try:
        settings = load_settings()
    except ConfigError as exc:
        for line in str(exc).splitlines():
            print(f"earshot-relay: config: {line}", file=sys.stderr)
        return EXIT_ERROR
    store = Store(settings.db_path)
    relay = Relay(settings, store, S3Writer(settings),
                  TtsClient(settings.tts, settings.max_audio_bytes) if settings.tts else None)

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # one handler below stops every listener together
            yield

    def config(app, port: int, lifespan: str) -> uvicorn.Config:
        return uvicorn.Config(app, host=settings.host, port=port, lifespan=lifespan, access_log=False, log_config=None,
                              server_header=False, proxy_headers=False, timeout_graceful_shutdown=10)

    servers = [_Server(config(create_app(relay), settings.port, "on"))]
    if settings.beacon_port:
        servers.append(_Server(config(create_beacon_app(relay), settings.beacon_port, "off")))
    if settings.compat_port:
        servers.append(_Server(config(create_compat_app(relay), settings.compat_port, "off")))
    log.info(json.dumps({
        "event": "start", "version": __version__, "listen": f"{settings.host}:{settings.port}",
        "beacon_listener": f"{settings.host}:{settings.beacon_port}" if settings.beacon_port else None,
        "compat": list(settings.compat),
        "compat_listener": f"{settings.host}:{settings.compat_port}" if settings.compat_port else None,
        "bucket_host": settings.endpoint_host, "channels": list(settings.channels),
        "receipts": settings.beacon_token is not None, "server_tts": settings.tts is not None,
    }))

    async def main() -> None:
        loop = asyncio.get_running_loop()

        def stop() -> None:
            for s in servers:
                if s.should_exit:
                    s.force_exit = True
                s.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop)
        await asyncio.gather(*(s.serve() for s in servers))

    try:
        asyncio.run(main())
    finally:
        store.close()
    return EXIT_OK


# ---- client commands -------------------------------------------------------------------------------------------


def _client():
    from .client import RelayClient, client_settings

    url, token = client_settings()
    return RelayClient(url, token)


def _read_text(parts: list[str]) -> str:
    if parts == ["-"]:
        return sys.stdin.read()
    return " ".join(parts)


def _print_sent(result: dict, args) -> None:
    if args.quiet:
        return
    print(json.dumps(result, indent=2) if args.json else result["id"])


PUSHOVER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def cmd_token(args) -> int:
    if getattr(args, "pushover", False):
        # 30 letters and digits: the shape Pushover clients check before they send (EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN)
        print("".join(secrets.choice(PUSHOVER_ALPHABET) for _ in range(30)))
    else:
        print(secrets.token_urlsafe(32))
    return EXIT_OK


def cmd_say(args) -> int:
    with _client() as c:
        _print_sent(c.say(_read_text(args.text), args.channel, args.speech, args.voice, args.idempotency_key), args)
    return EXIT_OK


def cmd_alarm(args) -> int:
    with _client() as c:
        _print_sent(c.say(_read_text(args.text), "alarm", args.speech, args.voice, args.idempotency_key), args)
    return EXIT_OK


def cmd_drop(args) -> int:
    with _client() as c:
        _print_sent(c.drop(args.file, args.channel, args.idempotency_key), args)
    return EXIT_OK


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return ""
    sign = "-" if ms < 0 else "+"
    ms = abs(ms)
    return f"{sign}{ms}ms" if ms < 10_000 else f"{sign}{ms / 1000:.1f}s"


def format_receipt(r: dict) -> str:
    lines = [f"{r['id']}  {r['type']}{'/' + r['speech'] if r.get('speech') else ''}  {r['channel']}  {r.get('key') or ''}"]
    lines.append(f"  {'sent':<9} {r['created_at']}  (relay clock)")
    if r.get("object_last_modified"):
        lines.append(f"  {'uploaded':<9} {r['object_last_modified']}  (bucket LastModified, as the phone saw it)")
    for h in r.get("hops", []):
        delta = f"{_fmt_ms(h['since_upload_ms']):>8} since upload" if h.get("since_upload_ms") is not None else ""
        err = f"  error: {h['error']}" if h.get("error") else ""
        skew = f"  skew {h['clock_skew_ms']}ms" if h.get("clock_skew_ms") is not None else ""
        lines.append(f"  {h['phase']:<9} {h['at_server']}  {delta}  device {h['device']}{skew}{err}".rstrip())
    lines.append(f"status: {r['status']}" + (f" at {r['status_at']}" if r.get("status_at") else ""))
    if r.get("hint"):
        lines.append(f"hint: {r['hint']}")
    lines.append("receipt = device playback, not proof a person heard it")
    return "\n".join(safe(line) for line in lines)


def cmd_receipt(args) -> int:
    with _client() as c:
        r = c.receipt(args.id, max(0, min(60, args.wait)))
    print(json.dumps(r, indent=2) if args.json else format_receipt(r))
    if not args.wait:
        return EXIT_OK
    if r["status"] == "finished":
        return EXIT_OK
    if receipt_is_final(r):
        return EXIT_NOT_PLAYED
    return EXIT_TIMEOUT


def cmd_ls(args) -> int:
    with _client() as c:
        data = c.recent(args.n, args.channel)
    if args.json:
        print(json.dumps(data, indent=2))
        return EXIT_OK
    for i in data["items"]:
        kind = i["type"] + ("/" + i["speech"] if i.get("speech") else "")
        print(f"{i['created_at']}  {i['status']:<11} {kind:<13} {i['channel']:<10} {i['id']}")
    if not data["items"]:
        print("# no messages yet", file=sys.stderr)
    return EXIT_OK


def cmd_status(args) -> int:
    with _client() as c:
        s = c.status()
    if args.json:
        print(json.dumps(s, indent=2))
        return EXIT_OK
    r = s["receipts"]
    print(f"relay      {s['relay']['version']}")
    print(f"bucket     {s['bucket']['name']} at {s['bucket']['endpoint_host']}  last write ok: "
          f"{s['bucket']['last_write_ok_at'] or 'never'}")
    print(f"channels   {', '.join(s['channels'])} (default {s.get('default_channel', s['channels'][0])})")
    print(f"speech     default {s['speech']['default']}; server TTS "
          f"{'configured' if s['speech']['server_configured'] else 'not configured'}")
    if not r["configured"]:
        print("receipts   off (set EARSHOT_RELAY_BEACON_TOKEN)")
    else:
        where = r["beacon_url"] or f"https://<your public host>{r['beacon_path']}"
        print(f"receipts   beacon URL for the app: {where}")
        print(f"           last beacon: {r['last_beacon_at'] or 'never'}")
        for d in r["devices"]:
            skew = f", skew {d['clock_skew_ms']}ms" if d.get("clock_skew_ms") is not None else ""
            print(safe(f"           device {d['device']}  app {d['app_version']}  last {d['last_phase']} at "
                       f"{d['last_seen']}{skew}"))
    c = s.get("compat") or {}
    if c.get("enabled"):
        port = f"; root of port {c['port']}" if c.get("port") else ""
        where = ", ".join(f"{p} {c['paths'][p]}" for p in c["enabled"])
        print(f"compat     {where}{port}; top priority -> {'alarm' if c.get('alarm') else 'normal (downgraded)'}")
    print(f"note       {RECEIPT_WORDING}")
    return EXIT_OK


def cmd_doctor(args) -> int:
    from .client import ClientError

    try:
        with _client() as c:
            d = c.doctor()
    except ClientError as exc:
        print(f"FAIL relay: {exc}", file=sys.stderr)
        if exc.status == 401:
            print("     EARSHOT_API_TOKEN does not match the relay's EARSHOT_RELAY_TOKEN", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(f"ok   relay {d['relay']['version']} reachable and the token is accepted")
        for chk in d["checks"]:
            print(f"{'ok  ' if chk['ok'] else 'WARN'} {chk['name']}: {chk['detail']}")
        if d["write"]["ok"]:
            print(f"     test object: {d['write']['key']} (a lone .txt; the phone never plays it)")
    return EXIT_OK if d["ok"] else EXIT_ERROR


def cmd_mcp(_args) -> int:
    from .client import client_settings
    from .mcp_server import run_stdio

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    url, token = client_settings()
    run_stdio(url, token)
    return EXIT_OK


# ---- parser ----------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="earshot-relay",
        description="Send text or audio to the Earshot Android app through your own bucket, and read playback receipts.",
        epilog="Client commands use EARSHOT_API_URL (default http://127.0.0.1:8080) and EARSHOT_API_TOKEN.",
    )
    p.add_argument("--version", action="version", version=f"earshot-relay {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    sub.add_parser("serve", help="run the relay service (the container entrypoint)").set_defaults(fn=cmd_serve)
    sp = sub.add_parser("token", help="print a random 32-byte URL-safe token")
    sp.add_argument("--pushover", action="store_true",
                    help="print 30 random letters and digits instead, for EARSHOT_RELAY_COMPAT_PUSHOVER_TOKEN")
    sp.set_defaults(fn=cmd_token)

    def out_flags(sp):
        sp.add_argument("--json", action="store_true", help="print the full JSON response")
        sp.add_argument("--quiet", "-q", action="store_true", help="print nothing on success")

    def idem_flag(sp):
        sp.add_argument("--idempotency-key", default=None, metavar="KEY",
                        help="send this Idempotency-Key: a retry with the same key within 24 h returns the first "
                             "result instead of playing twice")

    sp = sub.add_parser("say", help="store text for the phone to speak ('-' reads stdin)")
    sp.add_argument("text", nargs="+")
    sp.add_argument("-c", "--channel", default=None, help="channel (default: the relay's default)")
    sp.add_argument("--speech", choices=["device", "server"], default=None)
    sp.add_argument("--voice", default=None, help="server TTS voice")
    idem_flag(sp)
    out_flags(sp)
    sp.set_defaults(fn=cmd_say)

    sp = sub.add_parser("alarm", help="LOUD: speak on the alarm channel (must be enabled on the relay)",
                        description=ALARM_HELP)
    sp.add_argument("text", nargs="+")
    sp.add_argument("--speech", choices=["device", "server"], default=None)
    sp.add_argument("--voice", default=None)
    idem_flag(sp)
    out_flags(sp)
    sp.set_defaults(fn=cmd_alarm)

    sp = sub.add_parser("drop", help="send an audio file")
    sp.add_argument("file")
    sp.add_argument("-c", "--channel", default=None)
    idem_flag(sp)
    out_flags(sp)
    sp.set_defaults(fn=cmd_drop)

    sp = sub.add_parser("receipt", help="show what the phone did with a message (id or 'latest')")
    sp.add_argument("id", nargs="?", default="latest")
    sp.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                    help="wait up to 60 s for a final status; exit 0 finished, 3 skipped/failed/missed on every "
                         "device that reported, 4 timeout or still playing somewhere")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_receipt)

    sp = sub.add_parser("ls", help="recent messages from the relay's index")
    sp.add_argument("-n", type=int, default=20, help="how many (1-200)")
    sp.add_argument("-c", "--channel", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("status", help="relay, channels and the phone's beacon URL")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("doctor", help="check the relay and write one inert test object")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_doctor)

    sub.add_parser("mcp", help="run a stdio MCP server backed by the relay's HTTP API").set_defaults(fn=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .client import ClientError

    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"earshot-relay: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ClientError as exc:
        print(f"earshot-relay: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"earshot-relay: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
