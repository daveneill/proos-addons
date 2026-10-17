"""
ProOS Core -- app shortcuts: the widget model (design 5 Aug 2026).

NO list, anywhere. A room shows exactly the apps someone chose to add — never
enumerated from a device, never remembered, never guessed. Each shortcut carries
the ProOS-canonical icon (one look on every brand) and a launch command chosen
for the target device's platform. The room's existing watch activity powers the
device on and routes to it BEFORE the launch (appctl.launch_power_plan) — that is
unchanged; a shortcut only supplies the final app-launch step.

This module is pure model + a small per-room store. It calls no HA services and
depends on no device exposing anything. `appart` supplies the canonical identity
and icon; the REGISTRY below supplies default per-platform launch tokens (the ONE
place those ids live — it replaces appctl.ANDROID_APPS and prepare.SAMSUNG_APP_LIST
once Stage 5 removes them). An installer can override any token per room.
"""
from __future__ import annotations

import json
import os
import time

from . import appart


# ── THE REGISTRY ─────────────────────────────────────────────────────────────
# canonical slug -> per-platform launch token.
#   samsung : Tizen application id   (media_player.play_media, type "app")
#   android : package id             (media_player.play_media, type "app")
# Apple TV needs no column: it launches by the app's NAME (select_source), which
# `appart` already gives us. Add a brand by adding a key; add an app by adding a
# row. Nothing here is derived from a device — it is curated, and overridable.
REGISTRY = {
    "netflix":      {"samsung": "3201907018807", "android": "com.netflix.ninja"},
    "prime_video":  {"samsung": "3201512006785", "android": "com.amazon.amazonvideo.livingroom"},
    "disney":       {"samsung": "3201901017640", "android": "com.disney.disneyplus"},
    "youtube":      {"samsung": "111299001912",  "android": "com.google.android.youtube.tv"},
    "youtube_kids": {"samsung": "3201611010983"},
    "spotify":      {"samsung": "3201606009684", "android": "com.spotify.tv.android"},
    "kayo":         {"samsung": "3201910019354", "android": "au.com.kayosports.kayoapp"},
    "binge":        {"android": "au.com.streamotion.binge"},
    "stan":         {"samsung": "3201606009798", "android": "au.com.stan.and.tv"},
    "plex":         {"android": "com.plexapp.android"},
    "abc_iview":    {"samsung": "3201812017479", "android": "au.net.abc.iview"},
    "7plus":        {"samsung": "3201803015934"},
    "9now":         {"samsung": "3201607010031"},
    "10":           {"samsung": "3201704012147"},
    "foxtel":       {"samsung": "3201910019449"},
    "apple_tv":     {"samsung": "3201807016597"},
    "tubi":         {"samsung": "3201504001965"},
    "docplay":      {"samsung": "3201901017758"},
    "calm":         {"samsung": "3201909019241"},
    "animelab":     {"samsung": "3201808016819"},
}

# integration platform -> how a device on THAT platform launches an app.
#   play_media_app : media_player.play_media, media_content_type "app", id = token
#   select_source  : media_player.select_source, source = token (the app's name)
#   play_media_url : media_player.play_media, media_content_type "url", id = token
_PLATFORM_METHOD = {
    "samsungtv_smart":  "play_media_app",
    "androidtv_remote": "play_media_app",
    "androidtv":        "play_media_app",
    "apple_tv":         "select_source",
    # stage 1 of the planned build (register 889): the platform's own documented launch for two more brands — LG webOS
    # lists its apps in source_list and launches them by select_source; Roku's source_list IS its app list. Google Cast
    # launches an app by play_media with its own content type and is NOT assumed here — to be read on a device first.
    "webostv":          "select_source",
    "roku":             "select_source",
}
# which REGISTRY column a platform reads for its default token.
_PLATFORM_COLUMN = {
    "samsungtv_smart":  "samsung",
    "androidtv_remote": "android",
    "androidtv":        "android",
}
METHODS = ("play_media_app", "select_source", "play_media_url", "manual")

# ── REGISTER 918j: HOW AN ANDROID TV BOX OPENS AN APP ────────────────────────
# Seen on the box (918e–918i): a Shield REFUSES the platform's launch-by-package (market://launch?id=…) — the Play Store
# change the platform's own page names — and the connection drops. What it DOES open is the link the app itself
# declares in its manifest, through the platform's documented road (play_media, type url). Each row below was read
# from the app's own manifest or the platform's page AND seen opening the app on Dave's Shield; nothing is guessed.
#   link    — what is sent (the app's own declared link)
#   package — what the box must then say it is on (app_id), so the launch is checked
# An Android TV app with no row has no proven way in: its tile powers the room and says so ("manual").
ANDROID_LINKS = {
    "netflix": {"link": "netflix://", "package": "com.netflix.ninja"},              # platform page; seen 918g
    "foxtel":  {"link": "https://open.foxtel.com.au", "package": "au.com.foxtel.atv"},  # manifest 4.7.0; seen 918i
}


def android_recipe(app: str, package: str = ""):
    """The proven {link, package} for an Android TV app (by slug, or by the package the box reported), or None."""
    sl = canonical_slug(app) if app else ""
    if sl in ANDROID_LINKS:
        return ANDROID_LINKS[sl]
    for row in ANDROID_LINKS.values():
        if package and row["package"] == package:
            return row
    return None


def method_for(platform: str) -> str:
    """How this platform launches an app. Unknown platforms fall back to
    select_source (launch by name) — the most widely supported path."""
    return _PLATFORM_METHOD.get((platform or "").strip(), "select_source")


def canonical_slug(app: str) -> str:
    """The canonical identity slug (drives both the icon and the REGISTRY row).
    Unknown apps keep their own slug — never renamed by guesswork."""
    c = appart.canonical(app) or {}
    return c.get("slug") or appart.slug(app)


def display_name(app: str) -> str:
    c = appart.canonical(app) or {}
    return c.get("name") or app


def default_token(app: str, platform: str) -> str:
    """The launch token ProOS ships for this app on this platform. For platforms
    that launch by name (Apple TV, unknown) that is the app's display name."""
    col = _PLATFORM_COLUMN.get((platform or "").strip())
    if col:
        tok = (REGISTRY.get(canonical_slug(app)) or {}).get(col)
        if tok:
            return tok
    return display_name(app)


def make_shortcut(app: str, target: str, platform: str,
                  token: str = None, method: str = None) -> dict:
    """Build a shortcut, auto-filling the canonical icon, the launch method for
    the platform, and the default token — any of which an installer can override."""
    slug = canonical_slug(app)
    if (platform or "").strip() in ("androidtv_remote", "androidtv") and not method:
        return android_shortcut({"app": slug, "name": display_name(app), "target": target,
                                 "platform": (platform or "").strip(), "token": token or "",
                                 "icon": "/apps/art/tile/%s.png" % slug})
    return {
        "app": slug,
        "name": display_name(app),
        "target": target,
        "platform": (platform or "").strip(),
        "method": method or method_for(platform),
        "token": token if token not in (None, "") else default_token(app, platform),
        "icon": "/apps/art/tile/%s.png" % slug,
    }


def android_shortcut(sc: dict) -> dict:
    """An Android TV shortcut on the one road that works (918j): its proven link, read back by its package — or, with
    no proven link, 'manual' (the room is powered and routed, and the tile says to use the box's remote). A shortcut
    saved before 918j (method play_media_app, token = a name or package) is healed the same way at launch."""
    out = dict(sc or {})
    tok = str(out.get("token") or "")
    r = android_recipe(out.get("app") or out.get("name") or "", package=tok if "." in tok and "://" not in tok else "")
    if r:
        out.update({"method": "play_media_url", "token": r["link"], "package": r["package"]})
    elif out.get("method") != "play_media_url":
        out.update({"method": "manual", "token": out.get("token") or out.get("name") or out.get("app") or "manual"})
    return out


def validate(sc) -> tuple:
    """(ok, reason). A shortcut must name an app, a media_player target, a known
    method, and a non-empty token."""
    if not isinstance(sc, dict):
        return (False, "not a shortcut")
    for f in ("app", "target", "method", "token"):
        if not sc.get(f):
            return (False, "missing %s" % f)
    if sc["method"] not in METHODS:
        return (False, "unknown launch method %r" % sc.get("method"))
    if not str(sc["target"]).startswith("media_player."):
        return (False, "target must be a media_player entity")
    return (True, "")


def catalogue() -> list:
    """The registry as a pick-list for the setup UI: every app ProOS ships a
    token or an icon for, with the platforms it can launch on."""
    slugs = set(REGISTRY)
    for a, (canon, name, rank) in appart._canon_index().items():  # noqa: SLF001
        slugs.add(canon)
    out = []
    for s in sorted(slugs):
        plats = []
        if (REGISTRY.get(s) or {}).get("samsung"):
            plats.append("samsung")
        if (REGISTRY.get(s) or {}).get("android"):
            plats.append("android")
        plats.append("apple_tv")                       # always launchable by name
        out.append({"slug": s, "name": display_name(s),
                    "icon": "/apps/art/tile/%s.png" % s, "platforms": plats})
    return out


# ── per-room store ───────────────────────────────────────────────────────────
# {area_id: [shortcut, ...]}. Keyed by the room's immutable area_id, on the box,
# surviving add-on updates — same /data home and atomic-write pattern as the
# other ProOS ledgers.
def _path() -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "shortcuts.json")


def _all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, path)


def room(area_id: str) -> list:
    """The shortcuts an installer added to this room, in order."""
    return list(_all().get(area_id) or [])


def set_room(area_id: str, shortcuts) -> list:
    d = _all()
    d[area_id] = list(shortcuts or [])
    _write(d)
    return room(area_id)


def add(area_id: str, sc: dict) -> dict:
    """Add (or replace) a shortcut. One shortcut per app+target — adding the same
    app to the same device again updates it rather than duplicating."""
    ok, why = validate(sc)
    if not ok:
        return {"error": why}
    cur = [x for x in room(area_id)
           if not (x.get("app") == sc.get("app") and x.get("target") == sc.get("target"))]
    cur.append(sc)
    set_room(area_id, cur)
    return {"ok": True, "count": len(cur)}


def remove(area_id: str, app: str, target: str) -> dict:
    cur = room(area_id)
    new = [x for x in cur if not (x.get("app") == app and x.get("target") == target)]
    set_room(area_id, new)
    return {"ok": True, "removed": len(cur) - len(new)}


def reorder(area_id: str, order) -> dict:
    """Reorder a room's shortcuts by app slug; any not named keep their place at
    the end, so a partial order never drops a tile."""
    by_app = {}
    for x in room(area_id):
        by_app.setdefault(x.get("app"), []).append(x)
    new = []
    for a in (order or []):
        new.extend(by_app.pop(a, []))
    for rest in by_app.values():
        new.extend(rest)
    set_room(area_id, new)
    return {"ok": True, "count": len(new)}


# ── launching a shortcut ─────────────────────────────────────────────────────
# The single launch path. `command_for` is pure — it says exactly which HA
# service a shortcut fires — so it is fully benchable without a device. `launch`
# wraps it with the room's existing power-on-and-route step (the "system we
# had"): the pure decision stays in appctl.launch_power_plan, and the display is
# routed to the target's committed input when the target is a source, not the
# display itself. NOTHING here reads a device's exposed app list.
def command_for(sc: dict) -> dict:
    """The HA service call a shortcut fires: {domain, service, data}."""
    method = sc.get("method")
    target = sc.get("target")
    token = sc.get("token")
    if method == "play_media_app":
        return {"domain": "media_player", "service": "play_media",
                "data": {"entity_id": target, "media_content_type": "app",
                         "media_content_id": token}}
    if method == "play_media_url":
        return {"domain": "media_player", "service": "play_media",
                "data": {"entity_id": target, "media_content_type": "url",
                         "media_content_id": token}}
    return {"domain": "media_player", "service": "select_source",
            "data": {"entity_id": target, "source": token}}


FEATURE_FOR = {"select_source": 2048, "play_media_app": 512, "play_media_url": 512}   # the platform's own feature bits
RETRY_AFTER_S = 2.0        # plumbing: one more try after a device refused the command while waking
TARGET_WAKE_WAIT_S = 30.0  # plumbing: how long a streamer is given to come back to the platform after turn_on (an Apple TV
                           # read 20 s on the box, 14 Oct 14:46 — register 872); a command sent before that does nothing


def can_take(row: dict, method: str) -> bool:
    """The device's own word that it can take this command now: on, and the feature bit set."""
    if not row or str(row.get("state") or "") in ("off", "standby", "unavailable", "unknown", ""):
        return False
    bit = FEATURE_FOR.get(method or "select_source", 0)
    try:
        return not bit or bool(int((row.get("attributes") or {}).get("supported_features") or 0) & bit)
    except Exception:                                       # noqa: BLE001
        return False


# REGISTER 918: which devices say what app they opened. An Android TV box (both platform paths) reports the package it
# is on as app_id — the same id a play_media "app" launch sends — so the two can be compared. Nothing else is assumed.
OPENS_REPORTED = ("androidtv_remote", "androidtv")
OPEN_WAIT_S = 10.0         # plumbing: how long an Android box is given to bring the app up after the command


def reports_open(sc: dict) -> bool:
    return ((sc or {}).get("method") in ("play_media_app", "play_media_url")
            and (sc or {}).get("platform") in OPENS_REPORTED)


CHOOSER = "android"        # what an Android box reports while its "Open with…" box is up (seen 918i)


def opened(row: dict, sc: dict):
    """True: the device says it is on the app launched. False: it says it is on something else. None: it doesn't say."""
    if not reports_open(sc):
        return None
    on = str(((row or {}).get("attributes") or {}).get("app_id") or "")
    if not on:
        return None
    return on == str(sc.get("package") or sc.get("token") or "")


def still_on(attrs: dict) -> str:
    """What the device says it is showing, in words a person reads."""
    a = str((attrs or {}).get("app_name") or (attrs or {}).get("app_id") or "")
    if not a:
        return "what it was showing"
    return "its home screen" if "launcher" in a.lower() else a


def _ready(client, target: str, method: str):
    """Wake the target when it is asleep and wait until it can take the command (its own features), bounded."""
    from . import appctl, readback as _rb
    row = _state(client, target)
    if can_take(row, method):
        return True
    if str(row.get("state") or "") in ("off", "standby", "unavailable", "unknown", ""):
        client.call_service("media_player", "turn_on", target)
    rb = _rb.confirm(client, target, lambda r: can_take(r, method), wait=TARGET_WAKE_WAIT_S)
    return bool(rb.get("confirmed"))


def tv_agrees(dst: dict, rec: dict, target: str, disp: str) -> bool:
    """REGISTER 923 (Dave 17 Oct 11:26: "support the use case of any of these devices being turned on using the native
    remote controls … the activities created dont rely on confirming the TV or device is on but if it is on activity is
    quicker"). The room's activity marker says what ProOS last ran — not what the room is doing if someone has since used
    the TV's own remote. So "already watching" is believed only when the TV itself agrees:
      - the TV must read on (turned off by its own remote → the activity runs and turns it back on);
      - and when the TV reports its input (`source`): for a box, it must be on that box's committed input; for the TV
        itself, it must NOT be on any box's input (it is on its tuner or one of its own apps).
    A TV that does not report its input keeps the marker's word (today's behaviour). The generated activities already
    check before they act (TV on only if off, box woken only if asleep), so running one in a room that is already right
    costs a moment (Watch Apple TV read 0.1 s warm, ~20 s cold on the Bedroom)."""
    st = str((dst or {}).get("state") or "").lower()
    if st in ("off", "standby", "unavailable", "unknown", ""):
        return False
    src = str(((dst or {}).get("attributes") or {}).get("source") or "").strip()
    if not src:
        return True
    inputs = {k: str(v).strip() for k, v in ((rec or {}).get("inputs") or {}).items() if v}
    if target == disp:
        return src not in set(inputs.values())
    want = inputs.get(target)
    return (src == want) if want else True


class _RoomHandled(Exception):
    """The room's committed Watch activity took care of power and input (register 920)."""


ACTIVITY_DONE_WAIT_S = 60.0   # plumbing: how long a room's Watch activity is given to finish (Bedroom read 26 s, 920)


def _rec_for_area(project, area_id: str) -> dict:
    try:
        for key, rec in (project.load() or {}).get("areas", {}).items():
            if rec and (rec.get("area_id") or key) == area_id:
                return rec
    except Exception:
        pass
    return {}


def _state(client, eid: str) -> dict:
    try:
        return client._req("GET", "/api/states/%s" % eid) or {}
    except Exception:
        return {}


def launch(client, project, area_id: str, sc: dict, power: bool = True) -> dict:
    """Power on + route the target (via the room's watch activity), then fire the
    shortcut's app command. Returns a structured result. `power=False` fires the
    app command alone (the device is assumed already up on the right input)."""
    if (sc or {}).get("platform") in ANDROID:
        sc = android_shortcut(sc)                          # REGISTER 918j: the one road an Android box opens
    ok, why = validate(sc)
    if not ok:
        return {"error": why}
    from . import appctl                                   # launch_power_plan (pure, stays)
    rec = _rec_for_area(project, area_id)
    disp = rec.get("display")
    target = sc["target"]
    if power and isinstance(disp, str) and disp.startswith("media_player."):
        try:
            dst = _state(client, disp)
            res = project.activities_status(client, project.load(), area_id)
            acts = (res or {}).get("activities") or []
            act = next((a for a in acts
                        if a.get("source_eid") == target
                        and "off" not in str(a.get("key") or "")
                        and a.get("entity_id")), None)
            if act is None and target == disp:
                # REGISTER 922 (Dave 17 Oct 11:05: a Samsung TV app "works but its not triggering the watch TV activity
                # … so when I then try and use Apple TV shortcuts it stays on the TV app … as it thinks it is still in
                # watch Apple TV"). Read on the box: the Bedroom's watch_tv has source_eid null — the display's OWN
                # activity names no separate source — so a shortcut targeting the display matched nothing, fired
                # nothing, and the room's activity sensor stayed on watch_apple_tv; the next Apple TV tap read "already
                # watching" and skipped its activity. An app on the display is the room watching the TV: its Watch TV
                # activity (no source, or the display itself, and not an off) runs first, exactly as for a source.
                act = next((a for a in acts
                            if a.get("source_eid") in (None, "", disp)
                            and "off" not in str(a.get("key") or "")
                            and a.get("entity_id")), None)
            if act:
                vs = (_state(client, "sensor.proos_activity_%s"
                             % (rec.get("area_id") or area_id)) or {}).get("state")
                fire, wait = appctl.launch_power_plan(dst.get("state"), vs, act.get("key"))
                if not fire and not tv_agrees(dst, rec, target, disp):
                    fire = "fire"                          # REGISTER 923: the TV itself says otherwise — run it
                if fire:
                    client.call_service("script", "turn_on", act["entity_id"])
                    # REGISTER 920 (Dave 17 Oct 10:11: "with tv off it turns the TV on then off again then on again then
                    # launches app"). Read on the box: the Watch Apple TV activity started 10:07:41 and ran 26 s; the TV
                    # came on at 10:07:49; at 10:07:52 THIS launch sent its own select_source to the still-booting Samsung,
                    # which took it as power and went OFF (logbook context: media_player.select_source); the activity
                    # put it back on at 10:07:59. Two things drove one TV at once. The activity IS the room's power and
                    # input (that is what it was commissioned to do), so when it fired, the launch waits for it to
                    # FINISH — the script's own state, bounded — and sends no power or input of its own.
                    from . import readback as _rb
                    _rb.confirm(client, act["entity_id"],
                                lambda r: str(r.get("state") or "").lower() == "off", wait=ACTIVITY_DONE_WAIT_S)
                # Fired (and finished) or already watching this source: the room is already on the box. Nothing more.
                raise _RoomHandled()
            dst = _state(client, disp)
            if str(dst.get("state") or "") in ("off", "standby", "unavailable", ""):
                # REGISTER 867: a room with no committed watch activity for the target still gets its display ON —
                # the platform's own turn_on, then a bounded wait for it to read on (the same step appctl got in 865).
                client.call_service("media_player", "turn_on", disp)
                from . import readback as _rb
                _rb.confirm(client, disp, lambda r: str(r.get("state") or "").lower()
                            not in ("off", "standby", "unavailable", "unknown", ""), wait=appctl.DISPLAY_ON_WAIT_S)
            if target != disp:                              # route to the source's input
                inp = (rec.get("inputs") or {}).get(target)
                if inp:
                    client._req("POST", "/api/services/media_player/select_source",
                                {"entity_id": disp, "source": inp})
        except _RoomHandled:                                # the room's activity owns power and input (920)
            pass
        except Exception:                                   # power/route is best-effort
            pass
    # REGISTER 869 (seen on the box 14 Oct 13:41: "Entity media_player.bedroom_bedroom_apple_tv does not support action
    # media_player.select_source"): a streamer that is asleep reports NO app features to the platform; the app command
    # sent while it wakes is refused. So the TARGET is woken too — the platform's own turn_on — and the command waits
    # until the device says it can take it (its own supported_features), within the display's bound.
    if power:
        try:
            _ready(client, target, sc.get("method"))
        except Exception:                                   # noqa: BLE001
            pass
    if sc.get("method") == "manual":
        # REGISTER 918j: no proven way to open this app on this box. The room is on and on the box (above); nothing is
        # sent, and the tile says exactly that — never a launch that did nothing reported as done.
        nm = sc.get("name") or sc.get("app")
        return {"ok": False, "taken": False, "opened": False, "manual": True, "app": sc.get("app"), "target": target,
                "method": "manual",
                "error": "The TV is on the box — open %s with its remote. %s can't be opened directly on Android TV "
                         "yet." % (nm, nm)}
    cmd = command_for(sc)
    err = None
    for attempt in (1, 2):
        try:
            client._req("POST", "/api/services/%s/%s" % (cmd["domain"], cmd["service"]), cmd["data"])
            err = None
            break
        except Exception as e:                              # noqa: BLE001
            err = e
            if attempt == 1 and "does not support" in str(e):
                time.sleep(RETRY_AFTER_S)                   # the device woke between the check and the command — once more
                continue
            break
    if err is not None:
        return {"error": "couldn't launch %s: %s" % (sc.get("name") or sc.get("app"), err)}
    # REGISTER 918 (17 Oct, seen on the box): Foxtel "launched" on the Bedroom Shield — ok, taken — while the Shield sat
    # on its home screen (app_id com.google.android.tvlauncher): the token was the app's name, which an Android box
    # cannot open. A device that REPORTS the app it is on (an Android TV box: app_id = the package it launched) is
    # read back, bounded, and a launch it says did not open is a FAILED launch — the test, the proof and the tap all
    # say so. A device that does not report (Apple TV) stays on Dave's ruling below: awake + taken = done.
    from . import readback as _rb
    if reports_open(sc):
        rbo = _rb.confirm(client, target, lambda row: opened(row, sc),
                          attrs=("app_id", "app_name", "friendly_name"), wait=OPEN_WAIT_S)
        if rbo.get("confirmed") is False:
            at = rbo.get("attributes") or {}
            nm = sc.get("name") or sc.get("app")
            if str(at.get("app_id") or "") == CHOOSER:
                # REGISTER 918i: the box's "Open with…" box is up — asked once per app per box, answered on the TV.
                return {"ok": False, "taken": True, "opened": False, "chooser": True, "app": sc.get("app"),
                        "target": target, "method": sc["method"], "fired": cmd["data"], "read_back": rbo,
                        "error": "%s is asking which app opens %s — choose %s, then Always, and tap again."
                                 % (at.get("friendly_name") or target, nm, nm)}
            return {"ok": False, "taken": True, "opened": False, "app": sc.get("app"), "target": target,
                    "method": sc["method"], "fired": cmd["data"], "shows": still_on(at), "read_back": rbo,
                    "error": "%s didn't open %s — it is still on %s." % (at.get("friendly_name") or target, nm,
                                                                           still_on(at))}
    # DAVE'S RULING, 14 Oct 14:11 (register 871): "Apple TV will never be correct with saying what's on, so it just needs
    # to confirm it's on then send the command. There is no need to confirm if a device is on an app if the command
    # works." So: the device awake (above), the command taken by the platform without error = DONE. What the device
    # says it shows is read once and carried as information, never as the verdict.
    rb = _rb.confirm(client, target, lambda row: None, attrs=("source", "app_id", "app_name"), wait=0)
    shown = ((rb.get("attributes") or {}).get("app_name") or (rb.get("attributes") or {}).get("source") or None)
    return {"ok": True, "app": sc.get("app"), "target": target,
            "method": sc["method"], "fired": cmd["data"],
            "confirmed": True, "taken": True, "opened": True if reports_open(sc) and rbo_ok(rb, sc) else None,
            "shows": shown, "read_back": rb}


def rbo_ok(rb: dict, sc: dict) -> bool:
    """The final read says the app the shortcut launched (only for a device that reports it)."""
    return opened({"attributes": (rb or {}).get("attributes") or {}}, sc) is True


# ── REGISTER 867: the shortcuts ARE the room's apps for Assist and voice (Dave 14 Oct 12:32: "we confirmed ages ago that
# the app lists were not reliable from the devices so we built the app shortcuts … assist priorities are what is in the
# rooms first"). What is below answers "is this app set up here?", "which device could run it?" and, when Assist is
# asked for one that isn't, builds it ONLY after a test launch the device confirmed.
def _same(a, b):
    import re
    f = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower().replace("+", "plus"))   # noqa: E731
    return f(a) == f(b) and bool(f(a))


def find(area_id: str, app: str, target: str = None):
    """The room's shortcut for an app (by its name or slug), the target narrowing it when given; None when not set up."""
    for sc in room(area_id):
        if target and sc.get("target") != target:
            continue
        if _same(sc.get("name"), app) or _same(sc.get("app"), app) or _same(canonical_slug(app), sc.get("app")):
            return sc
    return None


def could_run(client, project, area_id: str, app: str) -> list:
    """[{entity_id, name, is_display}] — the room's committed devices that say they carry the app (a hint for the OFFER,
    never a launch: the device lists are the device's claim)."""
    try:
        from . import appctl
        return [{"entity_id": c["entity_id"], "name": c.get("name") or c["entity_id"], "is_display": bool(c.get("is_display"))}
                for c in appctl.candidates(client, project, area_id) if app in (c.get("apps") or [])
                or any(_same(app, a) for a in (c.get("apps") or []))]
    except Exception:                                       # noqa: BLE001
        return []


def offer_words(room_name: str, app: str, devices: list) -> str:
    """What Assist says when the app isn't set up in the room."""
    names = [d["name"] for d in devices or []]
    if not names:
        return "%s isn't set up in the %s, and none of the room's devices say they have it — add it under Apps in Pro." % (app, room_name)
    first = next((d for d in devices if d.get("is_display")), devices[0])
    if len(names) == 1:
        return "%s isn't set up in the %s — the %s has it. Add it?" % (app, room_name, names[0])
    return "%s isn't set up in the %s — it's on %s. Add it on the %s?" % (app, room_name, " and ".join(names), first["name"])


def build_tested(client, project, area_id: str, app: str, target: str, test=None) -> dict:
    """Build the shortcut for (app, target), TEST it — the real launch: the device awake and the command taken by the
    platform (Dave's ruling, 871) — and add it to the room only then. Returns {ok, added, shortcut, launch} or {error, launch}."""
    sc, err = from_request(client, {"app": app, "target": target})
    if err:
        return {"error": err}
    res = (test or launch)(client, project, area_id, sc)
    if not res.get("ok"):
        return {"error": res.get("error") or "the launch failed", "launch": res, "shortcut": sc}
    if not res.get("taken", res.get("confirmed")):
        return {"error": "%s wasn't taken by the device — not added" % (sc.get("name") or app), "launch": res, "shortcut": sc}
    added = add(area_id, sc)
    if added.get("error"):
        return {"error": added["error"], "launch": res, "shortcut": sc}
    return {"ok": True, "added": True, "shortcut": sc, "launch": res, "count": added.get("count")}


# ── REGISTER 918: WHAT THIS DEVICE CAN RUN (the add list) ────────────────────
# Dave, 17 Oct: "the add a shortcut list needs to be the apps that are synced to the device … Foxtel is an app on a
# shield but is not on Apple TV but the way the list is done you can add a foxtel app to apple tv it just wont do
# anything". And: "having the devices sync their app lists again". The list was every library app for every device.
# It is now the DEVICE'S OWN list, from the platform's own mechanism for that brand:
#   Apple TV / LG / Roku  — the source_list the device publishes (its app names; select_source takes them verbatim),
#                           remembered once seen (appctl's ledger — the attribute comes and goes, register 30 Jul).
#   Samsung               — the apps the TV publishes that ProOS holds a Tizen id for; a panel that publishes no list
#                           (the 2020 Frame) gets the ids ProOS ships, said as such.
#   Android TV (Shield)   — the platform entry's own app list ({package: name}, register 442), taught by watching an
#                           app open on the box or by Learn from Device. The package IS the launch token.
# The tile is always ours (the canonical icon) — the device decides only WHAT is offered, never how it looks.
ANDROID = ("androidtv_remote", "androidtv")


def device_apps(client, target: str, platform: str = None, android_held: dict = None) -> dict:
    """{target, platform, method, source, apps:[{app, name, word, token, tile}], unlaunchable:[word], note}."""
    from . import appctl
    plat = (platform or platform_of(client, target) or "").strip()
    method = method_for(plat)
    st = _state(client, target)
    name = ((st.get("attributes") or {}).get("friendly_name") or target)
    # An Apple TV's list is ALL apps — it has no inputs — so nothing is filtered (its "TV" is Apple's TV app, which
    # the input filter would drop). A TV's list mixes apps with HDMI/tuner inputs, which are never apps.
    keep_all = plat == "apple_tv"
    full = appctl._source_list(st)                                      # noqa: SLF001
    live = full if keep_all else appctl._source_list(st, apps_only=True)   # noqa: SLF001
    source = "published"
    if live:
        appctl._remember_list(target, full)                             # noqa: SLF001
    else:
        prev = appctl._remembered_list(target)                          # noqa: SLF001
        live = [x for x in prev if isinstance(x, str)
                and (keep_all or not appctl._INPUT_RE.match(x.strip()))]   # noqa: SLF001
        source = "remembered" if live else "none"
    apps, seen, unl = [], set(), []

    def put(word, token, slug=None, nm=None, extra=None):
        sl = slug or canonical_slug(word)
        if not sl or sl in seen:
            return
        seen.add(sl)
        row = {"app": sl, "name": nm or display_name(word), "word": word, "token": token,
               "tile": bool(appart.tile_path(sl))}
        row.update(extra or {})
        apps.append(row)

    if plat in ANDROID:
        source = "learned" if android_held else "none"
        for pkg, nm in sorted((android_held or {}).items(), key=lambda kv: str(kv[1]).lower()):
            c = appart.canonical(nm, pkg) or {}
            sl = c.get("slug") or appart.slug(nm)
            r = android_recipe(sl, package=pkg)
            # 918j: the app's proven link if ProOS has one (Foxtel, Netflix); otherwise offered as "manual" — it powers
            # the room and says to use the box's remote, and Pro shows that before it is added.
            put(nm, r["link"] if r else pkg, slug=sl, nm=c.get("name") or nm,
                extra={"method": "play_media_url" if r else "manual", "package": pkg, "opens": bool(r)})
        note = ("" if apps else
                "%s hasn't told ProOS any of its apps yet. Open an app on it with its remote, then press "
                "Learn from Device." % name)
    elif plat == "samsungtv_smart":
        if live:
            for w in live:
                tok = (REGISTRY.get(canonical_slug(w)) or {}).get("samsung")
                if tok:
                    put(w, tok)
                else:
                    unl.append(w)
        else:
            source = "library"
            for sl, row in REGISTRY.items():
                if row.get("samsung"):
                    put(sl, row["samsung"])
        note = ("" if live else "This TV doesn't publish its apps — these are the Samsung apps ProOS can open by id.")
    else:
        for w in live:
            put(w, w)                                       # select_source takes the device's own word, verbatim
        note = ("" if apps else
                "%s hasn't published its app list yet. Turn it on and its apps will appear here." % name)
    apps.sort(key=lambda a: (appart._canon_index().get(a["app"], (0, 0, 10 ** 6))[2], a["name"].lower()))   # noqa: SLF001
    return {"target": target, "name": name, "platform": plat, "method": method, "source": source,
            "apps": apps, "unlaunchable": unl, "note": note}


def offered(dapps: dict, app: str):
    """(entry, error). The device's own entry for this app, or why it can't be added. A device that has never
    published a list (an Apple TV not yet seen on) is not refused — it launches by name, as before."""
    for e in (dapps or {}).get("apps") or []:
        if e["app"] == app or _same(e["word"], app) or _same(e["name"], app) or e["app"] == canonical_slug(app):
            return (e, None)
    nm = display_name(app)
    dev = (dapps or {}).get("name") or "This device"
    if (dapps or {}).get("platform") in ANDROID:
        return (None, "%s doesn't have %s yet. Open %s on it with its remote, then press Learn from Device."
                % (dev, nm, nm))
    if (dapps or {}).get("source") in ("published", "remembered", "library"):
        return (None, "%s doesn't have %s." % (dev, nm))
    return (None, None)


# ── setup helpers (the Pro API leans on these) ───────────────────────────────
def platform_of(client, entity_id: str) -> str:
    """The integration platform behind a media_player, from the registry — the
    device's own truth, never taken from the caller."""
    try:
        for e in (client.entity_registry() or []):
            if e.get("entity_id") == entity_id:
                return e.get("platform") or ""
    except Exception:
        pass
    return ""


# ── brand support matrix (honest, shipped + shown in-product) ────────────────
# What each platform can actually do with a shortcut, and how sure we are. Gaps
# are stated, never hidden — an app a brand can't launch is documented, not
# silently missing (Dave's requirement, 5 Aug). status: proven | covered |
# limited | pending.
PLATFORM_SUPPORT = {
    "samsungtv_smart": {
        "brand": "Samsung (Tizen)", "method": "app id", "status": "proven",
        "note": "Launches any app by its Tizen id — including panels that won't "
                "list their apps (the 2020 Frame, proven on the box 5 Aug)."},
    "androidtv_remote": {
        "brand": "Android TV", "method": "package id", "status": "proven",
        "note": "Shield proven. Sony Bravia and other Android TVs use the same "
                "package ids (covered; validate per model)."},
    "androidtv": {
        "brand": "Android TV", "method": "package id", "status": "covered",
        "note": "Same package-id launch as the Android TV remote path."},
    "apple_tv": {
        "brand": "Apple TV", "method": "by name / deep link", "status": "limited",
        "note": "Opens an app by name where the unit lists it. Apple allows no "
                "universal id launch; deep links are best-effort per app and are "
                "validated when the shortcut is added."},
    "lg_webos": {
        "brand": "LG (webOS)", "method": "app id", "status": "pending",
        "note": "Architecture-ready; not yet validated on LG hardware."},
}
_STATUS_ORDER = {"proven": 0, "covered": 1, "limited": 2, "pending": 3}


def support_matrix() -> dict:
    """The brand-support matrix for the info tab + shipped docs: every platform
    with its launch method and how proven it is, plus the app catalogue (each app
    already carries the platforms it launches on)."""
    plats = [dict(platform=k, **v) for k, v in PLATFORM_SUPPORT.items()]
    plats.sort(key=lambda p: (_STATUS_ORDER.get(p["status"], 9), p["brand"]))
    return {"platforms": plats, "apps": catalogue()}


def from_request(client, body: dict) -> tuple:
    """Build a shortcut from a Pro 'add'/'test' request. Resolves the target's
    platform from the registry (so the launch method is the device's truth), then
    fills the canonical icon + default token, honouring any token/method override.
    Returns (shortcut, error)."""
    b = body or {}
    app = (b.get("app") or "").strip()
    target = (b.get("target") or "").strip()
    if not app or not target:
        return (None, "app and target are required")
    if not target.startswith("media_player."):
        return (None, "target must be a media_player")
    platform = (b.get("platform") or "").strip() or platform_of(client, target)
    sc = make_shortcut(app, target, platform,
                       token=(b.get("token") or None),
                       method=(b.get("method") or None))
    ok, why = validate(sc)
    if not ok:
        return (None, why)
    return (sc, None)
