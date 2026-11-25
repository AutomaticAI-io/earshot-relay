"""Local-only owner authenticated administration for scoped principals."""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, load_settings
from .control import Control, ControlError


def owner_secret(args):
    if args.owner_fd is not None:
        with os.fdopen(os.dup(args.owner_fd), "r", encoding="utf-8") as f:
            return f.readline().rstrip("\r\n")
    if not sys.stdin.isatty():
        raise ControlError("owner credential requires --owner-fd or a terminal")
    return getpass.getpass("Owner credential: ")


def command(args):
    try:
        settings = load_settings(check_ffmpeg=False)
        if not settings.control_db:
            raise ControlError("EARSHOT_RELAY_CONTROL_DB is required")
        owner = owner_secret(args)
        if args.admin_command == "init":
            contract = json.loads(Path(args.contract).read_text(encoding="utf-8"))
            Control.initialize(settings.control_db, settings, contract, owner)
            print("control initialized")
        else:
            control = Control(settings.control_db, settings)
            if args.admin_command == "create":
                ident = control.issue(owner, args.agent, args.channel, alarm=args.alarm, receipts=args.receipts,
                                      expires_at=args.expires_at, output=args.output, pushover=args.pushover)
                print(ident)
            elif args.admin_command == "revoke":
                control.revoke(owner, args.principal)
                print("principal revoked")
        return 0
    except (ControlError, ConfigError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, (ControlError, ConfigError)) else "administration failed"
        print(f"earshot-relay: {message}", file=sys.stderr)
        return 1


def install(sub):
    admin = sub.add_parser("admin", help="owner-only local workspace and principal administration")
    commands = admin.add_subparsers(dest="admin_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--contract", required=True)
    create = commands.add_parser("create")
    create.add_argument("--agent", required=True)
    create.add_argument("--channel", action="append", required=True, help="channel ID; repeat for each grant")
    create.add_argument("--output", required=True, help="new protected token file")
    create.add_argument("--receipts", action="store_true")
    create.add_argument("--alarm", action="store_true")
    create.add_argument("--expires-at")
    create.add_argument("--pushover", action="store_true")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("principal")
    for parser in (init, create, revoke):
        parser.add_argument("--owner-fd", type=int, help="read owner credential from this protected descriptor")
        parser.set_defaults(fn=command)
