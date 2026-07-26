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
import time


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("+", " plus").replace("&", " and ")


def _source_matches(app: str, source: str) -> bool:
    a, s = _norm(app), _norm(source)
    if not a or not s:
        return False
    return s == a or a in s or s in a


# ARCHITECTURE RULE (white-label): a device's apps come ONLY from what it (or
# its certified integration) exposes — NEVER a default/blind list (which risks
# launching apps that aren't installed). For androidtv_remote the per-device
# list is the integration's own "Configure Applications List" (exposed via
# source_list once configured, launched safely with select_source) — or the
# certified ADB harvest of the box's true installed apps. Configured/harvested
# → shows; nothing → shows nothing, honestly.
_ANDROID_PLATFORMS = ("androidtv_remote", "androidtv")
# ProOS Android TV app set — the Android TV Remote integration publishes no
# source_list, so this is the room-level equivalent of the dashboard's list.
# Launched by package id via play_media.
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
    """entity_id -> integration platform, from the live registry."""
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
        state = st.get("state")
        plat = plats.get(eid) or ""
        # CAPABILITY-FIRST, never brand-first: any device that publishes a live
        # app list (source_list) uses it verbatim — Shield, Fire TV, Google TV,
        # whatever comes later adapts automatically, launched by select_source.
        apps = _source_list(st, apps_only=True)   # inputs are never apps, on ANY device
        if apps:
            out.append({"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                        "input": inp, "apps": apps, "apps_unknown": False,
                        "state": state})
            return
        if plat in _ANDROID_PLATFORMS:
            # Android box: no source_list from the integration — use the ProOS
            # app set, launched by package id (works asleep or awake).
            out.append({"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                        "input": inp, "apps": [n for n, _ in ANDROID_APPS],
                        "app_ids": dict(ANDROID_APPS),
                        "apps_unknown": False, "state": state})
            return
        # A sleeping/off device reports NO source_list — that means its apps are
        # UNKNOWN, not absent. Callers must say "wake it first", never "it can't".
        unknown = state in (None, "off", "standby", "unavailable")
        out.append({"entity_id": eid, "name": _fname(st, eid), "is_display": is_display,
                    "input": inp, "apps": [], "apps_unknown": unknown,
                    "state": state})

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
            "devices": [{"entity_id": c["entity_id"], "name": c["name"], "is_display": c["is_display"],
                         "apps_unknown": bool(c.get("apps_unknown"))} for c in cands],
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


def _watch_script(client, project, area_id: str):
    """The room's generated WATCH activity script — the committed choreography
    that powers the display + sources on. Resolved live from the project by
    kind, never by name."""
    try:
        st = project.activities_status(client, project.load(), area_id) or {}
        for a in (st.get("activities") or []):
            if str(a.get("kind") or "").lower().startswith("watch"):
                eid = a.get("entity_id") or ("script." + a["object_id"] if a.get("object_id") else None)
                if eid:
                    return eid
    except Exception:
        pass
    return None


def _wake_room(client, project, area_id: str, wait_eids: list, display: str,
               timeout: float = 28.0) -> dict:
    """Fire the watch activity and WAIT until the display is on and each device
    in wait_eids reports an app list (or timeout). Power stays with the room's
    committed choreography — we never raw-power devices here."""
    script = _watch_script(client, project, area_id)
    if not script:
        return {"woke": False, "reason": "no watch activity committed for this room"}
    try:
        client._req("POST", "/api/services/script/turn_on", {"entity_id": script})
    except Exception as e:  # noqa: BLE001
        return {"woke": False, "reason": "couldn't run the watch activity: %s" % e}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.0)
        disp_ok = True
        if display:
            ds = _state(client, display)
            disp_ok = (ds.get("state") or "") not in ("off", "unavailable", "standby")
        devs_ok = all(_source_list(_state(client, e), apps_only=True) for e in (wait_eids or []))
        if disp_ok and devs_ok:
            break
    return {"woke": True, "script": script}


def launch(client, project, area_id: str, app: str, device: str = None,
           require_display_on: bool = True, auto_wake: bool = True) -> dict:
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
    woke = None

    def _enumerate():
        cands = candidates(client, project, area_id)
        hits = find(client, project, area_id, app)
        asleep = [c for c in cands if c.get("apps_unknown")]
        return cands, hits, asleep

    cands, hits, asleep = _enumerate()

    # If the CHOSEN device is asleep, or nothing awake shows the app but sleepers
    # exist — wake the room (its own watch choreography) and re-look. Faultless:
    # the user never gets told to power things on themselves.
    def _wake_and_relook(wait_eids):
        nonlocal cands, hits, asleep, woke
        if not auto_wake:
            return
        woke = _wake_room(client, project, area_id, wait_eids, disp)
        if woke.get("woke"):
            cands, hits, asleep = _enumerate()

    if device and any(c["entity_id"] == device for c in asleep):
        _wake_and_relook([device])
    elif not hits and asleep:
        _wake_and_relook([c["entity_id"] for c in asleep])

    if not hits:
        allapps = sorted({s for c in cands for s in c["apps"]})
        still_asleep = [c for c in cands if c.get("apps_unknown")]
        if still_asleep:
            return {"error": "woke the room but %s still isn't reporting its apps — it may need a "
                             "moment; try again shortly" % ", ".join(c["name"] for c in still_asleep)
                             if (woke or {}).get("woke") else
                             "no powered-on device is showing '%s' and this room has no watch "
                             "activity to wake it with" % app,
                    "asleep_devices": [{"entity_id": c["entity_id"], "name": c["name"]} for c in still_asleep],
                    "available_apps": allapps[:40]}
        return {"error": "'%s' isn't available on any device in this room" % app,
                "available_apps": allapps[:40],
                "note": "tell the user which apps ARE available; don't invent one"}

    # Resolve the target. ALWAYS ask when several devices could run it — awake
    # hits plus any still-sleeping device (selectable; it'll be woken on pick).
    options = ([{"entity_id": h["entity_id"], "name": h["name"], "is_display": h["is_display"],
                 "asleep": False} for h in hits]
               + [{"entity_id": c["entity_id"], "name": c["name"], "is_display": c["is_display"],
                   "asleep": True} for c in asleep])
    target = None
    if device:
        target = next((h for h in hits if h["entity_id"] == device), None)
        if not target:
            return {"error": "%s can't run '%s' (or isn't in this room)" % (device, app),
                    "options": options}
    elif len(options) == 1:
        target = hits[0]
    else:
        return {"needs_choice": True, "app": app, "options": options,
                "note": "several devices could run this — ASK the user which one, then call again "
                        "with device set. Options marked asleep will be woken automatically on pick."}
    # The display must be on to route/select; wake handles it when auto_wake is
    # on, otherwise keep the honest error.
    dst = _state(client, disp)
    if (dst.get("state") or "") in ("off", "unavailable", "standby"):
        if auto_wake and not (woke or {}).get("woke"):
            _wake_and_relook([])
            dst = _state(client, disp)
        if require_display_on and (dst.get("state") or "") in ("off", "unavailable", "standby"):
            return {"error": "the display is %s and the room's watch activity %s — check the room's "
                             "AV setup" % (dst.get("state") or "off",
                                           "didn't bring it up" if (woke or {}).get("woke") else "isn't available"),
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
    # Select the app on the target device. Android TV launches by PACKAGE ID
    # (play_media type 'app' — the integration has no source_list); everything
    # else selects the source by name.
    try:
        if target.get("app_id"):
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
            "woke_room": bool((woke or {}).get("woke")),
            "next": "verify the device's source is now '%s'" % target["source"]}
