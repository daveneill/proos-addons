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
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

from proos.live_ha import RestHAClient
from proos.controller import RoomController
from proos.monitor import Monitor, check_room
from proos.watcher import Watcher
from proos import sync

_controllers: dict[str, RoomController] = {}
_monitor: Monitor | None = None
_watcher: Watcher | None = None
_client = None
_cfg: dict | None = None


def get_controller(area: str) -> RoomController:
    if area not in _controllers:
        reach = (_cfg or {}).get("reachability", {})
        _controllers[area] = RoomController(_client, area, reachability=reach)
    return _controllers[area]


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
            return self._send(404, {"error": "not found"})
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
            return self._send(404, {"error": "not found"})
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
        }

    # --- Standalone mode (e.g. on a Mac) ------------------------------------
    if not os.path.exists(path):
        sys.exit("No config.json. Run: cp config.example.json config.json")
    with open(path) as f:
        cfg = json.load(f)
    if "PASTE" in cfg.get("token", ""):
        sys.exit("config.json still has the placeholder token.")
    return cfg


def main():
    global _client, _monitor, _watcher, _cfg
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
