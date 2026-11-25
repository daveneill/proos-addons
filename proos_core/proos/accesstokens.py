"""Access tokens from Pro's Tech Tools — REGISTER 1466 (one-list 10.37).

Dave, 25 Nov 2026: on the home unit a long-lived token could be made on the platform's own profile page, which is how the
sessions held a direct tool link to the box; the G-Flip hides that page, "and pro cant generate a token like native HA so
can this be added to Pro". Ruled the same afternoon: "just follow the tech tools gate" — Tech and Developer sign-ins only,
no new box setting.

THE PLATFORM'S OWN MECHANISM, NOT A PROOS ONE (the law of 18 Sep). The platform makes, lists and deletes a person's tokens
with three commands on that person's own sign-in:
    auth/long_lived_access_token   client_name, lifespan (days)   -> the token, once
    auth/refresh_tokens                                           -> every token the person holds
    auth/delete_refresh_token      refresh_token_id               -> gone
Core already uses the first for its own installer sign-in (proauth.login). This module only carries the three, on the
CALLER'S OWN bearer token — never Core's — so a token made here has exactly the access of the person who made it.

What ProOS adds, and nothing else:
  · a name is needed, and one name is one token (the platform refuses a second; said plainly before it is asked);
  · the token in use by this very sign-in cannot be revoked from here (it would sign Pro out mid-press);
  · a revoke is READ BACK — it is said gone only when the platform no longer lists it (actual state, never assumed);
  · the token's secret is returned by create() once and is stored nowhere by ProOS.
"""
from __future__ import annotations

LIFESPAN_DAYS = 365          # PLUMBING: the life asked of the platform for a token made here (Dave, 25 Nov 2026: a year)
NAME_MAX = 60
LONG = "long_lived_access_token"


def _ws(token: str, cmd: str, **fields):
    """One platform command on the caller's own sign-in, by the roads proauth uses."""
    from proos import proauth
    from proos.ha_ws import ws_command
    last = None
    for base, _sv in proauth._bases():
        try:
            return ws_command(base, token, cmd, **fields)
        except Exception as exc:  # noqa: BLE001 — transport or refusal: try the next road, then SAY it
            last = exc
    raise RuntimeError(str(last) if last else "no road to the platform")


def _words(exc) -> str:
    """The platform's own words for what went wrong, with its name taken out the way the logs take it out (one rule:
    platformlog._PLATFORM_NAME — register 1026)."""
    from proos.platformlog import _PLATFORM_NAME
    return _PLATFORM_NAME.sub("ProOS", str(exc) or type(exc).__name__)[:200]


def listing(token: str) -> dict:
    """Every long-lived token this person holds: id, name, made, last used, expires, and which one is in use right now."""
    if not token:
        return {"ok": False, "error": "no sign-in on this request"}
    try:
        rows = _ws(token, "auth/refresh_tokens") or []
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "The box did not answer: %s" % _words(exc)}
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("type") != LONG:
            continue
        out.append({"id": r.get("id"), "name": r.get("client_name") or "Unnamed", "created": r.get("created_at"),
                    "last_used": r.get("last_used_at"), "expires": r.get("expire_at"), "current": bool(r.get("is_current"))})
    out.sort(key=lambda t: str(t.get("created") or ""), reverse=True)
    return {"ok": True, "tokens": out, "days": LIFESPAN_DAYS}


def create(token: str, name: str) -> dict:
    """Make one token under `name`. The secret is in the answer once; ProOS keeps no copy."""
    name = " ".join(str(name or "").split())
    if not name:
        return {"ok": False, "error": "Give the token a name"}
    if len(name) > NAME_MAX or any(ord(c) < 32 for c in name):
        return {"ok": False, "error": "Use a name of up to %d characters" % NAME_MAX}
    have = listing(token)
    if not have.get("ok"):
        return {"ok": False, "error": have.get("error")}
    if any(str(t.get("name") or "").lower() == name.lower() for t in have["tokens"]):
        return {"ok": False, "error": "A token called “%s” is already on this account" % name}
    try:
        made = _ws(token, "auth/long_lived_access_token", client_name=name, lifespan=LIFESPAN_DAYS)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "The box did not make the token: %s" % _words(exc)}
    if not isinstance(made, str) or not made:
        return {"ok": False, "error": "The box answered without a token"}
    return {"ok": True, "name": name, "token": made, "days": LIFESPAN_DAYS}


def revoke(token: str, token_id: str) -> dict:
    """Delete one long-lived token of this person's — never the one in use — and read the list back."""
    have = listing(token)
    if not have.get("ok"):
        return {"ok": False, "error": have.get("error")}
    row = next((t for t in have["tokens"] if t.get("id") == token_id), None)
    if not row:
        return {"ok": False, "error": "That token is not on this account"}
    if row.get("current"):
        return {"ok": False, "error": "That is the sign-in in use right now — it cannot be revoked from here"}
    try:
        _ws(token, "auth/delete_refresh_token", refresh_token_id=token_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "The box did not revoke it: %s" % _words(exc)}
    after = listing(token)
    if not after.get("ok"):
        return {"ok": False, "error": "Asked, but the list could not be read back: %s" % after.get("error")}
    if any(t.get("id") == token_id for t in after["tokens"]):
        return {"ok": False, "error": "The box still lists “%s” — it was not revoked" % row.get("name")}
    return {"ok": True, "name": row.get("name")}
