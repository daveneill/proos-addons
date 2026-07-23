"""
ProOS Core - user management and commissioning.

The privileged half of "add users". pro.html never touches Home Assistant
auth directly; it calls ProCore, and ProCore performs owner-level auth
operations against Core over the websocket. The same functions are what
commissioning calls when it creates the homeowner login on a freshly
claimed box - build the backend once, and both the manual Users panel and
the automated commissioning step ride on it.

Roles map onto Home Assistant's built-in groups:
    homeowner -> non-admin  (system-users)
    pro       -> admin      (system-admin)

ws_call is an injected ``ws_call(msg_type, **fields) -> result`` adapter
around ProCore's existing HA websocket client, so this module stays
decoupled and unit-testable.

Verify on real hardware (the one open question from provisioning):
  * whether ProCore's supervisor-token connection is allowed to run the
    config/auth/* admin commands. Call ``manage_check(ws_call)`` on boot -
    it answers this for certain and, if the connection lacks owner rights,
    tells you to fall back to acting as the baked owner (owner token).
  * token minting (mint_user_token) uses Core's auth login-flow, whose
    exact transport through the supervisor proxy should be confirmed once;
    until then commissioning can hand the app the credentials it created
    and let the app perform the normal login itself.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.request

_LOG = logging.getLogger("proos.users")

CORE = "http://supervisor/core"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

GROUP_ADMIN = "system-admin"
GROUP_USER = "system-users"
GROUP_READONLY = "system-read-only"

_ROLE_GROUP = {
    "tech": GROUP_ADMIN,
    "pro": GROUP_ADMIN,
    "installer": GROUP_ADMIN,
    "admin": GROUP_ADMIN,
    "homeowner": GROUP_USER,
    "user": GROUP_USER,
    "readonly": GROUP_READONLY,
}

# 'tech' is not a Home Assistant group - HA only has admin/non-admin. A tech is
# an admin PLUS a ProOS capability flag, kept in our own store so we grant,
# scope, and revoke it without HA needing the concept. Persisted in /data.
TECH_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "tech_users.json")


def _load_tech() -> set:
    try:
        with open(TECH_STORE, encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def _save_tech(ids) -> None:
    tmp = TECH_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(ids), fh)
    os.replace(tmp, TECH_STORE)


def _set_tech(user_id: str, on: bool) -> None:
    ids = _load_tech()
    if on:
        ids.add(user_id)
    else:
        ids.discard(user_id)
    _save_tech(ids)


def is_tech(user_id: str) -> bool:
    return user_id in _load_tech()


def _caller_can_tech(ws_call, caller_id) -> bool:
    """Only an existing tech - or the owner (bootstrap) - may mint or grant
    tech. Fail-closed: no caller identity means no."""
    if not caller_id:
        return False
    if is_tech(caller_id):
        return True
    try:
        for u in (ws_call("config/auth/list") or []):
            if u.get("id") == caller_id and u.get("is_owner"):
                return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# permission self-check (answers the one open question on first boot)
# --------------------------------------------------------------------------
def manage_check(ws_call) -> dict:
    """Can this ProCore connection manage users? Read-only, side-effect free.

    Returns {"can_manage": bool, "users": int, "hint": str}. If can_manage is
    False, ProCore must perform auth ops as the baked owner (owner token)
    rather than over the supervisor-proxied connection."""
    try:
        users = ws_call("config/auth/list")
        n = len(users) if isinstance(users, list) else 0
        return {"can_manage": True, "users": n, "hint": "supervisor connection has auth rights"}
    except Exception as exc:
        return {
            "can_manage": False,
            "users": 0,
            "hint": "supervisor connection lacks auth rights - act as baked owner: %s" % exc,
        }


# --------------------------------------------------------------------------
# user CRUD
# --------------------------------------------------------------------------
def list_users(ws_call) -> list:
    """Normalised user list for the Users panel."""
    raw = ws_call("config/auth/list") or []
    tech = _load_tech()
    return [_normalise(u, tech) for u in raw]


def create_user(ws_call, name: str, role: str = "user",
                username: str | None = None, password: str | None = None,
                caller_id: str | None = None) -> dict:
    """Create a Home Assistant user in the group for ``role`` and give it a
    username/password login. Returns {user_id, username, password, role,
    admin}. A generated password is returned so the caller can hand it to
    the app or the installer; pass one in to set it explicitly."""
    group = _ROLE_GROUP.get(role.lower())
    if not group:
        raise ValueError("unknown role: %s" % role)
    if role.lower() == "tech" and not _caller_can_tech(ws_call, caller_id):
        raise PermissionError("only a tech (or the owner) can create a tech")
    username = (username or _slug(name)).strip().lower()
    password = password or secrets.token_urlsafe(18)

    created = ws_call("config/auth/create", name=name, group_ids=[group]) or {}
    user = created.get("user", created)
    user_id = user.get("id")
    if not user_id:
        raise RuntimeError("user create returned no id: %r" % created)

    ws_call(
        "config/auth_provider/homeassistant/create",
        user_id=user_id, username=username, password=password,
    )
    tech = role.lower() == "tech"
    if tech:
        _set_tech(user_id, True)
    _LOG.info("users - created %s (%s) as %s", name, username, role)
    return {
        "user_id": user_id, "username": username, "password": password,
        "role": role, "admin": group == GROUP_ADMIN, "tech": tech,
    }


def set_password(ws_call, user_id: str, password: str | None = None) -> str:
    """Reset a user's password. Returns the value set (generated if omitted)."""
    password = password or secrets.token_urlsafe(18)
    ws_call(
        "config/auth_provider/homeassistant/admin_change_password",
        user_id=user_id, password=password,
    )
    return password


def set_role(ws_call, user_id: str, role: str, caller_id: str | None = None) -> dict:
    """Move a user between admin / non-admin / read-only."""
    group = _ROLE_GROUP.get(role.lower())
    if not group:
        raise ValueError("unknown role: %s" % role)
    if role.lower() == "tech" and not _caller_can_tech(ws_call, caller_id):
        raise PermissionError("only a tech (or the owner) can grant tech")
    ws_call("config/auth/update", user_id=user_id, group_ids=[group])
    _set_tech(user_id, role.lower() == "tech")
    return {"user_id": user_id, "role": role, "admin": group == GROUP_ADMIN,
            "tech": role.lower() == "tech"}


def delete_user(ws_call, user_id: str) -> dict:
    """Remove a user. Guarded against deleting an owner."""
    for u in (ws_call("config/auth/list") or []):
        if u.get("id") == user_id and u.get("is_owner"):
            raise RuntimeError("refusing to delete the owner account")
    ws_call("config/auth/delete", user_id=user_id)
    _set_tech(user_id, False)
    return {"user_id": user_id, "deleted": True}


# --------------------------------------------------------------------------
# profile pictures
# --------------------------------------------------------------------------
# The avatar lives on the user's native Home Assistant *person* - the same
# place HA's People editor and the mobile app use - so it flows through to
# presence/location later. HA restricts person edits to admins, so a non-admin
# homeowner can't set their own from the Dashboard directly; ProCore performs
# the write over its owner connection on their behalf. This is a privileged
# write onto native HA data, NOT a parallel store.
def _people(ws_call) -> list:
    res = ws_call("person/list") or {}
    if isinstance(res, dict):
        return list(res.get("storage") or []) + list(res.get("config") or [])
    if isinstance(res, list):
        return res
    return []


def _person_by_user(ws_call, user_id: str):
    for p in _people(ws_call):
        if p.get("user_id") == user_id:
            return p
    return None


def picture_for(ws_call, user_id: str):
    """Current avatar URL for a user (None if unset). Cheap read for panels."""
    p = _person_by_user(ws_call, user_id)
    return (p or {}).get("picture")


def _name_for(ws_call, user_id: str) -> str:
    for u in (ws_call("config/auth/list") or []):
        if u.get("id") == user_id:
            return u.get("name") or "User"
    return "User"


def set_picture(ws_call, user_id: str, picture) -> dict:
    """Write ``picture`` (a served image URL, or None to clear) onto the user's
    HA person, creating the person if they don't have one yet. Elevated - call
    with Core's owner ws_call."""
    p = _person_by_user(ws_call, user_id)
    if p:
        ws_call("person/update", person_id=p.get("id"), name=p.get("name"),
                user_id=user_id, device_trackers=(p.get("device_trackers") or []),
                picture=picture)
    else:
        if not picture:
            return {"user_id": user_id, "picture": None}
        ws_call("person/create", name=_name_for(ws_call, user_id),
                user_id=user_id, device_trackers=[], picture=picture)
    _LOG.info("users - avatar %s for %s", "set" if picture else "cleared", user_id)
    return {"user_id": user_id, "picture": picture}


def clear_picture(ws_call, user_id: str) -> dict:
    """Remove a user's avatar."""
    return set_picture(ws_call, user_id, None)


# --------------------------------------------------------------------------
# commissioning entry point
# --------------------------------------------------------------------------
def create_homeowner(ws_call, name: str, password: str | None = None,
                     mint_token: bool = False) -> dict:
    """Called by commissioning once a box is claimed: create the homeowner's
    non-admin login. Returns credentials, and a Dashboard token when
    mint_token is True (see mint_user_token caveats)."""
    result = create_user(ws_call, name=name, role="homeowner", password=password)
    if mint_token:
        try:
            tok = mint_user_token(result["username"], result["password"], "ProOS Dashboard")
            result["token"] = tok
        except Exception as exc:
            _LOG.warning("users - homeowner token mint failed (hand credentials instead): %s", exc)
            result["token"] = None
    return result


# --------------------------------------------------------------------------
# token minting via Core's auth login-flow  (verify transport on box)
# --------------------------------------------------------------------------
def mint_user_token(username: str, password: str, client_name: str = "ProOS") -> dict:
    """Log in as the given user via Core's auth flow and return an access +
    refresh token pair for the app to store. Runs server-side so credentials
    never leave ProCore. The login-flow transport through the supervisor
    proxy is the one thing to confirm once on real hardware."""
    client_id = os.environ.get("PROOS_CLIENT_ID", "http://proos.local/")

    flow = _post_json("/auth/login_flow", {
        "client_id": client_id,
        "handler": ["homeassistant", None],
        "redirect_uri": client_id,
    })
    flow_id = flow["flow_id"]

    step = _post_json("/auth/login_flow/%s" % flow_id, {
        "client_id": client_id,
        "username": username,
        "password": password,
    })
    if step.get("type") != "create_entry":
        raise RuntimeError("login flow did not complete: %r" % step.get("type"))
    code = step["result"]

    tokens = _post_form("/auth/token", {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
    })
    return {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_in": tokens.get("expires_in"),
        "client_name": client_name,
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _normalise(u: dict, tech=frozenset()) -> dict:
    groups = [g.get("id") if isinstance(g, dict) else g for g in (u.get("group_ids") or u.get("groups") or [])]
    admin = GROUP_ADMIN in groups
    uid = u.get("id")
    is_t = admin and uid in tech
    return {
        "id": uid,
        "name": u.get("name"),
        "username": u.get("username"),
        "is_owner": bool(u.get("is_owner")),
        "is_active": bool(u.get("is_active", True)),
        "system": bool(u.get("system_generated")),
        "admin": admin,
        "tech": is_t,
        "role": "tech" if is_t else ("admin" if admin else ("readonly" if GROUP_READONLY in groups else "user")),
    }


def _slug(name: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in (name or "").lower())
    slug = "_".join(p for p in keep.split("_") if p) or "user"
    return slug


def _post_json(path: str, payload: dict) -> dict:
    return _request(path, json.dumps(payload).encode(), "application/json", auth=False)


def _post_form(path: str, fields: dict) -> dict:
    import urllib.parse
    body = urllib.parse.urlencode(fields).encode()
    return _request(path, body, "application/x-www-form-urlencoded", auth=False)


def _request(path: str, data: bytes, content_type: str, auth: bool) -> dict:
    req = urllib.request.Request(CORE + path, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    if auth:
        req.add_header("Authorization", "Bearer " + SUPERVISOR_TOKEN)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}
