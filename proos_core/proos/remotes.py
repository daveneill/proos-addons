"""THE ProOS REMOTE — a physical remote as a room controller (20 Oct 2026, register 969j).

Dave, 15:05: "I need our own built and integrated into ProOS as a room controller or multiple room controller similar
to the wall panel section … linking to our activities and devices". Rulings 15:20: our own app on the remote (the
Sanytron Astrion HA100A); rooms the same as wall panels (Only This Room / All Rooms). Mockups approved 18:04
("Love it can you build it all now"): docs/mockups/ProOS_Remote_Mockups_v2_2026-10-20.html.

WHAT THE PLATFORM DOES HERE: nothing — it has no idea of a remote, a room's activity, or which device "volume" moves.
So this module holds only what only ProOS knows:
  * the remotes this home has accepted, each with its home room, Only This Room / All Rooms, and a button map per room
    for the eight assignable keys (Light · Curtain · Music · AC · Red · Green · Blue · Yellow);
  * what every hard key means RIGHT NOW in a room, decided by the room's live activity (the D-pad drives the source
    that's on, Volume moves the room's committed volume device, Power is the room's own off activity);
  * the compact room picture the remote's screen draws.

SIGN-IN. A remote is a screen like the wall panel: it gets a real platform login of its own — a non-administrator,
local-only user per remote, its password random and kept only in Core's store (never logged, shown or returned) —
made the moment an installer ACCEPTS it in Pro. The remote has no camera, so there is no QR: it says hello, shows a
four-digit code, and the installer accepts the remote whose code matches. The token is handed to the remote once,
over the house network, to the app holding the secret it said hello with.

Every key press is re-decided HERE (POST /remote/key): the page never sends a service call of its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
PATH = os.path.join(DATA_DIR, "remotes.json")
LOGIN_PATH = os.path.join(DATA_DIR, "remote_logins.json")
_lock = threading.Lock()

PENDING_TTL = 30 * 60           # a hello waits half an hour for the installer (plumbing)
DELIVER_TTL = 10 * 60           # an accepted remote has ten minutes to collect its sign-in (plumbing)
ASSIGNABLE = ("light", "curtain", "music", "ac", "red", "green", "blue", "yellow")
HARD = ("power", "home", "back", "menu", "up", "down", "left", "right", "ok", "vol_up", "vol_down", "mute",
        "ch_up", "ch_down", "voice") + ASSIGNABLE

# The approved defaults (mockup §1). Installers change only the eight assignable keys, per room.
DEFAULT_MAP = {
    "light": {"press": {"open": "lights"}, "long": {"do": "lights_toggle"}},
    "curtain": {"press": {"open": "shades"}, "long": {"do": "covers_toggle"}},
    "music": {"press": {"open": "music"}, "long": {"do": "music_play_pause"}},
    "ac": {"press": {"open": "climate"}, "long": {"do": "climate_toggle"}},
    "red": {"press": None, "long": None}, "green": {"press": None, "long": None},
    "blue": {"press": None, "long": None}, "yellow": {"press": None, "long": None},
}
_deliver: dict = {}             # serial -> (nonce_hash, token, expires) — in memory only


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("remotes", {})
    d.setdefault("pending", {})
    return d


def _save(d: dict) -> None:
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, PATH)


def _clean_serial(s) -> str:
    s = "".join(ch for ch in str(s or "") if ch.isalnum() or ch in "-_")[:40]
    return s


# ── pairing ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def hello(serial: str, model: str, nonce: str, app: str = "") -> dict:
    """A remote asks to join, or asks whether it has been accepted. Public: it only ever creates a waiting entry
    (which an installer must accept) or hands a sign-in to the app holding the nonce it first said hello with."""
    serial = _clean_serial(serial)
    if not serial or not nonce or len(nonce) < 16:
        return {"error": "serial and nonce required"}
    nh = _h(nonce)
    with _lock:
        got = _deliver.get(serial)
        if got and got[0] == nh and got[2] > time.time():
            _deliver.pop(serial, None)
            return {"accepted": True, "token": got[1]}
        d = _load()
        now = time.time()
        d["pending"] = {k: v for k, v in d["pending"].items() if now - v.get("at", 0) < PENDING_TTL}
        p = d["pending"].get(serial)
        if not p or p.get("nonce") != nh:
            p = {"serial": serial, "model": str(model or "")[:24], "nonce": nh, "app": str(app or "")[:16],
                 "code": "%04d" % secrets.randbelow(10000), "at": now}
            d["pending"][serial] = p
            _save(d)
        return {"accepted": False, "code": p["code"], "paired_before": serial in d["remotes"]}


def accept(serial: str, ws_call, mint_llat) -> dict:
    """Installer accepts a waiting remote: make (or remake) its own platform login, mint a long-lived token for it,
    and hold the token for the remote to collect once."""
    serial = _clean_serial(serial)
    with _lock:
        d = _load()
        p = d["pending"].get(serial)
        if not p:
            return {"error": "no remote with that code is waiting"}
    username = "proos-remote-" + serial.lower()
    password = secrets.token_urlsafe(24)
    users = ws_call("config/auth/list") or []
    mine = next((u for u in users if u.get("username") == username), None)
    if mine is None:
        created = ws_call("config/auth/create", name="ProOS Remote " + serial[-4:], group_ids=["system-users"],
                          local_only=True) or {}
        user_id = (created.get("user") or created).get("id")
        ws_call("config/auth_provider/homeassistant/create", user_id=user_id, username=username, password=password)
    else:
        user_id = mine.get("id")
        ws_call("config/auth_provider/homeassistant/admin_change_password", user_id=user_id, password=password)
    _store_login(serial, user_id, username, password)
    token = mint_llat(username, password, "ProOS Remote %s" % serial[-4:])
    if not token:
        return {"error": "the home did not issue the remote a sign-in"}
    with _lock:
        d = _load()
        p = d["pending"].pop(serial, None) or {}
        r = d["remotes"].get(serial) or {"serial": serial, "name": "Remote %s" % serial[-4:], "home_room": None,
                                         "rooms": "home", "maps": {}}
        r.update({"model": p.get("model") or r.get("model") or "HA100", "user_id": user_id,
                  "paired_at": time.time(), "app": p.get("app") or r.get("app")})
        d["remotes"][serial] = r
        _save(d)
        _deliver[serial] = (p.get("nonce"), token, time.time() + DELIVER_TTL)
    return {"accepted": True, "serial": serial}


def _store_login(serial, user_id, username, password):
    try:
        with open(LOGIN_PATH, encoding="utf-8") as fh:
            k = json.load(fh)
    except Exception:  # noqa: BLE001
        k = {}
    k[serial] = {"user_id": user_id, "username": username, "password": password}
    tmp = LOGIN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(k, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, LOGIN_PATH)


def forget(serial: str, ws_call) -> dict:
    serial = _clean_serial(serial)
    with _lock:
        d = _load()
        r = d["remotes"].pop(serial, None)
        d["pending"].pop(serial, None)
        _save(d)
        _deliver.pop(serial, None)
    if r and r.get("user_id"):
        try:
            ws_call("config/auth/delete", user_id=r["user_id"])
        except Exception:  # noqa: BLE001
            pass
    return {"forgotten": bool(r)}


# ── settings ────────────────────────────────────────────────────────────────────────────────────────────────────────
def summary() -> dict:
    d = _load()
    now = time.time()
    rem = []
    for r in d["remotes"].values():
        x = {k: v for k, v in r.items() if k not in ("user_id",)}
        x["online"] = bool(r.get("last_seen") and now - r["last_seen"] < 120)
        rem.append(x)
    pend = [{"serial": p["serial"], "model": p.get("model"), "code": p.get("code"), "at": p.get("at")}
            for p in d["pending"].values() if now - p.get("at", 0) < PENDING_TTL]
    return {"remotes": sorted(rem, key=lambda r: r.get("name") or ""), "waiting": pend}


def set_settings(serial: str, body: dict) -> dict:
    serial = _clean_serial(serial)
    with _lock:
        d = _load()
        r = d["remotes"].get(serial)
        if not r:
            return {"error": "no such remote"}
        if "name" in body:
            r["name"] = str(body["name"] or "").strip()[:40] or r["name"]
        if "home_room" in body:
            r["home_room"] = body["home_room"] or None
        if body.get("rooms") in ("home", "all"):
            r["rooms"] = body["rooms"]
        if "map" in body and isinstance(body["map"], dict):
            area, key, which = body["map"].get("area"), body["map"].get("key"), body["map"].get("which")
            if area and key in ASSIGNABLE and which in ("press", "long"):
                slot = r.setdefault("maps", {}).setdefault(area, {}).setdefault(key, {})
                if body["map"].get("action") == "default":
                    slot.pop(which, None)                     # back to the approved default for this key
                else:
                    slot[which] = _clean_action(body["map"].get("action"))   # None = this key does nothing here
        if body.get("reset_map"):
            r.setdefault("maps", {}).pop(body["reset_map"], None)
        _save(d)
        return {"saved": True, "remote": r}


def _clean_action(a):
    """An assignable key may open a page, run a scene or script, start an activity, or do a room-wide toggle —
    nothing else (never a raw service call)."""
    if not isinstance(a, dict):
        return None
    if a.get("open") in ("lights", "shades", "music", "climate", "rooms", "home", "activities"):
        return {"open": a["open"]}
    if a.get("do") in ("lights_toggle", "covers_toggle", "music_play_pause", "climate_toggle"):
        return {"do": a["do"]}
    for k, pre in (("scene", "scene."), ("script", "script.")):
        if isinstance(a.get(k), str) and a[k].startswith(pre):
            return {k: a[k], "label": str(a.get("label") or "")[:40]}
    if isinstance(a.get("activity"), str):
        return {"activity": a["activity"][:80], "label": str(a.get("label") or "")[:40]}
    return None


def status(user_id: str, body: dict) -> dict | None:
    """The remote checks in: battery, dock, Wi-Fi, app version, and (now and then) a picture of its screen."""
    with _lock:
        d = _load()
        r = next((x for x in d["remotes"].values() if x.get("user_id") == user_id), None)
        if not r:
            return None
        r["last_seen"] = time.time()
        for k in ("battery", "docked", "rssi", "app", "room"):
            if k in body:
                r[k] = body[k]
        if isinstance(body.get("shot"), str) and body["shot"].startswith("data:image/") and len(body["shot"]) < 400000:
            r["shot"] = body["shot"]
            r["shot_at"] = time.time()
        _save(d)
        return r


def by_user(user_id: str) -> dict | None:
    d = _load()
    return next((x for x in d["remotes"].values() if x.get("user_id") == user_id), None)


def map_for(remote: dict, area: str) -> dict:
    m = {k: dict(v) for k, v in DEFAULT_MAP.items()}
    for k, v in ((remote or {}).get("maps") or {}).get(area, {}).items():
        if k in m:
            m[k].update(v)
    return m


def useful_rooms(areas: list, entity_registry: list, device_registry: list) -> list:
    """The rooms a remote can do something in: any light, shade, climate or player assigned there (by the platform's
    own room assignment). A room that holds only network gear or nothing at all is not offered."""
    from proos.membership import area_of
    dev_area = {d.get("id"): d.get("area_id") for d in device_registry or []}
    has = set()
    for e in entity_registry or []:
        if e.get("disabled_by") or e.get("hidden_by"):
            continue
        if str(e.get("entity_id", "")).split(".", 1)[0] in ("light", "cover", "climate", "media_player", "fan"):
            a = area_of(e, dev_area)
            if a:
                has.add(a)
    return [a for a in areas if a in has]


def allowed_rooms(remote: dict, areas: list) -> list:
    """Only This Room → the home room; All Rooms → every room in the house (the wall panel's own rule)."""
    if (remote or {}).get("rooms") == "all":
        return list(areas)
    hr = (remote or {}).get("home_room")
    return [hr] if hr else []


# ── what each key means, now ────────────────────────────────────────────────────────────────────────────────────────
def resolve(key: str, long: bool, ctx: dict, keyplan) -> dict | None:
    """One hard key in one room right now. ctx: {area, activity:{key,kind,source_eid,display}|None, vol_eid,
    music_eid, map, rooms_mode}. keyplan(logical_key, entity) -> platform call or None (remotekeys.plan).
    Returns {open|nav} for the screen, {call:{domain,service,data}} or {do}/{fire} for Core, or None (nothing)."""
    act = ctx.get("activity") or None
    watching = bool(act and act.get("key") not in (None, "", "off", "tv_off"))
    target = ((act or {}).get("source_eid") or (act or {}).get("display")) if watching else None
    if key == "power":
        if long:
            return {"do": "room_all_off", "label": "All Off"}
        return {"fire": "off", "label": "Room Off"} if watching else {"open": "activities"}
    if key == "home":
        return {"open": "home"}
    if key == "voice":
        return {"open": "voice"}
    if key in ("up", "down", "left", "right", "ok", "back", "menu"):
        logical = "ok" if key == "ok" else key
        if watching and target:
            p = keyplan(logical, target)
            if p:
                return {"call": p, "label": target}
        return {"nav": key}
    if key in ("ch_up", "ch_down"):
        if watching and target:
            p = keyplan(key, target)
            if p:
                return {"call": p}
        if ctx.get("rooms_mode") == "all":
            return {"nav": "room_next" if key == "ch_down" else "room_prev"}
        return None
    if key in ("vol_up", "vol_down", "mute"):
        v = ctx.get("vol_eid") if watching else (ctx.get("music_eid") or ctx.get("vol_eid"))
        if not v:
            return None
        svc = {"vol_up": "volume_up", "vol_down": "volume_down", "mute": "volume_mute"}[key]
        data = {"entity_id": v}
        if key == "mute":
            data["is_volume_muted"] = not bool(ctx.get("vol_muted"))
        return {"call": {"domain": "media_player", "service": svc, "data": data}, "hud": "volume", "vol_eid": v}
    if key in ASSIGNABLE:
        m = (ctx.get("map") or {}).get(key) or {}
        a = m.get("long" if long else "press")
        if a:
            return dict(a)
        if key in ("red", "green", "blue", "yellow") and not long and watching and target:
            p = keyplan(key, target)
            if p:
                return {"call": p}
        return None
    return None


def legend(ctx: dict, keyplan) -> dict:
    """For the screen: what each hard key does now, short and long — so the legend and the page's own navigation
    come from the same answer Core will act on."""
    out = {}
    for k in HARD:
        out[k] = {"press": _describe(resolve(k, False, ctx, keyplan)), "long": _describe(resolve(k, True, ctx, keyplan))}
    return out


def _describe(a):
    if not a:
        return None
    if "open" in a or "nav" in a:
        return {k: a[k] for k in ("open", "nav") if k in a}
    if "call" in a:
        return {"core": True, "hud": a.get("hud")}
    return {"core": True, "label": a.get("label") or a.get("do") or a.get("fire") or a.get("scene") or a.get("script")
            or a.get("activity")}


def mint_llat(username: str, password: str, client_name: str) -> str | None:
    """The platform's own sign-in for a remote's login, then its own long-lived token — the same road Pro's installer
    sign-in takes (proauth.login), without the installer-only check (a remote is a screen, not an installer)."""
    from proos import proauth
    from proos.ha_ws import ws_command
    last = None
    for base, sv in proauth._bases():
        cid = base.rstrip("/") + "/"
        try:
            flow = proauth._http(base, "/auth/login_flow", {"client_id": cid, "handler": ["homeassistant", None],
                                                            "redirect_uri": cid}, token=sv)
            step = proauth._http(base, "/auth/login_flow/" + flow["flow_id"], {
                "client_id": cid, "username": username, "password": password}, token=sv)
            if step.get("type") != "create_entry":
                raise RuntimeError("login flow did not complete")
            tokens = proauth._http(base, "/auth/token", {"grant_type": "authorization_code", "code": step["result"],
                                                         "client_id": cid}, token=sv, form=True)
            llt = ws_command(base, tokens["access_token"], "auth/long_lived_access_token",
                             client_name=client_name, lifespan=3650)
            if isinstance(llt, str) and llt:
                return llt
        except Exception as exc:  # noqa: BLE001 - transport: try the next base
            last = exc
            continue
    raise RuntimeError("could not sign the remote in: %s" % last)


# ── the room, as the remote's screen draws it ──────────────────────────────────────────────────────────────────────
def _ent(x):
    if isinstance(x, dict):
        return x.get("entity") or x.get("entity_id")
    return x


def _first(lst):
    for x in lst or []:
        e = _ent(x)
        if e:
            return e
    return None


def _pct(v, top=255.0):
    try:
        return int(round(float(v) / top * 100))
    except Exception:  # noqa: BLE001
        return None


def _match_activity(acts, cur_key, label):
    """The room's running activity among its stored ones. JOIN BY IDENTITY, NEVER BY NAME FIRST (project.py's own
    rule): the activity sensor names a watch activity by its source's entity (watch_<area>_<source object id>) while
    the stored script is named from the device label (watch_apple_tv) — so match the key, then the source's own id,
    and only then the label."""
    if not cur_key or cur_key in ("off", "unknown", "unavailable"):
        return next((a for a in acts if a.get("key") == cur_key), None) if cur_key == "off" else None
    for a in acts:
        if a.get("key") == cur_key:
            return a
    for a in acts:
        se = a.get("source_eid") or ""
        if se and cur_key.endswith(se.split(".", 1)[-1]):
            return a
    if label:
        for a in acts:
            if (a.get("label") or "").strip().lower() == str(label).strip().lower():
                return a
    return None


def room_state(area: str, remote: dict, client, project_mod, keyplan, keycaps) -> dict:
    """Everything one remote screen needs about one room, in one small answer — read from the platform's own state
    (Core's live cache) and ProOS's committed room. Nothing here is guessed: a value the platform doesn't report is
    left out, and the screen leaves its tile out with it."""
    areas = client.area_registry() or []
    names = {a.get("area_id"): a.get("name") or a.get("area_id") for a in areas}
    pics = {a.get("area_id"): bool(a.get("picture")) for a in areas}
    ents = client.entity_registry() or []
    devs = client.device_registry() or []
    dev_area = {d.get("id"): d.get("area_id") for d in devs}
    from proos.membership import area_of
    mine = [e for e in ents if area_of(e, dev_area) == area and not e.get("disabled_by") and not e.get("hidden_by")]
    by_dom = {}
    for e in mine:
        by_dom.setdefault(e["entity_id"].split(".", 1)[0], []).append(e["entity_id"])
    project = project_mod.load() if project_mod else {}
    rec = (project_mod._resolve_rec(project, area) if project_mod else None) or {}
    try:
        acts = (project_mod.activities_status(client, project, area) or {}).get("activities", []) if project_mod else []
    except Exception:  # noqa: BLE001
        acts = []
    vol_v = _first(rec.get("video_volume")) or _ent(rec.get("tvaudio")) or _ent(rec.get("display"))
    vol_a = _first(rec.get("audio_volume")) or _first(rec.get("speakers"))
    sens = "sensor.proos_activity_%s" % area
    want = [sens] + [x for d in ("light", "cover", "climate", "media_player", "fan") for x in by_dom.get(d, [])]
    want += [x for x in (vol_v, vol_a) if x]
    want += [a.get("source_eid") for a in acts if a.get("source_eid")]
    snap = client.snapshot(list(dict.fromkeys(want))) or {}
    s_act = snap.get(sens) or {}
    cur_key = (s_act.get("attributes") or {}).get("activity_key") or s_act.get("state")
    cur = _match_activity(acts, cur_key, (s_act.get("attributes") or {}).get("label"))
    disp = _ent(rec.get("display"))
    act_ctx = None
    if cur and cur.get("key") not in ("off", "tv_off") and cur_key != "off":
        act_ctx = {"key": cur.get("key"), "kind": cur.get("kind"), "label": cur.get("label"),
                   "source_eid": cur.get("source_eid"), "display": disp}
    elif cur_key and cur_key not in ("off", "tv_off", "unknown", "unavailable", "") and s_act:
        act_ctx = {"key": cur_key, "kind": (s_act.get("attributes") or {}).get("kind"),
                   "label": (s_act.get("attributes") or {}).get("label"), "source_eid": None,
                   "display": disp if (s_act.get("attributes") or {}).get("kind") != "music" else None}
    music_eid = (s_act.get("attributes") or {}).get("audio_entity") or vol_a
    vs = snap.get(vol_v if act_ctx and act_ctx.get("kind") != "music" else (music_eid or vol_v)) or {}
    ctx = {"area": area, "activity": act_ctx, "vol_eid": vol_v, "music_eid": music_eid,
           "vol_muted": bool((vs.get("attributes") or {}).get("is_volume_muted")),
           "map": map_for(remote, area), "rooms_mode": (remote or {}).get("rooms") or "home"}

    def nm(eid):
        st = snap.get(eid) or {}
        return (st.get("attributes") or {}).get("friendly_name") or eid

    plat = {e.get("entity_id"): e.get("platform") for e in ents}
    lights = []
    for eid in by_dom.get("light", []):
        st = snap.get(eid) or {}
        at = st.get("attributes") or {}
        modes = at.get("supported_color_modes") or []
        kind = "switch" if modes in (["onoff"], []) else ("dimmer" if set(modes) <= {"brightness", "onoff"} else "colour")
        lights.append({"entity": eid, "name": nm(eid), "on": st.get("state") == "on", "kind": kind,
                       "brightness": _pct(at.get("brightness")) if st.get("state") == "on" else None,
                       "rgb": at.get("rgb_color"), "kelvin": at.get("color_temp_kelvin"),
                       "since": st.get("last_changed"), "available": st.get("state") not in ("unavailable", None)})
    covers = []
    for eid in by_dom.get("cover", []):
        st = snap.get(eid) or {}
        at = st.get("attributes") or {}
        covers.append({"entity": eid, "name": nm(eid), "state": st.get("state"),
                       "position": at.get("current_position"), "since": st.get("last_changed")})
    climate = None
    for eid in by_dom.get("climate", [])[:1]:
        st = snap.get(eid) or {}
        at = st.get("attributes") or {}
        climate = {"entity": eid, "name": nm(eid), "mode": st.get("state"), "modes": at.get("hvac_modes") or [],
                   "target": at.get("temperature"), "current": at.get("current_temperature"),
                   "humidity": at.get("current_humidity"), "action": at.get("hvac_action"),
                   "fan": at.get("fan_mode"), "fans": at.get("fan_modes") or [],
                   "min": at.get("min_temp"), "max": at.get("max_temp"), "step": at.get("target_temp_step") or 0.5}
    music = None
    if music_eid:
        st = snap.get(music_eid) or client.snapshot([music_eid]).get(music_eid) or {}
        at = st.get("attributes") or {}
        music = {"entity": music_eid, "name": nm(music_eid), "state": st.get("state"),
                 "title": at.get("media_title"), "artist": at.get("media_artist"), "album": at.get("media_album_name"),
                 "app": at.get("app_name") or at.get("source"), "volume": _pct(at.get("volume_level"), 1.0),
                 "muted": bool(at.get("is_volume_muted")), "art": bool(at.get("entity_picture")),
                 "position": at.get("media_position"), "duration": at.get("media_duration"),
                 "updated": at.get("media_position_updated_at"), "platform": plat.get(music_eid)}
    watching = None
    if act_ctx and act_ctx.get("kind") != "music":
        src = act_ctx.get("source_eid") or disp
        sst = snap.get(src) or (client.snapshot([src]).get(src) if src else {}) or {}
        sat = sst.get("attributes") or {}
        try:
            feats = int(sat.get("supported_features") or 0)
        except Exception:  # noqa: BLE001
            feats = 0
        watching = {"label": act_ctx.get("label"), "source": src, "source_name": nm(src) if src else None,
                    "platform": plat.get(src), "title": sat.get("media_title"), "series": sat.get("media_series_title"),
                    "app": sat.get("app_name"), "art": bool(sat.get("entity_picture")),
                    "position": sat.get("media_position"), "duration": sat.get("media_duration"),
                    "updated": sat.get("media_position_updated_at"),
                    "caps": keycaps(src, feats) if src else {},
                    "vol_entity": vol_v, "vol_name": nm(vol_v) if vol_v else None,
                    "volume": _pct((vs.get("attributes") or {}).get("volume_level"), 1.0),
                    "muted": bool((vs.get("attributes") or {}).get("is_volume_muted"))}
    acts_out = [{"key": a.get("key"), "label": a.get("label"), "kind": a.get("kind"),
                 "source": a.get("source_eid"), "platform": plat.get(a.get("source_eid")),
                 "on": bool(cur) and a.get("key") == cur.get("key")} for a in acts]
    scenes = [{"entity": eid, "name": nm(eid)} for eid in by_dom.get("scene", [])]
    return {"area": area, "name": names.get(area, area), "photo": pics.get(area, False),
            "activity": act_ctx, "activities": acts_out, "lights": lights, "covers": covers, "climate": climate,
            "music": music, "watching": watching, "scenes": scenes,
            "keys": legend(ctx, keyplan), "at": time.time()}


def key_context(area: str, remote: dict, client, project_mod) -> dict:
    """The same context room_state builds, for a key press (re-decided on Core's side every time)."""
    st = room_state(area, remote, client, project_mod, lambda k, e: None, lambda e, f: {})
    w = st.get("watching") or {}
    m = st.get("music") or {}
    act = st.get("activity")
    return {"area": area, "activity": act,
            "vol_eid": w.get("vol_entity") or (m.get("entity") if not act else None),
            "music_eid": m.get("entity"),
            "vol_muted": bool(w.get("muted")) if w else bool(m.get("muted")),
            "map": map_for(remote, area), "rooms_mode": (remote or {}).get("rooms") or "home"}
