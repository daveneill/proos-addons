"""
ProOS Core - installer app authentication (the installer front door).

The installer app signs in HERE, not against Home Assistant directly. ProCore
validates the credentials against HA's own login flow AND enforces that the
account is an installer / tech / owner. A homeowner (non-admin) is refused, so a
Dashboard-only account's username and password can never open the installer app.

Identity still lives in Home Assistant (one account store for the whole system);
this endpoint is the *authorization gateway* that turns "a valid HA login" into
"an installer session" only for the right roles. It returns a long-lived HA
token the installer app then uses for its HA + ProCore calls.

Roles: owner (is_owner) > tech (admin + ProOS tech flag) > installer (admin) >
user (non-admin homeowner, refused here).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_LOG = logging.getLogger("proos.proauth")

_HA_DIRECT = os.environ.get("PROOS_HA_DIRECT", "").rstrip("/")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


class AuthError(Exception):
    """Login refused. ``kind`` maps to an HTTP status in the route:
    bad_input/bad_credentials -> 401, not_installer -> 403, unreachable -> 502."""

    def __init__(self, reason: str, kind: str = "bad_credentials"):
        super().__init__(reason)
        self.kind = kind


# --------------------------------------------------------------------------
# HA transport (direct-first, like provisioning/onboarding)
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


# --------------------------------------------------------------------------
# tier resolution
# --------------------------------------------------------------------------
def _resolve(base: str, access_token: str) -> dict:
    """Who is this token? Returns id/name/is_admin/is_owner/tier."""
    from proos.ha_ws import ws_command
    u = ws_command(base, access_token, "auth/current_user") or {}
    uid = u.get("id")
    is_admin = bool(u.get("is_admin"))
    is_owner = bool(u.get("is_owner"))
    tech = False
    try:
        from proos import users
        tech = bool(uid and users.is_tech(uid))
    except Exception:  # noqa: BLE001
        pass
    if is_owner:
        tier = "owner"
    elif is_admin and tech:
        tier = "tech"
    elif is_admin:
        tier = "installer"
    else:
        tier = "user"
    return {"id": uid, "name": u.get("name"), "is_admin": is_admin,
            "is_owner": is_owner, "tier": tier}


def tier_of(token: str) -> dict:
    """Resolve the tier for an existing token (used to re-validate a stored
    installer session). Raises AuthError('unreachable') if HA can't be reached,
    AuthError('bad_credentials') if the token is invalid."""
    last = None
    for base, _sv in _bases():
        try:
            who = _resolve(base, token)
            if not who.get("id"):
                raise AuthError("token is not valid", "bad_credentials")
            return who
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    raise AuthError("could not reach Home Assistant: %s" % last, "unreachable")


# --------------------------------------------------------------------------
# installer login
# --------------------------------------------------------------------------
def login(username: str, password: str) -> dict:
    """Validate credentials against HA, enforce installer/tech/owner, and return
    {ok, token, tier, name, id}. Raises AuthError otherwise. A homeowner is
    refused with kind='not_installer' AFTER a correct password, so we never leak
    whether the password was right for a non-installer account beyond that."""
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("username and password required", "bad_input")

    last = None
    for base, sv in _bases():
        cid = base.rstrip("/") + "/"
        try:
            flow = _http(base, "/auth/login_flow", {
                "client_id": cid, "handler": ["homeassistant", None],
                "redirect_uri": cid}, token=sv)
            fid = flow.get("flow_id")
            if not fid:
                raise RuntimeError("login flow did not start")
            step = _http(base, "/auth/login_flow/" + fid, {
                "client_id": cid, "username": username, "password": password}, token=sv)
            if step.get("type") != "create_entry":
                raise AuthError("wrong username or password", "bad_credentials")
            code = step["result"]
            tokens = _http(base, "/auth/token", {
                "grant_type": "authorization_code", "code": code,
                "client_id": cid}, token=sv, form=True)
            access = tokens.get("access_token")
            if not access:
                raise RuntimeError("no access token from auth/token")

            who = _resolve(base, access)
            if who["tier"] == "user":
                raise AuthError("this account only has Dashboard access", "not_installer")

            from proos.ha_ws import ws_command
            llt = ws_command(base, access, "auth/long_lived_access_token",
                             client_name="ProOS Pro %d" % int(time.time()), lifespan=3650)
            token = llt if isinstance(llt, str) else access
            _LOG.info("proauth - installer login ok: %s (%s)", username, who["tier"])
            return {"ok": True, "token": token, "tier": who["tier"],
                    "name": who["name"], "id": who["id"]}
        except AuthError:
            raise                       # definitive answer (bad creds / not installer)
        except Exception as exc:        # noqa: BLE001 - transport: try the next base
            last = exc
            _LOG.debug("proauth - login via %s failed: %s", base, exc)
            continue
    raise AuthError("could not reach Home Assistant to sign in: %s" % last, "unreachable")
