"""SHARE PROOS WITH ANOTHER CONTROL SYSTEM — over the platform's own MQTT (register 1311, 12 Nov 2026).

The design (11 Nov) §5.3: no ProOS MQTT engine. The platform's Mosquitto app is the broker and the platform's MQTT
integration does the publishing; ProOS only turns it on and says what is shared. What is shared is ProOS's own real
entities (1306): every room's activity (sensor.proos_activity_*) and the home summary. A Control4 (Chowmain), Crestron,
Savant, Node-RED or building-management system subscribes to:

    proos/room/<room>/activity   {"state": …, "attributes": {…}}   retained, every change
    proos/home/summary           {"state": …, "attributes": {…}}   retained, every change

The publishing is ONE platform automation, "ProOS — Share With Another Control System" (id proos_share_mqtt): the
installer can open it in the platform and see exactly what it does. Switching sharing off deletes it.

The login the other system uses is the installer's: he types it in Pro; Core hands it to Mosquitto's own settings and
never shows a password back. Nothing here invents a credential.
"""
from __future__ import annotations

AUTOMATION_ID = "proos_share_mqtt"
ALIAS = "ProOS — Share With Another Control System"
ADDON = "core_mosquitto"
PORT = 1883
REFRESH_EVENT = "proos_share_refresh"


def _sleep(sec):
    import time
    time.sleep(sec)

_TOPIC = ("{% set o = e.split('.')[1] %}"
          "{% if o.startswith('proos_activity_') %}proos/room/{{ o[15:] }}/activity"
          "{% elif o.startswith('proos_home_') %}proos/home/{{ o[11:] }}"
          "{% else %}proos/home/{{ o[6:] }}{% endif %}")
_PAYLOAD = ("{{ {'state': states(e), 'attributes': (states[e].attributes if states[e] is not none else {})}"
            " | to_json }}")


def shared_entities(states) -> list:
    """Which ProOS entities are shared: the room activities and the home summary — read from the platform."""
    out = []
    for s in states or []:
        eid = str((s or {}).get("entity_id") or "")
        if eid.startswith("sensor.proos_activity_") or eid == "sensor.proos_home_summary":
            out.append(eid)
    return sorted(out)


def topic_for(eid: str) -> str:
    """The same rule the automation's template applies — for Pro's list and the bench."""
    o = eid.split(".", 1)[1]
    if o.startswith("proos_activity_"):
        return "proos/room/%s/activity" % o[len("proos_activity_"):]
    if o.startswith("proos_home_"):
        return "proos/home/%s" % o[len("proos_home_"):]
    return "proos/home/%s" % o[len("proos_"):]


def automation(entities: list) -> dict:
    """The platform automation that does the publishing. Every change of a shared entity; all of them when the
    platform starts and whenever ProOS asks (after the list of rooms changes)."""
    pub = {"action": "mqtt.publish",
           "data": {"topic": _TOPIC, "payload": _PAYLOAD, "retain": True, "qos": 1}}
    return {
        "id": AUTOMATION_ID,
        "alias": ALIAS,
        "description": ("Written by ProOS (Pro › Systems › Share With Another Control System). Publishes every "
                        "ProOS room's activity and the home summary to MQTT, retained, so another control system "
                        "can follow them. Switch sharing off in Pro to remove it."),
        "mode": "queued",
        "max": 100,
        "triggers": [{"trigger": "state", "entity_id": list(entities), "id": "change"},
                     {"trigger": "homeassistant", "event": "start", "id": "all"},
                     {"trigger": "event", "event_type": REFRESH_EVENT, "id": "all"}],
        "conditions": [],
        "actions": [{"choose": [{"conditions": [{"condition": "trigger", "id": "change"}],
                                 "sequence": [{"variables": {"e": "{{ trigger.entity_id }}"}}, pub]}],
                     "default": [{"repeat": {"for_each": list(entities),
                                             "sequence": [{"variables": {"e": "{{ repeat.item }}"}}, pub]}}]}],
    }


def status(client, sup) -> dict:
    """Plain facts for Pro: the broker installed and running?, MQTT loaded?, sharing on?, what is shared, the
    logins' USERNAMES (never a password)."""
    out = {"broker": {"installed": False, "running": False, "port": PORT}, "mqtt_loaded": False,
           "sharing": False, "shared": [], "logins": [], "up_to_date": None}
    try:
        info = sup("GET", "/addons/%s/info" % ADDON, None) or {}
        info = info.get("data", info) if isinstance(info, dict) else {}
        out["broker"]["installed"] = bool(info.get("version"))
        out["broker"]["running"] = info.get("state") == "started"
        out["logins"] = [str((x or {}).get("username") or "") for x in ((info.get("options") or {}).get("logins") or [])]
    except Exception:  # noqa: BLE001
        pass
    try:
        out["mqtt_loaded"] = any(e.get("domain") == "mqtt" and e.get("state") == "loaded"
                                 for e in (client.config_entries() or []))
    except Exception:  # noqa: BLE001
        pass
    try:
        ents = shared_entities(client._req("GET", "/api/states") or [])
    except Exception:  # noqa: BLE001
        ents = []
    out["shared"] = [{"entity_id": e, "topic": topic_for(e)} for e in ents]
    try:
        cur = client._req("GET", "/api/config/automation/config/%s" % AUTOMATION_ID) or {}
    except Exception:  # noqa: BLE001
        cur = {}
    if cur.get("id") == AUTOMATION_ID:
        out["sharing"] = True
        have = sorted(((cur.get("triggers") or [{}])[0] or {}).get("entity_id") or [])
        out["up_to_date"] = have == ents
    return out


def enable(client, on: bool) -> dict:
    """Write (or remove) the platform automation. On: with the current list of shared entities, then ask it to
    publish them all once."""
    path = "/api/config/automation/config/%s" % AUTOMATION_ID
    if not on:
        try:
            client._req("DELETE", path)
        except Exception:  # noqa: BLE001
            pass
        return {"sharing": False}
    ents = shared_entities(client._req("GET", "/api/states") or [])
    if not ents:
        raise RuntimeError("no ProOS room states to share yet")
    client._req("POST", path, automation(ents))
    # 1312 — read on Dave's box: the ask fired straight after the write reached nobody (the platform was still loading
    # the automation; last_triggered stayed empty). Ask only once the platform shows it running, waiting at most 15 s.
    ready = False
    for _ in range(30):
        try:
            ready = any((s or {}).get("attributes", {}).get("id") == AUTOMATION_ID and (s or {}).get("state") == "on"
                        for s in (client._req("GET", "/api/states") or []) if str((s or {}).get("entity_id", "")).startswith("automation."))
        except Exception:  # noqa: BLE001
            ready = False
        if ready:
            break
        _sleep(0.5)
    try:
        client._req("POST", "/api/events/%s" % REFRESH_EVENT, {})
    except Exception:  # noqa: BLE001
        pass
    return {"sharing": True, "shared": len(ents), "published": ready}


def set_login(sup, username: str, password: str) -> dict:
    """The installer's login for the other system, handed to Mosquitto's own settings (replacing a login of the same
    name, keeping every other). The password is never returned."""
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError("a username and a password are both needed")
    info = sup("GET", "/addons/%s/info" % ADDON, None) or {}
    info = info.get("data", info) if isinstance(info, dict) else {}
    opts = dict(info.get("options") or {})
    logins = [x for x in (opts.get("logins") or []) if str((x or {}).get("username")) != username]
    logins.append({"username": username, "password": password})
    opts["logins"] = logins
    sup("POST", "/addons/%s/options" % ADDON, {"options": opts})
    sup("POST", "/addons/%s/restart" % ADDON, {})
    return {"logins": [str(x.get("username")) for x in logins]}
