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

We verify against HA core DIRECTLY (PROOS_HA_DIRECT, default
http://homeassistant:8123) rather than the supervisor proxy, because the proxy
injects its own auth and can't validate an arbitrary user's token.
"""
import logging
import os
import time

_LOG = logging.getLogger("proos.auth")

REQUIRE = os.environ.get("PROOS_REQUIRE_AUTH", "0") == "1"
_HA = os.environ.get("PROOS_HA_DIRECT", "http://homeassistant:8123")
_TTL = 60.0
_CACHE = {}  # token -> (user, expiry)

PUBLIC_PATHS = {"health", "handover/claim"}  # reachable without a token even when armed


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
    now = time.time()
    hit = _CACHE.get(token)
    if hit and hit[1] > now:
        return hit[0]
    try:
        from proos.ha_ws import ws_command
        u = ws_command(_HA, token, "auth/current_user") or {}
        if not u.get("id"):
            return None
        user = {"id": u.get("id"), "name": u.get("name"),
                "is_owner": bool(u.get("is_owner")), "is_admin": bool(u.get("is_admin"))}
        _CACHE[token] = (user, now + _TTL)
        return user
    except Exception as exc:
        _LOG.debug("auth - token verify failed: %s", exc)
        return None
