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
}
# which REGISTRY column a platform reads for its default token.
_PLATFORM_COLUMN = {
    "samsungtv_smart":  "samsung",
    "androidtv_remote": "android",
    "androidtv":        "android",
}
METHODS = ("play_media_app", "select_source", "play_media_url")


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


def icon_url(app: str) -> str:
    """The ONE canonical tile for this app — same face on every device."""
    return "/apps/art/tile/%s.png" % canonical_slug(app)


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
    return {
        "app": slug,
        "name": display_name(app),
        "target": target,
        "platform": (platform or "").strip(),
        "method": method or method_for(platform),
        "token": token if token not in (None, "") else default_token(app, platform),
        "icon": "/apps/art/tile/%s.png" % slug,
    }


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
            if act:
                vs = (_state(client, "sensor.proos_activity_%s"
                             % (rec.get("area_id") or area_id)) or {}).get("state")
                fire, wait = appctl.launch_power_plan(dst.get("state"), vs, act.get("key"))
                if fire:
                    client.call_service("script", "turn_on", act["entity_id"])
                    time.sleep(wait)
            if target != disp:                              # route to the source's input
                inp = (rec.get("inputs") or {}).get(target)
                if inp:
                    client._req("POST", "/api/services/media_player/select_source",
                                {"entity_id": disp, "source": inp})
        except Exception:                                   # power/route is best-effort
            pass
    cmd = command_for(sc)
    try:
        client._req("POST", "/api/services/%s/%s" % (cmd["domain"], cmd["service"]),
                    cmd["data"])
    except Exception as e:                                  # noqa: BLE001
        return {"error": "couldn't launch %s: %s" % (sc.get("name") or sc.get("app"), e)}
    return {"ok": True, "app": sc.get("app"), "target": target,
            "method": sc["method"], "fired": cmd["data"]}


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
