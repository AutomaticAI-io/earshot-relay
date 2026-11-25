"""Local-only owner authenticated administration for scoped principals."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from pathlib import Path

from .config import load_settings
from .control import Control, ControlError, parse_contract


def owner_secret(args):
    if args.owner_fd is not None:
        try:
            fd = int(args.owner_fd)
            if fd < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ControlError("invalid owner credential descriptor") from None
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            value = f.readline(514)
            if len(value) > 513 or len(value.rstrip("\r\n")) > 512:
                raise ControlError("invalid owner credential")
            return value.rstrip("\r\n")
    if not sys.stdin.isatty():
        raise ControlError("owner credential requires --owner-fd or a terminal")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass("Owner credential: ")


def command(args):
    try:
        settings = load_settings(check_ffmpeg=False)
        if not settings.control_db:
            raise ControlError("control database is required")
        owner = owner_secret(args)
        if args.admin_command == "init":
            Control._check_owner(settings, owner)
            contract = parse_contract(Path(args.contract).read_text(encoding="utf-8"))
            Control.initialize(settings.control_db, settings, contract, owner)
        else:
            control = Control(settings.control_db, settings)
            if args.admin_command == "create":
                def publish(ident):
                    print(ident, flush=True)

                control.issue(owner, args.agent, args.channel, alarm=args.alarm, receipts=args.receipts,
                              expires_at=args.expires_at, output=args.output, pushover=args.pushover,
                              publish_id=publish)
            elif args.admin_command == "revoke":
                control.revoke(owner, args.principal)
        return 0
    except (Exception, getpass.GetPassWarning):
        print("earshot-relay: administration failed", file=sys.stderr)
        return 1


class AdminParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "earshot-relay: invalid administration arguments\n")

    def parse_known_args(self, args=None, namespace=None):
        parsed, unknown = super().parse_known_args(args, namespace)
        if unknown:
            self.error("invalid administration arguments")
        return parsed, unknown


def install(sub):
    admin = sub.add_parser("admin", help="owner-only local workspace and principal administration")
    admin.error = lambda message: admin.exit(2, "earshot-relay: invalid administration arguments\n")
    commands = admin.add_subparsers(dest="admin_command", required=True, parser_class=AdminParser)
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
        parser.add_argument("--owner-fd", help="read owner credential from this protected descriptor")
        parser.set_defaults(fn=command)
