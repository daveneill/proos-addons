"""
ProOS Core - QR handover (one-time claim code, local-first).

The no-typing homeowner handover. The installer creates the homeowner login in
the Pro app, then shows a QR code. The homeowner scans it with their phone
camera, the Dashboard opens on the local host with a short ``?claim=CODE`` in
the URL, exchanges that code with ProCore for a Home Assistant token, and lands
signed in - never typing a username or password.

Why a claim code and not a token-in-QR:
  * The QR carries only a SHORT, single-use, short-lived code - never a token.
    A code that leaks after it's claimed (or after it expires) is worthless.
  * The token is minted server-side, on claim, from the homeowner credentials
    the installer just set - so no token exists until the homeowner actually
    claims, and an unclaimed code never creates one.
  * This is the same seam the own-cloud remote plugs into later: today the code
    is exchanged over the LAN; a relay can exchange the same code from anywhere.

Security model (deliberately simple, matching the box's trust boundary):
  * codes: 8 chars from an unambiguous 32-char alphabet (~40 bits), single-use,
    default 10-minute TTL.
  * a code is validated (exists / not used / not expired) BEFORE any HA call, so
    a wrong code never reaches HA's login and never risks its IP rate-limit.
  * per-code attempt cap + a global failed-claim throttle blunt brute force.
  * credentials sit in /data (already the box's sensitive store) only for the
    code's short life, and are dropped the moment it's claimed or pruned.

Token minting reuses Core's existing HA websocket client to turn the login-flow
access token into a LONG-LIVED token - exactly what the Dashboard stores - so
the homeowner gets a durable login, not a session that quietly expires.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOG = logging.getLogger("proos.handover")

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
STORE = os.path.join(DATA_DIR, "handover.json")

# One-time code: unambiguous alphabet (no 0/O/1/I/L) so it reads back cleanly if
# the camera fails and someone has to type it.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = int(os.environ.get("PROOS_HANDOVER_CODE_LEN", "8") or "8")
DEFAULT_TTL = int(os.environ.get("PROOS_HANDOVER_TTL", "600") or "600")  # seconds
MAX_ATTEMPTS = 5                 # wrong-code tries against a single code before it burns
_THROTTLE_WINDOW = 60.0          # seconds
_THROTTLE_MAX_FAILS = 20         # failed claims across all codes per window -> cool down

_LOCK = threading.RLock()
_FAILS: list[float] = []         # timestamps of recent failed claims (global throttle)

# HA base selection: direct first (dodges the Supervisor->Core proxy, which the
# rest of the codebase has learned not to trust for auth), proxy last.
_HA_DIRECT = os.environ.get("PROOS_HA_DIRECT", "").rstrip("/")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


class HandoverError(Exception):
    """Raised for an invalid/expired/used code or a throttled caller. ``code``
    is a short machine token the route maps to an HTTP status."""

    def __init__(self, reason: str, kind: str = "invalid"):
        super().__init__(reason)
        self.kind = kind


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------
def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, STORE)
    except Exception as exc:  # never let a persistence hiccup crash a request
        _LOG.warning("handover - could not persist store: %s", exc)


def _prune(d: dict) -> dict:
    """Drop expired codes and long-dead claimed ones. Mutates and returns d."""
    now = time.time()
    for code in list(d.keys()):
        e = d.get(code) or {}
        exp = float(e.get("expires_at", 0) or 0)
        claimed_at = float(e.get("claimed_at", 0) or 0)
        if e.get("used") and claimed_at and (now - claimed_at) > 3600:
            d.pop(code, None)           # claimed over an hour ago: forget it
        elif not e.get("used") and exp and now > exp:
            d.pop(code, None)           # unclaimed and expired
    return d


def _new_code(existing: dict) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))
        if code not in existing:
            return code
    # astronomically unlikely; widen rather than fail
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN + 4))


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def create(username: str, password: str, name: str | None = None,
           ttl: int | None = None) -> dict:
    """Issue a one-time claim code bound to the homeowner credentials the
    installer just set. Returns {code, expires_in, expires_at}. Raises
    ValueError on missing credentials."""
    username = (username or "").strip().lower()
    if not username or not password:
        raise ValueError("username and password are required")
    ttl = int(ttl or DEFAULT_TTL)
    now = time.time()
    with _LOCK:
        d = _prune(_load())
        code = _new_code(d)
        d[code] = {
            "username": username,
            "password": password,
            "name": name or username,
            "created_at": now,
            "expires_at": now + ttl,
            "used": False,
            "attempts": 0,
        }
        _save(d)
    _LOG.info("handover - issued claim code for %s (ttl=%ss)", username, ttl)
    return {"code": code, "expires_in": ttl, "expires_at": now + ttl}


def status(code: str) -> dict:
    """Non-mutating read for the installer's Pro app to poll: has the homeowner
    claimed yet? Returns {state, expires_in?}. state is one of
    pending|claimed|expired|unknown."""
    code = (code or "").strip().upper()
    now = time.time()
    with _LOCK:
        # Pure read: never prune here, so a belated claim on an expired code can
        # still report "expired" rather than "unknown". create() does the tidying.
        e = _load().get(code)
    if not e:
        return {"state": "unknown"}
    if e.get("used"):
        return {"state": "claimed", "claimed_at": e.get("claimed_at")}
    exp = float(e.get("expires_at", 0) or 0)
    if exp and now > exp:
        return {"state": "expired"}
    return {"state": "pending", "expires_in": max(0, int(exp - now))}


def revoke(code: str) -> dict:
    """Drop a code early (installer cancels a handover)."""
    code = (code or "").strip().upper()
    with _LOCK:
        d = _prune(_load())
        existed = d.pop(code, None) is not None
        _save(d)
    return {"revoked": existed}


def _throttled(now: float) -> bool:
    global _FAILS
    _FAILS = [t for t in _FAILS if now - t < _THROTTLE_WINDOW]
    return len(_FAILS) >= _THROTTLE_MAX_FAILS


def _record_fail(now: float) -> None:
    _FAILS.append(now)


def claim(code: str) -> dict:
    """Exchange a one-time code for a Home Assistant token, then burn the code.
    This is the only public (token-less) route - the homeowner has no token yet.
    Returns {ok, token, refresh_token?, expires_in?, name}. Raises
    HandoverError on a bad/expired/used code or throttle."""
    code = (code or "").strip().upper()
    now = time.time()
    with _LOCK:
        if _throttled(now):
            raise HandoverError("too many attempts, try again shortly", "throttled")
        d = _load()                     # read BEFORE pruning drops an expired code
        e = d.get(code)
        if not e:
            _record_fail(now)
            _save(d)
            raise HandoverError("that code isn't valid", "invalid")
        if e.get("used"):
            _record_fail(now)
            raise HandoverError("that code has already been used", "used")
        exp = float(e.get("expires_at", 0) or 0)
        if exp and now > exp:
            d.pop(code, None)
            _record_fail(now)
            _save(d)
            raise HandoverError("that code has expired", "expired")
        # attempt bookkeeping is only meaningful for a code that exists; a valid
        # code with the right credentials won't fail login, so this mainly caps
        # a code that keeps hitting a mint error.
        e["attempts"] = int(e.get("attempts", 0)) + 1
        if e["attempts"] > MAX_ATTEMPTS:
            d.pop(code, None)
            _record_fail(now)
            _save(d)
            raise HandoverError("that code is no longer valid", "invalid")
        username = e.get("username")
        password = e.get("password")
        name = e.get("name") or username
        _save(d)

    # Mint OUTSIDE the lock (network I/O). On success, burn the code.
    try:
        tok = mint_token(username, password, client_name="ProOS Dashboard")
    except Exception as exc:
        _LOG.warning("handover - mint failed for %s: %s", username, exc)
        with _LOCK:
            _record_fail(time.time())
        raise HandoverError("could not complete sign-in on this home", "mint_failed")

    with _LOCK:
        d = _load()
        e = d.get(code)
        if e:
            e["used"] = True
            e["claimed_at"] = time.time()
            e.pop("password", None)     # drop the secret the instant it's spent
            _save(d)
    _LOG.info("handover - code claimed, homeowner %s signed in", username)
    return {"ok": True, "name": name, **tok}


# --------------------------------------------------------------------------
# token minting via Core's auth login-flow  (direct-base-first)
# --------------------------------------------------------------------------
def _bases():
    bases = []
    if _HA_DIRECT:
        bases.append((_HA_DIRECT, None))
    bases.append(("http://homeassistant:8123", None))
    bases.append(("http://supervisor/core", SUPERVISOR_TOKEN))
    return bases


def _http(base, path, payload=None, token=None, form=False, timeout=15):
    if form:
        data = urllib.parse.urlencode(payload).encode() if payload is not None else None
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode() if payload is not None else None
        ctype = "application/json"
    req = urllib.request.Request(base + path, data=data, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


def mint_token(username: str, password: str, client_name: str = "ProOS") -> dict:
    """Log in as the homeowner via HA's auth login-flow and return a LONG-LIVED
    token the Dashboard can store, plus the login-flow tokens. Tries HA directly
    first, then the supervisor proxy. Raises the last error if every base fails."""
    last_err: Exception | None = None
    for base, sv_token in _bases():
        client_id = base.rstrip("/") + "/"
        try:
            flow = _http(base, "/auth/login_flow", {
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": client_id,
            }, token=sv_token)
            flow_id = flow.get("flow_id")
            if not flow_id:
                raise RuntimeError("login flow did not start")
            step = _http(base, "/auth/login_flow/" + flow_id, {
                "client_id": client_id,
                "username": username,
                "password": password,
            }, token=sv_token)
            if step.get("type") != "create_entry":
                errs = step.get("errors") or {}
                raise RuntimeError("login rejected: %s" % (errs or step.get("type")))
            auth_code = step["result"]
            tokens = _http(base, "/auth/token", {
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": client_id,
            }, token=sv_token, form=True)
            access = tokens.get("access_token")
            if not access:
                raise RuntimeError("no access token from auth/token")
            # Upgrade to a long-lived token over the websocket, exactly like the
            # Dashboard does client-side, so the homeowner's login doesn't expire.
            llt = _long_lived(base, access, client_name)
            return {
                "token": llt or access,
                "long_lived": bool(llt),
                "access_token": access,
                "refresh_token": tokens.get("refresh_token"),
                "expires_in": tokens.get("expires_in"),
                "base": base,
            }
        except Exception as exc:  # noqa: BLE001 - try the next base
            last_err = exc
            _LOG.debug("handover - mint via %s failed: %s", base, exc)
            continue
    raise RuntimeError("token mint failed on every base: %s" % last_err)


def _long_lived(base: str, access_token: str, client_name: str) -> str | None:
    """Create a long-lived access token for the just-logged-in user. Best-effort:
    returns None if the websocket path isn't available, so the caller can fall
    back to the short-lived access token."""
    try:
        from proos.ha_ws import ws_command
        name = "%s %d" % (client_name, int(time.time()))
        res = ws_command(base, access_token, "auth/long_lived_access_token",
                         client_name=name, lifespan=3650)
        return res if isinstance(res, str) else None
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("handover - long-lived upgrade failed (%s); using access token", exc)
        return None
