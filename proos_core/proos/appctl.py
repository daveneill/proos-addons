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
sole argument media_player.select_source accepts — read live each call, never
stored.
"""
from __future__ import annotations


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
        rec_out = {"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                   "input": inp, "apps": apps, "state": st.get("state")}
        if not apps and (plats.get(eid) or "") in ANDROID_PLATFORMS:
            # Android box: no list from the integration — ProOS app set, launched
            # by package id.
            rec_out["apps"] = [n for n, _ in ANDROID_APPS]
            rec_out["app_ids"] = dict(ANDROID_APPS)
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
