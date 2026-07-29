"""
ProOS Core -- HTTP API.

Stdlib only (http.server), so it runs as a plain script now and drops into an HA
add-on later with no dependency story. The dashboard talks to this instead of
computing device logic itself.

Endpoints:
  GET  /health
  GET  /rooms/<area>/activities      -> generated activities + live verdicts
  POST /rooms/<area>/intent          -> {"activity": "<key>"}  (kicks off async)
  GET  /rooms/<area>/status          -> live run state + transcript

Async model: POST returns immediately with status "reconciling"; the dashboard
polls /status and watches it go reconciling -> achieved | degraded | superseded.

Note: unauthenticated on the LAN for the PoC. Behind an HA add-on it sits on
HA's ingress; for direct exposure add a token check here.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
import shutil
import threading
import queue
import datetime
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

from proos.live_ha import RestHAClient
from proos.controller import RoomController
from proos.monitor import Monitor, check_room
from proos import commands as _commands
from proos import unifinet as _unifinet_mod
from proos.watcher import Watcher, discover_watches
from proos.music import MusicLayer
from proos.ma import MaCommissioner, MaUnavailable
from proos.ma_ws import MaAuthFailed, MaClient
from proos.credentials import CredentialStore
try:
    from proos import push          # optional: APNs sender (needs proos/push.py)
except Exception as _e:             # a missing/broken push module must NOT stop Core booting
    push = None
    print(f"  push · module unavailable ({_e}); /push disabled", flush=True)
try:
    from proos import proactive as _proactive_mod   # optional: fault -> notify loop
except Exception as _e:
    _proactive_mod = None
    print(f"  proactive · module unavailable ({_e})", flush=True)
_proactive = None
try:
    from proos import ctlbridge as _ctlbridge_mod   # optional: activity verdict publisher
    from proos import netevidence as _netev_mod     # network evidence providers
except Exception as _e:
    _ctlbridge_mod = None
    _netev_mod = None
    print(f"  ctlbridge · module unavailable ({_e})", flush=True)
_ctlbridge = None
try:
    from proos import journal as _journal_mod       # event journal + live bus
    from proos import healthmon as _healthmon_mod   # health & drift incidents
except Exception as _e:
    _journal_mod = None
    _healthmon_mod = None
    print(f"  journal/healthmon · module unavailable ({_e})", flush=True)
try:
    from proos import netmap        # optional: auto reachability harvesting
except Exception as _e:
    netmap = None
    print(f"  reachability · netmap unavailable ({_e}); manual reachability only", flush=True)
from proos import sync
try:
    from proos import project     # optional: commissioning project (AV orchestration model)
except Exception as _e:
    project = None
    print(f"  project · module unavailable ({_e}); /project disabled", flush=True)
try:
    from proos import auth
except Exception:
    auth = None
try:
    from proos import provision, users, consent, terminal, navconfig, catalog
    from proos import roomorder
    from proos import unifi
except Exception:  # optional - missing modules must not stop boot
    provision = users = consent = terminal = navconfig = catalog = None
    roomorder = None
try:
    from proos import assist as _assist          # Pro Assist AI gateway (optional)
except Exception:  # noqa: BLE001
    _assist = None
try:
    from proos import scenephotos as _scenephotos   # per-scene photo store + matching
except Exception:  # noqa: BLE001
    _scenephotos = None
try:
    from proos import roomart as _roomart           # room icon + generated background
except Exception:  # noqa: BLE001
    _roomart = None
try:
    from proos import roomdevices as _roomdevices   # per-room non-AV device commissioning
except Exception:  # noqa: BLE001
    _roomdevices = None
try:
    from proos import appart as _appart   # app tile artwork (shipped pack + Tech Tools uploads)
except Exception:  # noqa: BLE001
    _appart = None
try:
    from proos import appctl as _appctl   # room app launching (multi-source)
except Exception:  # noqa: BLE001
    _appctl = None
_ASSIST_HOME_NAME = ""   # cached HA location name for the assist system prompt
try:
    from proos import proauth
except Exception:  # optional - installer login must not stop boot
    proauth = None

_controllers: dict[str, RoomController] = {}
_state_version = 0  # bumps on any project/activity change; clients poll /health
def _bump_state():
    global _state_version
    _state_version += 1
    return _state_version


# ── Dashboard push channel ──
_dash_lock = threading.Lock()
_dash_clients = {}
_dash_pub_version = 0

def _dash_get(device, name=None):
    c = _dash_clients.get(device)
    if c is None:
        c = {"name": name or device, "version": None, "last_seen": time.time(), "q": queue.Queue()}
        _dash_clients[device] = c
    elif name:
        c["name"] = name
    return c

def _dash_publish():
    global _dash_pub_version
    with _dash_lock:
        _dash_pub_version += 1
        v = _dash_pub_version
        targets = list(_dash_clients.values())
    for c in targets:
        try:
            c["q"].put_nowait(v)
        except Exception:
            pass
    return v

def _dash_ack(device, version, name=None):
    with _dash_lock:
        c = _dash_get(device, name)
        try:
            c["version"] = int(version)
        except Exception:
            pass
        c["last_seen"] = time.time()

def _dash_snapshot():
    now = time.time()
    with _dash_lock:
        v = _dash_pub_version
        out = [{"device": d, "name": c["name"], "version": c["version"],
                "acked": c["version"] == v, "online": (now - c["last_seen"]) < 45}
               for d, c in _dash_clients.items()]
        for d in [d for d, c in _dash_clients.items() if now - c["last_seen"] > 600]:
            _dash_clients.pop(d, None)
    return {"version": v, "clients": out}
_monitor: Monitor | None = None
_watcher: Watcher | None = None
_music: MusicLayer | None = None
_unifi_layer = None  # proos.unifi.UnifiLayer — native Protect integration owner
_ma: "MaCommissioner | None" = None
_client = None
_cfg: dict | None = None


def _ws_call(msg_type, **fields):
    """Adapter: run one HA websocket command over Core's supervisor
    connection. Passed into provision/users so they stay decoupled."""
    from proos.ha_ws import ws_command
    return ws_command(_cfg["base_url"], _cfg["token"], msg_type, **fields)


def _reload_entry(entry_id):
    """Reload a config entry via HA's REST endpoint -- the proven path (the same
    one the app's Reload button uses). The WS 'config_entries/reload' command is
    not reliably available to Core over its connection."""
    try:
        _client._req("POST", "/api/config/config_entries/entry/%s/reload" % entry_id)
        print("[watcher] recovery: reloaded entry %s" % entry_id, flush=True)
        return True
    except Exception as e:
        print("[watcher] recovery reload failed for %s: %s" % (entry_id, e), flush=True)
        return False


def _remove_native_integration(domain):
    """Installer 'remove & re-add': delete every HA config entry for a domain.
    Runs on Core's Supervisor token, so an installer with no Home Assistant
    access can reset a certified integration straight from the ProOS panel.
    Returns the list of removed entry_ids."""
    try:
        entries = _client._req("GET", "/api/config/config_entries/entry") or []
    except Exception as e:
        raise RuntimeError("could not list config entries: %s" % e)
    removed = []
    for e in entries:
        if e.get("domain") == domain:
            eid = e.get("entry_id")
            _client._req("DELETE", "/api/config/config_entries/entry/%s" % eid)
            removed.append(eid)
    return removed


def _entity_to_entry(entity):
    """Resolve entity_id -> config_entry_id via HA's registries (the same loader
    discover_watches uses -- REST-backed, works from Core)."""
    try:
        from proos import netmap
        _e, _d, entities = netmap.load_registries(_HA_STORAGE_DIR, _client)
        for e in entities:
            if e.get("entity_id") == entity:
                return e.get("config_entry_id")
    except Exception as e:
        print("[watcher] entity->entry resolve failed for %s: %s" % (entity, e), flush=True)
    return None


_RECOVERY_CFG_PATH = os.environ.get("PROOS_RECOVERY_CFG", "/data/recovery_config.json")


def _unifi_poe_ports():
    """UniFi PoE port control switches from the entity registry (switch.<sw>_port_<n>, platform
    'unifi'), each with an enabled flag. HA registers these DISABLED by default; ProOS enables
    the one a device needs on demand so the installer never touches Home Assistant."""
    out = []
    try:
        for e in (_client.entity_registry() or []):
            eid = e.get("entity_id") or ""
            if e.get("platform") == "unifi" and eid.startswith("switch.") and "_port_" in eid:
                out.append({"entity_id": eid,
                            "name": e.get("name") or e.get("original_name") or eid,
                            "enabled": not e.get("disabled_by")})
    except Exception as _e:
        print("[unifi] poe port list failed: %s" % _e, flush=True)
    out.sort(key=lambda x: x["entity_id"])
    return out

def _recovery_cfg_read():
    """Installer-assigned recovery overrides {entity: {method, plug, off_time, tier}}."""
    try:
        with open(_RECOVERY_CFG_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _recovery_cfg_write(d):
    tmp = _RECOVERY_CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _RECOVERY_CFG_PATH)


def _power_cycle(entity, rec):
    """Watcher recovery action (SMART-PLUG): cut the device's power at its plug,
    wait, then restore it. For faults a config-entry reload can't fix -- e.g. a
    Samsung TV whose connection server wedges (ms.channel.unauthorized) until the
    TV is physically rebooted. Uses homeassistant.turn_off/on so the plug can be a
    switch, light, or any switchable entity."""
    plug = (rec or {}).get("plug")
    if not plug:
        print("[watcher] recovery: power_cycle for %s has no plug configured" % entity, flush=True)
        return False
    off_time = max(5, min(int((rec or {}).get("off_time", 25)), 120))
    try:
        _client.call_service("homeassistant", "turn_off", plug)
        print("[watcher] recovery: cut power to %s via %s (%ds)" % (entity, plug, off_time), flush=True)
        time.sleep(off_time)
        _client.call_service("homeassistant", "turn_on", plug)
        print("[watcher] recovery: restored power to %s via %s" % (entity, plug), flush=True)
        return True
    except Exception as e:
        print("[watcher] recovery: power_cycle failed for %s: %s" % (entity, e), flush=True)
        return False


def _poe_cycle(entity, rec):
    """Watcher recovery action (UniFi PoE): cut power to the device's PoE switch PORT, wait,
    then restore it -- a hard reboot for a PoE-powered device (AP, camera, a streamer on a PoE
    splitter) with no smart plug. Targets the UniFi PoE port switch entity
    (switch.<switch>_port_<n>_poe). Shorter default off-time than a mains plug: a few seconds
    with no power forces the device to reboot when the port comes back."""
    sw = (rec or {}).get("poe_switch") or (rec or {}).get("plug")
    if not sw:
        print("[watcher] recovery: poe_cycle for %s has no PoE switch configured" % entity, flush=True)
        return False
    off_time = max(3, min(int((rec or {}).get("off_time", 8)), 60))
    try:
        _client.call_service("switch", "turn_off", sw)
        print("[watcher] recovery: PoE off for %s via %s (%ds)" % (entity, sw, off_time), flush=True)
        time.sleep(off_time)
        _client.call_service("switch", "turn_on", sw)
        print("[watcher] recovery: PoE restored for %s via %s" % (entity, sw), flush=True)
        return True
    except Exception as e:
        print("[watcher] recovery: poe_cycle failed for %s: %s" % (entity, e), flush=True)
        return False


def _reload_integration(entity, action, rec=None):
    """Watcher recovery executor. Dispatches on the action string:
      reload_integration -- restart the wedged config entry (never touches the device).
      power_cycle        -- cut and restore the device's smart plug (for faults a
                            reload can't fix; needs a plug assigned via pro.html)."""
    if action == "power_cycle":
        return _power_cycle(entity, rec or {})
    if action == "poe_cycle":
        return _poe_cycle(entity, rec or {})
    if action != "reload_integration":
        return False
    entry_id = _entity_to_entry(entity)
    if not entry_id:
        print("[watcher] recovery: no config entry for %s" % entity, flush=True)
        return False
    return _reload_entry(entry_id)
# Centralised service-token registry. New integrations mint/store/rotate through
# this; Music Assistant keeps its existing /data files until we migrate it.
_creds = CredentialStore()
_unifinet = _unifinet_mod.UniFiNetClient(_creds)

# OAuth relay jobs, keyed by session_id: {status, auth_url, entries, error, ts}.
# status: 'starting' -> 'waiting' (URL ready, popup open) -> 'done' | 'error'.
_OAUTH_JOBS: dict = {}
_OAUTH_LOCK = threading.Lock()
_OAUTH_TTL = 300


def _oauth_reap():
    now = time.time()
    with _OAUTH_LOCK:
        for sid in [s for s, j in _OAUTH_JOBS.items() if now - j.get("ts", now) > _OAUTH_TTL]:
            _OAUTH_JOBS.pop(sid, None)


def _oauth_run(domain: str, session_id: str, action: str = "auth", values: dict | None = None):
    """Background worker: drive the MA OAuth flow to completion, recording the
    auth URL (as soon as MA emits it) and the final entries (or error)."""
    def _on_url(url):
        with _OAUTH_LOCK:
            j = _OAUTH_JOBS.get(session_id)
            if j is not None:
                j["auth_url"] = url
                j["status"] = "waiting"
                j["ts"] = time.time()
    try:
        entries = _ma.run_provider_auth(domain, session_id, _on_url, action=action, values=values)
        with _OAUTH_LOCK:
            j = _OAUTH_JOBS.get(session_id)
            if j is not None:
                j["entries"] = entries
                j["status"] = "done"
                j["ts"] = time.time()
    except Exception as e:
        with _OAUTH_LOCK:
            j = _OAUTH_JOBS.get(session_id)
            if j is not None:
                j["error"] = str(e)
                j["status"] = "error"
                j["ts"] = time.time()


# Apple Music sign-in is driven through MA's OWN native config-flow auth
# action (CONF_ACTION_AUTH) via the generic provider OAuth relay — the same
# path Spotify uses and exactly what native MA does. No custom ProOS MusicKit
# page (the earlier https/dev-token attempts were removed: MA's page works on
# LAN http, confirmed live).




def get_controller(area: str) -> RoomController:
    if area not in _controllers:
        reach = (_cfg or {}).get("reachability", {})
        _controllers[area] = RoomController(_client, area, reachability=reach)
    return _controllers[area]


_FLAGS_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "assist_flags.json")


def _flags_load():
    try:
        with open(_FLAGS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _flags_save(rows):
    try:
        os.makedirs(os.path.dirname(_FLAGS_PATH), exist_ok=True)
        tmp = _FLAGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows[-200:], fh, indent=1)
        os.replace(tmp, _FLAGS_PATH)
    except Exception:
        pass


def _flag_add(rec):
    """An issue the assistant (or a homeowner through it) is escalating to the
    Pro — with the diagnosis attached, so the installer arrives knowing the
    story instead of starting from 'customer says TV broken'."""
    rows = _flags_load()
    rec = dict(rec or {})
    rec["id"] = "flag_%d" % int(time.time() * 1000)
    rec["time"] = time.strftime("%Y-%m-%d %H:%M")
    rec["resolved"] = False
    rows.append(rec)
    _flags_save(rows)
    print("  [assist] flagged for pro: %s" % rec.get("summary"), flush=True)
    return rec


def _assist_awareness():
    """The awareness layer, packaged as callables for ProAssist's tools.

    This is the bridge that makes the assistant a Pro who has LOOKED rather
    than one who guesses: the same watcher verdicts, room health checks and
    recovery executor the dashboards and auto-recovery use, handed to chat as
    tools. Every callable is the existing implementation — nothing is
    duplicated, so chat-driven answers can never drift from what Pro shows."""
    def _watchers():
        return _watcher.report() if _watcher else None

    def _monitor_all():
        return _monitor.all() if _monitor else {}

    def _room_health(area_id):
        # Accept an area_id (the assistant's identity key) or a room name.
        name = area_id
        try:
            for a in (_client.area_registry() or []):
                if a.get("area_id") == area_id:
                    name = a.get("name") or area_id
                    break
        except Exception:
            pass
        return check_room(get_controller(name)).to_dict()

    def _audit_events():
        path = getattr(_watcher, "audit_path", "/data/watcher_audit.log") if _watcher else "/data/watcher_audit.log"
        out = []
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh.readlines()[-150:]:
                    p = ln.rstrip("\n").split("\t")
                    if len(p) >= 3:
                        out.append({"time": p[0], "entity": p[1], "event": p[2],
                                    "detail": p[3] if len(p) > 3 else ""})
        except Exception:
            pass
        out.reverse()
        return out

    def _recover(entity):
        rec = _recovery_cfg_read().get(entity) or {}
        method = rec.get("method") or "reload_integration"
        ok = bool(_reload_integration(entity, method, rec))
        try:
            if _watcher:
                _watcher._audit(entity, "recover",
                                ("%s:ok" % method) if ok else ("%s:failed" % method))
        except Exception:
            pass
        return {"ok": ok, "target": entity, "method": method,
                "message": (("recovered via %s" % method.replace("_", " "))
                            if ok else "the recovery ran but the device didn't come back")}

    return {"watchers": _watchers, "monitor": _monitor_all,
            "room_health": _room_health, "audit": _audit_events,
            "recover": _recover, "flag": _flag_add}


def apply_auto_reachability():
    """Derive a device-IP reachability map from HA's own registries and merge it
    UNDER the installer's manual `reachability` config (manual always wins), then
    hand the merged map to the Watcher and every RoomController. This is what
    makes the two-signal awareness automatic: add a device to HA, and its second
    signal appears with no ProOS configuration. Safe if netmap/stores are absent
    -- it just leaves the manual map untouched. Runs at boot and on
    POST /reachability/refresh (e.g. after commissioning a new device)."""
    if _cfg is None:
        return {"auto": 0, "manual": 0, "total": 0}
    manual = dict(_cfg.get("_reach_manual") or _cfg.get("reachability") or {})
    auto = {}
    if netmap is not None:
        try:
            auto = netmap.harvest(client=_client)
        except Exception as e:
            print(f"  reachability · auto-harvest failed ({e}); manual only", flush=True)
        # Overlay UniFi client trackers as the AUTHORITATIVE second-signal (home/not_home),
        # joined by IP. The controller's presence beats a TCP probe, so it takes precedence
        # over the ip-probe spec; a manual override still wins over everything (merged below).
        try:
            _tr = netmap.harvest_unifi_trackers(_client, auto)
            if _tr:
                auto = {**auto, **_tr}
                print(f"  reachability · {len(_tr)} device(s) matched to a UniFi client tracker", flush=True)
        except Exception as e:
            print(f"  reachability · UniFi tracker harvest failed ({e})", flush=True)
    merged = {**auto, **manual}                 # manual overrides auto per entity
    _cfg["reachability"] = merged
    _cfg["_reach_manual"] = manual              # remember the pure manual layer
    _cfg["_reach_auto"] = auto                  # ...and the auto layer, for /reachability
    if _watcher is not None:
        _watcher.reach_map = merged
    for c in _controllers.values():
        try:
            c.reachability = merged
        except Exception:
            pass
    print(f"  reachability · {len(auto)} auto + {len(manual)} manual = "
          f"{len(merged)} device{'' if len(merged)==1 else 's'} with a second signal",
          flush=True)
    return {"auto": len(auto), "manual": len(manual), "total": len(merged)}


# ── Factory reset ───────────────────────────────────────────────────────────
# "Reset this home" restores a clean Home Assistant Core state from a partial
# backup shipped in the image (named below). HA Core only: the OS and this add-on
# are left untouched, so nothing has to be reinstalled. A recovery point of the
# CURRENT home is taken first; both backups live in /backups (outside /config),
# so they survive the restore and an accidental reset is always recoverable.
# Needs hassio_api + hassio_role:manager in config.yaml.
BASELINE_NAME = "proos-baseline"
SUPERVISOR = "http://supervisor"

# The certified ProOS Music add-on — the white-label re-skin of the Music
# Assistant server, published from Dave's repo (b333b432). Core owns the
# integration that bridges it into HA; this slug drives the Supervisor
# running/stopped/install checks. (The upstream community add-on was
# d5369777_music_assistant; we now ship our own pinned wrapper.)
MA_ADDON_SLUG = "b333b432_proos_music"
# Store repository the ProOS Music add-on ships from — registered on demand so a
# fresh installer box can install it without the installer touching the HA store UI.
MA_ADDON_REPO = "https://github.com/daveneill/proos-addons"

# White-label: every backup ProOS creates is encrypted with this key, so a
# downloaded .tar can't be opened to inspect dashboards/config/automations
# (only backup.json metadata stays readable) and restore only works through
# ProOS. Override per-fleet via the add-on's masked `backup_password` option.
BACKUP_PASSWORD = "PrOoS-bk-9Fq2xT7m"


def _enc(payload):
    """Add the encryption password to a create-backup payload."""
    if BACKUP_PASSWORD:
        payload = dict(payload)
        payload["password"] = BACKUP_PASSWORD
    return payload


def _sv(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SUPERVISOR + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + os.environ.get("SUPERVISOR_TOKEN", ""))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    d = json.loads(raw) if raw else {}
    if d.get("result") != "ok":
        raise RuntimeError(f"supervisor {path}: {d}")
    return d.get("data", {})


def _addon_state(slug):
    """Supervisor add-on state ('started'/'stopped'/...), or 'unknown' off add-on."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return "unknown"
    try:
        return _sv("GET", f"/addons/{slug}/info").get("state", "unknown")
    except Exception:
        return "unknown"


_MA_CONN_FILE = "/data/ma_conn.json"


def _load_ma_conn():
    try:
        with open(_MA_CONN_FILE) as f:
            d = json.load(f)
        # token may legitimately be None — an MA server with auth disabled
        # connects tokenless (MaClient only auths when the server demands it).
        # host is the only required field; requiring a token here would break
        # reload of a validated tokenless connection.
        if d.get("host"):
            return (d["host"], d.get("port"), d.get("token") or None)
    except Exception:
        pass
    return None


def _save_ma_conn(host, port, token):
    try:
        with open(_MA_CONN_FILE, "w") as f:
            json.dump({"host": host, "port": port, "token": token}, f)
    except Exception as e:
        print(f"  MA · could not persist conn: {e}", flush=True)


# Admin token: MA gates provider config WRITES (config/providers/save) behind an
# admin user. Core's discovery/HA-integration token authenticates as the system
# user (read + OAuth only), so saves are refused. The installer pastes a
# long-lived token from MA's User Management once; Core then authenticates as
# that admin for all MA commands. Stored apart from the read conn so it can be
# rotated/cleared independently.
_MA_ADMIN_FILE = "/data/ma_admin.json"


def _ma_ingress_identity():
    """A CURRENT, VALID HA admin identity for MA's ingress channel.

    The registered admin user (ma_admin.json) wins while that user still
    exists. After a factory reset the auth wipe invalidates it — presenting a
    wiped user id (or connecting anonymously) makes the MA fork drop the
    socket on privileged commands ("WS closed mid-frame" on a provider save).
    Fall back to the CURRENT owner, which always exists on an onboarded box."""
    au = _load_ma_admin_user()
    rows = []
    try:
        if users:
            rows = users.list_users(_ws_call) or []
    except Exception:  # noqa: BLE001
        rows = []
    ids = {u.get("id") for u in rows}
    if au and au[0] in ids:
        return au
    for u in rows:
        if u.get("is_owner") and u.get("username"):
            return (u.get("id"), u.get("username"), u.get("name") or u.get("username"))
    for u in rows:
        if u.get("admin") and u.get("username"):
            return (u.get("id"), u.get("username"), u.get("name") or u.get("username"))
    return au


def _load_ma_admin_token():
    try:
        with open(_MA_ADMIN_FILE) as f:
            t = (json.load(f) or {}).get("token")
        return t or None
    except Exception:
        return None


def _save_ma_admin_token(token):
    with open(_MA_ADMIN_FILE, "w") as f:
        json.dump({"token": token}, f)


def _clear_ma_admin_token():
    try:
        os.remove(_MA_ADMIN_FILE)
    except FileNotFoundError:
        pass


def _ma_validate_admin_token(token):
    """Confirm MA accepts the token on the reachable host/port, then persist it.
    A clean connect proves the token authenticates; admin-role is confirmed in
    practice by the save that follows."""
    base = _load_ma_conn()
    if not base:
        _ma_conn()  # establish the read conn (host/port) first
        base = _load_ma_conn()
    if not base:
        return False
    h, p, _ = base
    try:
        with MaClient(h, p, token, timeout=5):
            pass
        _save_ma_admin_token(token)
        return True
    except Exception:
        return False


# Admin USER identity (the packageable path): instead of a pasted MA token, Core
# stores the installer's HA admin user (id/name) and presents it as X-Remote-User
# headers on MA's ingress channel for provider WRITES. pro.html reads it once from
# its authenticated HA session (auth/current_user) and hands it over — no MA UI,
# no manual token. Stored in the same /data file as a "user" object.
def _load_ma_admin_user():
    try:
        with open(_MA_ADMIN_FILE) as f:
            u = (json.load(f) or {}).get("user")
        if u and u.get("id") and u.get("username"):
            return (u["id"], u["username"], u.get("display_name") or u["username"])
    except Exception:
        pass
    return None


def _save_ma_admin_user(uid, username, display_name):
    data = {}
    try:
        with open(_MA_ADMIN_FILE) as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data["user"] = {"id": uid, "username": username,
                    "display_name": display_name or username}
    with open(_MA_ADMIN_FILE, "w") as f:
        json.dump(data, f)


# ── Room-speaker allowlist ────────────────────────────────────────────────
# ProOS curates which MA players are real room speakers (vs the TVs/Macs MA
# auto-discovers). The homeowner dashboard shows only these. Held in ProOS, not
# MA — no admin write to MA needed.
_MA_SPEAKERS_FILE = "/data/music_speakers.json"


def _load_speakers():
    """List of kept player_ids, or None if never curated."""
    try:
        with open(_MA_SPEAKERS_FILE) as f:
            d = json.load(f)
        ids = d.get("player_ids")
        if isinstance(ids, list):
            return [str(x) for x in ids]
    except Exception:
        pass
    return None


def _save_speakers(ids):
    out = sorted({str(x) for x in (ids or [])})
    with open(_MA_SPEAKERS_FILE, "w") as f:
        json.dump({"player_ids": out}, f)
    return out


_MA_ADDON_SLUG = "b333b432_proos_music"
_MA_API_PORT = 8095  # MA public API: /ws (WebSocket) + REST
# HA's config dir mounts at /homeassistant on current Supervisor map schemas and
# at /config on legacy interpretation — the SAME resolution _HA_CONFIG_DIR uses.
# Hardcoding /homeassistant here broke every .storage read on boxes where the
# mount is /config: the MA link failed with "config store not visible" even
# though factory reset (which uses _HA_CONFIG_DIR) worked fine on the same box.
_HA_STORAGE_DIR = ("/homeassistant" if os.path.isdir("/homeassistant/.storage")
                   else "/config") + "/.storage"
_HA_STORAGE_ENTRIES = _HA_STORAGE_DIR + "/core.config_entries"


# One reserved, non-existent entity id. Used as the include-list when the
# bridge should expose nothing: an include filter that matches no real entity
# bridges nothing — whereas an *empty* include list makes HA expose EVERYTHING
# (HomeKit's "no filter = all bridgeable" trap). Never name a real entity this.
_HOMEKIT_NONE_SENTINEL = "sensor.proos_homekit_none"


def _homekit_wide_open(filt):
    """True if a HomeKit filter has no explicit entity selection — i.e. it
    exposes everything (default domains, or all bridgeable for an empty filter).
    A real installer selection sets include_entities or exclude_entities (the
    sentinel counts as a selection), so those are treated as configured."""
    filt = filt or {}
    return not filt.get("include_entities") and not filt.get("exclude_entities")


def _homekit_brand(name="ProOS", title="ProOS Apple Home", entry_id=None):
    """White-label the HomeKit bridge so Apple Home shows 'ProOS', not the
    auto-generated 'HASS Bridge' name, AND guarantee the opt-in exposure model.
    HA's API can't change a bridge's name (config_entries/update only touches the
    title), so Core rewrites the name in the config-entries store in place; the
    caller then restarts HA so the bridge re-advertises. Do this while the bridge
    is UNPAIRED — renaming after pairing forces a re-pair.

    Exposure clamp: a freshly-created bridge is "wide open" (HA exposes every
    entity in its default domains). ProOS publishes opt-in — nothing until the
    installer enables devices. A *separate* filter write during publish races
    HA's own save of the just-created entry and gets silently reverted (the
    symptom: bridge ends up named ProOS but still exposing everything). So we
    fold the "expose nothing" clamp into THIS write: if the filter has no
    explicit entity selection, set it to the sentinel here, atomically with the
    name, so it lands last and survives the restart. A real selection
    (include_entities/exclude_entities set) is never touched — this won't wipe
    devices an installer has enabled, so it's safe on the rename button too.

    Needs homeassistant_config:rw. Backs up, validates, atomically replaces.
    Returns the number of homekit entries renamed (0 if already branded)."""
    with open(_HA_STORAGE_ENTRIES) as f:
        d = json.load(f)
    entries = d["data"]["entries"]
    total = len(entries)
    renamed = 0
    changed = False
    for e in entries:
        if e.get("domain") != "homekit":
            continue
        if entry_id and e.get("entry_id") != entry_id:
            continue
        data = e.setdefault("data", {})
        if data.get("name") != name:
            data["name"] = name
            renamed += 1
            changed = True
        if e.get("title") != title:
            e["title"] = title
            changed = True
        # Clamp a wide-open bridge to expose nothing, in this same write.
        o = e.setdefault("options", {})
        if _homekit_wide_open(o.get("filter")):
            o["mode"] = o.get("mode", "bridge")
            o["devices"] = o.get("devices", [])
            o["filter"] = {
                "include_domains": [],
                "include_entities": [_HOMEKIT_NONE_SENTINEL],
                "exclude_domains": [],
                "exclude_entities": [],
            }
            changed = True
    if changed:
        try:
            shutil.copy(_HA_STORAGE_ENTRIES, _HA_STORAGE_ENTRIES + ".proosbak")
        except Exception:
            pass
        tmp = _HA_STORAGE_ENTRIES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        with open(tmp) as f:
            json.load(f)  # re-parse to prove the file is valid before swapping
        assert len(d["data"]["entries"]) == total
        os.replace(tmp, _HA_STORAGE_ENTRIES)
    return {"renamed": renamed, "changed": changed}



# ── Android TV app list ──────────────────────────────────────────────────────
# The Android TV Remote integration keeps each box's launchable apps in its
# config-entry OPTIONS ({"apps": {package_id: {"app_name": ...}}}) and publishes
# them as source_list. Nothing discovers them automatically, so ProOS LEARNS
# them: the media player reports the package id of whatever is open on screen
# (app_id), so opening an app once is proof it's installed and we capture it.
#
# The list MUST be written through the integration's own options flow. Editing
# .storage/core.config_entries directly looks like it works and does nothing:
# HA holds config entries in MEMORY, an entry reload re-reads memory (not the
# file), and HA's next save rewrites the file — the edit silently disappears.
# (The HomeKit filter gets away with a direct write only because it is followed
# by a full HA restart.) So we drive exactly what the "Configure" button does:
#
#   POST /api/config/config_entries/options/flow  {"handler": entry_id} -> "init"
#   POST .../options/flow/<flow_id>  {"apps":"add_new","enable_ime":x}  -> "apps"
#   POST .../options/flow/<flow_id>  {"app_id":pkg,"app_name":name}     -> "init"
#      ... one add (or delete) pass per app, all inside the one flow ...
#   POST .../options/flow/<flow_id>  {"enable_ime": x}                  -> saved
#
# The handler holds the whole app set across the pass and reloads the entry
# itself (OptionsFlowWithReload), so one flow commits every app at once.
_ATV_DOMAINS = ("androidtv_remote", "androidtv")
_ATV_LEDGER = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "atv_apps.json")


_ATV_SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "proos", "atv_seed.json")


def _atv_seed():
    """Recovered app lists shipped with the release, keyed by config_entry_id.

    Only applied to a box that has learned nothing yet, so it can never override
    a site's own list. Entry ids are unique per install — inert elsewhere."""
    try:
        with open(_ATV_SEED_FILE) as f:
            d = json.load(f)
        return {k: v for k, v in d.items()
                if isinstance(v, dict) and not k.startswith("_")}
    except Exception:
        return {}


def _atv_ledger():
    """ProOS's own record of learned apps, keyed by config_entry_id (immutable).

    THIS is the master list for a box. An Android box exposes no API that says
    what's installed, and the remote-control integration publishes nothing, so
    the only trustworthy evidence is "this app has been seen running here".
    Kept in the add-on's data volume, so it survives HA restores, re-pairing and
    add-on updates, and a box never has to be taught twice.
    Shape: {entry_id: {package_id: name}}."""
    d = {}
    try:
        with open(_ATV_LEDGER) as f:
            got = json.load(f)
        if isinstance(got, dict):
            d = got
    except Exception:
        d = {}
    # Recovery data is MERGED, not used only as a fallback for an empty box.
    # A box that had learned one app was previously blocked from receiving the
    # rest of its recovered list — the seed has to top up, not just fill in.
    # `_seeded` records that it's been applied so a deliberate deletion sticks.
    done = set(d.get("_seeded") or [])
    changed = False
    for entry_id, apps in _atv_seed().items():
        if entry_id in done:
            continue
        cur = dict(d.get(entry_id) or {})
        for pkg, name in apps.items():
            cur.setdefault(pkg, name)
        d[entry_id] = cur
        done.add(entry_id)
        changed = True
    if changed:
        d["_seeded"] = sorted(done)
        try:
            os.makedirs(os.path.dirname(_ATV_LEDGER), exist_ok=True)
            tmp = _ATV_LEDGER + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, _ATV_LEDGER)
        except Exception:
            pass
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _atv_ledger_raw():
    try:
        with open(_ATV_LEDGER) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atv_ledger_put(entry_id, apps):
    """Merge {pkg: name} into the ledger for this entry."""
    if not entry_id:
        return
    d = _atv_ledger_raw()
    cur = dict(d.get(entry_id) or {})
    cur.update({k: v for k, v in (apps or {}).items() if k})
    d[entry_id] = cur
    try:
        os.makedirs(os.path.dirname(_ATV_LEDGER), exist_ok=True)
        tmp = _ATV_LEDGER + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, _ATV_LEDGER)
    except Exception as e:  # noqa: BLE001
        print("  [androidtv] ledger write failed: %s" % e, flush=True)


def _atv_entry_for(entity_id):
    """(entry_id, applied_apps) for an Android TV media_player, or (None, {}).

    `applied_apps` is what the integration is ACTUALLY publishing right now —
    read from the live entity's source_list, not from .storage. The store file
    is not truth: HA keeps entries in memory and only rewrites the file when
    something changes, so a stale file happily reports apps HA has never had.
    source_list is the same thing the dashboard consumes, so if it isn't there,
    it doesn't exist. Returns {name: True} — names are all the device gives."""
    entry_id = None
    try:
        for e in (_client.entity_registry() or []):
            if e.get("entity_id") == entity_id:
                if e.get("platform") not in _ATV_DOMAINS:
                    return None, {}
                entry_id = e.get("config_entry_id")
                break
    except Exception:
        return None, {}
    if not entry_id:
        return None, {}
    applied = {}
    try:
        st = _client._req("GET", "/api/states/%s" % entity_id) or {}
        for nm in ((st.get("attributes") or {}).get("source_list") or []):
            applied[str(nm)] = True
    except Exception:
        pass
    return entry_id, applied


_ATV_FLOW = "/api/config/config_entries/options/flow"


def _atv_schema_default(form, field, fallback=None):
    """Pull a field's current value out of a returned flow form. The form is
    rendered from the live entry, so its defaults are HA's real values — no need
    to guess them from a file."""
    try:
        for f in (form.get("data_schema") or []):
            if f.get("name") == field:
                return f.get("default", f.get("description", {}).get("suggested_value", fallback))
    except Exception:
        pass
    return fallback


def _atv_flow_apps(entry_id, add, verbose=False):
    """Push {package_id: name} onto an Android TV entry via HA's options flow.

    Adds are idempotent (re-adding an app just rewrites its name), so we always
    send the full desired set rather than a diff — no dependence on any cached
    idea of what's already there. Returns HA's committed options dict so the
    caller can check what actually landed instead of assuming.

    Raises naming the exact step that failed."""
    add = {k: v for k, v in (add or {}).items() if k and "." in k}
    if not add:
        return {}
    start = _client._req("POST", _ATV_FLOW, {"handler": entry_id}) or {}
    fid = start.get("flow_id")
    if not fid:
        raise RuntimeError("HA did not open an options flow for this device")
    url = "%s/%s" % (_ATV_FLOW, fid)
    # enable_ime is required on every init submit; take HA's live value off the
    # rendered form so we can't flip the installer's setting.
    ime = _atv_schema_default(start, "enable_ime", True)
    ime = True if ime in (None, "") else bool(ime)
    try:
        if start.get("step_id") != "init":
            raise RuntimeError("unexpected first step %r" % start.get("step_id"))
        for pkg, name in add.items():
            s = _client._req("POST", url, {"apps": "add_new", "enable_ime": ime}) or {}
            if s.get("step_id") != "apps":
                raise RuntimeError("add-app form did not open (step=%r errors=%r)"
                                   % (s.get("step_id"), s.get("errors")))
            s = _client._req("POST", url, {"app_id": pkg, "app_name": name,
                                           "app_icon": ""}) or {}
            if s.get("step_id") != "init":
                raise RuntimeError("%s rejected (step=%r errors=%r)"
                                   % (pkg, s.get("step_id"), s.get("errors")))
            if verbose:
                print("  [androidtv]   + %s (%s)" % (name, pkg), flush=True)
        done = _client._req("POST", url, {"enable_ime": ime}) or {}
        if done.get("type") != "create_entry":
            raise RuntimeError("HA did not save (type=%r errors=%r)"
                               % (done.get("type"), done.get("errors")))
    except Exception:
        try:                                   # never leave a flow half-open
            _client._req("DELETE", url, None)
        except Exception:
            pass
        raise
    return dict(done.get("data") or {})


def _atv_write_apps(entity_id, apps):
    """Teach an Android TV box an app list and CONFIRM it took.

    Confirmation matters: an options flow can report success and still commit
    nothing, so we check what HA committed and then what the entity actually
    publishes. Anything less and a silent no-op looks like a save."""
    entry_id, applied = _atv_entry_for(entity_id)
    if not entry_id:
        return {"error": "that device isn't an Android TV box"}
    want = {}
    for a in (apps or []):
        pkg = str((a or {}).get("id") or "").strip()
        if not pkg or "." not in pkg:
            continue
        want[pkg] = str((a or {}).get("name") or "").strip() or pkg.split(".")[-1].title()
    if not want:
        return {"error": "nothing to save"}
    # THE SAVE IS THE LEDGER. ProOS's own record is the master list — it drives
    # the dashboard and launching goes by package id — so once this succeeds the
    # apps work, full stop. Pushing the list into the integration as well is a
    # courtesy so they also show in HA's own UI; a driver that won't hold it
    # changes nothing here and must not be reported to an installer as failure.
    _atv_ledger_put(entry_id, want)
    result = {"ok": True, "count": len(want)}
    try:
        committed = _atv_flow_apps(entry_id, want, verbose=True)
    except Exception as e:  # noqa: BLE001
        print("  [androidtv] %s: %d apps stored; driver push failed (%s)"
              % (entity_id, len(want), e), flush=True)
        result["driver"] = "failed"
        result["driver_note"] = str(e)
        return result
    saved = dict(committed.get("apps") or {})
    if len(saved) < len(want):
        print("  [androidtv] %s: %d apps stored; this box's driver kept %d of them"
              % (entity_id, len(want), len(saved)), flush=True)
        result["driver"] = "partial"
        result["driver_note"] = ("this box's driver keeps %d of %d in its own "
                                 "settings — ProOS holds the full list"
                                 % (len(saved), len(want)))
        return result
    # Driver accepted it: confirm the entity really publishes them.
    _t2 = __import__("time")
    published = {}
    for _ in range(6):                          # entry reloads; give it a moment
        _t2.sleep(1)
        _e2, published = _atv_entry_for(entity_id)
        if len(published) >= len(want):
            break
    print("  [androidtv] %s: %d apps stored, %d published as source_list"
          % (entity_id, len(want), len(published)), flush=True)
    result["driver"] = "ok" if published else "silent"
    result["published"] = len(published)
    return result


def _homekit_expose(entities, entry_id=None):
    """Set the exact set of entities the HomeKit bridge exposes (opt-in model).

    Writes the EntityFilter directly in the config-entries store rather than
    driving HA's options flow: that flow is multi-step, its entity field only
    accepts HA's own bridgeable allow-list, and it cannot express "expose
    nothing" (an empty include there falls through to expose-everything). A
    direct write is the only reliable way to say "exactly these" or "none".

    `entities` is the complete allow-list. Empty/None => expose NOTHING (we
    substitute the reserved sentinel so the include matches nothing instead of
    everything). Needs homeassistant_config:rw. Backs up, validates, atomically
    swaps; caller restarts HA so the new filter loads. Returns entries updated."""
    ents = [e for e in (entities or []) if isinstance(e, str) and "." in e]
    inc = ents if ents else [_HOMEKIT_NONE_SENTINEL]
    with open(_HA_STORAGE_ENTRIES) as f:
        d = json.load(f)
    entries = d["data"]["entries"]
    total = len(entries)
    updated = 0
    for e in entries:
        if e.get("domain") != "homekit":
            continue
        if entry_id and e.get("entry_id") != entry_id:
            continue
        o = e.setdefault("options", {})
        o.setdefault("mode", "bridge")
        o["devices"] = o.get("devices", [])
        o["filter"] = {
            "include_domains": [],
            "include_entities": inc,
            "exclude_domains": [],
            "exclude_entities": [],
        }
        updated += 1
    if updated:
        try:
            shutil.copy(_HA_STORAGE_ENTRIES, _HA_STORAGE_ENTRIES + ".proosbak")
        except Exception:
            pass
        tmp = _HA_STORAGE_ENTRIES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        with open(tmp) as f:
            json.load(f)  # prove valid JSON before swapping
        assert len(d["data"]["entries"]) == total
        os.replace(tmp, _HA_STORAGE_ENTRIES)
    return updated


def _ma_host_candidates():
    """Reachable hostnames/IPs for the MA add-on, best first. MA runs on the host
    network, so we try its add-on hostname, its reported IP, then the Supervisor
    docker gateway as a fallback."""
    cands = []
    try:
        info = _sv("GET", f"/addons/{_MA_ADDON_SLUG}/info")
        for k in ("hostname", "ip_address"):
            v = info.get(k)
            if v:
                cands.append(v)
    except Exception as e:
        print(f"  MA · could not read add-on info: {e}", flush=True)
    cands.append("172.30.32.1")  # hassio docker gateway (host)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _ma_token_from_storage():
    """(url, token) the HA music_assistant integration already stored, or None.
    ProCore reuses HA's own working token (read-only) instead of minting one —
    no MA UI, no credentials, no HA access by the user."""
    try:
        with open(_HA_STORAGE_ENTRIES) as f:
            store = json.load(f)
    except FileNotFoundError:
        print("  MA · HA config store not visible (need homeassistant_config:ro map)",
              flush=True)
        return None
    except Exception as e:
        print(f"  MA · storage read failed: {e}", flush=True)
        return None
    for e in (store.get("data") or {}).get("entries", []) or []:
        if e.get("domain") == "music_assistant":
            d = e.get("data") or {}
            url, tok = d.get("url"), d.get("token")
            # A newer MA server with auth DISABLED stores ONLY the url — no
            # token exists anywhere and none is needed: MaClient.connect only
            # auths when the server demands it (schema >= AUTH_SCHEMA).
            # Requiring a token here made a fresh post-reset link fail with
            # "No Music token" even though MA was up and linked in HA
            # (observed live 2026-07-24). url alone is a valid entry.
            if url:
                return (url, tok or None)
    print("  MA · no music_assistant entry in HA store yet", flush=True)
    return None


def _ma_from_discovery():
    """(url, token|None) from the SUPERVISOR DISCOVERY message the ProOS Music
    add-on publishes (service 'music_assistant'). This is the token's actual
    source of truth — HA's config entry only mirrors it when the config flow
    version that created the entry stored one. A post-factory-reset entry is
    auto-confirmed from hassio discovery and stores only the url, which is why
    linking must never depend on the entry's data shape (observed live
    2026-07-24: MA up + linked in HA, Core refusing with 'No Music token')."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return None
    try:
        rows = (_sv("GET", "/discovery") or {}).get("discovery") or []
        for d in rows:
            if d.get("service") == "music_assistant":
                c = d.get("config") or {}
                host, port = c.get("host"), c.get("port")
                tok = c.get("token") or c.get("auth_token") or None
                if host:
                    return ("http://%s:%s" % (host, port or _MA_API_PORT), tok)
    except Exception as e:  # noqa: BLE001
        print(f"  MA · supervisor discovery read failed: {e}", flush=True)
    return None


def _parse_host_port(url, default_port):
    m = re.match(r"https?://([^:/]+)(?::(\d+))?", url or "")
    if not m:
        return (None, default_port)
    return (m.group(1), int(m.group(2)) if m.group(2) else default_port)


def _ma_validate_and_persist(token, url=None):
    """Find a host:port ProCore can reach MA on with this token, then persist it.
    Connecting auths with the token, so a clean connect proves token+host both.
    Returns (host, port, token) or None."""
    hosts, ports = [], []
    if url:
        h, p = _parse_host_port(url, _MA_API_PORT)
        if h:
            hosts.append(h)
        if p:
            ports.append(p)
    for h in _ma_host_candidates():
        if h not in hosts:
            hosts.append(h)
    for p in (_MA_API_PORT, 8094):
        if p not in ports:
            ports.append(p)
    for h in hosts:
        for p in ports:
            try:
                with MaClient(h, p, token, timeout=4):
                    pass  # connect() sends the auth command and raises on failure
                _save_ma_conn(h, p, token)
                print(f"  MA · token validated on {h}:{p}", flush=True)
                return (h, p, token)
            except Exception:
                continue
    print("  MA · token found but no reachable host accepted it", flush=True)
    return None


def _ma_conn():
    """Connection MA commissioning uses. Persisted conn first; otherwise read the
    token HA already stored for its own integration, validate it against a
    reachable host, and persist. If an admin token has been handed to Core, use
    it (same host/port) so provider writes are permitted. Fully headless after
    the one-time token paste.

    Self-heals a STALE cache: HA rotates the music_assistant integration token on
    re-auth / add-on swap / integration re-add, but our /data/ma_conn.json keeps
    the old one — MA then rejects the auth handshake (error 23) on every call with
    no recovery until someone manually hits /music/connect. So compare the cached
    token against HA's current stored token; if it changed, re-validate + re-persist
    the fresh one before using it (a reachability check only runs when it differs,
    so the healthy path stays a cheap file compare)."""
    persisted = _load_ma_conn()
    base = persisted
    try:
        st = _ma_token_from_storage()
    except Exception:
        st = None
    # Entry missing or tokenless → merge the Supervisor discovery message
    # (the token's source of truth) exactly like /music/connect does.
    if not st or not st[1]:
        disc = _ma_from_discovery()
        if disc:
            st = ((st[0] if st else None) or disc[0],
                  (st[1] if st else None) or disc[1])
    if st:
        if not persisted:
            base = _ma_validate_and_persist(st[1], st[0])
        elif st[1] and st[1] != persisted[2]:
            refreshed = _ma_validate_and_persist(st[1], st[0])
            if refreshed:
                base = refreshed
    if not base:
        return None
    admin = _load_ma_admin_token()
    if admin:
        return (base[0], base[1], admin)
    return base


def reset_prepare():
    """Validate + take a recovery backup. Returns the baseline slug to restore."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        raise RuntimeError("Reset is only available when running as an HA add-on")
    backups = _sv("GET", "/backups").get("backups", [])
    baseline = next((b["slug"] for b in backups if b.get("name") == BASELINE_NAME), None)
    if not baseline:
        raise RuntimeError(f"No '{BASELINE_NAME}' backup found — cannot reset")
    ts = time.strftime("%Y%m%d-%H%M%S")
    _sv("POST", "/backups/new/partial",
        _enc({"name": f"pre-reset-{ts}", "homeassistant": True, "compressed": True}))
    return baseline


def reset_restore(baseline):
    """Restore the clean baseline (HA Core only). Core restarts; add-on is untouched."""
    info = _sv("GET", f"/backups/{baseline}/info")
    payload = {"homeassistant": True}
    if info.get("protected") and BACKUP_PASSWORD:
        payload["password"] = BACKUP_PASSWORD
    _sv("POST", f"/backups/{baseline}/restore/partial", payload, timeout=600)
    # The commissioning project is add-on /data — the HA restore doesn't touch it — so
    # clear it here, otherwise it survives the reset stale (rooms/entities are wiped).
    try:
        if project:
            project.clear()
    except Exception:
        pass


# ── Factory reset (to a brand-new install) ───────────────────────────────────
# Unlike reset_restore (which restores the empty-but-ONBOARDED baseline), this
# wipes HA's onboarded state so the box comes back up as a NEW install and
# onboarding re-fires. /homeassistant/www (the ProOS dashboards) and this add-on
# are deliberately KEPT, so pro.html is reachable the instant HA is back and the
# installer walks the full first-run flow (including onboarding). A recovery
# backup is taken first, so an accidental factory reset is still reversible.
_HA_CONFIG_DIR = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
# Onboarded-state files cleared from .storage — registries, auth, onboarding,
# and HA's own Lovelace. Everything else HA rebuilds fresh on boot.
_FACTORY_WIPE = [
    "onboarding", "auth", "auth_provider.homeassistant", "http.auth",
    "core.config_entries", "core.config_entries.proosbak",
    "core.device_registry", "core.entity_registry",
    "core.restore_state", "lovelace", "lovelace.map",
    "lovelace_dashboards", "lovelace_resources",
    # People + the helper collections that never hold a standard object. (input_text
    # and core.area_registry are handled by _FACTORY_FILTER instead, because they
    # DO hold standard objects we must leave in place — see below.)
    "person",
    "input_boolean", "input_button", "input_datetime", "input_number",
    "input_select", "counter", "timer",
    # Per-project content added since the original spec:
    #   image  — uploaded avatar / room / scene photos (index; files handled below)
    #   cloud  — the previous owner's Nabu Casa login must never survive a reset
    #   zone   — per-project zones (the home location itself lives in core.config)
    "image", "cloud", "zone",
]

# Standard objects are LEFT on a factory reset, not removed-and-regenerated. These
# live in .storage collections that also hold per-project objects, so instead of
# deleting the whole file we filter it: keep the standard entries, drop the rest.
#   - core.area_registry: keep areas labelled dashboard_system (the standing global
#     "Services" room); drop per-project rooms.
#   - input_text: keep the ProOS system helpers (proos_dashboard_* / proos_sys_*);
#     drop per-project text helpers.
DASHBOARD_SYSTEM_LABEL = "dashboard_system"


def _keep_system_area(a):
    return DASHBOARD_SYSTEM_LABEL in (a.get("labels") or [])


def _keep_system_input_text(it):
    oid = str(it.get("id") or "")
    return oid.startswith("proos_dashboard_") or oid.startswith("proos_sys_")


def _purge_nonsystem_areas(ws_call):
    """Belt-and-braces area cleanup, run LIVE over WebSocket once HA is back up after a
    factory reset. Deletes every area that isn't a ProOS system area (the dashboard_system
    label = the standing 'Services' room). The .storage-level area filter can be silently
    defeated — HA re-flushes the area registry from memory if it wasn't fully stopped when
    the file was edited, and a storage-schema change makes the filter a no-op — so the
    authoritative, race-free path is to delete on a RUNNING HA. Devices in a deleted area
    fall back to Unassigned, which is exactly the fresh-box state. Returns removed names."""
    removed = []
    try:
        areas = ws_call("config/area_registry/list") or []
    except Exception:
        return removed
    for a in (areas or []):
        aid = a.get("area_id")
        if not aid or DASHBOARD_SYSTEM_LABEL in (a.get("labels") or []):
            continue
        try:
            ws_call("config/area_registry/delete", area_id=aid)
            removed.append(a.get("name") or aid)
        except Exception:
            pass
    return removed


_FACTORY_FILTER = [
    ("core.area_registry", "areas", _keep_system_area),
    ("input_text", "items", _keep_system_input_text),
]

# Scenes, activities (scripts) and automations are NOT in .storage — HA's config
# editor / ProOS write them to the config-dir !include files. So a .storage-only
# wipe leaves them behind (previous scenes + activities survive a factory reset).
# Emptying them returns the box to a clean commissioned state. We write the
# structure each include expects (scripts = a dict, scenes/automations = a list)
# so HA's !include stays valid on the fresh boot. Kept, not deleted, so the
# include target always exists.
_FACTORY_CONFIG_RESET = {
    "scripts.yaml": "{}\n",
    "scenes.yaml": "[]\n",
    "automations.yaml": "[]\n",
    "known_devices.yaml": "",   # GPS trackers from location reports (device_tracker.see)
}


# Auth-related .storage files. On a TRUE factory reset these are wiped (owner +
# users reset, box re-onboards). For a TEST box, keep_auth skips them so existing
# users + long-lived tokens (e.g. the Home Assistant connector token used for
# verification) survive the reset — everything else is still wiped. Gated by the
# keep_auth arg or PROOS_FACTORY_KEEP_AUTH=1; never set on a shipped box.
_FACTORY_AUTH_FILES = {
    "onboarding", "auth", "auth_provider.homeassistant", "http.auth", "person",
}


def _install_id():
    """Stable per-commission identity, minted on first read and DELETED by
    factory reset (a fresh one is minted on the next read). Served on GET
    /navconfig. The dashboard and Pro compare it against the copy in their
    browser storage and drop their per-home localStorage (widget layouts,
    ignored discoveries, default room) when it changes — which is exactly and
    only the factory-reset boundary. A routine sign-out, reboot, or Core update
    never changes it. This closes the last known reset residue: client devices
    keyed layouts by area slug, and slugs repeat across homes (office is
    "office" in every commission), so the old home's layout resurrected."""
    p = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "install_id")
    try:
        with open(p, encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return v
    except Exception:  # noqa: BLE001 - missing/unreadable → mint below
        pass
    import uuid
    v = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(v)
    except Exception:  # noqa: BLE001 - unwritable data dir: still return a value
        pass
    return v


def factory_reset(clear_proos_data=False, keep_auth=False):
    """Wipe the box to a brand-new install. Safety-backup, stop Core, delete the
    onboarded state from .storage (keeping /www so pro.html survives), optionally
    clear ProOS Core's own credentials/provision, then start Core fresh.

    keep_auth (test only): don't wipe the auth/onboarding/person files, so existing
    logins + tokens survive. The home content is still fully wiped; the box just
    isn't re-onboarded. Off for any shipped reset."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        raise RuntimeError("Factory reset is only available when running as an HA add-on")
    keep_auth = bool(keep_auth) or os.environ.get("PROOS_FACTORY_KEEP_AUTH", "0") == "1"
    out = {"ok": True, "wiped": [], "errors": [], "keep_auth": keep_auth}
    ts = time.strftime("%Y%m%d-%H%M%S")
    _sv("POST", "/backups/new/partial",
        _enc({"name": f"pre-factory-{ts}", "homeassistant": True, "compressed": True}),
        timeout=600)
    out["recovery_backup"] = f"pre-factory-{ts}"
    # Blank the commissioning project (add-on /data — not touched by the .storage wipe).
    try:
        if project and project.clear():
            out["wiped"].append("proos_project")
    except Exception as e:  # noqa: BLE001
        out["errors"].append("proos_project: %s" % e)
    # The MUSIC SERVER's own data is a reset-residue class no HA-side wipe
    # reaches: streaming logins (the old home's Spotify refresh token) AND its
    # user table (stale usernames later COLLIDE with the fresh owner's ingress
    # auth — sqlite UNIQUE violation — killing every MA socket "mid-frame";
    # both observed live 2026-07-24). The only complete wipe is REINSTALLING
    # the add-on: uninstall deletes its /data, reinstall starts it factory-
    # fresh and discovery re-publishes. Falls back to removing provider
    # instances over the API if the Supervisor path fails. Best-effort either
    # way — a home without ProOS Music is a no-op.
    try:
        _ma_slug = None
        for _a in (_sv("GET", "/addons") or {}).get("addons", []) or []:
            if str(_a.get("slug", "")).endswith("_proos_music"):
                _ma_slug = _a["slug"]
                break
        if _ma_slug:
            _sv("POST", "/addons/%s/uninstall" % _ma_slug, timeout=300)
            _sv("POST", "/store/addons/%s/install" % _ma_slug, timeout=900)
            try:
                _sv("POST", "/addons/%s/start" % _ma_slug, timeout=300)
            except Exception:  # noqa: BLE001 - may auto-start on boot config
                pass
            out["wiped"].append("proos_music_reinstalled")
    except Exception as e:  # noqa: BLE001
        out["errors"].append("music reinstall: %s" % e)
        try:
            if _ma:
                _mp = _ma.wipe_providers()
                out["music_providers"] = _mp
                if _mp.get("removed"):
                    out["wiped"].append("music_providers")
        except Exception as e2:  # noqa: BLE001
            out["errors"].append("music providers: %s" % e2)
    # Wipe the awareness/recovery activity history too. It lives in the add-on's /data
    # (not .storage), so without this a fresh box shows stale faults from the old home.
    try:
        _audit = getattr(_watcher, "audit_path", "/data/watcher_audit.log") if _watcher else "/data/watcher_audit.log"
        if os.path.exists(_audit):
            os.remove(_audit)
            out["wiped"].append("watcher_audit")
    except Exception as e:  # noqa: BLE001
        out["errors"].append("watcher_audit: %s" % e)
    try:
        _sv("POST", "/core/stop", timeout=120)
    except Exception as e:  # noqa: BLE001
        out["errors"].append("core stop: %s" % e)
    storage = os.path.join(_HA_CONFIG_DIR, ".storage")
    for name in _FACTORY_WIPE:
        if keep_auth and name in _FACTORY_AUTH_FILES:
            out.setdefault("kept_for_test", []).append(name)
            continue
        p = os.path.join(storage, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                out["wiped"].append(name)
        except Exception as e:  # noqa: BLE001
            out["errors"].append("%s: %s" % (name, e))
    # Uploaded images (avatars, room and scene photos) are FILES under
    # <config>/image plus the .storage/image index wiped above — all per-project.
    try:
        _img_dir = os.path.join(_HA_CONFIG_DIR, "image")
        if os.path.isdir(_img_dir):
            shutil.rmtree(_img_dir)
            out["wiped"].append("image_files")
    except Exception as e:  # noqa: BLE001
        out["errors"].append("image_files: %s" % e)
    # Filter (don't delete) the collections that hold standard objects: keep the
    # standard entries in place, drop the per-project ones. Standard things are
    # never removed on a reset.
    for name, list_key, keep in _FACTORY_FILTER:
        p = os.path.join(storage, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            items = ((doc.get("data") or {}).get(list_key))
            if not isinstance(items, list):
                continue
            kept = [it for it in items if keep(it)]
            removed = len(items) - len(kept)
            doc["data"][list_key] = kept
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, p)
            out.setdefault("filtered", {})[name] = {"kept": len(kept), "removed": removed}
        except Exception as e:  # noqa: BLE001 - on any parse issue leave the file untouched
            out["errors"].append("filter %s: %s" % (name, e))
    # Clear the config-dir !include files (scenes / activities / automations),
    # which live outside .storage and would otherwise survive the wipe.
    for fname, empty in _FACTORY_CONFIG_RESET.items():
        p = os.path.join(_HA_CONFIG_DIR, fname)
        try:
            if os.path.exists(p):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(empty)
                out["wiped"].append(fname)
        except Exception as e:  # noqa: BLE001
            out["errors"].append("%s: %s" % (fname, e))
    # Per-home ProOS data that must die on EVERY factory reset (not only a sold
    # box): the dashboard nav layout/caps belong to the old home, and deleting
    # the install epoch mints a fresh id on the next /navconfig read — the
    # signal that tells every dashboard/Pro browser to drop its per-home
    # localStorage (widget layouts, ignored discoveries). See _install_id.
    _pdata = os.environ.get("PROOS_DATA_DIR", "/data")
    try:
        if _assist:
            _assist.clear_data()
            out["wiped"].append("assist_config")
        if _scenephotos:
            _scenephotos.clear()
        if _roomdevices:
            _roomdevices.clear()
    except Exception as e:  # noqa: BLE001
        out["errors"].append("assist: %s" % e)
    # ma_conn/ma_admin reference users the auth wipe is about to delete —
    # stale identities are exactly what made post-reset saves die mid-frame.
    # /music/connect self-heals the link from HA storage + discovery on the
    # next add, so dropping them costs one tap and removes the residue.
    for _fname in ("navconfig.json", "navcaps.json", "install_id",
                   "ma_conn.json", "ma_admin.json"):
        _p = os.path.join(_pdata, _fname)
        try:
            if os.path.exists(_p):
                os.remove(_p)
                out["wiped"].append("proos_" + _fname)
        except Exception as e:  # noqa: BLE001
            out["errors"].append("%s: %s" % (_fname, e))
    if clear_proos_data:
        import glob as _glob
        data_dir = os.environ.get("PROOS_DATA_DIR", "/data")
        for p in _glob.glob(os.path.join(data_dir, "*.json")):
            try:
                os.remove(p)
            except Exception:  # noqa: BLE001
                pass
        out["proos_data_cleared"] = True
    try:
        _sv("POST", "/core/start", timeout=120)
    except Exception as e:  # noqa: BLE001
        out["errors"].append("core start: %s" % e)
    # HA now comes back FRESH. ProCore itself did not restart, so its boot-time
    # auto_onboard won't fire — drive it here: wait for HA to be ready, then
    # create the Developer owner + Installer on the fresh box, so it never sits
    # at native HA onboarding. Idempotent + best-effort.
    if provision:
        import time as _t
        onboarded = False
        for _ in range(30):  # up to ~2.5 min for HA to come back
            _t.sleep(5)
            try:
                r = provision.auto_onboard(ws_call=_ws_call)
                if r.get("owner_created") or r.get("state") == "done":
                    out["auto_onboard"] = r
                    onboarded = True
                    break
                # 'unknown' → HA not ready yet, keep polling
            except Exception as e:  # noqa: BLE001
                out.setdefault("auto_onboard_errors", []).append(str(e))
        # We wiped the helper store, so regenerate ProOS's required dashboard
        # helpers now (auto_onboard doesn't) — just like a fresh install's boot.
        if onboarded:
            try:
                out["dashboard_helpers"] = provision.ensure_dashboard_helper(_ws_call)
            except Exception as e:  # noqa: BLE001
                out.setdefault("errors", []).append("dashboard helpers: %s" % e)
            # Recreate the standing global 'Services' room too (system default).
            try:
                out["services_area"] = provision.ensure_services_area(_ws_call)
            except Exception as e:  # noqa: BLE001
                out.setdefault("errors", []).append("services area: %s" % e)
            # Force-remove any non-system area LIVE (the .storage filter can be defeated by
            # an HA registry re-flush / schema change). Runs AFTER Services is ensured, so
            # only 'Services' (dashboard_system) survives — the true fresh-box baseline.
            try:
                out["areas_purged"] = _purge_nonsystem_areas(_ws_call)
            except Exception as e:  # noqa: BLE001
                out.setdefault("errors", []).append("area purge: %s" % e)
            # Reset the awareness watcher. ProCore did NOT restart, so the Watcher
            # still holds the old (now-wiped) device watches and would keep faulting
            # on devices that no longer exist — leaving the state aura stuck on red.
            # Re-derive reachability and re-discover the CURRENT watch list; force
            # set_watches even when empty so the old list is always cleared.
            try:
                apply_auto_reachability()
                w = discover_watches(client=_client) or []
                if _watcher is not None:
                    _watcher.set_watches(w, allow_empty=True)  # force-clear old watches
                    out["watches"] = len(w)
            except Exception as e:  # noqa: BLE001
                out.setdefault("errors", []).append("watches: %s" % e)
    out["ok"] = not out["errors"]
    return out


# ── Auto-backup scheduler ────────────────────────────────────────────────────
# ProOS owns the schedule (stdlib thread, no Core backup-integration dependency):
# a daily encrypted backup, pruned to a retention count, optionally copied
# off-device (to a /share folder the installer points at a NAS/USB).
def _auto_cfg():
    c = _cfg or {}
    return {
        "enabled": bool(c.get("auto_backup", False)),
        "time": str(c.get("auto_backup_time", "03:30")),
        "full": bool(c.get("auto_backup_full", True)),
        "keep": int(c.get("auto_backup_keep", 7)),
        "copy_to": (c.get("auto_backup_copy_to") or "").strip(),
        "encrypted": bool(BACKUP_PASSWORD),
    }


def _copy_offdevice(slug, name, copy_to):
    os.makedirs(copy_to, exist_ok=True)
    dst = os.path.join(copy_to, f"{name} [{slug}].tar")
    req = urllib.request.Request(SUPERVISOR + f"/backups/{slug}/download", method="GET")
    req.add_header("Authorization", "Bearer " + os.environ.get("SUPERVISOR_TOKEN", ""))
    with urllib.request.urlopen(req, timeout=600) as up, open(dst, "wb") as out:
        shutil.copyfileobj(up, out)


def _run_auto_backup(cfg):
    name = time.strftime("ProOS auto %Y-%m-%d")
    payload = _enc({"name": name, "compressed": True})
    if cfg["full"]:
        data = _sv("POST", "/backups/new/full", payload, timeout=900)
    else:
        payload["homeassistant"] = True
        data = _sv("POST", "/backups/new/partial", payload, timeout=900)
    slug = data.get("slug")
    if cfg["copy_to"] and slug:
        try:
            _copy_offdevice(slug, name, cfg["copy_to"])
        except Exception as e:
            print(f"  auto-backup: off-device copy failed: {e}")
    # retention: keep the N most-recent ProOS auto backups
    try:
        autos = sorted(
            [b for b in _sv("GET", "/backups").get("backups", [])
             if b.get("name", "").startswith("ProOS auto")],
            key=lambda b: b.get("date", ""), reverse=True)
        for b in autos[cfg["keep"]:]:
            try:
                _sv("DELETE", f"/backups/{b['slug']}")
            except Exception:
                pass
    except Exception as e:
        print(f"  auto-backup: prune failed: {e}")


_last_auto_date = None


# Keep the awareness watch list in step with HA without a restart. Boot and the
# post-commission /reachability/refresh already re-discover; this is the periodic
# safety net so the list never drifts if a device is added or deleted outside
# those paths. Set PROOS_WATCH_REDISCOVER_SEC=0 to disable.
_WATCH_REDISCOVER_SEC = int(os.environ.get("PROOS_WATCH_REDISCOVER_SEC", "300") or "300")



# ── Android TV passive app learner ───────────────────────────────────────────
# Android TV boxes can't be asked what's installed (no API without ADB), but the
# integration DOES report the package id of whatever is open right now. So ProOS
# learns by watching: every time someone opens an app on a Shield, we record it
# into that box's app list, which is what the integration publishes as
# source_list. Normal viewing fills the list in; nobody sets anything up, and an
# app that's been opened is proof it's installed — so a launch can never fail
# with "item not found". Set PROOS_ATV_LEARN=0 to disable.
_ATV_LEARN = (os.environ.get("PROOS_ATV_LEARN", "1") or "1") != "0"
# entry_id -> does this box's driver actually keep an app list we push to it?
# False means "learn into our own ledger only" — everything still works.
_ATV_PUSH = {}
_ATV_LEARN_SEC = int(os.environ.get("PROOS_ATV_LEARN_SEC", "20") or "20")
# Launcher / system surfaces are not user apps.
_ATV_SKIP = re.compile(
    r"(tvlauncher|leanbacklauncher|tungsten|katniss|backdrop|tvrecommendations|"
    r"^com\.android\.|^android$|inputmethod|gsf$|\.tts$|packageinstaller|settings)", re.I)


# Package -> proper product name. NAMING ONLY: whether an app exists always
# comes from the device. Unknown packages fall back to a tidy guess.
_ATV_NAMES = {
    "com.netflix.ninja": "Netflix",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "au.com.foxtel.play": "Foxtel",
    "au.com.foxtel.now": "Foxtel Now",
    "com.wbd.stream": "HBO Max",
    "com.hbo.hbonow": "HBO Max",
    "au.com.streamotion.binge": "BINGE",
    "au.com.kayosports.kayoapp": "Kayo",
    "au.com.streamotion.ares": "Kayo",
    "au.com.stan.and.tv": "Stan",
    "au.com.stan.and": "Stan",
    "au.net.abc.iview": "ABC iview",
    "au.com.mi9.jumpin.tv": "9Now",
    "au.com.ninemsn.tv": "9Now",
    "au.com.seven.inferno": "7plus",
    "au.com.seventwo.android": "7plus",
    "au.com.sbs.ondemand.tv": "SBS On Demand",
    "au.com.network.ten.tenplay": "10 play",
    "com.plexapp.android": "Plex",
    "com.spotify.tv.android": "Spotify",
    "com.paramount.android.pplus": "Paramount+",
    "com.tubitv": "Tubi",
    "tv.twitch.android.app": "Twitch",
    "com.formulaone.production": "F1 TV",
    "com.ubnt.unifi.protect": "UniFi Protect",
    "org.xbmc.kodi": "Kodi",
    "com.android.vending": "Play Store",
    "com.nvidia.tegrazone3": "NVIDIA Games",
}
# Segments that carry no brand meaning — dropped when guessing a name.
_ATV_NOISE = {"com", "au", "net", "org", "tv", "android", "app", "apps", "and",
              "live", "livingroom", "play", "ninja", "stream", "production",
              "mobile", "client", "player", "main", "inferno", "ares"}


def _atv_pretty(pkg, app_name):
    """Readable name: curated map, else the integration's own label when it's a
    real one, else the most brand-like segment of the package id."""
    if pkg in _ATV_NAMES:
        return _ATV_NAMES[pkg]
    nm = (app_name or "").strip()
    if nm and nm != pkg and "." not in nm:
        return nm
    parts = [p for p in pkg.split(".") if p and p.lower() not in _ATV_NOISE]
    if not parts:
        parts = pkg.split(".")
    # Prefer the LAST meaningful segment, unless the one before it is longer and
    # more distinctive (abc.iview -> "ABC iview", foxtel.play -> "Foxtel").
    word = parts[-1]
    if len(parts) >= 2 and len(parts[-2]) > len(word):
        word = parts[-2]
    return word.replace("_", " ").title()


def _atv_copy_apps(from_entity, to_entities):
    """Copy one box's learned app list onto other Android TV boxes.

    Learning by watching is honest but slow, and a household's boxes are nearly
    always built the same. Copying is still EVIDENCE — an installer asserting
    "these boxes are identical" — rather than a guess, so it stays inside the
    rule that nothing is offered unless someone real vouched for it. The
    installer owns the claim; ProOS doesn't make it on its own."""
    src_entry, _ = _atv_entry_for(from_entity)
    if not src_entry:
        return {"error": "that source isn't an Android TV box"}
    apps = dict(_atv_ledger().get(src_entry) or {})
    if not apps:
        return {"error": "that box has no apps to copy yet"}
    copied, skipped = [], []
    for eid in (to_entities or []):
        if eid == from_entity:
            continue
        entry, _ = _atv_entry_for(eid)
        if not entry:
            skipped.append({"entity_id": eid, "why": "not an Android TV box"})
            continue
        _atv_ledger_put(entry, apps)
        _ATV_PUSH.pop(entry, None)            # let it try the driver once
        copied.append(eid)
    print("  [androidtv] copied %d apps from %s to %d box(es)"
          % (len(apps), from_entity, len(copied)), flush=True)
    return {"ok": True, "count": len(apps), "copied": copied, "skipped": skipped}


def _atv_boxes():
    """Every Android TV media player, with how many apps it knows.

    Names come from the live state's friendly_name — the registry's own name
    fields are frequently empty, which left an installer picking between raw
    entity_ids."""
    out = []
    try:
        led = _atv_ledger()
        for e in (_client.entity_registry() or []):
            eid = e.get("entity_id") or ""
            if not eid.startswith("media_player.") or e.get("platform") not in _ATV_DOMAINS:
                continue
            entry = e.get("config_entry_id") or ""
            name = (e.get("name") or e.get("original_name") or "").strip()
            if not name:
                try:
                    st = _client._req("GET", "/api/states/%s" % eid) or {}
                    name = ((st.get("attributes") or {}).get("friendly_name") or "").strip()
                except Exception:
                    name = ""
            if not name:
                name = eid.split(".", 1)[-1].replace("_", " ").title()
            out.append({"entity_id": eid, "entry_id": entry, "name": name,
                        "apps": len(led.get(entry) or {})})
    except Exception:
        pass
    return sorted(out, key=lambda r: r["name"].lower())


def _site_app_names():
    """Every app name any media player in this home reports, from any platform.

    Apple TV and Samsung publish a source_list; Android boxes have no such API
    so ProOS's own ledger stands in. One list, so ONE tile pack covers every
    device — which is the whole point of keeping artwork central."""
    names = set()
    try:
        for st in (_client._req("GET", "/api/states") or []):
            eid = st.get("entity_id") or ""
            if not eid.startswith("media_player."):
                continue
            for nm in ((st.get("attributes") or {}).get("source_list") or []):
                if isinstance(nm, str) and nm.strip():
                    names.add(nm.strip())
    except Exception:
        pass
    try:
        for apps in _atv_ledger().values():
            for nm in (apps or {}).values():
                if isinstance(nm, str) and nm.strip():
                    names.add(nm.strip())
    except Exception:
        pass
    return sorted(names)


def _atv_learn_pass():
    """One sweep per Android TV media_player.

    ProOS's ledger is the master list of what a box has been taught; the
    entity's live source_list is what the integration is actually publishing.
    When they disagree, push the ledger. So a capture, a lost list and a
    half-applied list are all the same repair, and it keeps re-trying until the
    device really is publishing every app."""
    try:
        ents = [e for e in (_client.entity_registry() or [])
                if (e.get("entity_id") or "").startswith("media_player.")
                and e.get("platform") in _ATV_DOMAINS]
    except Exception as ex:  # noqa: BLE001
        print("  [androidtv] cannot read the entity registry: %s" % ex, flush=True)
        return
    ledger = _atv_ledger()
    for e in ents:
        eid = e.get("entity_id")
        try:
            entry_id, published = _atv_entry_for(eid)
            if not entry_id:
                continue
            want = dict(ledger.get(entry_id) or {})
            # Capture whatever is on screen right now — an app that's open is
            # proof it's installed, which is the only reliable source we have.
            st = _client._req("GET", "/api/states/%s" % eid) or {}
            at = st.get("attributes") or {}
            pkg = (at.get("app_id") or "").strip()
            if pkg and "." in pkg and not _ATV_SKIP.search(pkg) and pkg not in want:
                want[pkg] = _atv_pretty(pkg, at.get("app_name"))
                _atv_ledger_put(entry_id, {pkg: want[pkg]})
                ledger.setdefault(entry_id, {})[pkg] = want[pkg]
                print("  [androidtv] learned %s (%s) on %s"
                      % (want[pkg], pkg, eid), flush=True)
            if not want or _ATV_PUSH.get(entry_id) is False:
                continue      # learned and stored; this box's driver won't hold it
            missing = [n for n in want.values() if n not in published]
            if not missing:
                continue
            res = _atv_write_apps(eid, [{"id": k, "name": v} for k, v in want.items()])
            if res.get("driver") == "ok":
                _ATV_PUSH[entry_id] = True
            else:
                # Pushing into the integration is a nice-to-have: it makes the
                # apps appear in HA's own UI too. ProOS doesn't need it — the
                # ledger drives the dashboard and launching goes by package id.
                # So note it once and stop hammering HA every pass.
                _ATV_PUSH[entry_id] = False
                print("  [androidtv] %s: driver won't hold an app list — ProOS "
                      "is using its own, apps work normally" % eid, flush=True)
        except Exception as ex:  # noqa: BLE001
            print("  [androidtv] %s: learn pass failed — %s" % (eid, ex), flush=True)
            continue


def _atv_learn_loop():
    if not _ATV_LEARN:
        return
    import time as _t
    _t.sleep(45)          # let HA settle after boot
    while True:
        try:
            _atv_learn_pass()
        except Exception as ex:  # never let the loop die
            print("  [androidtv] learn error: %s" % ex, flush=True)
        _t.sleep(_ATV_LEARN_SEC)


def _watch_rediscover_loop():
    """Every interval: re-derive reachability and re-discover the current always-on
    devices, so newly-added devices start being watched and deleted ones drop off
    (no stale red aura). Uses the default set_watches — it ignores an empty result
    so a transient HA hiccup can't wipe live watches; a genuine full wipe is left
    to the factory reset / boot."""
    if _WATCH_REDISCOVER_SEC <= 0:
        return
    import time as _t
    while True:
        _t.sleep(_WATCH_REDISCOVER_SEC)
        try:
            apply_auto_reachability()
            w = discover_watches(client=_client)   # raises on hard failure -> caught below, list kept
            if _watcher is not None:
                before = len(_watcher.watches)
                # allow_empty=True so removing the last device actually clears the list.
                # discover_watches returns [] ONLY when the registries genuinely have nothing:
                # load_registries falls back to on-disk .storage if the live read hiccups, so an
                # empty result is authoritative, never a transient blip that could wipe watches.
                _watcher.set_watches(w, allow_empty=True)
                if len(w) != before:
                    print(f"  watches · self-heal {before} -> {len(w)} device(s)", flush=True)
        except Exception as e:  # never let the loop die
            print(f"  watches · self-heal error: {e}", flush=True)
        # Re-provision room activities too, so a device added between reboots —
        # especially a slow-registering one like an Apple TV, whose entities appear
        # a beat after a fast TV's — gets its activity without a restart. sync_all is
        # create-if-absent, so this only ever fills gaps and never clobbers edits.
        try:
            _sres = sync.sync_all(_client)
            _created = (_sres.get("totals") or {}).get("created", 0)
            if _created:
                print(f"  sync · self-heal created {_created} activity script(s)", flush=True)
        except Exception as e:
            print(f"  sync · self-heal error: {e}", flush=True)


_QUARANTINE_SEC = int(os.environ.get("PROOS_QUARANTINE_SEC", "45") or "45")


def _quarantine_loop():
    """Periodically clear HA's auto‑stamped room off any unplaced AV device (see
    project.quarantine_auto_rooms), so a device added/re‑paired while Pro is closed still
    lands in Unassigned within a scan instead of showing up in a room. Honours the
    auto_room_quarantine option live (re‑read each pass) and never lets the loop die."""
    if _QUARANTINE_SEC <= 0:
        return
    import time as _t
    while True:
        _t.sleep(_QUARANTINE_SEC)
        try:
            if project is not None and _opt("auto_room_quarantine", True):
                res = project.quarantine_auto_rooms(_client)
                cl = (res or {}).get("cleared") or []
                if cl:
                    print(f"  no-auto-room · cleared {len(cl)} device(s) to Unassigned", flush=True)
        except Exception as e:
            print(f"  no-auto-room · loop error: {e}", flush=True)


def _auto_backup_loop():
    # Re-reads config every minute so in-app edits apply live (no restart). The
    # per-day guard means it fires once on/after the scheduled minute each day.
    global _last_auto_date
    while True:
        cfg = _auto_cfg()
        if cfg["enabled"]:
            now = datetime.datetime.now()
            try:
                hh, mm = [int(x) for x in cfg["time"].split(":")]
            except Exception:
                hh, mm = 3, 30
            today = now.date().isoformat()
            scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= scheduled and _last_auto_date != today:
                try:
                    print(f"  auto-backup running ({'full' if cfg['full'] else 'config'})…")
                    _run_auto_backup(cfg)
                    print("  auto-backup done")
                except Exception as e:
                    print(f"  auto-backup failed: {e}")
                _last_auto_date = today  # once per day, even on failure
        time.sleep(60)


def _ma_credential_status() -> dict:
    """Present Music Assistant's existing (un-migrated) credentials in the
    unified masked shape, so the central /credentials view is complete without
    moving MA off its own /data files yet. Never returns the raw token."""
    conn = _load_ma_conn()
    admin_tok = _load_ma_admin_token()
    admin_user = _load_ma_admin_user()
    tok = conn[2] if conn else None
    if conn:
        kind = "long_lived" if admin_tok else "ha_discovery"
    elif admin_tok:
        kind = "long_lived"
    else:
        kind = "admin_user" if admin_user else None
    return {
        "service": "music_assistant",
        "name": "Music Assistant",
        "set": bool(conn),
        "kind": kind,
        "host": conn[0] if conn else None,
        "admin": bool(admin_tok or admin_user),
        "last4": _last4(tok) if tok else None,
        "managed_by": "music",  # writes go through the /music routes, not /credentials
    }


def _last4(tok):
    if not tok:
        return None
    return tok[-4:] if len(tok) >= 4 else "*" * len(tok)


# ── Push (APNs) ──────────────────────────────────────────────────────────────
# The account-wide Apple .p8 key lives in the credential store under "apns":
# the PEM is the token; key_id/team_id/topic/env ride in meta. Per-device tokens
# are never stored -- they arrive inside each notification HA POSTs to /push.
def _apns_cred():
    p8 = _creds.get("apns")
    if not p8:
        return None
    m = _creds.meta("apns")
    return {"p8": p8, "key_id": m.get("key_id"), "team_id": m.get("team_id"),
            "topic": m.get("topic"), "env": m.get("env", "production")}


def _apns_config_status():
    p8 = _creds.get("apns")
    m = _creds.meta("apns")
    return {"set": bool(p8), "key_id": m.get("key_id"), "topic": m.get("topic"),
            "env": m.get("env", "production"), "team_id": m.get("team_id"),
            "key_last4": _last4(m.get("key_id"))}


# Match the official service's contract: cap per device token, answer 429 past it.
_PUSH_MAX_PER_DAY = 500
_push_counts: dict = {}
_push_lock = threading.Lock()


def _push_rate_ok(token: str) -> bool:
    now = time.time()
    with _push_lock:
        n, reset = _push_counts.get(token, (0, now + 86400))
        if now >= reset:
            n, reset = 0, now + 86400
        if n >= _PUSH_MAX_PER_DAY:
            _push_counts[token] = (n, reset)
            return False
        _push_counts[token] = (n + 1, reset)
        return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self):
        """Server-Sent Events: the live wire behind multi-device sync.
        Sends journal events, incident count changes and presence as they
        happen, with a 15s heartbeat so proxies don't reap the socket."""
        if _journal_mod is None:
            return self._send(503, {"error": "journal unavailable"})
        q = _journal_mod.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hello = {"kind": "hello", "ts": time.time(),
                     "devices": _journal_mod.presence_list()}
            if _healthmon_mod is not None:
                hello["incidents"] = len(_healthmon_mod.incidents())
            self.wfile.write(("data: %s\n\n" % json.dumps(hello)).encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % json.dumps(msg)).encode())
                except queue.Empty:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except Exception:
            pass          # client went away — normal
        finally:
            _journal_mod.unsubscribe(q)

    def _send_html(self, code: int, html: str):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight. Must be a CLEAN response: the CORS headers and NO body. The old
        # _send(204, {}) wrote a 2-byte "{}" body on a 204 — malformed (204 = No Content),
        # which Safari rejects, killing the whole preflight. That blocked every
        # authenticated / POST call to :8770 on Safari (the Pro/Developer login, etc.)
        # while Chrome tolerated it. 200 + explicit CORS headers + Max-Age is what Safari
        # accepts.
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass  # quiet

    def _room(self, parts):
        # parts like ['rooms', '<area>', '<verb>']
        return unquote(parts[1])

    def _auth(self):
        self._user = None
        if auth is None:
            return True
        try:
            tok = auth.bearer(self.headers)
            # EventSource cannot set headers — the live stream (and only the
            # live stream) may carry its token as ?token=.
            if not tok and self.path.split("?")[0].strip("/") == "events/stream":
                tok = (parse_qs(urlparse(self.path).query).get("token")
                       or [""])[0] or None
            self._user = auth.verify(tok) if tok else None
        except Exception:
            self._user = None
        if auth.REQUIRE and self._user is None:
            path = self.path.split("?")[0].strip("/")
            # App tile artwork is public brand imagery loaded by <img>/CSS.
            if path.startswith("apps/art/tile/"):
                return True
            if path not in auth.PUBLIC_PATHS:
                self._send(401, {"error": "authentication required"})
                return False
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode()) if raw else {}
        except Exception:
            return {}

    def _stream_download(self, slug):
        # Proxy the Supervisor backup tar straight to the browser so an installer
        # can keep their own copy. Streamed in chunks -- never buffered in memory.
        if not os.environ.get("SUPERVISOR_TOKEN"):
            return self._send(503, {"error": "Backups need the HA add-on"})
        req = urllib.request.Request(SUPERVISOR + f"/backups/{slug}/download", method="GET")
        req.add_header("Authorization", "Bearer " + os.environ.get("SUPERVISOR_TOKEN", ""))
        try:
            up = urllib.request.urlopen(req, timeout=600)
        except Exception as e:
            return self._send(502, {"error": str(e)})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Disposition", f'attachment; filename="{slug}.tar"')
            self.send_header("Access-Control-Allow-Origin", "*")
            cl = up.headers.get("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            self.end_headers()
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()

    def _send_bytes(self, code: int, ctype: str, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _unifi(self, parts, method, body=None):
        qs = parse_qs(urlparse(self.path).query)
        try:
            from proos import unifi as _u
        except Exception as e:
            return self._send(500, {"errorMessage": f"unifi module unavailable: {e}"})
        status, ctype, payload = _u.handle(method, parts[1:], qs, body, _creds)
        if isinstance(payload, (bytes, bytearray)):
            return self._send_bytes(status, ctype, bytes(payload))
        return self._send(status, payload)

    def do_GET(self):
        if not self._auth():
            return
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            if parts == ["rooms", "art", "styles"]:
                # curated backgrounds offered in Edit Room, matched to the
                # room's kind first, then the general pool
                if _roomart is None:
                    return self._send(503, {"error": "roomart unavailable"})
                q = parse_qs(urlparse(self.path).query)
                nm = (q.get("name") or [""])[0]
                return self._send(200, {"styles": _roomart.variants(nm or "room"),
                                        "can_generate": bool(
                                            _assist and _assist._image_key(None))})
            if parts == ["rooms", "identity"]:
                # Name-derived identity for every area: the icon that stands for
                # this room everywhere, plus the curated photo used when no
                # generated/uploaded picture exists. One mapping, one truth.
                if _roomart is None:
                    return self._send(503, {"error": "roomart unavailable"})
                out = {}
                try:
                    used = set()
                    # stable order so the same room keeps the same photo across
                    # restarts; each room takes the first variant not yet taken
                    areas = sorted((_client.area_registry() or []),
                                   key=lambda x: str(x.get("area_id") or ""))
                    for a in areas:
                        aid = a.get("area_id")
                        if not aid:
                            continue
                        nm = a.get("name") or aid
                        d = _roomart.describe(nm)
                        pick = None
                        for v in _roomart.variants(nm):
                            if v not in used:
                                pick = v
                                break
                        pick = pick or d["fallback_photo"]
                        used.add(pick)
                        d["fallback_photo"] = pick
                        d["has_picture"] = bool(a.get("picture"))
                        d["icon_set"] = a.get("icon") or ""     # installer override
                        out[aid] = d
                except Exception as e:  # noqa: BLE001
                    return self._send(502, {"error": str(e)})
                return self._send(200, {"rooms": out,
                                        "icon_choices": _roomart.ICON_CHOICES,
                                        "can_generate": bool(
                                            _assist and _assist._image_key(None))})
            if parts == ["assist", "registry"]:
                # Assist capability registry — what the copilot can SEE and DO,
                # visible to the installer. Trust through visible capability.
                # last_used stamped from the service journal (the audit trail).
                reg = {
                    "read": [
                        {"id": "read_verdicts", "label": "Room verdicts + evidence"},
                        {"id": "read_entities", "label": "Entity states & attributes"},
                        {"id": "read_history", "label": "Entity history"},
                        {"id": "read_journal", "label": "Room event journal"},
                        {"id": "read_incidents", "label": "Health incidents"},
                        {"id": "read_corelog", "label": "Core add-on log"},
                        {"id": "read_project", "label": "Committed project record"},
                        {"id": "read_network", "label": "Network witnesses"}],
                    "act": [
                        {"id": "act_reload", "label": "Reload an integration"},
                        {"id": "act_activity", "label": "Run a room activity"},
                        {"id": "act_recommit", "label": "Open guided recommit"},
                        {"id": "act_witness", "label": "Bind a traffic witness"},
                        {"id": "act_restart", "label": "Restart an add-on"}],
                    "audit": [
                        {"id": "audit_service", "label": "Service record (every Act, stamped)"}]}
                if _journal_mod is not None:
                    last = {}
                    for ev in _journal_mod.read("service", 500):
                        t = (ev.get("data") or {}).get("tool")
                        if t and t not in last:
                            last[t] = ev.get("ts")
                    for grp in reg.values():
                        for tool in grp:
                            if tool["id"] in last:
                                tool["last_used"] = last[tool["id"]]
                return self._send(200, reg)
            if parts == ["assist", "audit"]:
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, {"events": _journal_mod.read(
                    "service", limit=(q.get("limit") or ["100"])[0])})
            if parts == ["events", "stream"]:
                # Live bus: journal events + incident updates + presence, as
                # Server-Sent Events. One connection per open Pro client is
                # what keeps laptop + iPad + phone views identical.
                return self._sse_stream()
            if parts == ["health", "incidents"]:
                if _healthmon_mod is None:
                    return self._send(503, {"error": "healthmon unavailable"})
                return self._send(200, {"incidents": _healthmon_mod.incidents(),
                                        "ts": time.time()})
            if parts == ["presence"]:
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                return self._send(200, {"devices": _journal_mod.presence_list()})
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "journal":
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, {
                    "room": parts[1],
                    "events": _journal_mod.read(
                        parts[1],
                        limit=(q.get("limit") or ["200"])[0],
                        since=(q.get("since") or [None])[0])})
            if parts == ["journal", "rooms"]:
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                return self._send(200, {"rooms": _journal_mod.rooms()})
            if parts == ["commission", "options"]:
                # Zero-free-text commissioning: serve each room's pickers from
                # the devices' OWN reported facts (display source_list, AVR
                # source_list, live source states). Nothing typed, nothing
                # guessed — confirm, don't assume.
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                room = (q.get("room") or [""])[0]
                if not room:
                    return self._send(400, {"error": "room required"})
                try:
                    ctrl = get_controller(room)
                except Exception as e:
                    return self._send(404, {"error": "unknown room: %s" % e})
                acts = getattr(ctrl, "activities", None) or {}
                disp_eid, avr_eids, sources = None, [], []
                for a in acts.values():
                    for t in (getattr(a, "targets", None) or []):
                        disp_eid = disp_eid or getattr(t, "entity_id", None)
                    aw = getattr(a, "audio_witness", None) or {}
                    if aw.get("entity") and aw["entity"] not in avr_eids:
                        avr_eids.append(aw["entity"])
                    se = getattr(a, "source_eid", None)
                    if se:
                        sources.append({
                            "key": a.key, "label": getattr(a, "label", a.key),
                            "entity": se,
                            "provisional": bool(getattr(a, "provisional", False)),
                            "route": dict(getattr(a, "route", None) or {}),
                            "audio_witness": dict(aw)})

                def _ent(eid):
                    if not eid:
                        return None
                    try:
                        r = _client._req("GET", "/api/states/%s" % eid) or {}
                    except Exception:
                        r = {}
                    at = r.get("attributes") or {}
                    return {"entity": eid, "state": r.get("state"),
                            "friendly_name": at.get("friendly_name"),
                            "source": at.get("source"),
                            "source_list": at.get("source_list") or []}
                for s in sources:
                    st = _ent(s["entity"]) or {}
                    s["state"] = st.get("state")
                    s["friendly_name"] = st.get("friendly_name")
                return self._send(200, {
                    "room": room,
                    "display": _ent(disp_eid),
                    "avrs": [_ent(e) for e in avr_eids],
                    "sources": sources})
            if parts == ["net", "awareness"]:
                # Network-evidence readiness: certified providers observed in
                # the entity registry, witness coverage per committed source,
                # graceful-degradation note when no provider exists.
                if _netev_mod is None:
                    return self._send(503, {"error": "netevidence unavailable"})
                return self._send(200, _netev_mod.inspect(
                    _client, project, str(_opt("traffic_witnesses", "") or "")))
            if parts == ["unifi", "status"]:
                # One certified status merging native (cameras/integration loaded)
                # with private (API key valid, event search / clip export).
                if not _unifi_layer:
                    return self._send(503, {"error": "UniFi layer not started"})
                pc = unifi._get_client(_creds)   # shared client → ONE reused login session; a fresh client per poll re-logs-in every 3s and trips the console 429 (status light flicker)
                return self._send(200, unifi.unified_status(pc, _unifi_layer))
            if parts == ["unifi", "curation"]:
                # Exposure model: live cameras merged with the installer's curation.
                return self._send(200, unifi.curation_view(unifi._get_client(_creds)))
            if parts and parts[0] == "logs":
                # Full log access for the tech tier — core / supervisor / host /
                # any add-on, via the Supervisor token. Same gate as the terminal.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                from proos import logs as _logs
                if parts == ["logs", "targets"]:
                    return self._send(200, _logs.targets())
                if len(parts) >= 3 and parts[1] == "addon":
                    return self._send(200, _logs.fetch("addon", slug=parts[2]))
                if len(parts) == 2:
                    return self._send(200, _logs.fetch(parts[1]))
                return self._send(404, {"error": "unknown log route"})
            if parts == ["sysadmin", "health"]:
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                from proos import sysadmin as _sa
                return self._send(200, _sa.health())
            if parts and parts[0] == "unifi" and (len(parts) < 2 or parts[1] not in ("net", "poe")):
                return self._unifi(parts, "GET")
            if parts == ["health"]:
                return self._send(200, {"ok": True, "home_id": _client.home_id, "state_version": _state_version})
            if parts == ["events"]:
                qs = parse_qs(urlparse(self.path).query)
                device = (qs.get("device", [""])[0]).strip() or ("anon-%x" % int(time.time() * 1000))
                name = (qs.get("name", [""])[0]).strip()
                with _dash_lock:
                    c = _dash_get(device, name)
                    c["last_seen"] = time.time()
                    q = c["q"]
                    cur = _dash_pub_version
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    self.wfile.write(("event: hello\ndata: %d\n\n" % cur).encode())
                    self.wfile.flush()
                    while True:
                        try:
                            v = q.get(timeout=20)
                            self.wfile.write(("event: refresh\ndata: %d\n\n" % v).encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        with _dash_lock:
                            cc = _dash_clients.get(device)
                            if cc is not None:
                                cc["last_seen"] = time.time()
                except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                    pass
                return
            if parts == ["dashboard", "clients"]:
                return self._send(200, _dash_snapshot())
            if parts == ["unifi", "poe", "ports"]:
                return self._send(200, {"ports": _unifi_poe_ports()})
            if parts == ["unifi", "net", "status"]:
                return self._send(200, _unifinet.status())
            if parts == ["unifi", "poe", "detect"]:
                ent = (parse_qs(urlparse(self.path).query).get("entity", [""])[0]).strip()
                if not ent:
                    return self._send(400, {"error": "entity required"})
                try:
                    sw = _unifinet_mod.suggest_poe_switch(_unifinet, _client, ent)
                    return self._send(200, {"entity": ent, "switch": sw})
                except Exception as e:
                    return self._send(200, {"entity": ent, "switch": None, "error": str(e)})
            if parts == ["device_commands"]:
                ent = (parse_qs(urlparse(self.path).query).get("entity", [""])[0]).strip()
                if not ent:
                    return self._send(400, {"error": "entity required"})
                return self._send(200, _commands.device_commands(_client, ent))
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "activities":
                area = self._room(parts)
                ctrl = get_controller(area)
                # ONE list of truth = the room's STORED activity scripts (the exact same list
                # Pro shows). We overlay the controller's LIVE verdicts (keyed by script
                # entity_id) and hide the Display On helper (a Pro-only building block). A
                # room with no stored scripts yet (un-committed) falls back to the
                # controller's provisional discovery list so nothing breaks pre-commit.
                stored = []
                try:
                    if project:
                        stored = (project.activities_status(_client, project.load(), area).get("activities")) or []
                except Exception:
                    stored = []
                if stored:
                    vmap = {}
                    try:
                        for _a in ctrl.list_activities():
                            if _a.get("script"):
                                vmap[_a["script"]] = _a.get("verdict", "")
                    except Exception:
                        pass
                    acts = [{"key": a.get("key"), "label": a.get("label"),
                             "verdict": vmap.get(a.get("entity_id"), ""), "provisional": False,
                             "script": a.get("entity_id"), "source_eid": a.get("source_eid")}
                            for a in stored if a.get("kind") != "display_on"]
                else:
                    acts = ctrl.list_activities()
                return self._send(200, {"area": ctrl.area, "activities": acts})
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "fire_plan":
                # SINGLE SOURCE OF TRUTH for firing an activity: return the exact ordered
                # call_service steps (cancel the room's OTHER activities, then turn on the
                # target). The dashboard AND Pro's test-fire both execute THIS verbatim, so
                # they can never drift. Lean -- no live-state snapshot.
                area = self._room(parts)
                ctrl = get_controller(area)
                target = (parse_qs(urlparse(self.path).query).get("script", [""])[0]).strip()
                if not target:
                    return self._send(400, {"error": "script (target entity_id) required"})
                steps = None
                try:
                    if project:
                        steps = project.room_fire_plan(_client, project.load(), area, target)
                except Exception:
                    steps = None
                if steps is None:
                    steps = ctrl.fire_plan(target)
                return self._send(200, {"area": ctrl.area, "steps": steps})
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "status":
                ctrl = get_controller(self._room(parts))
                return self._send(200, ctrl.status())
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "health":
                ctrl = get_controller(self._room(parts))
                return self._send(200, check_room(ctrl).to_dict())
            if parts == ["monitor"]:
                return self._send(200, {"rooms": _monitor.all() if _monitor else {}})
            if parts == ["watchers"]:
                return self._send(200, _watcher.report() if _watcher else
                                   {"status": "ok", "summary": "Watcher not started", "items": []})
            if parts == ["watchers", "recovery"]:
                # Current installer-assigned recovery overrides (smart-plug power-cycle
                # per device). pro.html reads this to render the recovery config UI.
                return self._send(200, {"overrides": _recovery_cfg_read()})
            if parts == ["watchers", "audit"]:
                # Full awareness/recovery history: every fault, resolve, recovery
                # attempt and outcome. Tech-gated.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                path = getattr(_watcher, "audit_path", "/data/watcher_audit.log") if _watcher else "/data/watcher_audit.log"
                lines = []
                try:
                    with open(path, encoding="utf-8") as fh:
                        rows = fh.readlines()[-300:]        # last 300 events
                    for ln in rows:
                        parts_ln = ln.rstrip("\n").split("\t")
                        if len(parts_ln) >= 3:
                            lines.append({"time": parts_ln[0], "entity": parts_ln[1],
                                          "event": parts_ln[2],
                                          "detail": parts_ln[3] if len(parts_ln) > 3 else ""})
                except FileNotFoundError:
                    pass
                except Exception as e:
                    return self._send(200, {"events": [], "error": str(e)})
                lines.reverse()                              # newest first
                return self._send(200, {"events": lines})
            if parts == ["watchers", "history"]:
                # Homeowner-safe recovery history: the SAME events as /watchers/audit,
                # but readable by any authenticated resident (the homeowner dashboard
                # renders only the "handled" story, in plain language). require_auth
                # still applies, so tokenless callers are rejected; /watchers/audit
                # stays tech-gated for the raw installer/tech surface.
                path = getattr(_watcher, "audit_path", "/data/watcher_audit.log") if _watcher else "/data/watcher_audit.log"
                lines = []
                try:
                    with open(path, encoding="utf-8") as fh:
                        rows = fh.readlines()[-300:]        # last 300 events
                    for ln in rows:
                        parts_ln = ln.rstrip("\n").split("\t")
                        if len(parts_ln) >= 3:
                            lines.append({"time": parts_ln[0], "entity": parts_ln[1],
                                          "event": parts_ln[2],
                                          "detail": parts_ln[3] if len(parts_ln) > 3 else ""})
                except FileNotFoundError:
                    pass
                except Exception as e:
                    return self._send(200, {"events": [], "error": str(e)})
                lines.reverse()                              # newest first
                return self._send(200, {"events": lines})
            if parts == ["project"]:
                # The commissioning project (AV orchestration model). Tech/owner only —
                # it's an installer surface. The dashboard consumes membership via the
                # mirrored HA labels, not this endpoint.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                if project is None:
                    return self._send(503, {"error": "project module not loaded"})
                return self._send(200, project.load())
            if parts == ["project", "unassigned"]:
                # AV devices HA discovered but that aren't in any area yet (the Stage-1 tray).
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                if project is None:
                    return self._send(503, {"error": "project module not loaded"})
                return self._send(200, {"devices": project.unassigned(_client)})
            if parts == ["project", "activities"]:
                # A committed room's activities + per-activity EDITED flag.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                if project is None:
                    return self._send(503, {"error": "project module not loaded"})
                qs = parse_qs(urlparse(self.path).query)
                return self._send(200, project.activities_status(_client, project.load(), (qs.get("area", [""])[0])))
            if parts == ["rooms"]:
                # Diagnostic: exactly what Core sees for areas/rooms and how each
                # watch resolved. Reveals empty area registry or device area_ids.
                from proos import netmap
                areas = netmap.load_areas(client=_client)
                _e, devs, _en = netmap.load_registries(client=_client)
                named = [d for d in devs if (d.get("name_by_user") or d.get("name"))]
                with_area = [d for d in named if d.get("area_id")]
                sample = [{"name": d.get("name_by_user") or d.get("name"),
                           "area_id": d.get("area_id")} for d in named[:25]]
                watch_areas = [{"name": w["name"], "area": w.get("area")}
                               for w in (_watcher.watches if _watcher else [])]
                return self._send(200, {
                    "areas_loaded": len(areas),
                    "area_names": sorted(areas.values()),
                    "devices_named": len(named),
                    "devices_with_area_id": len(with_area),
                    "sample_devices": sample,
                    "watch_areas": watch_areas,
                })
            if parts == ["reachability"]:
                # The resolved second-signal map: auto-derived device IPs merged
                # under any manual config. Shows where each entity's signal came
                # from (config_entry / device_url / manual).
                auto = (_cfg or {}).get("_reach_auto", {}) or {}
                manual = (_cfg or {}).get("_reach_manual", {}) or {}
                merged = (_cfg or {}).get("reachability", {}) or {}
                rows = []
                for ent, spec in sorted(merged.items()):
                    src = "manual" if ent in manual else "auto"
                    rows.append({"entity": ent, "source": src,
                                 "ip": spec.get("ip"), "sensor": spec.get("sensor"),
                                 "domain": spec.get("domain"), "via": spec.get("via"),
                                 "port": spec.get("port")})
                return self._send(200, {"auto": len(auto), "manual": len(manual),
                                        "total": len(merged), "items": rows})
            if parts == ["integrations"]:
                return self._send(200, _integrations_report())
            if parts == ["credentials"]:
                # Central masked view of every stored service token. MA is bridged
                # from its own files; other services come from the unified store.
                services = [_ma_credential_status()]
                for s in _creds.status():
                    if s.get("service") != "music_assistant":
                        services.append(s)
                return self._send(200, {"services": services})
            if len(parts) == 2 and parts[0] == "credentials":
                svc = parts[1]
                if svc == "music_assistant":
                    return self._send(200, _ma_credential_status())
                return self._send(200, _creds.status(svc))
            if parts == ["push", "config"]:
                return self._send(200, _apns_config_status())
            if parts == ["music"]:
                rep = (_music.report() if _music else
                       {"status": "fault", "summary": "Music layer not started",
                        "loaded": False, "players": 0, "entities": 0})
                addon = _addon_state(MA_ADDON_SLUG)
                rep["addon"] = addon
                # Trust the Supervisor flag: a stopped server outranks a stale
                # 'loaded' (entities linger briefly after the server goes down).
                if addon not in ("started", "unknown") and rep.get("status") == "ok":
                    rep["status"] = "fault"
                    rep["summary"] = "Music server stopped"
                return self._send(200, rep)
            if parts == ["music", "inventory"]:
                return self._send(200, _ma.inventory())
            if parts == ["music", "admin-token"]:
                return self._send(200, {"set": bool(_load_ma_admin_token())})
            if parts == ["music", "admin-user"]:
                return self._send(200, {"set": bool(_load_ma_admin_user())})
            if (len(parts) == 4 and parts[:2] == ["music", "providers"]
                    and parts[3] == "oauth"):
                # Poll the OAuth relay job for a session (query: ?session_id=...).
                sid = (parse_qs(urlparse(self.path).query).get("session_id") or [None])[0]
                _oauth_reap()
                with _OAUTH_LOCK:
                    j = dict(_OAUTH_JOBS.get(sid) or {})
                if not j:
                    return self._send(404, {"status": "unknown"})
                return self._send(200, {"status": j.get("status"),
                                        "auth_url": j.get("auth_url"),
                                        "entries": j.get("entries"),
                                        "error": j.get("error")})
            if parts == ["music", "providers"]:
                return self._send(200, {"providers": _ma.providers()})
            if parts == ["music", "players"]:
                return self._send(200, {"players": _ma.players()})
            if parts == ["music", "playlists"]:
                return self._send(200, {"playlists": _ma.playlists()})
            if len(parts) == 3 and parts[:2] == ["music", "queue"]:
                # Full editable play queue for a player (queue_id == MA player_id).
                qid = unquote(parts[2])
                out = {"queue_id": qid, "items": _ma.queue_items(qid)}
                try:
                    out["current_index"] = (_ma.queue_get(qid) or {}).get("current_index")
                except Exception:
                    pass
                return self._send(200, out)
            if parts == ["music", "status"]:
                # Connection state for ProHost — never returns the token itself.
                conn = _load_ma_conn()
                return self._send(200, {
                    "connected": bool(conn),
                    "host": conn[0] if conn else None,
                })
            if parts == ["music", "speakers"]:
                # The curated room-speaker allowlist (None = not curated yet).
                return self._send(200, {"player_ids": _load_speakers()})
            if parts == ["backups"]:
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Backups need the HA add-on"})
                return self._send(200, _sv("GET", "/backups"))
            if parts == ["backups", "config"]:
                return self._send(200, _auto_cfg())
            if len(parts) == 3 and parts[0] == "backups" and parts[2] == "download":
                return self._stream_download(parts[1])
            if parts == ["users"]:
                if not users:
                    return self._send(200, {"users": [], "can_manage": False})
                try:
                    return self._send(200, {"users": users.list_users(_ws_call), "can_manage": True})
                except Exception as e:
                    return self._send(200, {"users": [], "can_manage": False, "error": str(e)})
            if parts == ["provision"]:
                return self._send(200, provision.provision_status() if provision else {"provisioned": False})
            if parts == ["navconfig"]:
                # install_id rides along so clients can detect the factory-reset
                # boundary (see _install_id). navconfig.save() whitelists keys,
                # so a round-tripped POST can never persist it into the store.
                _nc = navconfig.load() if navconfig else {}
                _nc["install_id"] = _install_id()
                return self._send(200, _nc)
            if parts == ["navcaps"]:
                return self._send(200, navconfig.load_caps() if navconfig else {})
            if parts == ["room_order"]:
                # Dashboard room order for THIS screen: its device override if any,
                # else the home default, else nothing (natural order). Homeowner
                # preference — not admin-gated.
                if not roomorder:
                    return self._send(200, {"order": [], "scope": "none", "default": []})
                dev = (parse_qs(urlparse(self.path).query).get("device") or [""])[0]
                return self._send(200, roomorder.resolve(dev))
            if parts == ["catalog"]:
                return self._send(200, catalog.load() if catalog else {"integrations": {}})
            if parts == ["catalog", "published"]:
                return self._send(200, catalog.load_published() if catalog else [])
            if parts == ["assist", "status"]:
                # any signed-in user may ask whether the AI assistant is on
                return self._send(200, _assist.status() if _assist else {"enabled": False})
            if len(parts) == 4 and parts[0] == "apps" and parts[1] == "art" and parts[2] == "tile":
                # THE tile pack: curated graphics shipped with the product plus
                # Tech Tools uploads, matched by slug/alias.
                if not _appart:
                    return self._send(404, {"error": "artwork unavailable"})
                # ?id=<package> lets the caller resolve by the device's own
                # immutable app id instead of whatever the app was named.
                pkg = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                p = _appart.tile_path(re.sub(r"\.png$", "", parts[3]), package=pkg)
                if not p:
                    return self._send(404, {"error": "no artwork"})
                try:
                    with open(p, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    # Short cache so a replacement upload shows within minutes.
                    self.send_header("Cache-Control", "public, max-age=300")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": str(e)})
                return
            if parts == ["androidtv", "apps"]:
                # Learn-apps panel: the box's saved list + the app open on screen.
                q = parse_qs(urlparse(self.path).query)
                eid = (q.get("entity_id") or [""])[0]
                entry_id, published = _atv_entry_for(eid)
                if not entry_id:
                    return self._send(400, {"error": "that device isn't an Android TV box"})
                live = {}
                try:
                    st = _client._req("GET", "/api/states/%s" % eid) or {}
                    at = st.get("attributes") or {}
                    live = {"app_id": at.get("app_id"), "app_name": at.get("app_name"),
                            "state": st.get("state")}
                except Exception:
                    live = {}
                # `apps` = what this box has been taught (ProOS's ledger, the
                # master list). `on_air` says whether the integration is actually
                # publishing each one, so a half-applied list is visible in Pro
                # instead of looking like a mystery.
                known = (_atv_ledger().get(entry_id) or {})
                return self._send(200, {
                    "entity_id": eid,
                    "apps": [{"id": k, "name": v, "on_air": v in published}
                             for k, v in sorted(known.items(), key=lambda kv: kv[1].lower())],
                    "published": sorted(published),
                    # Other Android boxes here, so Pro can offer to copy a
                    # list across instead of re-learning each one by hand.
                    "boxes": _atv_boxes(),
                    "live": live})
            if parts == ["apps", "art", "list"]:
                # Tile manager (Pro -> Tech Tools). Tech/owner only.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(200, {"tiles": []})
                out = _appart.list_tiles()
                # Audit against the apps this home really has, so the manager
                # shows a finite list of files to supply instead of a guess.
                out["audit"] = _appart.audit(_site_app_names())
                # Plus every brand in the catalogue with no tile yet — filling
                # those in advance means a newly-installed app is never bare.
                out["catalogue_gaps"] = _appart.catalogue_gaps()
                return self._send(200, out)
            if parts == ["scenes", "photos"]:
                # The dashboard reads the per-scene photo/name overrides + the
                # curated style catalog + whether AI generation is available.
                gen = bool(_assist and _assist._image_key(None)) if _assist else False
                return self._send(200, {
                    "photos": _scenephotos.load() if _scenephotos else {},
                    "catalog": _scenephotos.catalog() if _scenephotos else [],
                    "can_generate": gen})
            if len(parts) == 3 and parts[0] == "areas" and parts[2] == "devices":
                # Pro areas page: the room's controllable non-AV devices
                # (auto-discovered) with the installer's overlay (exclude / role /
                # name). Pro-only — it's a commissioning surface.
                u = self._user
                if not (u and (u.get("is_admin") or u.get("is_owner")
                               or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "installer access required"})
                if not _roomdevices:
                    return self._send(200, {"area_id": parts[1], "devices": []})
                return self._send(200, _roomdevices.discover(_client, parts[1]))
            if len(parts) == 3 and parts[0] == "areas" and parts[2] == "apps":
                # Dashboard quick-launch: the room's launchable apps and which
                # device(s) offer each. Any signed-in user (homeowner surface).
                if not (_appctl and project):
                    return self._send(200, {"area_id": parts[1], "apps": [], "devices": []})
                return self._send(200, _appctl.room_apps(_client, project, parts[1]))
            if parts == ["whoami"]:
                u = getattr(self, "_user", None)
                if not u:
                    return self._send(200, {"authenticated": False, "tier": None})
                is_tech = bool(users and users.is_tech(u.get("id")))
                if u.get("is_owner"):
                    tier = "owner"
                elif is_tech:
                    tier = "tech"
                elif u.get("is_admin"):
                    tier = "installer"
                else:
                    tier = "user"
                eff = {}
                try:
                    if consent:
                        eff = consent.effective()
                except Exception:
                    eff = {}
                return self._send(200, {"authenticated": True, "id": u.get("id"),
                    "name": u.get("name"), "is_owner": bool(u.get("is_owner")),
                    "is_admin": bool(u.get("is_admin")), "tech": is_tech,
                    "must_change": bool(provision and provision.installer_must_change(u.get("id"))),
                    "tier": tier, "consent": eff})
            if parts == ["consent"]:
                return self._send(200, consent.status() if consent else {"state": {}, "effective": {}})
            if parts == ["terminal", "audit"]:
                u = getattr(self, "_user", None)
                if not (terminal and users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                return self._send(200, {"audit": terminal.audit_tail()})
            return self._send(404, {"error": "not found"})
        except MaUnavailable as e:
            return self._send(503, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._auth():
            return
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            # ── Avatar write-shim: the ONLY privileged step. The browser has
            # already uploaded the image to HA's native store and passes us the
            # served URL as JSON {"picture": "/api/image/serve/<id>/original"};
            # Core writes it onto the user's HA person over its owner connection
            # (person edits are admin-only, homeowners aren't). Empty/omitted
            # picture clears it. No image bytes pass through Core.
            if parts == ["presence"]:
                # Multi-device presence beat: {"id","name","kind","section"}.
                # TTL-expired server-side; every beat fans out to all clients.
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                b = self._body() or {}
                return self._send(200, {"devices": _journal_mod.presence_beat(
                    b.get("id"), b.get("name"), b.get("kind"), b.get("section"))})
            if parts == ["health", "fix"]:
                # One-tap mechanical repair for an incident (reload only —
                # recommit/witness actions are navigation, handled by Pro).
                if _healthmon_mod is None:
                    return self._send(503, {"error": "healthmon unavailable"})
                b = self._body() or {}
                out = _healthmon_mod.resolve_action(
                    _client, str(b.get("id") or ""), b.get("action"))
                return self._send(200 if not out.get("error") else 400, out)
            if parts == ["journal", "note"]:
                # Pro-originated journal entries (commission commits, icon
                # changes, assist actions) — the audit half of the journal.
                if _journal_mod is None:
                    return self._send(503, {"error": "journal unavailable"})
                b = self._body() or {}
                room = str(b.get("room") or "site")
                etype = str(b.get("type") or "note")
                _journal_mod.emit(room, etype, b.get("data") or {})
                return self._send(200, {"ok": True})
            if parts == ["net", "witnesses"]:
                # Installer commits one source's traffic witness binding:
                # {"source": eid, "sensors": [eids]|null, "min": float}
                if _netev_mod is None:
                    return self._send(503, {"error": "netevidence unavailable"})
                b = self._body() or {}
                srcid = str(b.get("source") or "")
                if not srcid:
                    return self._send(400, {"error": "source required"})
                _netev_mod.save_witness(srcid, b.get("sensors"), b.get("min"))
                return self._send(200, {"ok": True, "witnesses":
                    _netev_mod.load_witnesses(str(_opt("traffic_witnesses", "") or ""))})
            if parts == ["me", "picture"] or (len(parts) == 3 and parts[0] == "users" and parts[2] == "picture"):
                if not users:
                    return self._send(503, {"error": "user management unavailable"})
                u = getattr(self, "_user", None)
                if not u:
                    return self._send(401, {"error": "authentication required"})
                if parts[0] == "me":
                    target = u.get("id")
                else:
                    target = unquote(parts[1])
                    if target != u.get("id") and not (u.get("is_admin") or u.get("is_owner") or users.is_tech(u.get("id"))):
                        return self._send(403, {"error": "not allowed"})
                body = self._body() or {}
                pic = (body.get("picture") or "").strip()
                try:
                    if not pic:
                        print("[avatar] clear for %s" % target, flush=True)
                        return self._send(200, users.clear_picture(_ws_call, target))
                    print("[avatar] set for %s -> %s" % (target, pic), flush=True)
                    return self._send(200, users.set_picture(_ws_call, target, pic))
                except Exception as e:
                    print("[avatar] FAILED for %s: %r" % (target, e), flush=True)
                    return self._send(500, {"error": str(e)})
            # ── Location report: the Dashboard PWA posts the signed-in user's GPS
            # fix; Core feeds it to HA's native device_tracker.see and attaches
            # the tracker to their person, so zone state (Home/Away) flows free.
            if parts == ["me", "location"]:
                u = getattr(self, "_user", None)
                if not u:
                    return self._send(401, {"error": "authentication required"})
                b = self._body() or {}
                lat, lon = b.get("lat"), b.get("lon")
                if lat is None or lon is None:
                    return self._send(400, {"error": "lat and lon required"})
                dev_id = "proos_loc_" + re.sub(r"[^a-z0-9]", "", (u.get("id") or "user"))[:12]
                try:
                    _client._req("POST", "/api/services/device_tracker/see", {
                        "dev_id": dev_id, "gps": [float(lat), float(lon)],
                        "gps_accuracy": int(b.get("accuracy") or 0),
                    })
                    try:
                        if users:
                            users.attach_tracker(_ws_call, u.get("id"), "device_tracker." + dev_id)
                    except Exception as e:
                        print("[location] attach failed for %s: %r" % (u.get("id"), e), flush=True)
                    return self._send(200, {"ok": True, "tracker": "device_tracker." + dev_id})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if parts == ["dashboard", "publish"]:
                v = _dash_publish()
                return self._send(200, {"ok": True, "version": v})
            if parts == ["dashboard", "ack"]:
                b = self._body() or {}
                qs = parse_qs(urlparse(self.path).query)
                device = (b.get("device") or (qs.get("device", [""])[0]) or "").strip()
                name = (b.get("name") or (qs.get("name", [""])[0]) or "").strip()
                ver = b.get("version", qs.get("version", [None])[0])
                if not device:
                    return self._send(400, {"error": "device required"})
                _dash_ack(device, ver, name)
                return self._send(200, {"ok": True})
            if parts == ["command", "build"]:
                b = self._body() or {}
                ent = (b.get("entity") or "").strip()
                cmd = (b.get("command") or "").strip()
                if not ent or not cmd:
                    return self._send(400, {"error": "entity and command required"})
                return self._send(200, _commands.build_command(_client, ent, cmd, b.get("params") or {}))
            if parts == ["integrations", "remove"]:
                # Installer-facing "remove & re-add" for a certified integration —
                # deletes the native HA config entry (and optionally the stored
                # ProOS credential) so it can be set up fresh from the panel.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech or owner access required"})
                b = self._body() or {}
                domain = (b.get("domain") or "").strip()
                if domain not in ("unifiprotect", "unifi"):
                    return self._send(400, {"error": "domain must be 'unifiprotect' or 'unifi'"})
                try:
                    removed = _remove_native_integration(domain)
                except Exception as e:
                    return self._send(502, {"error": str(e)})
                cleared = []
                if b.get("clear_creds"):
                    svc = {"unifiprotect": "unifi_protect", "unifi": "unifi_network"}[domain]
                    if _creds.delete(svc):
                        cleared.append(svc)
                _bump_state()
                return self._send(200, {"ok": True, "domain": domain, "removed": removed,
                                        "count": len(removed), "creds_cleared": cleared})
            if parts == ["unifi", "net", "config"]:
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech or owner access required"})
                b = self._body() or {}
                try:
                    return self._send(200, _unifinet.set_config(b.get("host"), b.get("username"),
                                                                b.get("password"), bool(b.get("verify_ssl"))))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if parts == ["unifi", "poe", "enable"]:
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech or owner access required"})
                b = self._body() or {}
                sw = (b.get("switch") or "").strip()
                if not sw:
                    return self._send(400, {"error": "switch required"})
                try:
                    _client.enable_entity(sw)
                    return self._send(200, {"ok": True, "switch": sw})
                except Exception as e:
                    return self._send(502, {"ok": False, "error": str(e)})
            if parts == ["auth", "login"]:
                # Installer app front door (PUBLIC — you have no token yet).
                # Validates credentials against HA and enforces installer/tech/
                # owner; a homeowner is refused here so their login can't open the
                # installer app. Returns a long-lived token for admins.
                if not proauth:
                    return self._send(503, {"error": "auth module not loaded"})
                b = self._body() or {}
                try:
                    return self._send(200, proauth.login(b.get("username") or "", b.get("password") or ""))
                except proauth.AuthError as e:
                    status = 403 if e.kind == "not_installer" else (502 if e.kind == "unreachable" else 401)
                    return self._send(status, {"error": str(e), "kind": e.kind})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if parts and parts[0] == "project":
                # Commissioning project (AV orchestration model) — tech/owner only.
                #   POST /project/suggest -> merged preview (stored ∪ fresh discover_av),
                #                            NOT saved; the installer reviews then commits.
                #   POST /project         -> save the posted project, then mirror every
                #                            COMMITTED area's membership/role to HA labels
                #                            (the dashboard reads those labels).
                #   POST /project/mirror  -> re-reconcile HA labels to the stored project.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                if project is None:
                    return self._send(503, {"error": "project module not loaded"})
                if parts == ["project", "suggest"]:
                    # No‑auto‑room: before scanning, drop any unplaced AV device HA parked in
                    # a room (by name‑match) into Unassigned, so nothing shows in a room until
                    # the installer places it. AV‑scoped, idempotent (see project.py). Also
                    # runs on the background loop so it fires without Pro open. On by default;
                    # disable with option auto_room_quarantine:false.
                    if _opt("auto_room_quarantine", True):
                        try:
                            project.quarantine_auto_rooms(_client)
                        except Exception:
                            pass
                    return self._send(200, project.merge(project.load(), project.suggest(_client)))
                if parts == ["project", "mirror"]:
                    return self._send(200, project.mirror(_client, project.load()))
                if parts == ["project", "verify"]:
                    # Stage-4 proof: reachability of every committed room's members.
                    return self._send(200, project.verify(_client, project.load()))
                if parts == ["project", "activities", "regenerate"]:
                    # Overwrite ONE activity from the room's declared config (popup 'Reset').
                    b = self._body() or {}
                    _out = project.regenerate_activity(_client, project.load(), b.get("area"), b.get("object_id"))
                    _bump_state()
                    return self._send(200, _out)
                if parts == ["project", "assign"]:
                    # Place (or un-place) a device in a room by writing its entity area
                    # OVERRIDE — the explicit-placement signal the no-auto-room quarantine
                    # treats as pinned (so it won't clear a device the installer is placing).
                    # {entity_id, area_id}. Empty/absent area_id clears the room (Unassign).
                    b = self._body() or {}
                    eid, aid = b.get("entity_id"), b.get("area_id")
                    if not eid:
                        return self._send(400, {"error": "entity_id required"})
                    try:
                        _client.set_entity_area(eid, aid or None)
                        return self._send(200, {"ok": True, "entity_id": eid, "area_id": aid or None})
                    except Exception as e:
                        return self._send(502, {"ok": False, "error": str(e)})
                if parts == ["project"]:
                    saved = project.save(self._body() or {})
                    res = {"project": saved}
                    try:
                        res["mirror"] = project.mirror(_client, saved)
                    except Exception as e:
                        res["mirror"] = {"ok": False, "error": str(e)}
                    # Generate each committed room's one-touch activities (create-if-absent;
                    # installer-edited scripts are protected by the generator's hash).
                    qs = parse_qs(urlparse(self.path).query)
                    ow = (qs.get("overwrite", ["0"])[0]).lower() in ("1", "true", "yes")
                    try:
                        res["activities"] = project.generate_committed(_client, saved, overwrite=ow)
                    except Exception as e:
                        res["activities"] = {"error": str(e)}
                    # Reflect the just-committed config in the cached controllers so the
                    # dashboard's activity list + verdicts update on its next poll (they
                    # build from the committed record). Done ONCE here, not per read.
                    for _c in list(_controllers.values()):
                        try:
                            _c.refresh()
                        except Exception:
                            pass
                    res["state_version"] = _bump_state()
                    return self._send(200, res)
                return self._send(404, {"error": "unknown project route"})
            if parts == ["unifi", "setup"]:
                # ProCore owns the native Protect integration: run discovery-flow
                # ownership on demand (idempotent). The API key / local user come
                # from the same store the private-API proxy uses — entered once.
                if not _unifi_layer:
                    return self._send(503, {"error": "UniFi layer not started"})
                meta = (_creds.meta(unifi.SERVICE) or {}) if _creds else {}
                res = _unifi_layer.ensure_integration(
                    api_key=(_creds.get(unifi.SERVICE) if _creds else "") or "",
                    username=meta.get("username", "") or "",
                    password=meta.get("password", "") or "",
                    host=meta.get("host", "") or "",
                    port=meta.get("port", 443) or 443,
                )
                return self._send(200, res)
            if parts == ["unifi", "curation"]:
                # Save the installer's exposure model (exposed set + doorbell + talkback).
                return self._send(200, unifi.curation_save(self._body() or {}))
            if parts and parts[0] == "sysadmin":
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                from proos import sysadmin as _sa
                if len(parts) == 4 and parts[1] == "addon":
                    return self._send(200, _sa.addon_action(parts[2], parts[3]))
                if len(parts) == 3 and parts[1] == "core":
                    return self._send(200, _sa.core_action(parts[2]))
                return self._send(404, {"error": "unknown sysadmin route"})
            if parts and parts[0] == "unifi":
                return self._unifi(parts, "POST", self._body())
            if parts == ["watchers", "audit", "clear"]:
                # Wipe the awareness/recovery activity history. Installer surface, so
                # tech/owner only. Truncates the audit log the Activity feed reads; the
                # watcher recreates it on the next event. Useful after a factory reset,
                # since this log lives in the add-on's /data and survives the HA wipe.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                path = getattr(_watcher, "audit_path", "/data/watcher_audit.log") if _watcher else "/data/watcher_audit.log"
                try:
                    open(path, "w", encoding="utf-8").close()   # truncate to empty
                except FileNotFoundError:
                    pass
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "cleared": True})
            # Execution routes (/intent, /heal, /recover) retired: Core no longer
            # drives rooms; activities run as HA scripts fired from the dashboard.
            if parts == ["reachability", "refresh"]:
                # Re-derive device-IP signals from HA's registries and re-merge
                # under manual config. Call after commissioning a new device so
                # its second signal appears without a Core restart.
                res = apply_auto_reachability()
                # ...and re-discover the watch list, so a newly-added always-on
                # device is actually watched too (state of existing watches kept).
                try:
                    w = discover_watches(client=_client)
                    if w and _watcher is not None:
                        _watcher.set_watches(w)
                        res["watches"] = len(w)
                        print(f"  watches · re-discovered {len(w)} device(s)", flush=True)
                except Exception as e:
                    res["watches_error"] = str(e)
                return self._send(200, res)
            if parts == ["sync"]:
                # Whole-home provision: walk every room with a display and create
                # its activity scripts. create-if-absent by default (installer edits
                # survive); POST /sync?overwrite=1 force-regenerates.
                qs = parse_qs(urlparse(self.path).query)
                overwrite = (qs.get("overwrite", ["0"])[0]).lower() in ("1", "true", "yes")
                return self._send(200, sync.sync_all(_client, overwrite=overwrite))
            if len(parts) == 2 and parts[0] == "credentials":
                # Mint/store, rotate or revoke a service token in the unified store.
                #   {token, name?, host?, port?}  -> store (records rotation if changed)
                #   {clear:true}                  -> revoke locally
                # Music Assistant is intentionally managed via the /music routes and
                # is read-only here, so its live connection is never disturbed.
                svc = parts[1]
                if svc == "music_assistant":
                    return self._send(409, {"error": "Manage Music Assistant via the /music routes"})
                b = self._body()
                if b.get("clear"):
                    _creds.delete(svc)
                    return self._send(200, {"ok": True, "set": False})
                tok = (b.get("token") or "").strip()
                if not tok:
                    return self._send(400, {"error": "token required"})
                st = _creds.put(svc, tok, name=b.get("name"),
                                host=b.get("host"), port=b.get("port"))
                return self._send(200, {"ok": True, **st})
            if parts == ["push", "config"]:
                # Store the account-wide Apple .p8 push key. {clear:true} removes it.
                b = self._body()
                if b.get("clear"):
                    _creds.delete("apns")
                    return self._send(200, {"ok": True, "set": False})
                p8 = (b.get("p8") or "").strip()
                key_id = (b.get("key_id") or "").strip()
                team_id = (b.get("team_id") or "").strip()
                topic = (b.get("topic") or "").strip()  # app bundle id
                env = (b.get("env") or "production").strip()
                miss = [k for k, v in (("p8", p8), ("key_id", key_id),
                        ("team_id", team_id), ("topic", topic)) if not v]
                if miss:
                    return self._send(400, {"error": "missing: " + ", ".join(miss)})
                if "PRIVATE KEY" not in p8:
                    return self._send(400, {"error": "p8 must be the .p8 private-key PEM"})
                _creds.put("apns", p8, name="Apple Push (APNs)", kind="apns_key",
                           extra={"key_id": key_id, "team_id": team_id,
                                  "topic": topic, "env": env})
                return self._send(200, {"ok": True, **_apns_config_status()})
            if parts == ["push"]:
                # HA's mobile_app notify platform POSTs notifications here.
                # Contract: 201 queued, 429 rate-limited, else error JSON.
                if push is None:
                    return self._send(503, {"errorMessage": "push module not deployed on Core"})
                b = self._body()
                cred = _apns_cred()
                if not cred:
                    return self._send(500, {"errorMessage": "APNs key not configured in ProOS Core"})
                token = b.get("push_token")
                if not token:
                    return self._send(400, {"errorMessage": "no push_token"})
                if not _push_rate_ok(token):
                    return self._send(429, {"message": "rate limit reached", "errorMessage": "rate limit reached"})
                try:
                    res = push.send(cred, b)
                except push.PushError as e:
                    return self._send(500, {"errorMessage": str(e)})
                if res.get("ok"):
                    return self._send(201, {"queued": True})
                return self._send(500, {"errorMessage": "APNs rejected: " + (res.get("reason") or str(res.get("status"))),
                                        "unregistered": res.get("unregistered", False)})
            if parts == ["music", "setup"]:
                # Re-run integration ownership on demand (idempotent): confirm the
                # MA add-on discovery flow if the integration isn't up yet.
                if not _music:
                    return self._send(503, {"error": "Music layer not started"})
                return self._send(200, _music.ensure_integration())
            if parts == ["music", "connect"]:
                # Headless connect: read the token HA already stored for its
                # music_assistant integration, validate it against a reachable
                # host, and persist. No login, no MA UI. Idempotent + re-runnable.
                st = _ma_token_from_storage()
                # The HA entry may carry no token (auto-confirmed post-reset).
                # The Supervisor discovery message from the ProOS Music add-on
                # is the token's source of truth — merge it in whenever the
                # entry is missing or tokenless.
                if not st or not st[1]:
                    disc = _ma_from_discovery()
                    if disc:
                        st = ((st[0] if st else None) or disc[0],
                              (st[1] if st else None) or disc[1])
                if not st:
                    return self._send(503, {"error": "Home Assistant has no Music "
                                            "integration yet — make sure the Music server "
                                            "add-on is running, then try again."})
                conn = _ma_validate_and_persist(st[1], st[0])
                if not conn:
                    return self._send(503, {"error": "Found the Music link but couldn't "
                                            "connect to the Music server. If the server has "
                                            "authentication enabled, Home Assistant stored no "
                                            "token for it — open the Music integration once in "
                                            "HA so it saves one, then retry."})
                print(f"  MA · connected (host {conn[0]}:{conn[1]})", flush=True)
                return self._send(200, {"ok": True, "connected": True, "host": conn[0]})
            if parts == ["music", "speakers"]:
                # Replace the curated room-speaker allowlist (ProOS-side only).
                b = self._body()
                ids = b.get("player_ids")
                if not isinstance(ids, list):
                    return self._send(400, {"error": "player_ids must be a list"})
                saved = _save_speakers(ids)
                return self._send(200, {"ok": True, "player_ids": saved, "count": len(saved)})
            if len(parts) == 4 and parts[:2] == ["music", "players"] and parts[3] == "enabled":
                b = self._body()
                return self._send(200, _ma.set_player_enabled(
                    unquote(parts[2]), bool(b.get("enabled"))))
            if len(parts) == 4 and parts[:2] == ["music", "queue"] and parts[3] == "move":
                # Reorder a queue item: {item: queue_item_id, pos_shift: N}
                b = self._body()
                item = b.get("item")
                if not item:
                    return self._send(400, {"error": "item required"})
                return self._send(200, _ma.queue_move(
                    unquote(parts[2]), item, int(b.get("pos_shift") or 0)))
            if len(parts) == 4 and parts[:2] == ["music", "queue"] and parts[3] == "remove":
                # Remove a queue item: {item: queue_item_id}
                b = self._body()
                item = b.get("item")
                if not item:
                    return self._send(400, {"error": "item required"})
                return self._send(200, _ma.queue_delete(unquote(parts[2]), item))
            if len(parts) == 4 and parts[:2] == ["music", "queue"] and parts[3] == "play":
                # Jump to a queue entry: {item: queue_item_id}
                b = self._body()
                item = b.get("item")
                if item is None:
                    return self._send(400, {"error": "item required"})
                return self._send(200, _ma.queue_play_index(unquote(parts[2]), item))
            if parts == ["music", "favorite"]:
                # Add an item (by uri) to the library / favourites: {item: uri}
                b = self._body()
                item = b.get("item")
                if not item:
                    return self._send(400, {"error": "item required"})
                return self._send(200, _ma.favorite_add(item))
            if parts == ["music", "playlists", "add"]:
                # Append an item to a playlist: {playlist: playlist_id, item: uri}
                b = self._body()
                pid = b.get("playlist"); item = b.get("item")
                if not pid or not item:
                    return self._send(400, {"error": "playlist and item required"})
                return self._send(200, _ma.playlist_add(pid, item))
            if parts == ["music", "browse"]:
                # Browse the MA tree by service/provider: {path: <folder path or null>}
                b = self._body()
                return self._send(200, {"items": _ma.browse(b.get("path") or None)})
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "enabled":
                b = self._body()
                return self._send(200, _ma.set_provider_enabled(
                    unquote(parts[2]), bool(b.get("enabled"))))
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "remove":
                # Remove a service instance outright. config/providers/remove is
                # admin-required in MA, so it MUST ride the ingress-admin channel
                # with the installer/owner identity — the anonymous API silently
                # refuses it (the 'couldn't remove' bug). parts[2] is the INSTANCE id.
                try:
                    return self._send(200, _ma.remove_provider(
                        unquote(parts[2]), _ma_ingress_identity()))
                except Exception as e:  # noqa: BLE001
                    return self._send(502, {"error": str(e)})
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "entries":
                b = self._body()
                return self._send(200, {"entries": _ma.provider_entries(
                    unquote(parts[2]), b.get("instance_id"), b.get("action"),
                    b.get("values"), session_id=b.get("session_id"))})
            if parts == ["music", "admin-token"]:
                # One-time admin handoff: store a long-lived MA token so Core can
                # authenticate as an admin for provider writes. {clear:true} removes it.
                b = self._body()
                if b.get("clear"):
                    _clear_ma_admin_token()
                    return self._send(200, {"ok": True, "set": False})
                tok = (b.get("token") or "").strip()
                if not tok:
                    return self._send(400, {"error": "token required"})
                if _ma_validate_admin_token(tok):
                    return self._send(200, {"ok": True, "set": True})
                return self._send(400, {"error": "Music Assistant rejected that token — "
                                        "check it's a valid long-lived token from User Management"})
            if parts == ["music", "admin-user"]:
                # Packageable admin handoff: store the installer's HA admin user
                # (id/name) so Core can present it as X-Remote-User-* on MA's
                # ingress channel for provider writes. pro.html supplies it from
                # its authenticated HA session — no MA UI, no manual token.
                b = self._body()
                if b.get("clear"):
                    try:
                        os.remove(_MA_ADMIN_FILE)
                    except FileNotFoundError:
                        pass
                    return self._send(200, {"ok": True, "set": False})
                uid = (b.get("user_id") or "").strip()
                uname = (b.get("username") or "").strip()
                if not uid or not uname:
                    return self._send(400, {"error": "user_id and username required"})
                _save_ma_admin_user(uid, uname, (b.get("display_name") or "").strip())
                return self._send(200, {"ok": True, "set": True})
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "probe":
                # Diagnostic for the OAuth relay: fire a provider 'auth' action and
                # capture the frame shapes MA returns (confirms AUTH_SESSION event).
                b = self._body()
                secs = float(b.get("seconds") or 12)
                return self._send(200, _ma.provider_auth_probe(unquote(parts[2]), seconds=secs))
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "oauth":
                # Start the OAuth relay for a provider. Spawns a background worker
                # that holds one MA connection through the whole login, captures
                # the auth URL, and returns it here so ProHost can open the popup.
                b = self._body()
                sid = b.get("session_id")
                if not sid:
                    return self._send(400, {"error": "session_id required"})
                domain = unquote(parts[2])
                action = b.get("action") or "auth"   # provider's auth action (Apple Music: CONF_ACTION_AUTH)
                values = b.get("values") if isinstance(b.get("values"), dict) else None
                # Apple Music's sign-in page (served by MA during the flow) builds its
                # glue script with values["music_user_token"] and
                # values["music_user_token_timestamp"] accessed DIRECTLY — if either
                # is missing the glue route KeyErrors, the script never loads, and the
                # page shows a dead 'Sign In' button + 'v[object HTMLSpanElement]'.
                # MA's own frontend always sends them; Core must too. Seed empties.
                if domain == "apple_music":
                    values = dict(values or {})
                    values.setdefault("music_user_token", "")
                    values.setdefault("music_user_token_timestamp", 0)
                _oauth_reap()
                with _OAUTH_LOCK:
                    job = _OAUTH_JOBS.get(sid)
                    if job is None or job.get("status") == "error":
                        job = {"status": "starting", "auth_url": None, "entries": None,
                               "error": None, "ts": time.time(), "domain": domain}
                        _OAUTH_JOBS[sid] = job
                        threading.Thread(target=_oauth_run, args=(domain, sid, action, values),
                                         name="proos-oauth", daemon=True).start()
                # Wait briefly for MA to emit the auth URL (it fires near-instantly).
                for _ in range(60):
                    with _OAUTH_LOCK:
                        j = _OAUTH_JOBS.get(sid) or {}
                        if j.get("auth_url") or j.get("status") in ("error", "done"):
                            break
                    time.sleep(0.25)
                with _OAUTH_LOCK:
                    j = dict(_OAUTH_JOBS.get(sid) or {})
                return self._send(200, {"status": j.get("status"),
                                        "auth_url": j.get("auth_url"),
                                        "error": j.get("error")})
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "save":
                b = self._body()
                # ADMIN save on the ingress channel is the working path (proven
                # in MA debug: token delivered, "Ingress user authenticated:
                # developer", provider loaded). The earlier session-first branch
                # fired a redundant system-user save that MA drops — removed.
                # NOTE: if MA then errors 'refresh_token' on a Spotify GLOBAL-app
                # login, that is an UPSTREAM MA provider bug on the shared-app
                # path; entering an own Spotify Client ID avoids it.
                # Admin WRITE: prefer the installer's registered HA admin user,
                # routed over MA's ingress channel (:8094). An inline ingress_user
                # in the body overrides (used for verification). Falls back to the
                # direct :8095 save (system user) only when no admin user is known.
                au = _ma_ingress_identity()
                iu = b.get("ingress_user")
                if iu and iu.get("id") and iu.get("username"):
                    au = (iu["id"], iu["username"], iu.get("display_name") or iu["username"])
                if au:
                    return self._send(200, _ma.save_provider_admin(
                        unquote(parts[2]), b.get("values") or {}, au,
                        instance_id=b.get("instance_id")))
                return self._send(200, _ma.save_provider(
                    unquote(parts[2]), b.get("values") or {}, b.get("instance_id"),
                    session_id=b.get("session_id")))
            if parts == ["players", "area"]:
                # Assign a player (any HA media_player entity) to a room, or clear
                # it (area_id=""). The only ProOS-native way to place an MA player;
                # writes the HA entity-registry area override over the socket.
                if not _client:
                    return self._send(503, {"error": "HA client not started"})
                b = self._body()
                eid = (b.get("entity_id") or "").strip()
                if not eid:
                    return self._send(400, {"error": "entity_id required"})
                area = b.get("area_id")
                try:
                    entry = _client.set_entity_area(eid, area)
                except Exception as e:
                    return self._send(502, {"error": f"area assign failed: {e}"})
                return self._send(200, {
                    "ok": True, "entity_id": eid,
                    "area_id": (entry or {}).get("area_id"),
                })
            if parts == ["music", "enabled"]:
                # Flip Core's own music_assistant option so the MA connection is
                # auto-restored on every boot (persisted to the add-on options) —
                # add sets true, remove sets false. Survives HA restarts.
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Enable-flag needs the HA add-on"})
                b = self._body()
                enabled = bool(b.get("enabled"))
                try:
                    opts = {}
                    if os.path.exists("/data/options.json"):
                        with open("/data/options.json") as fp:
                            opts = json.load(fp)
                    opts["music_assistant"] = enabled
                    _sv("POST", "/addons/self/options", {"options": opts})
                    if _cfg is not None:
                        _cfg["music_assistant"] = enabled
                except Exception as e:
                    return self._send(502, {"error": f"could not persist: {e}"})
                return self._send(200, {"ok": True, "music_assistant": enabled})
            if parts == ["music", "install"]:
                # Fresh-box provision: register the MA add-on store repo (idempotent),
                # install the image, set boot=auto, start it. Long-running, so it runs
                # in the background; ProHost polls /integrations (installed→running).
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Install needs the HA add-on"})
                if _addon_state(_MA_ADDON_SLUG) == "started":
                    return self._send(200, {"ok": True, "already_running": True})
                threading.Thread(target=_ma_provision, daemon=True).start()
                return self._send(200, {"ok": True, "installing": True})
            if parts == ["backups", "new"]:
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Backups need the HA add-on"})
                b = self._body()
                name = (b.get("name") or "").strip() or time.strftime("ProOS %Y-%m-%d %H-%M")
                if b.get("scope") == "ha":
                    data = _sv("POST", "/backups/new/partial",
                               _enc({"name": name, "homeassistant": True, "compressed": True}), timeout=600)
                else:
                    data = _sv("POST", "/backups/new/full",
                               _enc({"name": name, "compressed": True}), timeout=600)
                return self._send(200, data)
            if len(parts) == 3 and parts[0] == "backups" and parts[2] == "restore":
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Backups need the HA add-on"})
                b = self._body()
                if b.get("confirm") != "RESTORE":
                    return self._send(400, {"error": "restore requires confirm=RESTORE"})
                slug = parts[1]
                info = _sv("GET", f"/backups/{slug}/info")
                is_full = info.get("type") == "full"
                pw = {"password": BACKUP_PASSWORD} if (info.get("protected") and BACKUP_PASSWORD) else {}
                self._send(200, {"ok": True, "restarting": True})
                try:
                    if is_full:
                        _sv("POST", f"/backups/{slug}/restore/full", dict(pw), timeout=900)
                    else:
                        _sv("POST", f"/backups/{slug}/restore/partial",
                            {"homeassistant": True, **pw}, timeout=900)
                except Exception:
                    pass
                return
            if len(parts) == 3 and parts[0] == "backups" and parts[2] == "delete":
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Backups need the HA add-on"})
                _sv("DELETE", f"/backups/{parts[1]}")
                return self._send(200, {"ok": True})
            if parts == ["backups", "config"]:
                # Edit auto-backup settings: apply to the running config immediately
                # (the scheduler re-reads each minute) AND persist to the add-on's
                # options so they survive a restart -- no add-on restart needed.
                if not os.environ.get("SUPERVISOR_TOKEN"):
                    return self._send(503, {"error": "Auto-backup config needs the HA add-on"})
                b = self._body()
                updates = {}
                if "enabled" in b:
                    updates["auto_backup"] = bool(b["enabled"])
                if "full" in b:
                    updates["auto_backup_full"] = bool(b["full"])
                if "keep" in b:
                    try:
                        updates["auto_backup_keep"] = max(1, min(365, int(b["keep"])))
                    except Exception:
                        return self._send(400, {"error": "keep must be a number"})
                if "time" in b:
                    t = str(b.get("time", "")).strip()
                    try:
                        hh, mm = [int(x) for x in t.split(":")]
                        assert 0 <= hh < 24 and 0 <= mm < 60
                        t = f"{hh:02d}:{mm:02d}"
                    except Exception:
                        return self._send(400, {"error": "time must be HH:MM"})
                    updates["auto_backup_time"] = t
                if "copy_to" in b:
                    updates["auto_backup_copy_to"] = str(b.get("copy_to") or "").strip()
                if _cfg is not None:
                    _cfg.update(updates)
                try:
                    opts = {}
                    if os.path.exists("/data/options.json"):
                        with open("/data/options.json") as fp:
                            opts = json.load(fp)
                    opts.update(updates)
                    _sv("POST", "/addons/self/options", {"options": opts})
                except Exception as e:
                    return self._send(200, {**_auto_cfg(), "persist_error": str(e)})
                return self._send(200, _auto_cfg())
            if parts == ["reset"]:
                # Destructive: restore the clean baseline. Requires an explicit
                # confirm token in the body (the dashboard also makes the installer
                # type the home name). Recovery backup is taken before anything is
                # touched; the restore runs after the response so the dashboard gets
                # confirmation even though HA Core restarts mid-restore.
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode()) if raw else {}
                except Exception:
                    body = {}
                if body.get("confirm") != "RESET":
                    return self._send(400, {"error": "reset requires confirm=RESET"})
                baseline = reset_prepare()
                self._send(200, {"ok": True, "restarting": True})
                try:
                    reset_restore(baseline)
                except Exception:
                    pass
                return
            if parts == ["factory-reset"]:
                # Destructive: wipe to a brand-new install. OWNER (Developer) only
                # — an installer must not be able to wipe their own commission.
                # Explicit confirm token; recovery backup taken inside factory_reset.
                u = getattr(self, "_user", None)
                if not (u and u.get("is_owner")):
                    return self._send(403, {"error": "owner (Developer) access required"})
                body = self._body() or {}
                if body.get("confirm") != "FACTORY":
                    return self._send(400, {"error": "factory reset requires confirm=FACTORY"})
                clear_data = bool(body.get("clear_proos_data"))
                keep_auth = bool(body.get("keep_auth"))  # test only: keep logins/tokens
                self._send(200, {"ok": True, "restarting": True})
                try:
                    factory_reset(clear_proos_data=clear_data, keep_auth=keep_auth)
                except Exception:
                    pass
                return
            if parts == ["homekit", "brand"]:
                # White-label the HomeKit bridge to 'ProOS' (see _homekit_brand).
                # The dashboard calls this right after publishing a bridge so the
                # customer pairs with 'ProOS', not 'HASS Bridge'. Renames in the
                # config-entries store, then restarts HA so the change loads.
                body = self._body()
                try:
                    res = _homekit_brand(
                        name=(body.get("name") or "ProOS"),
                        title=(body.get("title") or "ProOS Apple Home"),
                        entry_id=body.get("entry_id"),
                    )
                except FileNotFoundError:
                    return self._send(503, {"error": "config-entries store not visible"})
                except PermissionError:
                    return self._send(503, {"error": "config dir is read-only — set "
                                                     "homeassistant_config:rw and rebuild Core"})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                renamed = res["renamed"]
                # Restart if anything changed — a filter clamp with no rename
                # still needs a restart to load (HA reads options at startup).
                do_restart = bool(res["changed"]) and body.get("restart", True)
                self._send(200, {"ok": True, "renamed": renamed, "restarting": do_restart})
                if do_restart:
                    # Response already flushed; restart in the background so the
                    # dashboard isn't held open for the full core boot.
                    threading.Thread(
                        target=lambda: _sv("POST", "/core/restart"), daemon=True
                    ).start()
                return
            if parts == ["homekit", "expose"]:
                # Opt-in allow-list: set exactly which entities the bridge
                # exposes. Body: {entities:[...], entry_id?, restart?}. An empty
                # list exposes NOTHING. The dashboard calls this on publish (with
                # []) and on every Save of the device switchboard (with the
                # enabled set), so step 3 is the single source of truth.
                body = self._body()
                ents = body.get("entities")
                if not isinstance(ents, list):
                    return self._send(400, {"error": "entities must be a list"})
                try:
                    updated = _homekit_expose(ents, entry_id=body.get("entry_id"))
                except FileNotFoundError:
                    return self._send(503, {"error": "config-entries store not visible"})
                except PermissionError:
                    return self._send(503, {"error": "config dir is read-only — set "
                                                     "homeassistant_config:rw and rebuild Core"})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                do_restart = bool(updated) and body.get("restart", True)
                self._send(200, {"ok": True, "updated": updated,
                                 "count": len(ents), "restarting": do_restart})
                if do_restart:
                    threading.Thread(
                        target=lambda: _sv("POST", "/core/restart"), daemon=True
                    ).start()
                return
            if parts == ["users"]:
                if not users:
                    return self._send(503, {"error": "user module not loaded"})
                b = self._body()
                name = (b.get("name") or "").strip()
                if not name:
                    return self._send(400, {"error": "name required"})
                try:
                    return self._send(200, users.create_user(
                        _ws_call, name=name, role=b.get("role") or "homeowner",
                        password=(b.get("password") or None), caller_id=((getattr(self, "_user", None) or {}).get("id") or b.get("caller_id"))))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if parts == ["users", "change-password"]:
                # The password itself is changed client-side via HA's self-service
                # flow (the caller's OWN session — ProCore's admin connection can't
                # call the owner-only admin_change_password). This endpoint only
                # clears the forced-change flag once that has succeeded.
                u = getattr(self, "_user", None)
                if not (users and u):
                    return self._send(403, {"error": "authentication required"})
                if provision:
                    provision.clear_installer_must_change(u.get("id"))
                return self._send(200, {"ok": True})
            if len(parts) == 3 and parts[0] == "users" and parts[2] == "password":
                if not users:
                    return self._send(503, {"error": "user module not loaded"})
                try:
                    pw = users.set_password(_ws_call, unquote(parts[1]),
                                            (self._body().get("password") or None))
                    return self._send(200, {"ok": True, "password": pw})
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[0] == "users" and parts[2] == "role":
                if not users:
                    return self._send(503, {"error": "user module not loaded"})
                try:
                    _rb = self._body()
                    return self._send(200, users.set_role(
                        _ws_call, unquote(parts[1]), _rb.get("role") or "", caller_id=((getattr(self, "_user", None) or {}).get("id") or _rb.get("caller_id"))))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[0] == "areas" and parts[2] == "devices":
                # Pro areas page saves the room-device overlay: a list of
                # {entity_id, excluded?, role?, name?}. Installer-gated.
                u = self._user
                if not (u and (u.get("is_admin") or u.get("is_owner")
                               or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "installer access required"})
                if not _roomdevices:
                    return self._send(503, {"error": "room devices unavailable"})
                b = self._body() or {}
                res = _roomdevices.set_devices(parts[1], b.get("updates") or [])
                if res.get("error"):
                    return self._send(400, res)
                # Return the fresh discovered view so the UI repaints from truth.
                return self._send(200, _roomdevices.discover(_client, parts[1]))
            if len(parts) == 3 and parts[0] == "areas" and parts[2] == "app-launch":
                # Dashboard app button: launch an app in the room. Any signed-in
                # user. Body {app, device?}. Returns needs_choice when >1 device
                # can run it (the dashboard then shows a chooser), else launches.
                if not (_appctl and project):
                    return self._send(503, {"error": "app launch unavailable"})
                b = self._body() or {}
                app = (b.get("app") or "").strip()
                if not app:
                    return self._send(400, {"error": "app required"})
                res = _appctl.launch(_client, project, parts[1], app,
                                     device=(b.get("device") or "").strip() or None)
                return self._send(200, res)
            if parts == ["androidtv", "apps", "copy"]:
                # Installer asserts these boxes carry the same apps.
                u = self._user
                if not (u and (u.get("is_admin") or u.get("is_owner")
                               or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "installer access required"})
                b = self._body() or {}
                res = _atv_copy_apps((b.get("from") or "").strip(), b.get("to") or [])
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["androidtv", "apps"]:
                # Save the learned app list into the integration's own options.
                u = self._user
                if not (u and (u.get("is_admin") or u.get("is_owner")
                               or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "installer access required"})
                b = self._body() or {}
                res = _atv_write_apps((b.get("entity_id") or "").strip(), b.get("apps") or [])
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "source"]:
                # Look a brand's artwork up by domain. Fetched ONCE here; the
                # finished tile is stored locally and served from the box, so
                # dashboards never depend on an internet connection.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                cid = (_opt("brandfetch_client_id", "") or "").strip()
                res = _appart.fetch_source(b.get("app") or b.get("slug") or "", cid,
                                           domain=(b.get("domain") or "").strip(),
                                           kind=(b.get("kind") or "logo"),
                                           theme=(b.get("theme") or "dark"))
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "import"]:
                # Bulk-add a ZIP of brand logos — how the pack grows quickly.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                res = _appart.import_zip(b.get("zip") or b.get("image") or "")
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "export"]:
                # Pull this install's uploaded tiles back out, to fold into the
                # shipped pack so every future install carries them.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                res = _appart.export_zip()
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "upload"]:
                # Tile manager upload. Body {name, image:<data-url|b64>}.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                res = _appart.save_upload(b.get("name") or b.get("slug") or "",
                                          b.get("image") or "",
                                          origin=(b.get("origin") or "uploaded"))
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "clear"]:
                # Wipe stored artwork. Reversible in one click — every fetched
                # tile can be pulled again.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                res = _appart.clear_all((b.get("what") or "all").strip())
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "restore"]:
                # Bring back a shipped tile that was hidden on this system.
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                res = _appart.restore_tile(b.get("slug") or b.get("name") or "")
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["apps", "art", "delete"]:
                u = self._user
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if not _appart:
                    return self._send(503, {"error": "artwork unavailable"})
                b = self._body() or {}
                res = _appart.delete_tile(b.get("slug") or b.get("name") or "")
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["scenes", "photo"]:
                # Set/remove a scene's photo + display-name override. Homeowner
                # editing (dashboard) and the assistant both write here. Body:
                #   {entity_id, photo:<url|catalog-key>} — pick a curated style
                #   {entity_id, upload:<data-url|b64>} — homeowner's own photo
                #   {entity_id, generate:<mood>} — AI-generate (if key set)
                #   {entity_id, name:<str>} — rename override
                #   {entity_id, remove:true} — clear
                if not _scenephotos:
                    return self._send(503, {"error": "scene photos unavailable"})
                b = self._body() or {}
                eid = (b.get("entity_id") or "").strip()
                if not eid:
                    return self._send(400, {"error": "entity_id required"})
                if b.get("remove"):
                    return self._send(200, _scenephotos.remove(eid))
                slug = re.sub(r"[^a-z0-9_]+", "_", eid.split(".", 1)[-1].lower()) or "scene"
                photo = b.get("photo")
                if photo is not None and not str(photo).startswith(("http", "/local/")):
                    photo = _scenephotos.match(photo)   # a catalog key
                if photo is None and b.get("upload"):
                    photo = _scenephotos.save_upload(slug, b.get("upload"))
                    if not photo:
                        return self._send(400, {"error": "couldn't read the uploaded image"})
                if photo is None and b.get("generate"):
                    ik = _assist._image_key(None) if _assist else ""
                    if not ik:
                        return self._send(400, {"error": "no image key configured for generation"})
                    png, err = _scenephotos.generate(
                        _scenephotos.build_prompt(b.get("name") or "scene", b.get("generate")), ik)
                    photo = _scenephotos.save_generated(slug, png) if png else None
                    if not photo:
                        return self._send(502, {"error": "generation failed: %s" % err})
                return self._send(200, _scenephotos.set_photo(eid, photo=photo, name=b.get("name")))
            if parts == ["rooms", "art"]:
                # Generate a room background from the room's NAME and hand the
                # PNG back as base64. Pro uploads it to HA and writes it to the
                # area picture — so the dashboard serves it like any other room
                # image, and any later upload simply replaces it.
                #   {area_id, name}            -> generate
                #   {area_id, name, curated:1} -> keyless: return the curated URL
                if _roomart is None:
                    return self._send(503, {"error": "roomart unavailable"})
                b = self._body() or {}
                nm = (b.get("name") or "").strip()
                if not nm:
                    return self._send(400, {"error": "name required"})
                if b.get("curated"):
                    return self._send(200, {"url": _roomart.photo_for(nm),
                                            "icon": _roomart.icon_for(nm),
                                            "origin": "curated"})
                ik = _assist._image_key(None) if _assist else ""
                if not ik:
                    return self._send(400, {"error": "no image key configured",
                                            "url": _roomart.photo_for(nm),
                                            "icon": _roomart.icon_for(nm),
                                            "origin": "curated"})
                if not _scenephotos:
                    return self._send(503, {"error": "image engine unavailable"})
                png, err = _scenephotos.generate(
                    _roomart.build_prompt(nm, b.get("describe")), ik)
                if not png:
                    return self._send(502, {"error": "generation failed: %s" % err})
                import base64 as _b64
                if _journal_mod is not None:
                    _journal_mod.emit(b.get("area_id") or "site", "room_art",
                                      {"room": nm, "origin": "generated"})
                return self._send(200, {"b64": _b64.b64encode(png).decode(),
                                        "icon": _roomart.icon_for(nm),
                                        "origin": "generated"})
            # ── Pro Assist AI gateway ──
            if parts == ["assist", "chat"]:
                if not _assist:
                    return self._send(503, {"error": "assist module not loaded"})
                u = getattr(self, "_user", None)
                if not u:
                    return self._send(401, {"error": "sign in required"})
                b = self._body() or {}
                uinfo = {"id": u.get("id"), "name": u.get("name"),
                         "is_admin": bool(u.get("is_admin")),
                         "tech": bool(users and users.is_tech(u.get("id")))}
                global _ASSIST_HOME_NAME
                try:
                    if not _ASSIST_HOME_NAME:
                        _ASSIST_HOME_NAME = ((_client._req("GET", "/api/config") or {})
                                             .get("location_name") or "")
                except Exception:  # noqa: BLE001
                    pass
                # WHERE the person is asking from. A request without a room is
                # only answerable by interrogation; with one, "the lights"
                # means the lights in front of them.
                where = {}
                aid = (b.get("area_id") or b.get("area") or "").strip()
                if aid:
                    where["area_id"] = aid
                    try:
                        for a in (_client.area_registry() or []):
                            if a.get("area_id") == aid:
                                where["area_name"] = a.get("name") or aid
                                break
                    except Exception:
                        pass
                    where.setdefault("area_name", aid)
                return self._send(200, _assist.chat(
                    _client, _ws_call, project, uinfo,
                    b.get("text") or "", session=b.get("session") or "default",
                    home_name=_ASSIST_HOME_NAME, ma=_ma, where=where,
                    awareness=_assist_awareness()))
            if parts == ["assist", "config"] or parts == ["assist", "test"]:
                if not _assist:
                    return self._send(503, {"error": "assist module not loaded"})
                u = getattr(self, "_user", None)
                if not (u and (u.get("is_owner") or (users and users.is_tech(u.get("id"))))):
                    return self._send(403, {"error": "tech access required"})
                if parts[1] == "test":
                    return self._send(200, _assist.test_provider())
                return self._send(200, _assist.save_config(self._body() or {}))
            if len(parts) == 3 and parts[0] == "users" and parts[2] == "rename":
                if not users:
                    return self._send(503, {"error": "user module not loaded"})
                try:
                    return self._send(200, users.rename_user(
                        _ws_call, unquote(parts[1]), (self._body() or {}).get("name") or ""))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[0] == "users" and parts[2] == "delete":
                if not users:
                    return self._send(503, {"error": "user module not loaded"})
                try:
                    return self._send(200, users.delete_user(_ws_call, unquote(parts[1])))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
            if parts == ["provision", "claim"]:
                b = self._body()
                out = provision.mark_claimed((b.get("site_name") or "").strip() or None) if provision else {}
                name = (b.get("homeowner") or "").strip()
                if name and users:
                    try:
                        out["homeowner"] = users.create_homeowner(
                            _ws_call, name=name, password=(b.get("password") or None))
                    except Exception as e:
                        out["homeowner_error"] = str(e)
                return self._send(200, out)
            if parts == ["consent"]:
                if not consent:
                    return self._send(503, {"error": "consent module not loaded"})
                b = self._body()
                return self._send(200, consent.set_grant(
                    _ws_call, installer=b.get("installer_access"), tech=b.get("tech_access")))
            if parts == ["terminal"]:
                u = getattr(self, "_user", None)
                if not (terminal and users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                b = self._body() or {}
                uname = (u.get("name") or u.get("id"))
                if b.get("shell"):
                    return self._send(200, terminal.run_shell(
                        b.get("command", ""), user=uname, cwd=b.get("cwd")))
                return self._send(200, terminal.run(b.get("command", ""), user=uname))
            if parts == ["navconfig"]:
                u = getattr(self, "_user", None)
                if not (navconfig and u and u.get("is_admin")):
                    return self._send(403, {"error": "installer access required"})
                return self._send(200, navconfig.save(self._body() or {}))
            if parts == ["navcaps"]:
                return self._send(200, navconfig.save_caps(self._body() or {}) if navconfig else {})
            if parts == ["room_order"]:
                # Save dashboard room order. Body: {device, order, scope, clear}.
                #   scope "device" (default) -> this screen's override
                #   scope "default"         -> home-wide order for all screens
                #   clear:true              -> drop this screen's override
                # Homeowner preference — not admin-gated (any screen can rearrange
                # itself; the token gate on the API is the security layer).
                if not roomorder:
                    return self._send(503, {"error": "room order unavailable"})
                b = self._body() or {}
                dev = str(b.get("device") or "")
                if b.get("clear"):
                    return self._send(200, roomorder.clear_device(dev))
                if str(b.get("scope") or "device") == "default":
                    return self._send(200, roomorder.save_default(b.get("order")))
                return self._send(200, roomorder.save_device(dev, b.get("order")))
            if parts == ["catalog", "sync"]:
                u = getattr(self, "_user", None)
                if not (catalog and users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                return self._send(200, catalog.sync())
            if parts == ["watchers", "recovery"]:
                # Assign or clear a smart-plug POWER-CYCLE recovery for a device.
                # Installer-gated. Body: {entity, plug, off_time?, tier?} to set;
                # {entity, clear:true} to remove. Re-discovers so it takes effect live.
                u = getattr(self, "_user", None)
                if not (users and u and u.get("is_admin")):
                    return self._send(403, {"error": "installer access required"})
                b = self._body() or {}
                entity = (b.get("entity") or "").strip()
                if not entity:
                    return self._send(400, {"error": "entity required"})
                cfg = _recovery_cfg_read()
                if b.get("clear"):
                    cfg.pop(entity, None)
                else:
                    method = b.get("method") if b.get("method") in ("power_cycle", "poe_cycle") else "power_cycle"
                    tier = b.get("tier") if b.get("tier") in ("safe", "risky") else "risky"
                    if method == "poe_cycle":
                        sw = (b.get("poe_switch") or b.get("plug") or "").strip()
                        if not sw:
                            return self._send(400, {"error": "poe_switch required"})
                        cfg[entity] = {"method": "poe_cycle", "poe_switch": sw,
                                       "off_time": int(b.get("off_time", 8)), "tier": tier}
                    else:
                        plug = (b.get("plug") or "").strip()
                        if not plug:
                            return self._send(400, {"error": "plug required"})
                        cfg[entity] = {"method": "power_cycle", "plug": plug,
                                       "off_time": int(b.get("off_time", 25)), "tier": tier}
                _recovery_cfg_write(cfg)
                try:
                    if _watcher is not None:
                        _watcher.set_watches(discover_watches(client=_client))   # pick up the change now
                except Exception as _e:
                    print("[watcher] recovery-config re-discover failed: %s" % _e, flush=True)
                return self._send(200, {"ok": True, "overrides": cfg})
            if parts == ["watchers", "rediscover"]:
                # Re-derive the awareness watch list from the CURRENT device/entity
                # registry NOW. Called right after the installer removes a device or
                # integration so its watch drops off immediately, instead of leaving
                # a stale "offline / missing" aura until the 5-min self-heal loop.
                # allow_empty force-clears the old list even when nothing remains to
                # watch. Reconcile-only (no destructive capability), so ungated like
                # /sync.
                try:
                    w = discover_watches(client=_client) or []
                    if _watcher is not None:
                        _watcher.set_watches(w, allow_empty=True)
                    return self._send(200, {"ok": True, "watches": len(w)})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if parts == ["assist", "scenes", "apply"]:
                # The ONE apply path for a scene + its music companion. The
                # dashboard's scene tap posts here so a moment behaves the same
                # from chat and from a tile. Any signed-in resident.
                if not _assist:
                    return self._send(503, {"error": "assist module not loaded"})
                b = self._body() or {}
                res = _assist.apply_scene(_client, _ws_call, project, _ma,
                                          (b.get("scene_entity_id") or b.get("entity_id") or ""),
                                          getattr(self, "_user", None))
                return self._send(400 if res.get("error") else 200, res)
            if parts == ["assist", "flags"]:
                # Issues escalated to the Pro, diagnosis attached. Installer+.
                u = getattr(self, "_user", None)
                if not (users and u and (u.get("is_admin") or u.get("is_owner")
                                         or users.is_tech(u.get("id")))):
                    return self._send(403, {"error": "installer access required"})
                b = self._body() or {}
                if b.get("resolve"):
                    rows = _flags_load()
                    for r in rows:
                        if r.get("id") == b.get("resolve"):
                            r["resolved"] = True
                    _flags_save(rows)
                    return self._send(200, {"ok": True})
                return self._send(200, {"flags": [r for r in reversed(_flags_load())
                                                  if not r.get("resolved") or b.get("all")]})
            if parts == ["proactive", "sweep"]:
                # Run one proactive pass now and report what it did — the test
                # button for "would this fault have notified?". Tech/owner only.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                if _proactive is None:
                    return self._send(503, {"error": "proactive module not running"})
                return self._send(200, _proactive.sweep())
            if parts == ["watchers", "test-recovery"]:
                # Prove the live recovery path on demand (certification checklist
                # 8.5) without inducing a real wedge. Runs the exact reload the
                # Watcher would, audits it. Tech-gated; reload is a SAFE action.
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                body = self._body() or {}
                entity = (body.get("entity") or "").strip()
                entry_id = (body.get("entry_id") or "").strip()
                if not entity and not entry_id:
                    return self._send(400, {"error": "entity or entry_id required"})
                ok = False
                try:
                    if entry_id:
                        ok = _reload_entry(entry_id)
                    else:
                        ok = bool(_reload_integration(entity, "reload_integration"))
                except Exception as e:
                    return self._send(200, {"ok": False, "target": entity or entry_id, "error": str(e)})
                try:
                    if _watcher:
                        _watcher._audit(entity or entry_id, "test_recovery", "ok" if ok else "failed")
                except Exception:
                    pass
                return self._send(200, {"ok": ok, "target": entity or entry_id,
                                        "message": ("Integration reloaded — recovery path works" if ok
                                                    else "Reload could not run")})
            if parts == ["watchers", "recover"]:
                # On-demand self-heal for ONE device using its CONFIGURED recovery method:
                # a config-entry reload by default, or the installer-assigned smart-plug
                # power-cycle when one is set. Runs the SAME executor the Watcher uses, so
                # chat-driven recovery and auto-recovery behave identically. Non-destructive;
                # installer / tech / owner only (the homeowner never triggers a repair).
                u = getattr(self, "_user", None)
                if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner") or u.get("is_admin"))):
                    return self._send(403, {"error": "installer or tech access required"})
                body = self._body() or {}
                entity = (body.get("entity") or "").strip()
                if not entity:
                    return self._send(400, {"error": "entity required"})
                rec = _recovery_cfg_read().get(entity) or {}
                method = rec.get("method") or "reload_integration"
                try:
                    ok = bool(_reload_integration(entity, method, rec))
                except Exception as e:
                    return self._send(200, {"ok": False, "target": entity, "method": method, "error": str(e)})
                try:
                    if _watcher:
                        _watcher._audit(entity, "recover", ("%s:ok" % method) if ok else ("%s:failed" % method))
                except Exception:
                    pass
                return self._send(200, {"ok": ok, "target": entity, "method": method,
                    "message": (("Recovered via %s" % method.replace("_", " ")) if ok else "Recovery could not run")})
            if parts == ["catalog", "published"]:
                u = getattr(self, "_user", None)
                if not (catalog and users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):
                    return self._send(403, {"error": "tech access required"})
                return self._send(200, catalog.save_published(self._body() or []))
            return self._send(404, {"error": "not found"})
        except MaAuthFailed as e:
            return self._send(401, {"error": str(e)})
        except MaUnavailable as e:
            return self._send(503, {"error": str(e)})
        except KeyError as e:
            return self._send(400, {"error": f"unknown activity {e}"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def _opt(key, default=None):
    """Read a single add-on option from /data/options.json (Supervisor-managed).
    Cheap file read; options change rarely and only via the add-on config UI. Returns
    `default` when not packaged as an add-on or the key is absent."""
    try:
        with open("/data/options.json") as f:
            o = json.load(f) or {}
        return o.get(key, default)
    except Exception:
        return default


def load_config(path="config.json"):
    # --- Add-on mode --------------------------------------------------------
    # When packaged as an HA add-on, the Supervisor injects SUPERVISOR_TOKEN and
    # proxies HA at http://supervisor/core. No manual long-lived token, no
    # base_url -- the seam that always treated HA as a remote endpoint pays off
    # here: same Core, different endpoint. UI options arrive at /data/options.json.
    if os.environ.get("SUPERVISOR_TOKEN"):
        opts = {}
        if os.path.exists("/data/options.json"):
            with open("/data/options.json") as f:
                opts = json.load(f)
        # Add-on schema can't express a free dict, so reachability is a list of
        # {entity, sensor?, ip?} -- fold it back into the map Core expects.
        reach = {}
        for item in (opts.get("reachability") or []):
            spec = {}
            if item.get("sensor"):
                spec["sensor"] = item["sensor"]
            if item.get("ip"):
                spec["ip"] = item["ip"]
            if spec and item.get("entity"):
                reach[item["entity"]] = spec
        return {
            "home_id": opts.get("home_id", "home"),
            "base_url": "http://supervisor/core",
            "token": os.environ["SUPERVISOR_TOKEN"],
            "area": opts.get("area", "Family Room"),
            "port": int(opts.get("port", 8770)),
            "monitor_interval": opts.get("monitor_interval", 20),
            "auto_heal": bool(opts.get("auto_heal", False)),
            "reachability": reach,
            "auto_backup": bool(opts.get("auto_backup", False)),
            "auto_backup_time": opts.get("auto_backup_time", "03:30"),
            "auto_backup_full": bool(opts.get("auto_backup_full", True)),
            "auto_backup_keep": int(opts.get("auto_backup_keep", 7)),
            "auto_backup_copy_to": opts.get("auto_backup_copy_to", ""),
            "backup_password": opts.get("backup_password", ""),
        }

    # --- Standalone mode (e.g. on a Mac) ------------------------------------
    if not os.path.exists(path):
        sys.exit("No config.json. Run: cp config.example.json config.json")
    with open(path) as f:
        cfg = json.load(f)
    if "PASTE" in cfg.get("token", ""):
        sys.exit("config.json still has the placeholder token.")
    return cfg


def _ma_provision():
    """Background install of the Music server add-on on a fresh box.

    Registers the store repo (idempotent), installs the image, sets boot=auto,
    and starts it. Runs off-thread because the image pull can take minutes;
    ProHost watches /integrations (installed→running) for progress. Best-effort:
    each step is logged, and an already-present repo/add-on is not an error.
    """
    try:
        try:
            _sv("POST", "/store/repositories", {"repository": MA_ADDON_REPO}, timeout=120)
        except Exception:
            pass  # already registered
        try:
            _sv("POST", "/store/reload", None, timeout=180)
        except Exception:
            pass
        # Install: newer Supervisors expose /store/addons/{slug}/install; older
        # ones /addons/{slug}/install. Try the store path, fall back.
        try:
            _sv("POST", f"/store/addons/{_MA_ADDON_SLUG}/install", None, timeout=1800)
        except Exception:
            _sv("POST", f"/addons/{_MA_ADDON_SLUG}/install", None, timeout=1800)
        try:
            _sv("POST", f"/addons/{_MA_ADDON_SLUG}/options", {"boot": "auto"}, timeout=60)
        except Exception:
            pass
        _sv("POST", f"/addons/{_MA_ADDON_SLUG}/start", None, timeout=180)
        print("  MA · install complete (started)", flush=True)
    except Exception as e:
        print(f"  MA · install failed: {e}", flush=True)


def _integrations_report():
    """ProOS's certified-integrations catalog with live status. Everything ProOS
    needs is native; the one optional, certified integration today is Music
    Assistant (cross-brand speaker grouping). ProHost renders this as the
    integrations screen. Status is derived, never cached."""
    enabled = bool((_cfg or {}).get("music_assistant", False))
    addon = _addon_state(_MA_ADDON_SLUG)        # 'started' / 'stopped' / 'unknown'
    installed = addon != "unknown"
    running = addon == "started"
    connected = bool(_load_ma_conn())
    if not enabled:
        status = "disabled"
        summary = "Optional. Enable for cross-brand speaker grouping."
    elif not installed:
        status = "not_installed"
        summary = "Enabled, but the Music server add-on isn't installed."
    elif not running:
        status = "stopped"
        summary = "Enabled, but the Music server add-on is stopped."
    elif connected:
        status = "connected"
        summary = "Connected to the Music server."
    else:
        status = "running"
        summary = "Music server running; not yet connected."
    return {
        "integrations": [
            {
                "id": "music_assistant",
                "name": "ProOS Music",
                "certified": True,
                "category": "music",
                "optional": True,
                "enabled": enabled,
                "installed": installed,
                "running": running,
                "connected": connected,
                "status": status,
                "summary": summary,
            }
        ]
    }


def main():
    global _client, _monitor, _watcher, _music, _ma, _cfg, _unifi_layer
    cfg = load_config()
    _cfg = cfg
    _client = RestHAClient(cfg["home_id"], cfg["base_url"], cfg["token"])
    print(f"ProOS Core API  home={_client.home_id}  ha={cfg['base_url']}")
    print(f"  HA says: {_client.ping()}")
    apply_auto_reachability()      # auto device-IP signals, merged under manual config
    port = int(cfg.get("port", 8770))
    area = cfg.get("area", "Family Room")
    get_controller(area)
    # Whole-home sync: provision activity scripts for EVERY room with a display,
    # not just the configured area. create-if-absent, so a restart never clobbers
    # edited scripts -- it only fills gaps. This is what makes a fresh install (or a
    # post-delete restart) rebuild every room's activities on its own.
    try:
        _res = sync.sync_all(_client)
        for _r in _res["rooms"]:
            print(f"  sync · {_r['area']}: display={_r['display']} "
                  f"created={len(_r['created'])} kept={len(_r['kept'])}")
        for _s in _res["skipped"]:
            print(f"  sync · {_s['area']}: skipped ({_s['skipped']})")
        _t = _res["totals"]
        print(f"  sync total: {_t['rooms']} room(s), "
              f"{_t['created']} created, {_t['kept']} kept")
    except Exception as _e:
        print(f"  sync skipped: {_e}")
    _monitor = Monitor(_controllers, interval=float(cfg.get("monitor_interval", 20)),
                       auto_heal=bool(cfg.get("auto_heal", False)))
    _monitor.start()
    print(f"  monitor running (interval {_monitor.interval}s, "
          f"auto-heal {'ON' if _monitor.auto_heal else 'off'})")
    try:
        _watches = discover_watches(client=_client)
        print(f"  watches · {len(_watches)} device(s) auto-discovered")
    except Exception as _e:
        # Only a genuine discovery EXCEPTION leaves _watches unknown. An empty result
        # is authoritative (the box really has nothing watchable) and must pass through
        # as [] — never fall back to a hardcoded list, or awareness invents devices.
        _watches = []
        print(f"  watches · discovery failed ({_e}); starting with none")
    _watcher = Watcher(_client, watches=_watches,
                       reachability=(_cfg or {}).get("reachability"),
                       recover_fn=_reload_integration)
    _watcher.run_forever(interval=5)
    print("  watcher running (interval 5s) -> GET /watchers")
    if provision:
        try:
            _pv = provision.ensure_provisioned(ws_call=_ws_call)
            print(f"  provision \u00b7 site={_pv.get('site_id','n/a')} host={_pv.get('hostname','')}")
            try:
                _ob = provision.auto_onboard(ws_call=_ws_call)
                if _ob.get("owner_created"):
                    print(f"  auto-onboard \u00b7 fresh box -> owner+installer via {_ob.get('base')} ({_ob})")
                elif _ob.get("already"):
                    print("  auto-onboard \u00b7 already onboarded (no-op)")
                else:
                    print(f"  auto-onboard \u00b7 FAILED: {_ob}")
            except Exception as _oe:
                print(f"  auto-onboard skipped: {_oe}")
            try:
                provision.ensure_dashboard_helper(ws_call=_ws_call)
            except Exception:
                pass
            try:
                _sa = provision.ensure_services_area(ws_call=_ws_call)
                if _sa.get("created"):
                    print(f"  services area · created standing global room ({_sa.get('area_id')})")
            except Exception:
                pass
            try:
                _dd = provision.deploy_dashboards(overwrite=False)
                if _dd.get("copied"):
                    print(f"  dashboards \u00b7 deployed {_dd['copied']} -> {_dd['dest']}")
            except Exception as _de:
                print(f"  dashboards \u00b7 deploy skipped: {_de}")
        except Exception as _e:
            print(f"  provision skipped: {_e}")
    if catalog:
        try:
            _cat = catalog.sync()
            print(f"  catalog \u00b7 v{_cat.get('version','?')} ({len(_cat.get('integrations',{}))} integrations, synced={_cat.get('synced')})")
        except Exception as _e:
            print(f"  catalog sync skipped: {_e}")
    if users:
        try:
            _chk = users.manage_check(_ws_call)
            print(f"  users \u00b7 {_chk['hint']}")
        except Exception as _e:
            print(f"  users check skipped: {_e}")
    if consent:
        try:
            _ce = consent.apply(_ws_call)
            print(f"  consent applied={_ce.get('applied')} eff={_ce.get('effective')}")
        except Exception as _e:
            print(f"  consent skipped: {_e}")
    # Music Assistant is an optional, certified ProOS integration (off by
    # default). The layer + commissioner are always constructed so the
    # on-demand endpoints (/music, /music/setup, /music/connect, /integrations)
    # keep working, but Core only auto-commissions and connects MA at boot when
    # the integration is enabled — so a disabled or absent MA produces no boot
    # retries and no connect errors.
    _music = MusicLayer(_client)
    # UniFi Protect is a certified ProOS integration too: ProCore owns the native
    # HA integration (camera entities + go2rtc) via the same discovery-flow path.
    try:
        _unifi_layer = unifi.UnifiLayer(_client) if unifi else None
    except Exception as _e:
        _unifi_layer = None
        print(f"  unifi layer skipped: {_e}", flush=True)
    _ma = MaCommissioner(_ma_conn, get_ingress_user=_ma_ingress_identity)
    ma_enabled = bool(cfg.get("music_assistant", False))

    if ma_enabled:
        def _music_boot():
            for _ in range(6):
                try:
                    r = _music.ensure_integration()
                    print(f"  music · ensure: {r}", flush=True)
                    if r.get("loaded"):
                        return
                except Exception as e:
                    print(f"  music · ensure error: {e}", flush=True)
                time.sleep(15)
        threading.Thread(target=_music_boot, name="proos-music-boot", daemon=True).start()

        def _ma_probe():
            time.sleep(20)  # let MA settle after a cold boot
            conn = _ma_conn()  # persisted, or read+validate HA's stored token
            if not conn:
                print("  MA · not connected — no usable Music token yet "
                      "(is the Music integration set up?)", flush=True)
                return
            try:
                inv = _ma.inventory()
                sv = inv.get("server", {})
                print(f"  MA · connected v{sv.get('version')} schema {sv.get('schema')}: "
                      f"{len(inv.get('providers', []))} provider(s), "
                      f"{len(inv.get('players', []))} player(s)", flush=True)
            except Exception as e:
                print(f"  MA · token present but connect failed: {e}", flush=True)
        threading.Thread(target=_ma_probe, name="proos-ma-probe", daemon=True).start()
        print("  Music Assistant: certified integration ENABLED "
              "-> GET /music, /music/setup, /music/connect", flush=True)
    else:
        print("  Music Assistant: certified integration available (disabled) "
              "-> enable in add-on options to use", flush=True)
    global BACKUP_PASSWORD
    if cfg.get("backup_password"):
        BACKUP_PASSWORD = cfg["backup_password"]
    if os.environ.get("SUPERVISOR_TOKEN"):
        threading.Thread(target=_auto_backup_loop, daemon=True).start()
    if _WATCH_REDISCOVER_SEC > 0:
        threading.Thread(target=_watch_rediscover_loop, name="proos-watch-heal", daemon=True).start()
        threading.Thread(target=_atv_learn_loop, name="proos-atv-learn", daemon=True).start()
        threading.Thread(target=_quarantine_loop, name="proos-no-auto-room", daemon=True).start()
        # The proactive Pro: confirmed watcher faults -> plain-language notice
        # to every phone in the home, and a close-out when it recovers.
        global _proactive
        if _proactive_mod is not None and _watcher is not None:
            _proactive = _proactive_mod.Proactive(
                _client, _watcher, enabled=lambda: bool(_opt("proactive_notify", True)))
            _proactive.start()
        # Room activity verdicts as HA sensors — the one-trigger-per-room feed
        # for overlaid control systems (Savant now, Control4 next) and for any
        # HA automation that wants "what is this room doing" as a fact.
        global _ctlbridge
        if _ctlbridge_mod is not None:
            _ctlbridge = _ctlbridge_mod.ActivityPublisher(
                _client, project, get_controller,
                enabled=lambda: bool(_opt("activity_sensors", True)),
                savant_host=str(_opt("savant_host", "") or ""),
                witnesses=(lambda: _netev_mod.load_witnesses(
                    str(_opt("traffic_witnesses", "") or ""))) if _netev_mod
                    else _ctlbridge_mod.ActivityPublisher.parse_witnesses(
                    str(_opt("traffic_witnesses", "") or "")))
            _ctlbridge.converge = bool(_opt("intent_convergence", True))
            # additive: throttled health scan riding the sweep's own snapshot
            if _healthmon_mod is not None:
                _ctlbridge.healthcheck = lambda snap: _healthmon_mod.scan(
                    snap, project, get_controller,
                    (_netev_mod.load_witnesses(
                        str(_opt("traffic_witnesses", "") or ""))
                     if _netev_mod else {}))
            _ctlbridge.start()
        print(f"  watches · self-heal every {_WATCH_REDISCOVER_SEC}s", flush=True)
        _ab = _auto_cfg()
        print(f"  auto-backup {'ON @ ' + _ab['time'] if _ab['enabled'] else 'off'} "
              f"(encrypted: {'yes' if BACKUP_PASSWORD else 'no'})")
    # Remove the stale custom Apple sign-in page an earlier build wrote to /www —
    # Apple auth now runs entirely through MA's native config-flow (nothing here).
    try:
        _stale = os.path.join(_HA_CONFIG_DIR, "www", "proos_apple_auth.html")
        if os.path.exists(_stale):
            os.remove(_stale)
    except Exception:  # noqa: BLE001
        pass
    print(f"  serving on http://0.0.0.0:{port}  (room: {area})")
    print(f"  try: curl http://localhost:{port}/rooms/{area.replace(' ', '%20')}/activities")
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except OSError as e:
        if e.errno in (48, 98):  # macOS / Linux "address already in use"
            sys.exit(f"\nPort {port} is already in use — is Core already running?\n"
                     f"  Stop it with:  lsof -ti:{port} | xargs kill\n"
                     f"  Or set a different \"port\" in config.json.")
        raise


if __name__ == "__main__":
    main()
