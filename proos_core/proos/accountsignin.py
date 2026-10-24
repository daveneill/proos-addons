"""ProOS Core — the box side of the one ProOS account (register 970rr, 23 Oct 2026).

Dave, 23 Oct 17:25 — one account on ProOS Connect, like Control4; 17:29 rulings: the username is the email; the box keeps
its own one-way copy so a home signs in with the internet down; existing logins stay until the new way is seen working.

THE PLATFORM'S OWN MECHANISM (law, 18 Sep): its COMMAND-LINE SIGN-IN PROVIDER. On each sign-in the platform runs the
small program named in its configuration with `username` and `password` in the environment; exit 0 lets the person in,
and with `meta: true` the program's `name = …`, `group = …`, `local_only = …` lines describe them. The platform makes
the user on first sign-in and reuses it after. Its documented caveats, each handled here:
  * the group defaults to ADMIN — we say `group = system-users` on every answer;
  * meta is read on FIRST sign-in only — the group never changes for a home role, and on/off is kept by `upkeep`;
  * listing providers disables any not listed — the platform's own local provider is listed first, so installer and
    developer logins (the box's own, like Control4's Composer) are untouched.

So: the box's sign-in page stays the platform's. The program (written to the platform's config folder) asks Core;
Core asks Connect (`/v1/verify`, this box only); with Connect UNREACHABLE (never when Connect says no) Core checks its
own salted one-way copy, kept after each good check. The password itself is never stored.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error

CONFIG_DIR = os.environ.get("PROOS_HA_CONFIG", "/config")            # the platform's config folder, as Core mounts it
PROGRAM_DIR = ".proos"                                                # inside it; the platform runs the program from here
PROGRAM_NAME = "signin.py"
KEY_NAME = "signin.key"
PROGRAM_PATH_IN_PLATFORM = "/config/.proos/signin.py"                 # the same file as the platform sees it
CORE_PORT = 8770
CHECK_PATH = "/signin/check"
HEADER = "X-ProOS-Signin"
TRIES = 10                              # plumbing: sign-in attempts per username in TRIES_WINDOW
TRIES_WINDOW = 600
CONNECT_TIMEOUT_S = 6.0                 # plumbing: how long a sign-in waits for Connect before using the box's copy
PBKDF2_ROUNDS = 200000
PROVIDER = "command_line"

_lock = threading.Lock()
_tries: dict = {}


def _data(name: str) -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), name)


# ── the program the platform runs ────────────────────────────────────────────
PROGRAM = '''#!/usr/bin/env python3
# ProOS sign-in (register 970rr) — written by ProOS Core; rewritten by Core on every start. Do not edit.
# The platform runs this for its ProOS sign-in: it asks ProOS Core whether this ProOS account may come in.
import json, os, sys, urllib.request
here = os.path.dirname(os.path.abspath(__file__))
try:
    key = open(os.path.join(here, "{key}")).read().strip()
    req = urllib.request.Request("http://{host}:{port}{path}", method="POST",
                                 data=json.dumps({{"u": os.environ.get("username", ""),
                                                   "pw": os.environ.get("password", "")}}).encode(),
                                 headers={{"Content-Type": "application/json", "{header}": key}})
    got = json.loads(urllib.request.urlopen(req, timeout=15).read() or b"{{}}")
except Exception as e:
    sys.stderr.write("ProOS sign-in: ProOS Core did not answer (%s)\\n" % str(e)[:80])
    sys.exit(1)
if not got.get("ok"):
    sys.exit(1)
print("name = " + str(got.get("name") or "").replace("\\n", " ")[:80])
print("group = system-users")
print("local_only = false")
sys.exit(0)
'''

CONFIG_BLOCK = """
# ProOS — one ProOS account (register 970rr). The platform's own local sign-in stays first (installers' logins);
# ProOS accounts are checked by ProOS Core and ProOS Connect. Added by ProOS Core.
homeassistant:
  auth_providers:
    - type: homeassistant
    - type: command_line
      command: /usr/bin/env
      args: ["python3", "%s"]
      meta: true
""" % PROGRAM_PATH_IN_PLATFORM


def key() -> str:
    """The secret the program shows Core — made once on the box, kept in Core's data and the platform's config."""
    p = _data("signin.key")
    try:
        k = open(p).read().strip()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(32)
    with open(p + ".tmp", "w") as fh:
        fh.write(k)
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)
    return k


def program_text(host: str = None) -> str:
    return PROGRAM.format(key=KEY_NAME, host=host or socket.gethostname(), port=CORE_PORT, path=CHECK_PATH,
                          header=HEADER)


def write_program(config_dir: str = None, host: str = None) -> str:
    """Put the program and its key in the platform's config folder (only when different). Returns what it did."""
    d = os.path.join(config_dir or CONFIG_DIR, PROGRAM_DIR)
    os.makedirs(d, exist_ok=True)
    did = []
    for name, text, mode in ((PROGRAM_NAME, program_text(host), 0o755), (KEY_NAME, key(), 0o600)):
        p = os.path.join(d, name)
        try:
            if open(p).read() == text:
                continue
        except OSError:
            pass
        with open(p + ".tmp", "w") as fh:
            fh.write(text)
        os.chmod(p + ".tmp", mode)
        os.replace(p + ".tmp", p)
        did.append(name)
    return "written " + ", ".join(did) if did else "unchanged"


def ensure_config(config_dir: str = None) -> str:
    """Add the ProOS sign-in to the platform's configuration. 'present' | 'added' | 'conflict' (a homeassistant:
    section Core did not write — never edited; written to Core's log at start, not yet on Health)."""
    p = os.path.join(config_dir or CONFIG_DIR, "configuration.yaml")
    try:
        text = open(p, encoding="utf-8").read()
    except OSError:
        return "missing"
    if PROGRAM_PATH_IN_PLATFORM in text:
        return "present"
    if re.search(r"^homeassistant\s*:", text, re.M):
        return "conflict"
    with open(p + ".before-970rr", "w", encoding="utf-8") as fh:      # what undo_config puts back
        fh.write(text)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text.rstrip("\n") + "\n" + CONFIG_BLOCK)
    os.replace(p + ".tmp", p)
    return "added"


def undo_config(config_dir: str = None) -> str:
    """The platform's own check refused the configuration Core added: put back exactly what was there."""
    p = os.path.join(config_dir or CONFIG_DIR, "configuration.yaml")
    try:
        before = open(p + ".before-970rr", encoding="utf-8").read()
        now = open(p, encoding="utf-8").read()
    except OSError:
        return "nothing to undo"
    if PROGRAM_PATH_IN_PLATFORM not in now:
        return "nothing to undo"
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(before)
    os.replace(p + ".tmp", p)
    return "restored"


# ── the check ────────────────────────────────────────────────────────────────
def _copies() -> dict:
    try:
        with open(_data("account_copies.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_copies(d: dict) -> None:
    p = _data("account_copies.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(d, fh)
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def _keep_copy(u: str, pw: str, name: str, role: str) -> None:
    salt = secrets.token_hex(16)
    with _lock:
        d = _copies()
        d[u] = {"salt": salt, "h": _hash(pw, salt), "name": name, "role": role, "at": int(time.time())}
        _save_copies(d)


def _drop_copy(u: str) -> None:
    with _lock:
        d = _copies()
        if d.pop(u, None) is not None:
            _save_copies(d)


def _copy_ok(u: str, pw: str):
    row = _copies().get(u)
    if not row:
        return None
    return row if hmac.compare_digest(_hash(pw, row["salt"]), row["h"]) else None


def _allowed(u: str, now: float) -> bool:
    with _lock:
        t = [x for x in _tries.get(u, []) if now - x < TRIES_WINDOW]
        if len(t) >= TRIES:
            _tries[u] = t
            return False
        t.append(now)
        _tries[u] = t
        return True


def check(u: str, pw: str, verify=None, now: float = None) -> dict:
    """May this ProOS account come in at this box? {ok, name, role, by: connect|box-copy} or {ok: False, why}.
    `verify(u, pw)` asks Connect (raises when Connect cannot be reached)."""
    now = now or time.time()
    u = str(u or "").strip().lower()
    pw = str(pw or "")
    if not u or not pw:
        return {"ok": False, "why": "empty"}
    if not _allowed(u, now):
        return {"ok": False, "why": "too many tries"}
    if verify is None:
        from . import connectvoice as _cv
        verify = lambda a, b: _cv._call("POST", "/v1/verify", {"u": a, "pw": b}, timeout=CONNECT_TIMEOUT_S)  # noqa: E731
    try:
        got = verify(u, pw) or {}
    except urllib.error.HTTPError as e:
        return {"ok": False, "why": "Connect refused this box (%s)" % e.code}       # answered — not "unreachable"
    except Exception:                                                                 # noqa: BLE001
        row = _copy_ok(u, pw)
        if row:
            return {"ok": True, "name": row.get("name") or u, "role": row.get("role"), "by": "box-copy"}
        return {"ok": False, "why": "Connect unreachable and no copy on this box"}
    if not got.get("ok"):
        # Connect said no. If what was typed MATCHES the box's copy, the copy is stale (the password was changed, or the
        # person unlinked) — drop it. A typo never matches it, so a mistyped attempt never costs the internet-down sign-in.
        if _copy_ok(u, pw):
            _drop_copy(u)
        return {"ok": False, "why": "Connect said no"}
    _keep_copy(u, pw, got.get("name") or u, got.get("role"))
    return {"ok": True, "name": got.get("name") or u, "role": got.get("role"), "by": "connect"}


def key_ok(header_value: str) -> bool:
    return bool(header_value) and hmac.compare_digest(str(header_value), key())


# ── keeping the platform's users right ───────────────────────────────────────
def provider_users(users: list) -> list:
    """The platform users made by the ProOS sign-in (their credential is the command-line provider)."""
    out = []
    for x in users or []:
        if any((c or {}).get("type") == PROVIDER or (c or {}).get("auth_provider_type") == PROVIDER
               for c in (x.get("credentials") or [])):
            out.append(x)
    return out


def usernames_by_user(config_dir: str = None) -> dict:
    """{platform user id: the email it signs in with} for the ProOS sign-in's users — read from the platform's own
    record of credentials (.storage/auth). REGISTER 974d: the platform's user list gives a command-line user NO username
    (read on Dave's box, 24 Oct: "username": null), so upkeep matched every ProOS account to nothing and switched it
    off on the next round — the owner could sign in once, then never again. Only the command-line credentials' user id
    and username are taken; nothing else in the file is read out."""
    p = os.path.join(config_dir or CONFIG_DIR, ".storage", "auth")
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh).get("data") or {}
    except Exception:                                                         # noqa: BLE001
        return {}
    out = {}
    for c in data.get("credentials") or []:
        if (c or {}).get("auth_provider_type") == PROVIDER:
            un = str(((c.get("data") or {}).get("username")) or "").strip().lower()
            if un and c.get("user_id"):
                out[c["user_id"]] = un
    return out


def upkeep(people: list, ws_call, names: dict = None) -> dict:
    """Connect's list of this home's accounts → each ProOS-account user on the box is switched on or off to match, and
    a person no longer listed loses the box's copy. Never touches the box's own logins. A user whose email cannot be
    read is LEFT AS IT IS — never switched off on a guess (974d)."""
    listed = {str(p.get("u") or "").strip().lower() for p in people or []}
    try:
        users = ws_call("config/auth/list") or []
    except Exception as e:                                                    # noqa: BLE001
        return {"error": str(e)[:120]}
    names = usernames_by_user() if names is None else names
    changed, unknown = [], 0
    for x in provider_users(users):
        un = str(x.get("username") or names.get(x.get("id")) or "").strip().lower()
        if not un:
            unknown += 1
            continue
        want = un in listed
        if bool(x.get("is_active", True)) != want:
            try:
                ws_call("config/auth/update", user_id=x["id"], is_active=want)
                changed.append(("on" if want else "off", un))
            except Exception:                                                 # noqa: BLE001
                pass
    with _lock:
        d = _copies()
        gone = [u for u in d if u not in listed]
        for u in gone:
            d.pop(u)
        if gone:
            _save_copies(d)
    return {"changed": changed, "copies_dropped": len(gone), "listed": len(listed), "unknown": unknown}
