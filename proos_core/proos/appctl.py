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

ONE thing is remembered, deliberately (see _remembered_apps below): the app list
a display has PUBLISHED. A Samsung Frame resting in Art Mode strips its apps out
of source_list and reports inputs only, so a room that plainly has Netflix
reports none. Standing constraint (START_HERE §5): "Keep all built-in TV apps —
ProOS may be installed in a home with only the Samsung TV." Remembering what the
device itself said is not the guessed fallback this module refuses to make; it
replays that exact device's own words when the vendor integration stops saying
them. Nothing is ever invented, and a fresh publish always overwrites.
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



# ── PUBLISHED-APP MEMORY ─────────────────────────────────────────────────────
# entity_id -> {"apps": [...], "seen": iso}. Written only when a device publishes
# a NON-EMPTY app list, so a reduced list (Art Mode, off, a connection blip) can
# never overwrite the good one. Mirrors server.py's Android TV ledger
# (atv_apps.json): ProOS's own record of what a device told us, on the box, in
# /data, surviving add-on updates.
def _ledger_path():
    # Resolved per call, not at import, so tests can point PROOS_DATA_DIR at a
    # temp dir (see tests/display_apps_bench.py).
    return _os.path.join(_os.environ.get("PROOS_DATA_DIR", "/data"), "display_apps.json")


def _ledger() -> dict:
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            d = _json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _remember_apps(eid: str, apps: list) -> None:
    """Record a device's published app list. No-op unless the list has content --
    never let a stripped list erase the real one."""
    if not (eid and apps):
        return
    try:
        cur = _ledger()
        if (cur.get(eid) or {}).get("apps") == list(apps):
            return                                   # unchanged, no write
        import datetime as _dt
        cur[eid] = {"apps": list(apps),
                    "seen": _dt.datetime.now().isoformat(timespec="seconds")}
        path = _ledger_path()
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(cur, fh, indent=1)
        _os.replace(tmp, path)                       # atomic
    except Exception:
        pass                                         # memory is a nicety, never a failure


def _remembered_apps(eid: str) -> list:
    """What this exact device published last time it was willing to say. Empty
    for a device we have never heard from -- we do not invent for strangers."""
    a = (_ledger().get(eid) or {}).get("apps")
    return list(a) if isinstance(a, list) and a else []


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
        apps = _source_list(st, apps_only=True)   # inputs are never apps
        # NOTE: still no GUESSED fallback list. A guessed list offers apps that
        # aren't installed and hides ones that are. Android boxes publish their
        # real list once learned in Pro (room -> device -> Learn apps).
        # What we DO fall back to is this device's own last published list --
        # its words, not ours -- because a Frame in Art Mode reports inputs only
        # and would otherwise strip a working room down to no apps at all.
        remembered = False
        if apps:
            _remember_apps(eid, apps)
        else:
            apps = _remembered_apps(eid)
            remembered = bool(apps)
        rec_out = {"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                   "input": inp, "apps": apps, "state": st.get("state"),
                   # so a caller can say WHY it has a list for a device that is
                   # currently reporting none, instead of looking like a guess
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
            "devices": [{"entity_id": c["entity_id"], "name": c["name"], "is_display": c["is_display"]} for c in cands],
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


def launch(client, project, area_id: str, app: str, device: str = None,
           require_display_on: bool = True) -> dict:
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
    # The display must be on to route/select. Power belongs to the room activity,
    # so we don't force it here — the caller runs the watch activity first.
    dst = _state(client, disp)
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
