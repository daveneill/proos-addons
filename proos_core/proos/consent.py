"""
ProOS Core - per-site access consent.

Two grants, two controllers, two defaults:
  installer_access  standing, ON by default after commissioning; the
                    HOMEOWNER turns it off (Dashboard settings).
  tech_access       OFF by default; the INSTALLER turns it on (Pro app).

Cascade: tech is only live when installer is ALSO on, so a homeowner
revoking their installer collapses any tech grant beneath it.

Enforcement toggles each HA account's is_active flag - reversible, and it
makes a revoked account's tokens stop working. The owner and homeowners are
NEVER touched. Enforcement is OFF by default (PROOS_CONSENT_ENFORCE=1 to arm
it) so deploying only records consent state until you've confirmed that
config/auth/update accepts is_active over Core's supervisor connection - at
which point the switches gain real teeth.
"""
import json
import logging
import os

try:
    from proos import users
except Exception:
    users = None

_LOG = logging.getLogger("proos.consent")
STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "consent.json")
_ENFORCE = os.environ.get("PROOS_CONSENT_ENFORCE", "0") == "1"
_DEFAULT = {"installer_access": True, "tech_access": False}


def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return {"installer_access": bool(d.get("installer_access", True)),
                "tech_access": bool(d.get("tech_access", False))}
    except Exception:
        return dict(_DEFAULT)


def save(state: dict) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STORE)


def effective(state=None) -> dict:
    s = state or load()
    inst = bool(s.get("installer_access", True))
    return {"installer": inst, "tech": inst and bool(s.get("tech_access", False))}


def status() -> dict:
    s = load()
    return {"state": s, "effective": effective(s), "enforcing": _ENFORCE}


def _desired_active(u: dict, eff: dict) -> bool:
    if u.get("is_owner") or u.get("system"):
        return True                     # owner / system: never gated
    if not u.get("admin"):
        return True                     # homeowner / non-admin: never gated
    if u.get("tech"):
        return eff["tech"]              # tech: live only if installer AND tech
    return eff["installer"]             # installer


def apply(ws_call) -> dict:
    """Reconcile each account's is_active with the current consent. Never
    raises; never deactivates the owner or a homeowner. When enforcement is
    disarmed this is a dry run - it reports what WOULD change, changing
    nothing."""
    if users is None:
        return {"applied": False, "reason": "users module unavailable"}
    eff = effective()
    try:
        people = users.list_users(ws_call)
    except Exception as exc:
        return {"applied": False, "reason": "cannot list users: %s" % exc}
    changed = []
    for u in people:
        if u.get("is_owner") or u.get("system") or not u.get("admin"):
            continue
        want = _desired_active(u, eff)
        if bool(u.get("is_active", True)) == want:
            continue
        entry = {"name": u.get("name"), "role": u.get("role"), "active": want}
        if _ENFORCE:
            try:
                ws_call("config/auth/update", user_id=u["id"], is_active=want)
            except Exception as exc:
                _LOG.warning("consent - could not set is_active for %s: %s", u.get("name"), exc)
                entry["error"] = str(exc)
        changed.append(entry)
    return {"applied": _ENFORCE, "enforcing": _ENFORCE, "effective": eff, "changed": changed}


def set_grant(ws_call, installer=None, tech=None) -> dict:
    s = load()
    if installer is not None:
        s["installer_access"] = bool(installer)
    if tech is not None:
        s["tech_access"] = bool(tech)
    save(s)
    return {"state": s, "effective": effective(s), "enforcement": apply(ws_call)}
