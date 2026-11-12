"""OUTSIDE TRIGGERS — another system starts something in ProOS (register 1328, 13 Nov 2026 — plan item F23).

Dave, 13 Nov 07:32 "go" on: named webhooks, so another system (Control4, Savant, a doorbell, a UniFi alarm) can start
something in ProOS. PLATFORM FIRST: the platform's own automations have a webhook trigger; it does the listening. An
Outside Trigger is ONE platform automation — alias "ProOS Outside Trigger: <name>", a webhook trigger, one action (an
activity's script or a scene the installer picks) — so it is visible, editable, and keeps working without ProOS. ProOS
adds only what the platform has not: a name, the two addresses to paste into the other system, "last used", a test,
and removal.

THE ADDRESS IS THE KEY. The trigger code is 32 random characters (secrets.token_urlsafe(24)). The platform's own
"local only" switch cannot help here: calls arriving through ProOS's front door — at home or through ProOS Connect —
reach the platform from INSIDE the box, so to the platform every one is local. A switch that looked like protection and
was not would be worse than none; the address is shown and kept like a password instead.
"""
from __future__ import annotations

import re
import secrets

AUTO_PREFIX = "proos_trigger_"
ALIAS_PREFIX = "ProOS Outside Trigger: "
TARGET_DOMAINS = ("script", "scene")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    return s[:40] or "trigger"


def automation(name: str, target: str, webhook_id: str | None = None) -> dict:
    """The one platform automation an Outside Trigger is."""
    name = str(name or "").strip()
    target = str(target or "").strip()
    if not name:
        raise ValueError("a name is needed — the other system's installer will see it")
    dom = target.split(".", 1)[0]
    if dom not in TARGET_DOMAINS or "." not in target:
        raise ValueError("an Outside Trigger starts an activity or a scene (script.* or scene.*), not %r" % target)
    wid = webhook_id or ("proos_" + secrets.token_urlsafe(24))
    return {
        "id": AUTO_PREFIX + _slug(name) + "_" + wid[-6:].lower().replace("-", "x").replace("_", "x"),
        "alias": ALIAS_PREFIX + name,
        "description": ("Made by ProOS Pro (register 1328): another system starts %s by calling this trigger's "
                        "address. The address is the key — keep it like a password." % target),
        "mode": "single",
        "triggers": [{"trigger": "webhook", "webhook_id": wid, "allowed_methods": ["POST", "PUT", "GET"],
                      "local_only": False}],
        "conditions": [],
        "actions": [{"action": "%s.turn_on" % dom, "target": {"entity_id": target}}],
    }


def addresses(webhook_id: str, home_base: str | None, away_base: str | None) -> dict:
    path = "/api/webhook/" + webhook_id
    return {"home": (home_base.rstrip("/") + path) if home_base else None,
            "away": (away_base.rstrip("/") + path) if away_base else None}


def from_config(cfg: dict, state: dict | None) -> dict | None:
    """A platform automation → an Outside Trigger, or None when it is not one of ProOS's."""
    if not str((cfg or {}).get("id") or "").startswith(AUTO_PREFIX):
        return None
    trig = next((t for t in (cfg.get("triggers") or cfg.get("trigger") or [])
                 if (t.get("trigger") or t.get("platform")) == "webhook"), None)
    act = next(iter(cfg.get("actions") or cfg.get("action") or []), {}) or {}
    tgt = ((act.get("target") or {}).get("entity_id"))
    if isinstance(tgt, list):
        tgt = tgt[0] if tgt else None
    a = (state or {}).get("attributes") or {}
    return {"id": cfg["id"], "name": str(cfg.get("alias") or "").replace(ALIAS_PREFIX, "", 1),
            "webhook_id": (trig or {}).get("webhook_id"), "target": tgt,
            "last_used": a.get("last_triggered"), "on": (state or {}).get("state") == "on",
            "entity_id": (state or {}).get("entity_id")}


def listing(client) -> list:
    """Every Outside Trigger on the box, from the platform's own automations."""
    out = []
    for st in client._req("GET", "/api/states") or []:
        eid = st.get("entity_id") or ""
        aid = ((st.get("attributes") or {}).get("id")) or ""
        if not eid.startswith("automation.") or not str(aid).startswith(AUTO_PREFIX):
            continue
        try:
            cfg = client._req("GET", "/api/config/automation/config/%s" % aid) or {}
        except Exception:  # noqa: BLE001 — listed without its details rather than dropped
            cfg = {"id": aid, "alias": (st.get("attributes") or {}).get("friendly_name") or aid}
        t = from_config(cfg, st)
        if t:
            out.append(t)
    return sorted(out, key=lambda t: t["name"].lower())


def create(client, name: str, target: str) -> dict:
    auto = automation(name, target)
    client._req("POST", "/api/config/automation/config/%s" % auto["id"], auto)
    return from_config(auto, None)


def remove(client, aid: str) -> bool:
    if not str(aid or "").startswith(AUTO_PREFIX):
        raise ValueError("only ProOS's own Outside Triggers are removed here")
    client._req("DELETE", "/api/config/automation/config/%s" % aid)
    return True


def test(client, webhook_id: str) -> None:
    """Call the trigger the way the other system will — the platform runs its action."""
    client._req("POST", "/api/webhook/%s" % webhook_id, {"from": "ProOS Pro test"})
