"""GARAGE DOORS AND GATES — one proper door made from the pieces a driver gives (register 974).

Dave, 24 Oct 2026, 12:17–12:46: a Ness gives a garage door as three unrelated pieces — a TASK (shown by the platform as
a scene: elkm1_task_001 "Main Garage Door"), an OUTPUT (a switch) and a ZONE that says open or closed — so ProOS could
press the door but not know where it was, and the task landed in Scenes. His design (12:22–12:28): on the door sensor's
own page (Shows As → Garage Door) a Garage Door card: Opens With (a task, an output/relay or a button, suggested by
name, the installer confirms); for a relay a Pulse Time and NO/NC; one button or separate Open and Close relays; the
sensor Inverted for how it is wired; a Travel Time after which "Didn't Finish" is said; Test; Undo. 12:46: it must be a
Garage Door in Apple Home — and any other bridge later — never a blind.

THE PLATFORM'S MECHANISM: the template integration's cover (read on Dave's box, 2026.10.2: the Template helper's menu
offers "cover", whose form takes name, state template, open/close/stop actions and device_class). ProOS fills that form
through the platform's own config flow; the door IS a platform cover. With device_class garage or gate, the platform's
HomeKit bridge makes it a GarageDoorOpener (homekit/accessories.py, read: garage|gate + open/close → GarageDoorOpener;
any other cover without a position → WindowCoveringBasic, i.e. a blind — the fault Dave once hit).

WHAT ONLY PROOS KNOWS stays here: which pieces make which door, the wiring settings, and the travel watch.

SAFETY (single-button openers press open/stop/close alike): Open presses only when the sensor reads Closed, Close only
when it reads Open; a sensor with no reading presses nothing. Written into the door's own actions as conditions, so it
holds from every surface — the dashboard, Apple Home, voice.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

KINDS = ("garage", "gate")
CONTACTS = ("NO", "NC")
PULSE_MIN, PULSE_MAX, PULSE_DEFAULT = 0.5, 5.0, 1.0          # seconds a relay is closed for one press
TRAVEL_MIN, TRAVEL_MAX, TRAVEL_DEFAULT = 5, 120, 20           # seconds a door normally takes; then "Didn't Finish"
OPENER_DOMAINS = ("scene", "switch", "button", "script")      # a panel task, a relay, a button, a script
OPEN_WORDS = ("on", "open", "violated", "opening")            # what a door sensor says when the door is open
CLOSED_WORDS = ("off", "closed", "normal")                    # … and when it is closed


def _data(name: str) -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), name)


STORE = "garage_doors.json"
_lock = threading.Lock()


def load() -> dict:
    try:
        with open(_data(STORE)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_all(doors: dict) -> None:
    p = _data(STORE)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doors, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)


# ── the settings, checked ──────────────────────────────────────────────────────────────────────────────────────────
def clean(raw: dict) -> dict:
    """The installer's settings, checked. Raises ValueError with words a person reads."""
    if not isinstance(raw, dict):
        raise ValueError("no settings were sent")
    sensor = str(raw.get("sensor") or "")
    if not re.match(r"^(binary_sensor|sensor)\.[a-z0-9_]+$", sensor):
        raise ValueError("the door's sensor is not a sensor")
    opener = str(raw.get("opener") or "")
    if opener.split(".", 1)[0] not in OPENER_DOMAINS or not re.match(r"^[a-z_]+\.[a-z0-9_]+$", opener):
        raise ValueError("choose what opens the door — a task, an output, a button or an activity")
    closer = str(raw.get("closer") or "") or None
    if closer and (closer.split(".", 1)[0] not in OPENER_DOMAINS or closer == opener):
        raise ValueError("the Close relay must be a different output from the Open one")
    kind = raw.get("kind") or "garage"
    if kind not in KINDS:
        raise ValueError("a door is a Garage Door or a Gate")
    out = {"sensor": sensor, "opener": opener, "closer": closer, "kind": kind,
           "invert": bool(raw.get("invert")),
           "name": str(raw.get("name") or "").strip()[:60] or None}
    relay = opener.split(".", 1)[0] == "switch"
    if relay:
        try:
            pulse = float(raw.get("pulse_s", PULSE_DEFAULT))
        except (TypeError, ValueError):
            raise ValueError("Pulse Time is a number of seconds") from None
        if not PULSE_MIN <= pulse <= PULSE_MAX:
            raise ValueError("Pulse Time is from %.1f to %.0f seconds" % (PULSE_MIN, PULSE_MAX))
        contact = raw.get("contact") or "NO"
        if contact not in CONTACTS:
            raise ValueError("Relay Contact is NO or NC")
        out.update({"pulse_s": round(pulse, 1), "contact": contact})
    try:
        travel = int(raw.get("travel_s", TRAVEL_DEFAULT))
    except (TypeError, ValueError):
        raise ValueError("Travel Time is a whole number of seconds") from None
    if not TRAVEL_MIN <= travel <= TRAVEL_MAX:
        raise ValueError("Travel Time is from %d to %d seconds" % (TRAVEL_MIN, TRAVEL_MAX))
    out["travel_s"] = travel
    return out


# ── what the door reads ────────────────────────────────────────────────────────────────────────────────────────────
def reads(sensor_state, invert=False):
    """'open' / 'closed' from a sensor's state, or None when it has no reading."""
    s = str(sensor_state or "").strip().lower()
    if s in OPEN_WORDS:
        v = "open"
    elif s in CLOSED_WORDS:
        v = "closed"
    else:
        return None
    if invert:
        v = "closed" if v == "open" else "open"
    return v


def _reads_expr(sensor: str, invert: bool) -> str:
    """The same rule as reads(), as the platform's template language."""
    o, c = ("'closed'", "'open'") if invert else ("'open'", "'closed'")
    return ("{%% set s = states('%s') | lower %%}"
            "{%% if s in %s %%}{{ %s }}{%% elif s in %s %%}{{ %s }}{%% else %%}{{ none }}{%% endif %%}"
            % (sensor, list(OPEN_WORDS), o, list(CLOSED_WORDS), c))


def state_template(door: dict) -> str:
    return _reads_expr(door["sensor"], door.get("invert", False))


def _press(entity: str, door: dict) -> list:
    """One press of what opens (or closes) the door, as the platform's actions."""
    dom = entity.split(".", 1)[0]
    if dom == "scene":
        return [{"action": "scene.turn_on", "target": {"entity_id": entity}}]
    if dom == "button":
        return [{"action": "button.press", "target": {"entity_id": entity}}]
    if dom == "script":
        return [{"action": "script.turn_on", "target": {"entity_id": entity}}]
    first, then = ("turn_on", "turn_off") if door.get("contact", "NO") == "NO" else ("turn_off", "turn_on")
    return [{"action": "switch." + first, "target": {"entity_id": entity}},
            {"delay": {"milliseconds": int(round(float(door.get("pulse_s", PULSE_DEFAULT)) * 1000))}},
            {"action": "switch." + then, "target": {"entity_id": entity}}]


def _inline(door: dict) -> str:
    """reads() as one template EXPRESSION (for a condition)."""
    o, c = ("'closed'", "'open'") if door.get("invert") else ("'open'", "'closed'")
    s = "(states('%s') | lower)" % door["sensor"]
    return "(%s if %s in %s else (%s if %s in %s else 'none'))" % (o, s, list(OPEN_WORDS), c, s, list(CLOSED_WORDS))


def open_actions(door: dict) -> list:
    """Open: one press — and with one button for everything, only when the door reads Closed."""
    if door.get("closer"):
        return [{"condition": "template", "value_template": "{{ %s != 'open' }}" % _inline(door)}] + _press(door["opener"], door)
    return [{"condition": "template", "value_template": "{{ %s == 'closed' }}" % _inline(door)}] + _press(door["opener"], door)


def close_actions(door: dict) -> list:
    """Close: the Close relay when there is one; otherwise the same button, only when the door reads Open."""
    if door.get("closer"):
        return [{"condition": "template", "value_template": "{{ %s != 'closed' }}" % _inline(door)}] + _press(door["closer"], door)
    return [{"condition": "template", "value_template": "{{ %s == 'open' }}" % _inline(door)}] + _press(door["opener"], door)


def cover_form(door: dict, name: str) -> dict:
    """The platform's Template → Cover form, filled."""
    return {"name": name, "state": state_template(door), "open_cover": open_actions(door),
            "close_cover": close_actions(door), "device_class": door.get("kind", "garage")}


# ── what opens it, suggested ───────────────────────────────────────────────────────────────────────────────────────
def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if w not in ("the", "elkm1", "ness", "zone")}


def candidates(sensor: str, entities: list, devices: list, names: dict) -> list:
    """Everything that could open the door: the sensor's own device first, then anything sharing its words —
    best first. Never chosen for the installer; only put in order."""
    sent = next((e for e in entities if e.get("entity_id") == sensor), {}) or {}
    sdev = sent.get("device_id")
    want = _words(names.get(sensor) or sensor.split(".", 1)[1].replace("_", " "))
    out = []
    for e in entities:
        eid = str(e.get("entity_id") or "")
        dom = eid.split(".", 1)[0]
        if dom not in OPENER_DOMAINS or e.get("disabled_by"):
            continue
        nm = names.get(eid) or eid.split(".", 1)[1].replace("_", " ")
        shared = len(want & _words(nm))
        same_dev = bool(sdev) and e.get("device_id") == sdev
        if not shared and not same_dev:
            continue
        kind = {"scene": "task", "switch": "output", "button": "button", "script": "activity"}[dom]
        out.append({"entity": eid, "name": nm, "kind": kind, "score": shared * 2 + (1 if same_dev else 0)})
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return out


# ── making, changing and undoing the door, through the platform's own config flow ──────────────────────────────────
def create(req, door: dict, name: str) -> str:
    """Template → Cover through the platform's flow. Returns the new config entry id."""
    f = req("POST", "/api/config/config_entries/flow", {"handler": "template"}) or {}
    fid = f.get("flow_id")
    if not fid:
        raise RuntimeError("the platform's template helper did not start")
    try:
        s = req("POST", "/api/config/config_entries/flow/" + fid, {"next_step_id": "cover"}) or {}
        if s.get("step_id") != "cover":
            raise RuntimeError("the platform's template helper offered no cover")
        r = req("POST", "/api/config/config_entries/flow/" + fid, cover_form(door, name)) or {}
    except Exception:
        try:
            req("DELETE", "/api/config/config_entries/flow/" + fid)
        except Exception:  # noqa: BLE001
            pass
        raise
    if r.get("type") != "create_entry":
        raise RuntimeError("the platform did not make the door: %s" % (r.get("errors") or r.get("reason") or r.get("type")))
    return (r.get("result") or {}).get("entry_id") or ""


def update(req, entry_id: str, door: dict, name: str) -> None:
    """Change an existing door's settings through the platform's options flow."""
    f = req("POST", "/api/config/config_entries/options/flow", {"handler": entry_id}) or {}
    fid = f.get("flow_id")
    if not fid:
        raise RuntimeError("the platform would not open the door's settings")
    form = cover_form(door, name)
    form.pop("name", None)
    r = req("POST", "/api/config/config_entries/options/flow/" + fid, form) or {}
    if r.get("type") != "create_entry":
        raise RuntimeError("the platform did not change the door: %s" % (r.get("errors") or r.get("type")))


def remove(req, entry_id: str) -> None:
    req("DELETE", "/api/config/config_entries/entry/" + entry_id)


def pieces() -> set:
    """Every entity that is only a PART of a door (what presses it) — kept out of Apple Home and off Scenes."""
    out = set()
    for d in load().values():
        out.add(d.get("opener"))
        if d.get("closer"):
            out.add(d["closer"])
    out.discard(None)
    return out


# ── the travel watch: "Didn't Finish" ──────────────────────────────────────────────────────────────────────────────
class TravelWatch:
    """A press is SEEN (the opener's own state changes — a task's timestamp, a relay closing); if the door's sensor has
    not changed within its Travel Time, say so; when it changes, clear. Never inferred from a tap on a screen."""

    def __init__(self, state_of, raise_fn, clear_fn, now=time.time, timer=threading.Timer):
        self.state_of, self.raise_fn, self.clear_fn, self.now, self.timer = state_of, raise_fn, clear_fn, now, timer
        self._last = {}
        self._pending = {}

    def _door_for_opener(self, eid):
        for k, d in load().items():
            if eid in (d.get("opener"), d.get("closer")):
                return k, d
        return None, None

    def _door_for_sensor(self, eid):
        d = load().get(eid)
        return (eid, d) if d else (None, None)

    def on_change(self, eid: str) -> None:
        new = self.state_of(eid)
        old = self._last.get(eid)
        self._last[eid] = new
        k, d = self._door_for_sensor(eid)
        if d:
            p = self._pending.pop(k, None)
            if p:
                try:
                    p["t"].cancel()
                except Exception:  # noqa: BLE001
                    pass
            self.clear_fn(k, d)
            return
        k, d = self._door_for_opener(eid)
        if not d or old is None or new == old:
            return
        dom = eid.split(".", 1)[0]
        pressed = (dom in ("scene", "button", "script")) or \
                  (dom == "switch" and new == ("on" if d.get("contact", "NO") == "NO" else "off"))
        if not pressed or k in self._pending:
            return
        before = reads(self.state_of(d["sensor"]), d.get("invert"))
        t = self.timer(float(d.get("travel_s", TRAVEL_DEFAULT)), self._expire, args=(k, before))
        t.daemon = True
        self._pending[k] = {"t": t, "before": before, "at": self.now()}
        t.start()

    def _expire(self, k, before):
        self._pending.pop(k, None)
        d = load().get(k)
        if not d:
            return
        now = reads(self.state_of(d["sensor"]), d.get("invert"))
        if now == before:
            self.raise_fn(k, d, before)
