"""
ProOS Core -- app launching across a room's app-capable devices.

A room can have several devices that run streaming apps: the smart TV itself,
plus committed sources like an Apple TV or an Nvidia Shield. "Put Netflix on"
is therefore ambiguous — so this module enumerates every device in the room
that actually offers the requested app (read live from each device's
source_list) and, when more than one can, hands back the choices so the caller
ASKS which. Once a device is chosen it routes the display to that device's input
(from the committed inputs map) and selects the app on it.

Everything keys off IMMUTABLE ids: the room by area_id, the display and each
source by entity_id, the input by the committed inputs map (source_entity ->
display input). The only name involved is HA's own source string, which is the
sole argument media_player.select_source accepts — read live each call.

ONE thing is remembered, deliberately (see _remembered_list below): the
source_list a device has PUBLISHED. Measured on the live house 30 Jul 2026:
every display and Apple TV published a full list earlier in the day and NONE of
them did by late afternoon — the attribute flickers in and out as the vendor
integrations refresh, and the recorder shows it missing from most updates
entirely. A value that is absent half the time cannot be read on demand, so the
room's app list has to be remembered or it is a coin toss. Remembering what the
device itself said is not a guess: it replays that exact device's own words when
the integration stops saying them, and a fresh publish always wins.
"""
from __future__ import annotations

import json as _json
import os as _os


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("+", " plus").replace("&", " and ")


def _source_matches(app: str, source: str) -> bool:
    a, s = _norm(app), _norm(source)
    if not a or not s:
        return False
    return s == a or a in s or s in a


# ProOS Android TV app set. The Android TV Remote integration publishes no
# source_list of its own, so this is the room-level twin of the dashboard's
# list — launched by package id via play_media. A device that DOES publish a
# list always wins over this.
ANDROID_PLATFORMS = ("androidtv_remote", "androidtv")
ANDROID_APPS = [
    ("Netflix", "com.netflix.ninja"),
    ("Disney+", "com.disney.disneyplus"),
    ("Prime Video", "com.amazon.amazonvideo.livingroom"),
    ("YouTube", "com.google.android.youtube.tv"),
    ("Kayo", "au.com.kayosports.kayoapp"),
    ("Binge", "au.com.streamotion.binge"),
    ("Stan", "au.com.stan.and.tv"),
    ("Plex", "com.plexapp.android"),
    ("Spotify", "com.spotify.tv.android"),
    ("ABC iview", "au.net.abc.iview"),
]


def _platforms(client) -> dict:
    try:
        return {e.get("entity_id"): e.get("platform")
                for e in (client.entity_registry() or [])}
    except Exception:
        return {}


def _rec_for_area(project, area_id: str) -> dict:
    try:
        for key, rec in (project.load() or {}).get("areas", {}).items():
            if rec and (rec.get("area_id") or key) == area_id:
                return rec
    except Exception:
        pass
    return {}


def _state(client, eid):
    try:
        return client._req("GET", "/api/states/%s" % eid) or {}
    except Exception:
        return {}


import re as _re

# A smart TV's source_list mixes streaming APPS with physical INPUTS (HDMI, Live
# TV, tuner…). For app launching we only want the apps, so drop input-like names.
_INPUT_RE = _re.compile(
    r"^(hdmi|av|input|source|component|composite|video|vga|dvi|scart|usb|pc|rgb|"
    r"cable|antenna|tuner|aux|line|dtv|atv|tv|live tv|screen ?mirroring|airplay)\b",
    _re.I)


def _source_list(st, apps_only: bool = False) -> list:
    lst = [s for s in ((st.get("attributes") or {}).get("source_list") or []) if isinstance(s, str)]
    if apps_only:
        lst = [s for s in lst if not _INPUT_RE.match(s.strip())]
    return lst



# ── PUBLISHED SOURCE-LIST MEMORY ─────────────────────────────────────────────
# entity_id -> {"list": [...verbatim...], "seen": iso}. The FULL published list
# is stored, inputs and apps together in the device's own order, because that is
# what the remote shows when the device is publishing properly and replaying
# anything less would change how the room looks.
#
# Written ONLY when the published list contains at least one real app. A list of
# nothing but inputs (a Frame resting in Art Mode publishes exactly its five
# inputs) must never overwrite a good one.
#
# Same pattern, and same /data home, as server.py's Android TV ledger
# (atv_apps.json): ProOS's own record of what a device told us, on the box,
# surviving add-on updates.
def _ledger_path():
    # Resolved per call, not at import, so tests can point PROOS_DATA_DIR at a
    # temp dir (see tests/display_apps_bench.py).
    return _os.path.join(_os.environ.get("PROOS_DATA_DIR", "/data"),
                         "display_apps.json")


def _ledger() -> dict:
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            d = _json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _remember_list(eid: str, full: list) -> bool:
    """Record a device's published source_list verbatim. Returns True if it
    changed. No-op unless the list carries at least one real app."""
    if not eid or not full:
        return False
    # appctl has its OWN input filter (_INPUT_RE, above). appart.is_app() is a
    # different module and is NOT imported here -- calling it would raise on
    # every sweep.
    if not [x for x in full
            if isinstance(x, str) and not _INPUT_RE.match(x.strip())]:
        return False                                  # inputs only -- not evidence
    try:
        cur = _ledger()
        if (cur.get(eid) or {}).get("list") == list(full):
            return False                              # unchanged, no write
        import datetime as _dt
        cur[eid] = {"list": list(full),
                    "seen": _dt.datetime.now().isoformat(timespec="seconds")}
        path = _ledger_path()
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(cur, fh, indent=1)
        _os.replace(tmp, path)                        # atomic
        return True
    except Exception:
        return False                                  # memory is never a failure


def _remembered_list(eid: str) -> list:
    """What this exact device published last time it was willing to say. Empty
    for a device we have never heard from -- we do not invent for strangers."""
    v = (_ledger().get(eid) or {}).get("list")
    return list(v) if isinstance(v, list) and v else []


def observe_snapshot(snap: dict) -> int:
    """Learn every media_player's published source_list from the sweep snapshot.

    Rides the ctlbridge sweep's EXISTING snapshot, so it costs zero extra HA
    traffic -- the same rule healthmon follows. Learning must be continuous
    rather than on-request: the attribute is only present on some updates, so a
    request-driven read would simply miss it. Returns how many entities changed.
    """
    n = 0
    for eid, rec in (snap or {}).items():
        if not (isinstance(eid, str) and eid.startswith("media_player.")):
            continue
        full = _source_list(rec or {})               # verbatim, inputs included
        if full and _remember_list(eid, full):
            n += 1
    return n


def _fname(st, eid):
    return (st.get("attributes") or {}).get("friendly_name") or eid


def candidates(client, project, area_id: str) -> list:
    """App-capable devices committed to the room: the display (if its source_list
    carries app entries) and each source. Each: {entity_id, name, is_display,
    input (display input for a source, from the committed map), apps:[...]}."""
    rec = _rec_for_area(project, area_id)
    if not rec:
        return []
    plats = _platforms(client)
    out = []

    def _add(eid, is_display, inp):
        st = _state(client, eid)
        full = _source_list(st)                   # verbatim, inputs included
        apps = _source_list(st, apps_only=True)   # inputs are never apps
        # NOTE: still no GUESSED fallback list. A guessed list offers apps that
        # aren't installed and hides ones that are. Android boxes publish their
        # real list once learned in Pro (room -> device -> Learn apps).
        # What we DO fall back to is this device's OWN last published list --
        # its words, not ours -- because the attribute is simply absent on most
        # updates (see the module note). Replayed VERBATIM: inputs and apps in
        # the device's own order, so the remote looks exactly as it does when
        # the device is behaving.
        remembered = False
        if apps:
            _remember_list(eid, full)
        else:
            prev = _remembered_list(eid)
            if prev:
                full = prev
                apps = [x for x in prev
                        if isinstance(x, str) and not _INPUT_RE.match(x.strip())]
                remembered = True
        rec_out = {"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                   "input": inp, "apps": apps, "state": st.get("state"),
                   "apps_full": full,
                   "apps_remembered": remembered}
        out.append(rec_out)

    disp = rec.get("display")
    if isinstance(disp, str) and disp.startswith("media_player."):
        _add(disp, True, None)
    inputs = rec.get("inputs") or {}
    for s in (rec.get("sources") or []):
        if isinstance(s, str) and s.startswith("media_player."):
            _add(s, False, inputs.get(s))
    return out


def room_apps(client, project, area_id: str) -> dict:
    """For the dashboard: the union of launchable apps in the room and, per app,
    which devices offer it. Apps are the raw source strings from each device."""
    cands = candidates(client, project, area_id)
    apps = {}
    for c in cands:
        for a in c["apps"]:
            apps.setdefault(a, []).append({"entity_id": c["entity_id"], "name": c["name"],
                                           "is_display": c["is_display"]})
    return {"area_id": area_id,
            # apps_full = the device's FULL published list, inputs and apps in
            # its own order, replayed from memory when the device is not
            # publishing. A device remote renders this verbatim.
            "devices": [{"entity_id": c["entity_id"], "name": c["name"],
                         "is_display": c["is_display"],
                         "apps_full": c.get("apps_full") or [],
                         "apps_remembered": c.get("apps_remembered", False)}
                        for c in cands],
            "apps": [{"app": a, "devices": ds} for a, ds in sorted(apps.items(), key=lambda kv: kv[0].lower())]}


def find(client, project, area_id: str, app: str) -> list:
    """Devices in the room whose source_list has a match for `app`, each with the
    exact source string to select."""
    hits = []
    for c in candidates(client, project, area_id):
        for s in c["apps"]:
            if _source_matches(app, s):
                hits.append({"entity_id": c["entity_id"], "name": c["name"],
                             "is_display": c["is_display"], "input": c["input"],
                             "source": s, "state": c["state"],
                             "app_id": (c.get("app_ids") or {}).get(s)})
                break
    return hits


def launch_power_plan(display_state, verdict_state, target_key):
    """Pure: what to do about POWER before an app command (3 Aug — the
    widget's client-side rule moved into Core so EVERY launch path
    inherits it: device on before the app command).
    Returns (action, settle_seconds): ("fire", 1.8) cold display,
    ("fire", 0.7) room lit on a different source, (None, 0) when the
    room is already watching the target."""
    if target_key and str(verdict_state or "") == str(target_key):
        return (None, 0)
    if str(display_state or "") in ("off", "unavailable", "standby", ""):
        return ("fire", 1.8)
    return ("fire", 0.7)


def launch(client, project, area_id: str, app: str, device: str = None,
           require_display_on: bool = True, power_on: bool = True) -> dict:
    """Enumerate → (always) ask when >1 → route + select. Returns a structured
    result the assistant or dashboard can act on:
      {needs_choice: True, app, options:[...]}   — caller must ask which device
      {ok: True, launched, device, display, source}
      {error: ...}
    """
    app = (app or "").strip()
    if not area_id or not app:
        return {"error": "area_id and app required"}
    rec = _rec_for_area(project, area_id)
    disp = rec.get("display")
    if not (isinstance(disp, str) and disp.startswith("media_player.")):
        return {"error": "this room has no committed display — set one in the AV activity setup"}
    hits = find(client, project, area_id, app)
    if not hits:
        allapps = sorted({s for c in candidates(client, project, area_id) for s in c["apps"]})
        return {"error": "'%s' isn't available on any device in this room" % app,
                "available_apps": allapps[:40],
                "note": "tell the user which apps ARE available; don't invent one"}
    # Resolve the target. Always ask when several devices can run it.
    target = None
    if device:
        target = next((h for h in hits if h["entity_id"] == device), None)
        if not target:
            return {"error": "%s can't run '%s' (or isn't in this room)" % (device, app),
                    "options": [{"entity_id": h["entity_id"], "name": h["name"]} for h in hits]}
    elif len(hits) == 1:
        target = hits[0]
    else:
        return {"needs_choice": True, "app": app,
                "options": [{"entity_id": h["entity_id"], "name": h["name"],
                             "is_display": h["is_display"]} for h in hits],
                "note": "several devices can run this — ASK the user which one, then call again with device set"}
    # POWER (3 Aug): Core runs the room's watch activity for the target
    # itself — device on (or input routed) BEFORE the app command, with the
    # same settle waits the dashboard widget proved live. One mechanism;
    # every caller (room Apps list, widgets, Assist) inherits it.
    dst = _state(client, disp)
    if power_on and target is not None:
        try:
            import time as _t
            _res = project.activities_status(client, project.load(), area_id)
            _acts = (_res or {}).get("activities") or []
            _act = next((a for a in _acts
                         if a.get("source_eid") == target["entity_id"]
                         and "off" not in str(a.get("key") or "")
                         and a.get("entity_id")), None)
            if _act:
                _vs = (_state(client, "sensor.proos_activity_%s"
                              % (rec.get("area_id") or area_id))
                       or {}).get("state")
                _fire, _wait = launch_power_plan(dst.get("state"), _vs,
                                                 _act.get("key"))
                if _fire:
                    client.call_service("script", "turn_on",
                                        _act["entity_id"])
                    _t.sleep(_wait)
                    dst = _state(client, disp)
        except Exception:                                        # noqa: BLE001
            pass
    if require_display_on and (dst.get("state") or "") in ("off", "unavailable", "standby"):
        return {"error": "the display is %s — run the room's watch activity first, then launch"
                         % (dst.get("state") or "off"),
                "display": disp}
    # Route the display to the target source's input (if the target isn't the
    # display itself and we know its committed input).
    routed = None
    if not target["is_display"] and target.get("input"):
        try:
            client._req("POST", "/api/services/media_player/select_source",
                        {"entity_id": disp, "source": target["input"]})
            routed = target["input"]
        except Exception:
            routed = None
    # Select the app on the target device.
    try:
        if target.get("app_id"):
            # Android TV launches by PACKAGE ID (the integration has no source_list).
            client._req("POST", "/api/services/media_player/play_media",
                        {"entity_id": target["entity_id"],
                         "media_content_type": "app",
                         "media_content_id": target["app_id"]})
        else:
            client._req("POST", "/api/services/media_player/select_source",
                        {"entity_id": target["entity_id"], "source": target["source"]})
    except Exception as e:  # noqa: BLE001
        return {"error": "couldn't launch %s on %s: %s" % (target["source"], target["name"], e)}
    return {"ok": True, "launched": target["source"], "app": target["source"],
            "device": target["entity_id"], "device_name": target["name"],
            "display": disp, "routed_input": routed,
            "next": "verify the device's source is now '%s'" % target["source"]}
