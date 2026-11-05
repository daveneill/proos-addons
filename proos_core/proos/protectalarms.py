"""PROTECT → ALARM MANAGER — register 1230 (the mockup Dave agreed; 6 Nov: "yes all of it please").

The console's own Alarm Manager list, as UniFi's own app reads it (/proxy/protect/api/automations — the internal
interface, not the official key interface, which offers no such list; measured on Dave's console 6 Nov: 53 alarms).
Read through ProOS's own console user. NAMED FIELDS ONLY: an alarm's actions carry web addresses and user ids, and the
device list (bootstrap) carries the console's access key and its users — none of that leaves Core. What a condition
watches for is passed only as short plain words (person, vehicle, motion …), never an id, address or token.
"""
import re

# device lists in Protect's bootstrap whose members can be an alarm's source
_DEVICE_LISTS = ("cameras", "sensors", "lights", "chimes", "viewers", "sirens", "relays", "hubs", "locks",
                 "readers", "speakers", "linkstations", "bridges", "fobs")
_ACTION_WORDS = {"SEND_NOTIFICATION": "Notify", "HTTP_REQUEST": "Web Request", "PLAY_SIREN": "Sound Siren",
                 "TURN_ON_LIGHT": "Turn On Light", "PLAY_CHIME": "Ring Chime", "TRIGGER_RELAY": "Trigger Relay"}
_WORD = re.compile(r"^[A-Za-z][A-Za-z _-]{1,28}$")   # a plain word, never an id, address or token


def _words(v):
    """Short plain words from a condition value — anything that looks like an id, a number string or an address is
    dropped, not shown."""
    out = []
    for x in (v if isinstance(v, list) else [v]):
        if isinstance(x, str) and _WORD.match(x) and not re.search(r"[0-9a-f]{8}", x):
            out.append(x.replace("_", " ").strip())
    return out


def device_names(bootstrap):
    """{device id: name} from Protect's bootstrap — the names only."""
    names = {}
    b = bootstrap if isinstance(bootstrap, dict) else {}
    for k in _DEVICE_LISTS:
        for d in (b.get(k) or []):
            if isinstance(d, dict) and d.get("id"):
                names[str(d["id"])] = d.get("name") or d.get("displayName") or d.get("type") or "Unnamed"
    return names


def _watches(conditions):
    seen = []
    for c in (conditions or []):
        cond = (c or {}).get("condition") if isinstance(c, dict) else None
        if not isinstance(cond, dict):
            continue
        for k, v in cond.items():
            if k in ("type",):
                continue
            for w in _words(v):
                w = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", w)          # sensorOpened → Sensor Opened
                w = " ".join(x[:1].upper() + x[1:] for x in w.split())
                if w not in seen:
                    seen.append(w)
    return seen


def alarms_view(automations, bootstrap=None):
    """Each alarm as named fields only. Deleted alarms are left out."""
    names = device_names(bootstrap)
    out = []
    for a in (automations or []):
        if not isinstance(a, dict) or a.get("deleted"):
            continue
        devs, unknown = [], 0
        for s in (a.get("sources") or []):
            did = str((s or {}).get("device") or "") if isinstance(s, dict) else ""
            if did in names:
                if names[did] not in devs:
                    devs.append(names[did])
            elif did:
                unknown += 1
        acts = []
        for x in sorted((a.get("actions") or []), key=lambda z: (z or {}).get("order") or 0):
            t = str((x or {}).get("type") or "")
            w = _ACTION_WORDS.get(t) or (t.replace("_", " ").title() if _WORD.match(t.replace("_", " ")) else "Action")
            if w not in acts:
                acts.append(w)
        st = a.get("status") if isinstance(a.get("status"), dict) else {}
        cd = a.get("cooldown") if isinstance(a.get("cooldown"), dict) else {}
        sch = a.get("schedules") or []
        out.append({
            "id": a.get("id"), "name": a.get("name") or "Unnamed Alarm", "on": bool(a.get("enable")),
            "devices": devs, "devices_unknown": unknown, "watches": _watches(a.get("conditions")),
            "schedule": "Always" if not sch else ("%d schedule%s" % (len(sch), "" if len(sch) == 1 else "s")),
            "actions": acts,
            "cooldown_s": int((cd.get("timeout") or 0) / 1000) if cd.get("enable") else 0,
            "last_fired": st.get("lastExecutedAt"), "fired_total": st.get("total"),
            "used_in_arm_mode": bool(a.get("usedInArmMode")), "blocked_by_arm_mode": bool(a.get("isBlockedByArmMode")),
            "editable": bool(a.get("editable")),
        })
    out.sort(key=lambda x: str(x["name"]).lower())
    return out
