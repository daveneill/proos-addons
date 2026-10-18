"""ProOS Core — the wall panel sets itself up (register 940).

Dave, 18 Oct 2026: a G-Flip flashed with the ProOS image must "install and configure everything as if the mic and
touchscreen are fully working … nothing from home assistant can be seen". Everything done by hand on the G-Flip master
on 17–18 Oct (registers 927–939) is done here, by Core, on a box whose add-on option `panel` is on — the image's first
boot turns it on; a panel is SAID, never guessed.

What it converges (every step idempotent — a right box gets no writes; each step reports, none blocks the next):
  1. the two add-on stores the panel's own add-ons come from (the kiosk, the room microphone)
  2. the add-ons: the kiosk screen, the room microphone, speech-to-text, the local voice, Matter — installed, started
     on boot, with the options the master runs (read on the box 18 Oct)
  3. the platform's own discovered integrations for them, confirmed (Whisper, Piper, the panel's microphone)
  4. the voice pipeline: Whisper → ProOS Assist → Piper in the Northern English voice (register 933), the preferred one
  5. the microphone's input level (register 936c: 50 % measured better than 25 %)
  6. a login for the screen the box makes itself — a random password kept only in Core's own store, never shown —
     and the kiosk signed in with it, so a restart never asks anyone to sign in (register 939a)
  7. the kiosk: the dashboard as a panel, 2.4× (register 937), ProOS's own keyboard (the kiosk's off, register 939),
     no sidebar; restarted only when something changed
The platform restart the door needs on a fresh box (register 928) is asked for once per door build, nowhere else.
"""
from __future__ import annotations

import json
import os
import secrets
import time

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "panel_setup.json")
LOGIN_PATH = os.path.join(DATA_DIR, "panel_login.json")

REPOS = {
    "kiosk": "https://github.com/puterboy/HAOS-kiosk",
    "voice": "https://github.com/OHF-Voice/apps",
}
PANEL_USERNAME = "proos-panel"
PANEL_NAME = "ProOS Panel Screen"
VOICE = "en_GB-northern_english_male-medium"      # register 933 — Dave's choice, 18 Oct
VOICE_LANG = "en_GB"
WAKE_WORD = "hey_jarvis"                           # register 933 — "make it Jarvis to start with"
INPUT_LEVEL = 0.5                                  # register 936c — measured on the G-Flip
KIOSK_ZOOM = 240                                   # register 937 — 800 × 450 on the 5" glass
PIPELINE_NAME = "ProOS"


def _merge(have: dict, want: dict) -> tuple[dict, bool]:
    out = dict(have or {})
    changed = False
    for k, v in want.items():
        if out.get(k) != v:
            out[k] = v
            changed = True
    return out, changed


def _repo_slug(sup, url: str):
    for r in (sup("GET", "/store/repositories") or []):
        if isinstance(r, dict) and str(r.get("source", "")).rstrip("/") == url.rstrip("/"):
            return r.get("slug")
    return None


def ensure_repos(sup) -> dict:
    out = {}
    for key, url in REPOS.items():
        slug = _repo_slug(sup, url)
        if not slug:
            sup("POST", "/store/repositories", {"repository": url})
            slug = _repo_slug(sup, url)
            out[key] = {"added": slug}
        else:
            out[key] = {"current": slug}
    return out


def addon_slugs(sup) -> dict:
    k = _repo_slug(sup, REPOS["kiosk"])
    v = _repo_slug(sup, REPOS["voice"])
    return {"kiosk": k and k + "_haoskiosk", "satellite": v and v + "_assist_satellite",
            "whisper": "core_whisper", "piper": "core_piper", "matter": "core_matter_server"}


def ensure_addon(sup, slug: str, want_options: dict | None = None, start: bool = True) -> dict:
    """Installed, on boot, with these options, running. Returns what it did; restarts only when options changed."""
    out = {"slug": slug}
    info = None
    try:
        info = sup("GET", "/addons/%s/info" % slug) or {}
    except Exception:  # noqa: BLE001 — not installed answers 400
        info = {}
    if not info.get("version"):
        sup("POST", "/store/addons/%s/install" % slug, None, 1800)
        out["installed"] = True
        info = sup("GET", "/addons/%s/info" % slug) or {}
    changed = False
    if want_options:
        merged, changed = _merge(info.get("options") or {}, want_options)
        if changed:
            sup("POST", "/addons/%s/options" % slug, {"options": merged})
            out["options"] = sorted(k for k in want_options if (info.get("options") or {}).get(k) != want_options[k])
    if info.get("boot") != "auto":
        sup("POST", "/addons/%s/options" % slug, {"boot": "auto"})
        out["boot"] = "auto"
    if start:
        state = (sup("GET", "/addons/%s/info" % slug) or {}).get("state")
        if state != "started":
            sup("POST", "/addons/%s/start" % slug, None, 300)
            out["started"] = True
        elif changed:
            # the kiosk crashes on its own restart (register 927e): stop, then start
            sup("POST", "/addons/%s/stop" % slug, None, 120)
            sup("POST", "/addons/%s/start" % slug, None, 300)
            out["restarted"] = True
    return out


def confirm_discovered(req, ws) -> dict:
    """The platform's own discovered integrations for the panel's add-ons, confirmed (no human click)."""
    out = {"confirmed": [], "errors": []}
    try:
        flows = ws("config_entries/flow/progress") or []
    except Exception:  # noqa: BLE001
        flows = []
    for f in flows if isinstance(flows, list) else []:
        if f.get("handler") not in ("wyoming", "esphome", "matter"):
            continue
        try:
            r = req("POST", "/api/config/config_entries/flow/%s" % f.get("flow_id"), {}) or {}
            out["confirmed"].append("%s:%s" % (f.get("handler"), r.get("type")))
        except Exception as e:  # noqa: BLE001
            out["errors"].append("%s: %s" % (f.get("handler"), e))
    return out


def ensure_satellite_entry(req, ws) -> dict:
    """The panel's microphone as the platform's ESPHome device at 127.0.0.1:6053 — when discovery has not offered it."""
    try:
        entries = ws("config_entries/get", domain="esphome") or []
    except Exception:  # noqa: BLE001
        entries = []
    if entries:
        return {"current": True}
    r = req("POST", "/api/config/config_entries/flow", {"handler": "esphome", "show_advanced_options": False}) or {}
    if r.get("type") == "form" and r.get("flow_id"):
        r = req("POST", "/api/config/config_entries/flow/%s" % r["flow_id"], {"host": "127.0.0.1", "port": 6053}) or {}
    return {"created": r.get("type")}


def ensure_pipeline(ws) -> dict:
    """Whisper → ProOS Assist → Piper (Northern English), named ProOS, the preferred pipeline."""
    lst = ws("assist_pipeline/pipeline/list") or {}
    pipes = lst.get("pipelines") or []
    want = {"name": PIPELINE_NAME, "language": "en", "conversation_engine": "conversation.proos_assist",
            "conversation_language": "en", "stt_engine": "stt.faster_whisper", "stt_language": "en",
            "tts_engine": "tts.piper", "tts_language": VOICE_LANG, "tts_voice": VOICE,
            "wake_word_entity": None, "wake_word_id": None}
    mine = next((p for p in pipes if p.get("name") == PIPELINE_NAME), None)
    out = {}
    if mine is None:
        # the platform's own fields as its default pipeline carries them, with ours over the top
        body = {k: v for k, v in (pipes[0] if pipes else {}).items() if k != "id"}
        body.update(want)
        mine = ws("assist_pipeline/pipeline/create", **body) or {}
        out["created"] = mine.get("id")
    else:
        if any(mine.get(k) != v for k, v in want.items()):
            body = {k: v for k, v in mine.items() if k != "id"}
            body.update(want)
            ws("assist_pipeline/pipeline/update", pipeline_id=mine.get("id"), **body)
            out["updated"] = mine.get("id")
        else:
            out["current"] = mine.get("id")
    if mine.get("id") and lst.get("preferred_pipeline") != mine.get("id"):
        ws("assist_pipeline/pipeline/set_preferred", pipeline_id=mine.get("id"))
        out["preferred"] = True
    return out


def ensure_input_level(sup) -> dict:
    info = (sup("GET", "/audio/info") or {}).get("audio") or {}
    dev = next((i for i in (info.get("input") or []) if i.get("default")), None)
    if not dev:
        return {"why": "no microphone input on the box"}
    if abs(float(dev.get("volume") or 0) - INPUT_LEVEL) < 0.01:
        return {"current": INPUT_LEVEL}
    sup("POST", "/audio/volume/input", {"index": dev.get("index"), "volume": INPUT_LEVEL})
    return {"set": INPUT_LEVEL, "was": dev.get("volume")}


def _read_login():
    try:
        with open(LOGIN_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def ensure_panel_login(ws) -> dict:
    """A login for the screen, made by the box: a non-administrator, local-only, its password random and kept only in
    Core's own store (never logged, never shown, never returned)."""
    kept = _read_login()
    users = ws("config/auth/list") or []
    mine = next((u for u in users if u.get("username") == PANEL_USERNAME), None)
    if mine and kept.get("user_id") == mine.get("id") and kept.get("password"):
        return {"current": True, "user_id": mine.get("id")}
    password = secrets.token_urlsafe(24)
    if mine is None:
        created = ws("config/auth/create", name=PANEL_NAME, group_ids=["system-users"], local_only=True) or {}
        user_id = (created.get("user") or created).get("id")
        ws("config/auth_provider/homeassistant/create", user_id=user_id, username=PANEL_USERNAME, password=password)
    else:
        user_id = mine.get("id")
        ws("config/auth_provider/homeassistant/admin_change_password", user_id=user_id, password=password)
    tmp = LOGIN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"user_id": user_id, "username": PANEL_USERNAME, "password": password}, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, LOGIN_PATH)
    return {"made": True, "user_id": user_id}


def platform_port(sup) -> int:
    """The platform's own port, asked of the Supervisor (/core/info) — 80 on a new box, 8123 on an older one, and 8123
    on a ProOS image box, where the platform is kept inside the box and ProOS answers the box's address (register 945)."""
    try:
        return int((sup("GET", "/core/info") or {}).get("port") or 80)
    except Exception:  # noqa: BLE001
        return 80


# THE KIOSK'S OWN FINGER GESTURES (register 954). Dave, 19 Oct 06:37, photo: the G-Flip showing google.com — "somehow it
# ended up in google". The kiosk ships with gestures that leave ProOS: two fingers tapped four times opens Google, three
# fingers swipe back/forward through the browser's history, three-finger taps refresh, mute or toggle its keyboard, and
# a tap in the top-right corner toggles the keyboard. A wall panel keeps only the two that cannot leave: two fingers
# tapped three times brings back ProOS's own page, and three fingers tapped four times saves a screenshot.
KIOSK_GESTURES = [
    '"2_TOUCH_3_TAP": {"cmds": "kiosk.launch_url", "msg": "Restore the ProOS panel page"}',
    '"3_TOUCH_4_TAP": {"cmds": "kiosk.screenshot", "msg": "Save screenshot"}',
]


# A REAL PICTURE OF THE SCREEN (register 963). Dave, 19 Oct 10:16–10:31: Pro shows "a real picture of the screen that just
# updates when you navigate to the page", and chose (option 1) the kiosk's own command door opened on the box's INTERNAL
# network only, locked with a key Core makes and never shows. The kiosk's REST server listens on 127.0.0.1 by default —
# inside the kiosk alone; Core is in its own compartment. The kiosk runs on the host's network, so it can listen on the
# box's internal address (the gateway Core's own compartment routes through — READ from Core's routing table, never
# assumed); that address is not on the home's network. Every request then needs the key (the kiosk's rest_bearer_token).
ROUTE_PATH = "/proc/net/route"
KIOSK_REST_PATH = os.path.join(DATA_DIR, "kiosk_rest.json")
KIOSK_REST_PORT = 8080


def internal_ip(route_path: str | None = None) -> str:
    """The box's internal address as Core's compartment sees it: its default gateway, read from the routing table.
    "" when it cannot be read — then nothing about the kiosk's door is changed."""
    try:
        with open(route_path or ROUTE_PATH, encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) > 3 and f[1] == "00000000" and int(f[3], 16) & 2:
                    g = int(f[2], 16)
                    ip = ".".join(str((g >> (8 * i)) & 0xFF) for i in range(4))
                    return ip if ip != "0.0.0.0" else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def kiosk_rest() -> dict:
    """The kiosk door Core uses: {ip, port, token}. The key is made once, kept only in Core's own store (never logged,
    never shown, never returned by any route). {} when the internal address cannot be read."""
    ip = internal_ip()
    if not ip:
        return {}
    try:
        with open(KIOSK_REST_PATH, encoding="utf-8") as fh:
            kept = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        kept = {}
    if not kept.get("token"):
        kept = {"token": secrets.token_urlsafe(32)}
        tmp = KIOSK_REST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kept, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, KIOSK_REST_PATH)
    return {"ip": ip, "port": KIOSK_REST_PORT, "token": kept["token"]}


def kiosk_options(sat_entity: str | None, port: int = 80) -> dict:
    login = _read_login()
    dash = "local/dashboard.html?panel=1" + (("&sat=" + sat_entity) if sat_entity else "")
    url = "http://127.0.0.1" + ("" if int(port or 80) == 80 else ":%d" % int(port))
    out = {"ha_url": url, "ha_dashboard": dash, "zoom_level": KIOSK_ZOOM,
           "onscreen_keyboard": False, "ha_sidebar": "none", "dark_mode": True, "screen_timeout": 0,
           "ha_username": login.get("username") or PANEL_USERNAME, "ha_password": login.get("password") or "",
           "gestures": list(KIOSK_GESTURES)}
    door = kiosk_rest()
    if door:
        out.update({"rest_ip": door["ip"], "rest_port": door["port"], "rest_bearer_token": door["token"]})
    return out


def satellite_entity(ws):
    try:
        ents = ws("config/entity_registry/list") or []
    except Exception:  # noqa: BLE001
        return None
    return next((e.get("entity_id") for e in ents
                 if str(e.get("entity_id", "")).startswith("assist_satellite.") and e.get("platform") == "esphome"), None)


def run(sup, ws, req, door_state=None, restart_core=None) -> dict:
    """One pass. Each step is tried; a failure is written down and the next step still runs."""
    st = {"at": time.time(), "steps": {}, "done": False}

    def step(name, fn):
        try:
            st["steps"][name] = fn()
        except Exception as e:  # noqa: BLE001
            st["steps"][name] = {"error": str(e)[:300]}

    step("repos", lambda: ensure_repos(sup))
    slugs = {}
    step("slugs", lambda: slugs.update(addon_slugs(sup)) or dict(slugs))
    step("whisper", lambda: ensure_addon(sup, "core_whisper", {"language": "en"}))
    step("piper", lambda: ensure_addon(sup, "core_piper", {"voice": VOICE}))
    step("matter", lambda: ensure_addon(sup, "core_matter_server"))
    if slugs.get("satellite"):
        step("satellite", lambda: ensure_addon(sup, slugs["satellite"], {
            "name": "ProOS Panel", "wake_word": WAKE_WORD, "mic_auto_gain": 31, "mic_noise_suppression": 2}))
    step("input_level", lambda: ensure_input_level(sup))
    step("discovered", lambda: confirm_discovered(req, ws))
    step("satellite_entry", lambda: ensure_satellite_entry(req, ws))
    # the door's assistant must be loaded before the pipeline can name it: a fresh box asks the platform to restart
    # ONCE per door build (register 928), never again
    ds = None
    if door_state is not None:
        try:
            ds = door_state()
        except Exception as e:  # noqa: BLE001
            ds = {"state": "error", "reason": str(e)}
        st["steps"]["door"] = ds
        if ds and ds.get("state") == "restart" and restart_core is not None:
            prev = _load_state().get("restarted_for")
            if prev != ds.get("build"):
                st["restarted_for"] = ds.get("build")
                _save_state(dict(_load_state(), restarted_for=ds.get("build")))
                step("platform_restart", lambda: restart_core() or {"asked": True})
                st["done"] = False
                _save_state(st)
                return st
    step("pipeline", lambda: ensure_pipeline(ws))
    step("panel_login", lambda: ensure_panel_login(ws))
    sat = None
    try:
        sat = satellite_entity(ws)
    except Exception:  # noqa: BLE001
        sat = None
    if slugs.get("kiosk"):
        step("kiosk", lambda: ensure_addon(sup, slugs["kiosk"], kiosk_options(sat, platform_port(sup))))
    errs = [k for k, v in st["steps"].items() if isinstance(v, dict) and v.get("error")]
    st["errors"] = errs
    st["done"] = not errs and bool(sat) and (ds is None or ds.get("state") == "open")
    prev = _load_state()
    if prev.get("restarted_for"):
        st["restarted_for"] = prev["restarted_for"]
    _save_state(st)
    return st


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st: dict):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    s = _load_state()
    return {"done": bool(s.get("done")), "at": s.get("at"), "errors": s.get("errors") or [],
            "steps": {k: (("error: " + v["error"]) if isinstance(v, dict) and v.get("error") else "ok")
                      for k, v in (s.get("steps") or {}).items()}}
