"""Request-local authentication and authorization shared by relay adapters."""
from __future__ import annotations

import contextvars
import hmac
from contextlib import contextmanager

from .control import OWNER, ControlError, Principal
from .keys import ApiError

current_principal = contextvars.ContextVar("earshot_principal", default=OWNER)
current_token = contextvars.ContextVar("earshot_token", default=None)


def authenticate(relay, token, *, compat=False, pushover=False):
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    try:
        encoded = token.encode("utf-8")
    except UnicodeError:
        return None
    control = relay.control
    if control is None and relay.settings.control_db:
        return None
    if control is not None:
        try:
            known, principal = control.resolve(token)
        except ControlError:
            return None
        if known:
            return principal
    if hmac.compare_digest(encoded, relay.settings.token.encode()):
        return OWNER
    if compat:
        for kind, expected in (("compat", relay.settings.compat_token),
                               ("pushover", relay.settings.compat_pushover_token if pushover else None)):
            if expected and hmac.compare_digest(encoded, expected.encode()):
                return Principal("compat", "compat_token" if kind == "compat" else "pushover_token")
    return None


def authorize(relay, channel, *, receipt=False, principal=None, db=None):
    p = principal if principal is not None else current_principal.get()
    if channel == "replies":
        raise ApiError(403, "channel_not_allowed", "channel is reserved", "channel")
    if p == OWNER:
        return channel
    if p.kind == "compat" and not receipt:
        return channel
    if p.kind != "scoped" or relay.control is None or not relay.control.permits(p, channel, receipt=receipt, db=db):
        raise ApiError(403, "channel_not_allowed", "request not permitted")
    return channel


def owner_only():
    if current_principal.get().kind != "owner":
        raise ApiError(403, "forbidden", "request not permitted")


@contextmanager
def use_principal(principal, token=None):
    p = current_principal.set(principal)
    t = current_token.set(token)
    try:
        yield
    finally:
        current_token.reset(t)
        current_principal.reset(p)
