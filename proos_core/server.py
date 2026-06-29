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
import datetime
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

from proos.live_ha import RestHAClient
from proos.controller import RoomController
from proos.monitor import Monitor, check_room
from proos.watcher import Watcher
from proos.music import MusicLayer
from proos.ma import MaCommissioner, MaUnavailable
from proos.ma_ws import MaAuthFailed, MaClient
from proos import sync

_controllers: dict[str, RoomController] = {}
_monitor: Monitor | None = None
_watcher: Watcher | None = None
_music: MusicLayer | None = None
_ma: "MaCommissioner | None" = None
_client = None
_cfg: dict | None = None


def get_controller(area: str) -> RoomController:
    if area not in _controllers:
        reach = (_cfg or {}).get("reachability", {})
        _controllers[area] = RoomController(_client, area, reachability=reach)
    return _controllers[area]


# ── Factory reset ───────────────────────────────────────────────────────────
# "Reset this home" restores a clean Home Assistant Core state from a partial
# backup shipped in the image (named below). HA Core only: the OS and this add-on
# are left untouched, so nothing has to be reinstalled. A recovery point of the
# CURRENT home is taken first; both backups live in /backups (outside /config),
# so they survive the restore and an accidental reset is always recoverable.
# Needs hassio_api + hassio_role:manager in config.yaml.
BASELINE_NAME = "proos-baseline"
SUPERVISOR = "http://supervisor"

# The Music Assistant server add-on. Core owns the integration that bridges it
# into HA; this slug is for the Supervisor running/stopped check only. When MA is
# repackaged as the pinned "ProOS Music" add-on, update this to that slug.
MA_ADDON_SLUG = "d5369777_music_assistant"
# Store repository the MA add-on ships from — registered on demand so a fresh
# installer box can install MA without the installer touching the HA store UI.
MA_ADDON_REPO = "https://github.com/music-assistant/home-assistant-addon"

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
        if d.get("host") and d.get("token"):
            return (d["host"], d.get("port"), d["token"])
    except Exception:
        pass
    return None


def _save_ma_conn(host, port, token):
    try:
        with open(_MA_CONN_FILE, "w") as f:
            json.dump({"host": host, "port": port, "token": token}, f)
    except Exception as e:
        print(f"  MA · could not persist conn: {e}", flush=True)


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


_MA_ADDON_SLUG = "d5369777_music_assistant"
_MA_API_PORT = 8095  # MA public API: /ws (WebSocket) + REST
_HA_STORAGE_ENTRIES = "/homeassistant/.storage/core.config_entries"


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
            if url and tok:
                return (url, tok)
    print("  MA · no music_assistant entry/token in HA store yet", flush=True)
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
    reachable host, and persist. Fully headless."""
    persisted = _load_ma_conn()
    if persisted:
        return persisted
    st = _ma_token_from_storage()
    if st:
        return _ma_validate_and_persist(st[1], st[0])
    return None


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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def log_message(self, *a):
        pass  # quiet

    def _room(self, parts):
        # parts like ['rooms', '<area>', '<verb>']
        return unquote(parts[1])

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

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            if parts == ["health"]:
                return self._send(200, {"ok": True, "home_id": _client.home_id})
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "activities":
                ctrl = get_controller(self._room(parts))
                return self._send(200, {"area": ctrl.area, "activities": ctrl.list_activities()})
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
            if parts == ["integrations"]:
                return self._send(200, _integrations_report())
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
            if parts == ["music", "providers"]:
                return self._send(200, {"providers": _ma.providers()})
            if parts == ["music", "players"]:
                return self._send(200, {"players": _ma.players()})
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
            return self._send(404, {"error": "not found"})
        except MaUnavailable as e:
            return self._send(503, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            # Execution routes (/intent, /heal, /recover) retired: Core no longer
            # drives rooms; activities run as HA scripts fired from the dashboard.
            if parts == ["sync"]:
                # Whole-home provision: walk every room with a display and create
                # its activity scripts. create-if-absent by default (installer edits
                # survive); POST /sync?overwrite=1 force-regenerates.
                qs = parse_qs(urlparse(self.path).query)
                overwrite = (qs.get("overwrite", ["0"])[0]).lower() in ("1", "true", "yes")
                return self._send(200, sync.sync_all(_client, overwrite=overwrite))
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
                if not st:
                    return self._send(503, {"error": "No Music token in Home Assistant "
                                            "yet — make sure the Music integration is set up."})
                conn = _ma_validate_and_persist(st[1], st[0])
                if not conn:
                    return self._send(503, {"error": "Found the Music token but couldn't "
                                            "reach the Music server to validate it."})
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
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "enabled":
                b = self._body()
                return self._send(200, _ma.set_provider_enabled(
                    unquote(parts[2]), bool(b.get("enabled"))))
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "entries":
                b = self._body()
                return self._send(200, {"entries": _ma.provider_entries(
                    unquote(parts[2]), b.get("instance_id"), b.get("action"), b.get("values"))})
            if len(parts) == 4 and parts[:2] == ["music", "providers"] and parts[3] == "save":
                b = self._body()
                return self._send(200, _ma.save_provider(
                    unquote(parts[2]), b.get("values") or {}, b.get("instance_id")))
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
            return self._send(404, {"error": "not found"})
        except MaAuthFailed as e:
            return self._send(401, {"error": str(e)})
        except MaUnavailable as e:
            return self._send(503, {"error": str(e)})
        except KeyError as e:
            return self._send(400, {"error": f"unknown activity {e}"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


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
                "name": "Music Assistant",
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
    global _client, _monitor, _watcher, _music, _ma, _cfg
    cfg = load_config()
    _cfg = cfg
    _client = RestHAClient(cfg["home_id"], cfg["base_url"], cfg["token"])
    print(f"ProOS Core API  home={_client.home_id}  ha={cfg['base_url']}")
    print(f"  HA says: {_client.ping()}")
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
    _watcher = Watcher(_client)
    _watcher.run_forever(interval=5)
    print("  watcher running (interval 5s) -> GET /watchers")
    # Music Assistant is an optional, certified ProOS integration (off by
    # default). The layer + commissioner are always constructed so the
    # on-demand endpoints (/music, /music/setup, /music/connect, /integrations)
    # keep working, but Core only auto-commissions and connects MA at boot when
    # the integration is enabled — so a disabled or absent MA produces no boot
    # retries and no connect errors.
    _music = MusicLayer(_client)
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
