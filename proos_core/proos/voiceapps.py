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
        out += ["(open|launch|start|put on) %s in [the] %s" % (a, r),
                "(open|launch|start|put on) %s on [the] %s tv" % (a, r),
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


def voice_apps_config(rooms: list):
    """rooms: [(area_id, room name, candidates)] -> the automation, or None when no room has an app."""
    triggers, said, seen = [], {}, set()
    for area, room, cands in rooms:
        apps = []
        for c in cands or []:
            for a in c.get("apps") or []:
                if a not in apps:
                    apps.append(a)
        for app in apps:
            dev = choose_device(cands, app)
            if not dev:
                continue
            cmds = [s for s in app_sentences(room, app) if s not in seen]
            if not cmds:
                continue
            seen.update(cmds)
            tid = "%s|%s" % (area, app)
            triggers.append({"trigger": "conversation", "command": cmds, "id": tid})
            said[tid] = {"say": app_said(room, app, dev.get("name"), bool(dev.get("is_display"))),
                         "area": area, "app": app, "device": dev.get("entity_id"), "room": room}
    if not triggers:
        return None
    cfg = {"id": VOICE_APPS_ID, "alias": "ProOS · Voice · Apps",
           "description": "Written by ProOS Core from each room's devices' own app lists. Edit it and Core leaves it alone.",
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
    rooms = []
    for key, rec in sorted(((project or {}).get("areas") or {}).items()):
        if not (rec and rec.get("display")):
            continue
        area = rec.get("area_id") or key
        room = names.get(area) or rec.get("name") or key
        try:
            cands = _appctl.candidates(client, _project, area)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "apps unreadable in %s: %s" % (room, str(e)[:120])}
        rooms.append((area, room, cands))
    want = voice_apps_config(rooms)
    out = _project.voice_write(client, VOICE_APPS_ID, want)
    if out.get("ok") and want:
        out["apps"] = sorted({v["app"] for v in want["variables"]["said"].values()})
    return out


def handle(client, project_mod, data: dict, launch=_appctl.launch) -> dict:
    """The event's act: the same launch the app tiles use. Returns the launch's own answer."""
    data = data or {}
    if data.get("kind") != "app":
        return {"ignored": True}
    area, app, dev = str(data.get("area") or ""), str(data.get("app") or ""), str(data.get("device") or "") or None
    if not area or not app:
        return {"error": "the event named no room or app"}
    try:
        return launch(client, project_mod, area, app, device=dev) or {}
    except Exception as e:                                       # noqa: BLE001
        return {"error": str(e)[:200]}
