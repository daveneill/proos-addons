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


def _key(v):
    """One spelling for an identifier: a MAC with or without colons, any case, reads the same (1230b)."""
    return re.sub(r"[^0-9a-z]", "", str(v or "").lower())


def device_names(bootstrap):
    """{identifier: name} from Protect's bootstrap — the names only. MEASURED on Dave's box (1230 on the box): an
    alarm's sources did not match the devices' ids — 12 unnamed on the faces/plates alarm, with 12 cameras — so each
    device is known by its id AND its MAC, matched exactly."""
    names = {}
    b = bootstrap if isinstance(bootstrap, dict) else {}
    for k in _DEVICE_LISTS:
        for d in (b.get(k) or []):
            if not isinstance(d, dict):
                continue
            n = d.get("name") or d.get("displayName") or d.get("type") or "Unnamed"
            for ident in (d.get("id"), d.get("mac")):
                if ident and len(_key(ident)) >= 8:
                    names[_key(ident)] = n
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


def unnamed_shapes(automations, bootstrap=None):
    """For the box check only: the LENGTHS of source identifiers no device answers to — never the identifiers."""
    names = device_names(bootstrap)
    out = set()
    for a in (automations or []):
        for s in ((a or {}).get("sources") or []) if isinstance(a, dict) else []:
            k = _key((s or {}).get("device")) if isinstance(s, dict) else ""
            if k and k not in names:
                out.add(len(k))
    return sorted(out)


def alarms_view(automations, bootstrap=None):
    """Each alarm as named fields only. Deleted alarms are left out."""
    names = device_names(bootstrap)
    out = []
    for a in (automations or []):
        if not isinstance(a, dict) or a.get("deleted"):
            continue
        devs, unknown = [], 0
        for s in (a.get("sources") or []):
            did = _key((s or {}).get("device")) if isinstance(s, dict) else ""
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
