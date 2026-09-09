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
_TTL = 900.0  # 15 min: verify is an HA websocket round-trip; a 60s cache re-verified
              # every minute, and a cold token (dashboard-only device) stalled/401'd on it
_CACHE = {}  # token -> (user, expiry)

PUBLIC_PATHS = {"health", "auth/login", "events", "dashboard/ack"}  # reachable without a token even when armed


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
