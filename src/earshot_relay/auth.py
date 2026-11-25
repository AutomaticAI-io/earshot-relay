"""Request-local authentication and authorization shared by relay adapters."""
from __future__ import annotations

import contextvars
import hmac
from contextlib import contextmanager

from .control import OWNER, Principal
from .keys import ApiError

current_principal = contextvars.ContextVar("earshot_principal", default=OWNER)
current_token = contextvars.ContextVar("earshot_token", default=None)


def authenticate(relay, token, *, compat=False, pushover=False):
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    if hmac.compare_digest(token.encode(), relay.settings.token.encode()):
        return OWNER
    if relay.control is not None:
        principal = relay.control.authenticate(token)
        if principal is not None:
            return principal
    if compat:
        for kind, expected in (("compat", relay.settings.compat_token),
                               ("pushover", relay.settings.compat_pushover_token if pushover else None)):
            if expected and hmac.compare_digest(token.encode(), expected.encode()):
                return Principal(kind, kind)
    return None


def authorize(relay, channel, *, receipt=False, principal=None):
    p = principal or current_principal.get()
    if channel == "replies":
        raise ApiError(403, "channel_not_allowed", "channel is reserved", "channel")
    if p.kind == "scoped" and (relay.control is None or not relay.control.permits(p, channel, receipt=receipt)):
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
