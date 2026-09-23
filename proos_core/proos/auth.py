"""
ProOS Core - caller authentication.

The API on :8770 is otherwise open on the LAN. This verifies a caller's Home
Assistant bearer token against HA itself - so it can't be forged - and
resolves WHO they are (id, owner/admin), which the tech gate, consent, and
(later) the terminal all depend on. Verified results are cached briefly so it
isn't a round-trip per request.

Enforcement - rejecting token-less callers with 401 - is behind
PROOS_REQUIRE_AUTH so it can be armed only once the apps are sending tokens.
Until armed, ProCore still resolves the caller when a token is present (so the
tech gate uses a verified id) but does not reject anonymous calls.

We verify against HA core directly by the Supervisor's own name for it
(http://homeassistant:8123) rather than the supervisor proxy, because the proxy
injects its own auth and can't validate an arbitrary user's token. Register
439: there is no typed override — nothing on the box is told where the
platform is.
"""
from __future__ import annotations   # register 499: "str | None" in a
# signature is evaluated AT RUNTIME without this line, and that needs Python
# 3.10. The gate on Dave's Mac runs Apple's python3, which is 3.9.6, so this
# module could not be IMPORTED there and every bench that touches it died --
# for weeks, while builds were being called green. Core's own container is
# 3.10+, so nothing about the product changes; this makes the module loadable
# on 3.9 as well, which is what lets the gate tell the truth again. Every
# other module in proos/ already carries this line.
import logging
import os
import time

_LOG = logging.getLogger("proos.auth")

REQUIRE = os.environ.get("PROOS_REQUIRE_AUTH", "0") == "1"
# register 439: the platform is beside Core on the Supervisor network, by the
# Supervisor's own name. Nothing on the box is told where the platform is.
_HA = "http://homeassistant:8123"
_TTL = 900.0  # 15 min: verify is an HA websocket round-trip; a 60s cache re-verified
              # every minute, and a cold token (dashboard-only device) stalled/401'd on it
_CACHE = {}  # token -> (user, expiry)
# REGISTER 447: the cache is only as old as the platform's last word about
# its users. server.py points this at the live stream; when the platform
# reports a user removed or updated, every cached verdict is dropped and
# the next request re-verifies — a revoked login no longer keeps its tier
# for up to fifteen minutes.
_STREAM = None
_SEEN_USER_GEN = None


def watch(stream):
    global _STREAM
    _STREAM = stream


def _follow_platform():
    global _SEEN_USER_GEN
    s = _STREAM
    if s is None:
        return
    try:
        if not s.user_watched():
            return
        g = s.user_generation()
    except Exception:  # noqa: BLE001
        return
    if _SEEN_USER_GEN is None:
        _SEEN_USER_GEN = g
    elif g != _SEEN_USER_GEN:
        _SEEN_USER_GEN = g
        _CACHE.clear()

PUBLIC_PATHS = {"health", "auth/login", "auth/claim", "events", "dashboard/ack"}  # reachable without a token even when armed (auth/claim: reg 438)


def bearer(headers) -> str | None:
    """Extract a bearer token from request headers (case-insensitive)."""
    h = headers.get("Authorization") or headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def verify(token):
    """Return {id, name, is_owner, is_admin} for a valid token, else None.
    Cached for a minute per token."""
    if not token:
        return None
    _follow_platform()
    now = time.time()
    hit = _CACHE.get(token)
    if hit and hit[1] > now:
        return hit[0]
    try:
        from proos.ha_ws import ws_command
        u = ws_command(_HA, token, "auth/current_user") or {}
    except Exception as exc:
        # ws_command RAISES on a real auth failure AND on any transient blip (HA busy, socket
        # timeout, momentary disconnect). Distinguish them: a genuine auth failure is a hard
        # reject; a network blip must NOT drop a session we've already validated, or a
        # dashboard-only device (cold homeowner token) flickers 401 -> "hit and miss". On a
        # blip we serve the last-known-good user and let the NEXT request re-verify.
        msg = str(exc).lower()
        if "auth failed" in msg or "auth_invalid" in msg or "unauthorized" in msg:
            _CACHE.pop(token, None)
            return None
        _LOG.debug("auth - transient verify error, serving cached: %s", exc)
        return hit[0] if hit else None
    if not u.get("id"):
        # Clean response, no user -> token isn't valid. Serve stale only if we had a prior
        # good verify (covers an odd empty result during an HA reload); else reject.
        return hit[0] if hit else None
    user = {"id": u.get("id"), "name": u.get("name"),
            "is_owner": bool(u.get("is_owner")), "is_admin": bool(u.get("is_admin"))}
    _CACHE[token] = (user, now + _TTL)
    return user
