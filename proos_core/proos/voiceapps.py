"""APPS AS INSTANT SENTENCES — "open netflix in the bedroom" — the Assist plan, build 2 (register 853; 14 Oct 2026).

THE PLATFORM FIRST. The platform's own sentence library understands lights, covers, volume and media; it has no idea which
apps a room's TV and boxes carry. ProOS does (appctl: each device's own published source list, the same list the app tiles
draw). The platform's own mechanism for a sentence the home is given is a conversation TRIGGER on an automation (register 733
used it for activities); matched on the box, ~150 ms, no AI. So Core writes ONE automation, "ProOS · Voice · Apps", with a
trigger per (room, app), at start and on every commit. Its action fires the platform event proos_voice {kind: app, area, app,
device, room}; Core hears the event on its own stream and runs the SAME appctl.launch the app tiles use (power plan, display
input, select source) — one mechanism, nothing done twice. The reply is set at once ("Bedroom, Netflix.") and the act follows;
the check after it is build 4.

WHICH DEVICE. Several devices in a room may carry the same app. appctl's rule for the AI road is "always ask" (30 Jul); a
voice sentence can't ask and stay instant, so the sentence is written for ONE device, chosen when the sentences are written:
the room's display when it carries the app, else the first source that does — and the reply NAMES it when it isn't the
display ("Bedroom, Netflix on the Apple TV."). Recorded for Dave's ruling (853).
"""
from __future__ import annotations

import json
import re

from proos import appctl as _appctl
from proos import project as _project

VOICE_APPS_ID = "proos_voice_apps"
EVENT = "proos_voice"
_WORDY = re.compile(r"[^a-z0-9 +&'.-]")


def speakable_app(app: str) -> str:
    """An app's name as a sentence can hold it: 'Netflix' -> 'netflix', 'Disney+' -> 'disney+' and 'disney plus'."""
    a = _project._speakable(app)
    return _WORDY.sub(" ", a).strip()


def app_names(app: str) -> list:
    base = speakable_app(app)
    if not base:
        return []
    out = [base]
    if "+" in base:
        out.append(base.replace("+", " plus").replace("  ", " ").strip())
    if "&" in base:
        out.append(base.replace("&", "and"))
    return list(dict.fromkeys(x for x in out if x))


def app_sentences(room: str, app: str) -> list:
    r = _project._speakable(room)
    if not r:
        return []
    out = []
    for a in app_names(app):
        # register 863: every way a person says it — "turn on", "play", "watch" included — so the app sentence, matched
        # before the platform's own play sentence, is the one that takes it (seen on the box 14 Oct 10:35: "play Netflix
        # in the bedroom" went to the radio).
        out += ["(open|launch|start|put on|turn on|play|watch|switch to|go to) %s in [the] %s" % (a, r),
                "(open|launch|start|put on|turn on|play|watch|switch to|go to) %s on [the] %s tv" % (a, r),
                "%s in [the] %s" % (a, r), "[the] %s %s" % (r, a)]
    return out


def app_said(room: str, app: str, device: str | None, is_display: bool) -> str:
    """What the home says at once, the room first; the device named only when it isn't the room's display."""
    tail = "" if (is_display or not device) else " on the %s" % device
    return "%s, %s%s." % (str(room or "").strip(), str(app or "").strip(), tail)


def choose_device(cands: list, app: str):
    """The device a room plays an app on, by the rule above; None when nobody carries it."""
    disp = next((c for c in cands if c.get("is_display") and app in (c.get("apps") or [])), None)
    if disp:
        return disp
    return next((c for c in cands if app in (c.get("apps") or [])), None)


def shortcut_rows(area: str, room: str, shortcuts: list, display: str = None, names: dict = None) -> list:
    """REGISTER 867: the room's APP SHORTCUTS (what the installer set up in Pro) as the sentences' rows —
    [{app, device, device_name, is_display}] — one per shortcut, the first shortcut for an app winning."""
    out, seen = [], set()
    for sc in shortcuts or []:
        app = sc.get("name") or sc.get("app")
        tgt = sc.get("target")
        if not app or not tgt or app in seen:
            continue
        seen.add(app)
        out.append({"app": app, "device": tgt, "device_name": (names or {}).get(tgt) or tgt,
                    "is_display": (tgt == display), "shortcut": {"app": sc.get("app"), "target": tgt}})
    return out


def voice_apps_config(rooms: list):
    """rooms: [(area_id, room name, rows)] — rows from shortcut_rows (register 867: the shortcuts, never a device's own
    app list) -> the automation, or None when no room has a shortcut."""
    triggers, said, seen = [], {}, set()
    for area, room, rows in rooms:
        for row in rows or []:
            app = row["app"]
            cmds = [s for s in app_sentences(room, app) if s not in seen]
            if not cmds:
                continue
            seen.update(cmds)
            tid = "%s|%s" % (area, app)
            triggers.append({"trigger": "conversation", "command": cmds, "id": tid})
            said[tid] = {"say": app_said(room, app, row.get("device_name"), bool(row.get("is_display"))),
                         "area": area, "app": app, "device": row.get("device"), "room": room,
                         "shortcut": row.get("shortcut")}
    if not triggers:
        return None
    cfg = {"id": VOICE_APPS_ID, "alias": "ProOS · Voice · Apps",
           "description": "Written by ProOS Core from each room's App Shortcuts (set up in Pro). Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10,
           "triggers": triggers, "conditions": [],
           "actions": [{"event": EVENT,
                        "event_data": {"kind": "app", "area": "{{ said[trigger.id].area }}", "app": "{{ said[trigger.id].app }}",
                                       "device": "{{ said[trigger.id].device }}", "room": "{{ said[trigger.id].room }}"}},
                       {"set_conversation_response": "{{ said[trigger.id].say }}"}],
           "variables": {"said": said}}
    cfg["variables"]["proos_hash"] = _project._voice_hash(cfg)
    return cfg


def voice_apps(client, project: dict) -> dict:
    """Write the apps' sentences into the platform (differences only; an installer-edited automation is left alone)."""
    names = {}
    try:
        names = {a.get("area_id"): a.get("name") for a in (client.area_registry() or [])}
    except Exception:                                            # noqa: BLE001
        pass
    from proos import shortcuts as _sc
    dev_names = {}
    try:
        dev_names = {e.get("entity_id"): (e.get("name") or e.get("original_name")) for e in (client.entity_registry() or [])}
    except Exception:                                            # noqa: BLE001
        pass
    rooms = []
    for key, rec in sorted(((project or {}).get("areas") or {}).items()):
        if not rec:
            continue
        area = rec.get("area_id") or key
        room = names.get(area) or rec.get("name") or key
        rows = shortcut_rows(area, room, _sc.room(area), rec.get("display"), dev_names)
        if rows:
            rooms.append((area, room, rows))
    want = voice_apps_config(rooms)
    out = _project.voice_write(client, VOICE_APPS_ID, want)
    if out.get("ok") and want:
        out["apps"] = sorted({v["app"] for v in want["variables"]["said"].values()})
    return out


def handle(client, project_mod, data: dict, launch=None) -> dict:
    """The event's act: the room's SHORTCUT, launched as the app tile launches it (register 867); the device-list launch
    only where no shortcut exists for the pair (older sentences). Returns the launch's own answer."""
    data = data or {}
    if data.get("kind") != "app":
        return {"ignored": True}
    area, app, dev = str(data.get("area") or ""), str(data.get("app") or ""), str(data.get("device") or "") or None
    if not area or not app:
        return {"error": "the event named no room or app"}
    try:
        from proos import shortcuts as _sc
        sc = _sc.find(area, app, dev) or _sc.find(area, app)
        if sc is not None:
            res = (launch or _sc.launch)(client, project_mod, area, sc) or {}
            if res.get("ok"):
                res.setdefault("launched", sc.get("name") or app)
                res.setdefault("device", sc.get("target"))
            return res
        return {"error": "%s isn't set up in this room — add it under Apps in Pro" % app}
    except Exception as e:                                       # noqa: BLE001
        return {"error": str(e)[:200]}
