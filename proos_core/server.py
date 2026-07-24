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


# ── ProOS-owned Apple Music (MusicKit) sign-in ───────────────────────────────
# MA's own served auth page is broken for API-driven flows (it references a
# developer token variable it never injects), so ProOS serves its OWN MusicKit
# sign-in page: it reads the developer token MA already holds, runs authorize()
# in the browser, and posts the resulting Music User Token back here. ProOS-branded.
_APPLE_AUTH: dict = {}          # session_id -> {"token": <music_user_token>, "ts": t}
_APPLE_LOCK = threading.Lock()

def _apple_reap():
    now = time.time()
    with _APPLE_LOCK:
        for k in [k for k, v in _APPLE_AUTH.items() if now - v.get("ts", 0) > 900]:
            _APPLE_AUTH.pop(k, None)

def _apple_dev_token() -> str:
    """Apple Music's MusicKit developer token (music_app_token), read from MA."""
    try:
        for e in (_ma.provider_entries("apple_music") or []):
            if e.get("key") == "music_app_token":
                v = e.get("value")
                if v is None:
                    v = e.get("default_value")
                if v:
                    return str(v)
    except Exception:
        pass
    return ""

_APPLE_AUTH_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ProOS Music \u2014 Connect Apple Music</title>
<style>
*{box-sizing:border-box;-webkit-touch-callout:none}
html,body{margin:0;height:100%;background:#0b0b0f;color:#fff;font-family:-apple-system,'Inter','Helvetica Neue',sans-serif;-webkit-font-smoothing:antialiased}
.wrap{min-height:100%;display:flex;align-items:center;justify-content:center;padding:28px}
.card{width:100%;max-width:400px;text-align:center}
.brand{font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:#8b8b93;margin-bottom:26px}
.logo-img{height:60px;width:auto;margin:0 auto 22px;display:block}
h1{font-size:23px;font-weight:600;margin:0 0 10px;letter-spacing:-.4px}
p{font-size:14px;line-height:1.5;color:#a5a5ad;margin:0 0 26px}
button{width:100%;padding:15px;border:none;border-radius:13px;font-size:16px;font-weight:600;font-family:inherit;cursor:pointer;background:#fff;color:#000;transition:opacity .15s}
button:disabled{opacity:.45;cursor:default}
.msg{margin-top:16px;font-size:13px;color:#8b8b93;min-height:18px}
.msg.err{color:#ff6b5e}
.foot{margin-top:30px;font-size:11px;color:#55555c}
</style></head><body>
<div class="wrap"><div class="card">
  <div class="brand">ProOS Music</div>
  <img class="logo-img" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAAAOdElEQVR42u1de2xUV3r/fefOePBj4pgQHuEVyY4Jztp5QIusbJCNlCqK2XSjxV5ptS2ioZFWidSqXWnZhNRD2M3KyaZKGpVuCIqIGkTkSRFLaNXIgXGATRAMsmPXPGoHMH7EMTH2eGyPd+ae8/UP37udsiLF836cn3RlZA/3zj3f73zf7/vOC9DQ0NDQ0NCYL5hZ6FbIUzQ3N2vj53vP/+CDDxo/+eSTJdbvSLdMHqC1tdUAgLa2ttqhoaGxjo6OOzUB8qfnEzOLlpYW9/Xr169NTU1NPfvss0WaAPlDAAcA9Pf3e5mZg8Hg0L59+9zZTAAtZOZhfCIyz58//7NVq1ZtUUoxETmz/b00AW4z7hOR+emnn24qLy//JYCIECInXL5Dm/e2FL968803V1RXVx8sKCgwpJQwDAOaAHkg+gAQEalr164dLCsrW6yUkkSUM55Th4Bvh0FEsq+v7/WVK1d+V0ppCiGMXHpBTYBbwOfzOYjI9Pv9TeXl5X8HwBRC5JzHzNgXstMqj8eTcrFVV1cn6uvrzSNHjty/du3adwBIpZQhhO4vyQK1trYazOxg5oxo6MbGxpLR0dFuZmYppeQoSCkVM/PU1NRovHWAdNcPHGnu5QKAICKzqalJRv+tubnZ4XK5ShcsWFCayu9UWFhohEIh+eSTT/7i7rvv/o5SKqmun4iYmam9vd2oq6uTRMQ5TwB7MIWIFAAFwNne3r5+9erVtQUFBRtcLle52+1eaJpmaTgcLgXAAFLaU+68804DgEp23G9paXETURCAaWuPdBAhZXGdmf+gotva2h7u6+v7p2AweCESiXCGQd3qD4kIAT6fzwEAwWDwFzdu3Dh99erVv3777bcXRbWVkWvG/8MLnTp1qnZkZMQ7PT1t3tTgESmlacVcaTV0ui5OJgHscQVm/rV930Ag8FV/f//uPXv2LLY9ZU7MO7CN39zcvLS/v3/fzMxMdANHmFkqpThbkEgCSClfZWZTShmy7z81NTV49uzZ5+3P2kPQWanubeO3t7d/78aNG9fsBpRSmtlk9CR6gFejOoKyfjIz8+Dg4NHdu3evTGZIEEnu+YKIZEdHx4u1tbVHysrKViqlTCIiIYRBpIfQb+4wABxKKQZgLl++vOGZZ545e/To0Q1EJG3dkC1u3wEAly5d+pVFaPPmfDpbkUQPcPNzIszMk5OTk6dOnaqPFo8Z7QH8fr+TiMze3t7dlZWVOwBEAAihS2nzM44QDimlcrvd7gcffPA/zpw58yf19fVmIsOBIwk93yCiSGdn508qKip2WsZ3pDqPzxUYhiEAyJKSkgX333//kQMHDjwqhLhshVeVUR6AmYUQQh4/fnxdZWXlGwCUUkobPwE8kFJKt9u9dOPGjd61a9cWRGmGjAoBtGXLFqOqqmp/YWFhgVKKc2XmTAZ4AgNAZMWKFY8cOHBgJxGpRCxMcSSw9xtEJHt6en66ZMmS7yBHh0/TDAcAWVFRseOjjz76VwB98YYCkSDjEwD12muvLV6xYsXPMVff14IvCWmiUgolJSXOBx54YJc1XhCXh02UkQwi4oaGhm133HHHwkQQQCnFSimJuUGSjLyY2UxDZiAA8OLFi5sOHTpUSUQynnJxogggq6qqCu65556/BMBKqXjuywCkEIKs6VeOTLuEEE4ADma+e3p6OtUahwDI4uJio6qqajsAeDyemNs77hhtTZmWJ0+e/FO3212FuSFUEWuvt0SjEQwGvw4Gg2dKSkq6iouLw5YQyoghUntWMDPP3LhxYxaYG9dP1fOVUoKIsGjRoj8H8HMA0iJG6tvHrmiNjIz8w8217FiqazMzM9M9PT0v7tmzpyyXg/ntVAJvOU49N4aiwuGwbGtre9juiOnKAhQAOByOeouFsbhEFkJgfHx8prOz8882bdr0OzuzyPQaAhGZaXgmAEin0+lYvnx5LYCOxsZGSjkBmJmISG3evLnIMIw1cRQnlJSSurq6tm3atOl3zFwAIEJEUgv/b0dJSUl1Ot2YAIBjx46tDgaDkf9vJs0tYDIzDw8Pn4zq9TmPeEKAFTJNZuavvvqqLZ52S0gWMDMzs6SoqEjY4SAG1Y/R0dHD9koc3a9vKx0EAJSVlRXE02ZxEcDr9RIA9Pb2LrPz0xi+jACAoqIiv6WkWZv3tjIBAoCBgYFyAC4hhIyFCAnxAA6Hw4xybbHktbjvvvuC2qwxC8L0VgJtNsaDSCRiaHOmIZRkyhdxOp3a9eczATQ0ATQ0ATQ0AeIUxVGrjJN2+Xw+R67sFJpLM3aIiPjmVcbJRKImZmoCJMD4c/ZgnD59ur6wsLBiaGhomVU1TXhlcfXq1VeHh4dPEtEVazyENQHSBNvAzz33XMnzzz//27Vr124CgJqamqQ+t7y8fKanp+enRPQvra2tRio9jybA/4VBRObFixd/tmbNmk2YW4eQ9PGEwsLCooqKij1tbW2nH3/88Y5sDQe5QAAGgHvvvfdRzK1DECnayStSUFDg3LBhwzoAHZagzjoC5EwW4HK57OVnKdUdbrfbzOZ2y6U0kNL0TNIE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAE0NAESChSujhDKWVvhsGaABmA2dlZF+aOeE0Z4YgIgUCgQBMgvSAAsHYZMzA3Nz/Z+wRHhBDOcDiMzs7Os7ZT0ARIDyQz0969e3/d29t70t7HN8mXMxQKhS9fvvy3dXV1Hdm8SDTrVwZFbZkeaGlp2djV1fV4dXX1ciTpuFkpJSYmJuD3+08/8cQTF7N9hXCurA5mZiYhBNfU1LSl8KF6eXiGeYJU7i+sst34OUWAKCLo/YXztA6goQmgoQmgoQmQTUjENrtpJ4AQQm/zGnvbxbXBRLwbVCWEAKZp6gMiYygjEBFGR0cXxXMTKWVcR/PGRYDGxkYGgEceeeQa5k6uEtm8ZVqqCcDMiEQi9pEvNE/PwQCwZMmSAQBh66g+TikBPB6P/Sbj09PTgD7t43YtLwDg4MGDK8vKyh60yDCvja3sUc9wODyB/z02LuUvQgDw1FNPucfHx0fs42zmeWaQfcbQeuueRq4bn5mdADA8PPxu9LlJ80SEmfnKlSv7rPvGFIbjit1ExFY9PBiJRHoBLEGaJkhYZMx08tjlY+X3+19etmzZNqWUJCIj1pM/QqFQZzxfKBHiTQBQkUikHcCj6SKApT0yfsu21tbWNRs2bNi5atWqH1uEmLfxmRlEZITDYQQCgRM2udJCAK/XywDQ29t7fOnSpTtTuVEfADQ3N4tdu3apM2fOrLnrrrt+NDg4uNSOsZkAwzBQVlY26nK5ihcuXPiAy+X6bnFx8QLMzWOIqeczswIgQqHQxR07dpxP937FxMy0devWBWNjY33MrKSUMlUawOfzOQDgyy+//BFnD8w4/78d/1viif+JqgMwAOO9996bDQQC7wAgIUQ6hkl/b4UA+2cmXtJurzjbW8zOzoavXr36jpWNxdzeCSngeDwexcy0f//+9xYuXPhiaWlpcdRJ4CnzRNb7ZIMYjF1Fzm2GbAwPD/97fX19nxVGZDo9AHbt2qUAiG3bto1cuXLlH+fqFEKPyych2RFCYGZmJtLV1dXMzOT1euNW8AkjJzOLd99991fffPPNBQAOlcI52nkCE4AxMDDw6tNPP90NQMR7TkHCCEBE7PV66a233vr9xYsX/yoUCikhBCuldGk4Ma5fAnCOjo52vPTSSy9bYjnuDpbQdKmpqUn6fD7HY489dvqLL754AYAhhDBjOE5W44/jvpicnAwcO3bsLz788MMw5gaTMrNh7bTk3Llze620Jfwtx8onKg3ckqAUK6NgHxMfCAQiXV1dtbG0Uco8QFQ4kMxsrFu37tm+vr7fAHBaoUBrgvn1fFMIYUxNTUW6u7u31NTUfO7z+RyJnPiarHF8JiJljRP85Pz588MVFRUvW0VCEzk4GznBhmchhBRCOKampq6cOHFie0NDw3FmdhBRQsvdySyZsjVYZFRVVe3+7LPPfjg+Pn7dMj5bRRGNPy7ymFb9xDEyMtJ2+PDhjQ0NDcetnp+dx9PYmmDv3r0rBgYG3g+Hw3PBXylbH8g81gDK+s4R+xcTExPfXLhw4W/sd2xtbc3+wla0YT///PO6oaGhT0KhULQwXJdCAij7Ukpxsq/o51lktw0eif5SExMTk5cuXfrn/fv3L7fagpJ9RG3KYrElDAlz08baAbT39PQ8XFpauq20tPR7UspUsVxFh74UpVJ0i39jdnY2HA6Hz0xOTh76+OOPD23fvr3f7giW2OOcIEBUY8vW1lajsbFREVEHgI7NmzfvaGpqIuszScsU7Hw6EAhACBFRSjkp1pkY84MUQnBRUdHE5OTkLICBYDD437OzsyfHxsZO1NbW9t7kKVWqlrilRY3b5Utr3F4Q0czRo0ejhVBSKmlCCOPatWv/+f777/99cXFxZHx83BkMBl3JfFen04nS0tJpl8ultm7dOvrQQw9FBgcHQzeFRwJgeDwelZdrG60GiKkn3o4GsOcnfP3115dfeeWVuzLgfQUz28fcp3XySkbk40mOw8oaQQt2dHQ8/cILL4z5fD5He3t7yotSHo+Hkx3m8g7f5gEsBR5RSvG5c+d+EP15jQzyAEl0tREicvb19f1y3bp1/+b3+53r16+PaLPnAQEs0eccHBw8XFlZuTMZZdRcgMhh4xtjY2P/9cYbb/zYElpJz6k1ATLE/kIImp6enuzo6PjB66+/Pu31ekmvWcwfEThrmib7/f7va9GXZwSQUipm5u7ubg8A+P1+p26hPBGB1uQJunz58qHq6mqPFn15pgFcLtfCsbGxvh07dmzVoi+PPMD169cZALq7uwdmZ2d/6PV6p6A3qshf2HsWaOSh4bXxNTQ0NDQ0NDQ0NG4D/wOGq0hC5g01jgAAAABJRU5ErkJggg==" alt="ProOS">
  <h1>Connect Apple Music</h1>
  <p>Sign in with your Apple Account so ProOS Music can play your Apple Music library across the home.</p>
  <button id="go" disabled>Loading\u2026</button>
  <div class="msg" id="msg"></div>
  <div class="foot">You can close this window when you're done.</div>
</div></div>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
<script>
  var APP_TOKEN="__DEV_TOKEN__", SESSION="__SESSION__", CORE=location.origin;
  var go=document.getElementById('go'), msg=document.getElementById('msg');
  function fail(t){ go.disabled=false; go.textContent='Sign in with Apple'; msg.textContent=t; msg.className='msg err'; }
  document.addEventListener('musickitloaded', async function(){
    try{
      await MusicKit.configure({ developerToken: APP_TOKEN, app:{ name:'ProOS Music', build:'1.0.0' } });
      var music=MusicKit.getInstance();
      go.disabled=false; go.textContent='Sign in with Apple';
      go.onclick=async function(){
        go.disabled=true; go.textContent='Signing in\u2026'; msg.className='msg'; msg.textContent='';
        try{
          var token=await music.authorize();
          if(!token && music.musicUserToken) token=music.musicUserToken;
          if(!token) return fail('No token returned \u2014 try again.');
          await fetch(CORE+'/music/apple_auth/'+encodeURIComponent(SESSION),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({music_user_token:token})});
          go.textContent='Connected'; msg.className='msg'; msg.textContent='Apple Music connected. You can close this window.';
          setTimeout(function(){ try{ window.close(); }catch(e){} }, 1400);
        }catch(e){ console.error('authorize error', e); fail('Sign-in failed: '+((e&&(e.name||e.message))||'unknown')); }
      };
    }catch(e){ fail('Could not start Apple Music. '+(e&&e.message||'')); }
  });
  setTimeout(function(){ if(typeof MusicKit==='undefined'){ fail('Apple MusicKit failed to load.'); } }, 8000);
</script></body></html>"""


def get_controller(area: str) -> RoomController:
    if area not in _controllers:
        reach = (_cfg or {}).get("reachability", {})
        _controllers[area] = RoomController(_client, area, reachability=reach)
    return _controllers[area]


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
    except Exception as e:  # noqa: BLE001
        out["errors"].append("assist: %s" % e)
    for _fname in ("navconfig.json", "navcaps.json", "install_id"):
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
            self._user = auth.verify(tok) if tok else None
        except Exception:
            self._user = None
        if auth.REQUIRE and self._user is None:
            path = self.path.split("?")[0].strip("/")
            if path not in auth.PUBLIC_PATHS and not path.startswith("music/apple_auth/"):
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
            if len(parts) >= 3 and parts[:2] == ["music", "apple_auth"]:
                sess = unquote(parts[2])
                if len(parts) == 4 and parts[3] == "status":
                    _apple_reap()
                    with _APPLE_LOCK:
                        rec = _APPLE_AUTH.get(sess) or {}
                    return self._send(200, {"music_user_token": rec.get("token")})
                # Serve the ProOS-branded MusicKit sign-in page (public, session-keyed).
                html = _APPLE_AUTH_PAGE.replace("__DEV_TOKEN__", _apple_dev_token()).replace("__SESSION__", sess)
                return self._send_html(200, html)
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
            if len(parts) == 3 and parts[:2] == ["music", "apple_auth"]:
                # The ProOS sign-in page posts the Music User Token here (public route).
                sess = unquote(parts[2])
                b = self._body()
                tok = b.get("music_user_token")
                if tok:
                    with _APPLE_LOCK:
                        _APPLE_AUTH[sess] = {"token": tok, "ts": time.time()}
                return self._send(200, {"ok": bool(tok)})
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
                # Admin WRITE: prefer the installer's registered HA admin user,
                # routed over MA's ingress channel (:8094). An inline ingress_user
                # in the body overrides (used for verification). Falls back to the
                # direct :8095 save (system user) only when no admin user is known.
                au = _load_ma_admin_user()
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
                return self._send(200, _assist.chat(
                    _client, _ws_call, project, uinfo,
                    b.get("text") or "", session=b.get("session") or "default",
                    home_name=_ASSIST_HOME_NAME))
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
    _ma = MaCommissioner(_ma_conn)
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
        threading.Thread(target=_quarantine_loop, name="proos-no-auto-room", daemon=True).start()
        print(f"  watches · self-heal every {_WATCH_REDISCOVER_SEC}s", flush=True)
        _ab = _auto_cfg()
        print(f"  auto-backup {'ON @ ' + _ab['time'] if _ab['enabled'] else 'off'} "
              f"(encrypted: {'yes' if BACKUP_PASSWORD else 'no'})")
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
