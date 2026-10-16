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
    # register 439: the platform is reached by the Supervisor's own names only
    bases = []
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
    from proos import users
    tech = False
    try:
        tech = bool(uid and users.is_tech(uid))
    except Exception:  # noqa: BLE001
        pass
    # The four tiers, decided in ONE place (users.tier, register 721).
    return {"id": uid, "name": u.get("name"), "is_admin": is_admin,
            "is_owner": is_owner, "tier": users.tier(is_owner, is_admin, tech)}


_TOKEN_CACHE = os.environ.get("PROOS_INSTALLER_TOKENS", "/data/proos_installer_tokens.json")


def _load_token_cache() -> dict:
    try:
        with open(_TOKEN_CACHE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_token_cache(d: dict) -> None:
    try:
        tmp = _TOKEN_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, _TOKEN_CACHE)
    except Exception:
        pass


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
            if who["tier"] == "homeowner":
                raise AuthError("this account only has Dashboard access", "not_installer")

            from proos.ha_ws import ws_command
            # Reuse this installer's cached long-lived token if it's still valid,
            # instead of minting a fresh one every sign-in (the token flood).
            cache = _load_token_cache()
            token = None
            cached = cache.get(who["id"])
            if cached:
                try:
                    if _resolve(base, cached).get("id"):
                        token = cached
                except Exception:
                    token = None
            if not token:
                llt = ws_command(base, access, "auth/long_lived_access_token",
                                 client_name="ProOS Pro (installer)", lifespan=3650)
                token = llt if isinstance(llt, str) else access
                cache[who["id"]] = token
                _save_token_cache(cache)
            _LOG.info("proauth - installer login ok: %s (%s)%s", username, who["tier"],
                      "" if cached and token == cached else " [new token]")
            return {"ok": True, "token": token, "tier": who["tier"],
                    "name": who["name"], "id": who["id"]}
        except AuthError:
            raise                       # definitive answer (bad creds / not installer)
        except Exception as exc:        # noqa: BLE001 - transport: try the next base
            last = exc
            _LOG.debug("proauth - login via %s failed: %s", base, exc)
            continue
    raise AuthError("could not reach the system to sign in: %s" % last, "unreachable")


def first_password(user_id: str, username: str, starter: str, new_password: str) -> dict:
    """REGISTER 901 (Dave 16 Oct 14:17, the app: "Please sign out and log in again, then set your password"): the
    first-sign-in password change, ONE road for the app and the browser. The page used to change it with the starter
    password it had kept from its own sign-in form; the app signs in natively, so the page never had it. Core set the
    starter password, so Core changes it: the platform's own login flow with the starter password gives THAT user's
    session, and the platform's own self-service change_password runs on it (no owner rights, nothing kept — the new
    password passes through and is never stored). Refused unless the starter login IS the caller."""
    if not new_password or len(new_password) < 6:
        raise AuthError("use at least 6 characters", "bad_input")
    last = None
    for base, sv in _bases():
        cid = base.rstrip("/") + "/"
        try:
            flow = _http(base, "/auth/login_flow", {"client_id": cid, "handler": ["homeassistant", None],
                                                     "redirect_uri": cid}, token=sv)
            fid = flow.get("flow_id")
            if not fid:
                raise RuntimeError("login flow did not start")
            step = _http(base, "/auth/login_flow/" + fid, {"client_id": cid, "username": username,
                                                            "password": starter}, token=sv)
            if step.get("type") != "create_entry":
                raise AuthError("the starter password no longer signs this login in", "bad_credentials")
            tokens = _http(base, "/auth/token", {"grant_type": "authorization_code", "code": step["result"],
                                                  "client_id": cid}, token=sv, form=True)
            access = tokens.get("access_token")
            if not access:
                raise RuntimeError("no access token from auth/token")
            if _resolve(base, access).get("id") != user_id:
                raise AuthError("this is not your login", "not_you")
            from proos.ha_ws import ws_command
            ws_command(base, access, "config/auth_provider/homeassistant/change_password",
                       current_password=starter, new_password=new_password)
            return {"ok": True}
        except AuthError:
            raise
        except Exception as exc:        # noqa: BLE001 - transport: try the next base
            last = exc
            continue
    raise AuthError("could not reach the system to set the password: %s" % last, "unreachable")


# REGISTER 902 (Dave 16 Oct 2026: "only support username and password"): the homeowner sign-in CODE behind the QR
# (register 438 — mint_code / claim_code, held in memory ten minutes) is gone with the QR. Every login is typed.
