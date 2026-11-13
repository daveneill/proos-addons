"""GOOGLE AND ALEXA THROUGH THE MATTER HUB — register 1330 (13 Nov 2026, plan item 24). DRIVES the real
proos/matterhub.py against a stand-in Supervisor and a stand-in hub (its 2.0.59 API: GET/POST /api/matter/bridges,
GET/DELETE /api/matter/bridges/<id>, the BridgeDataWithMetadata shape).
Run: python3 tests/google_and_alexa_through_the_matter_hub_bench.py

  1. the hub is found by asking the Supervisor (its slug ends _hamh) — not installed / not running is said, not guessed
  2. Core reaches it from INSIDE the box: the platform asks for the ingress session (the Supervisor takes that from the
     platform only — Core asking directly got 401 on the box), and the Supervisor's own ingress route carries the call
  3. the test bridge is ONE room's lights: area AND domain light ("all"), named "ProOS Bridge"
  4. what Pro shows is the hub's reading: status, devices, the ones that failed, paired, pairing code
  5. who it is paired with comes from the controller's own vendor id; an unknown id is shown as its number
  6. one ProOS Bridge at a time; remove takes back only ProOS's own
  7. (SOURCE) the routes are installer-only, and the module rides in the zip
"""
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
from proos import matterhub as M  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


class Sup:
    def __init__(self, installed=True, state="started"):
        self.installed, self.state, self.calls = installed, state, []

    def __call__(self, m, path, payload=None, timeout=120):
        self.calls.append((m, path))
        if path == "/addons":
            return {"addons": [{"slug": "core_matter_server", "installed": True}] +
                    ([{"slug": "3fd9e6b0_hamh", "installed": True}] if self.installed else [])}
        if path == "/addons/3fd9e6b0_hamh/info":
            return {"version": "2.0.59", "state": self.state, "ingress_entry": "/api/hassio_ingress/ENTRY/"}
        raise AssertionError(path)


class HubSrv:
    def __init__(self):
        self.bridges, self.seen = [{"id": "b-other", "name": "Garage things", "status": "running", "deviceCount": 2}], []

    def __call__(self, req, timeout=None):
        self.seen.append((req.get_method(), req.full_url, req.get_header("Cookie")))
        path = req.full_url.split("/api/matter/", 1)[1]
        m = req.get_method()
        out = None
        if path == "bridges" and m == "GET":
            out = self.bridges
        elif path == "bridges" and m == "POST":
            body = json.loads(req.data)
            b = dict(body, id="b-proos", port=5541, status="running", deviceCount=2,
                     failedEntities=[{"entityId": "light.office_strip", "reason": "unsupported color mode"}],
                     commissioning={"isCommissioned": True, "manualPairingCode": "3497-011-2332",
                                    "fabrics": [{"rootVendorId": 0x6006}, {"rootVendorId": 0x1217}, {"rootVendorId": 4999}]})
            self.bridges.append(b)
            out = b
        elif path.startswith("bridges/") and m == "GET":
            out = next(b for b in self.bridges if b["id"] == path.split("/")[1])
        elif path.startswith("bridges/") and m == "DELETE":
            self.bridges = [b for b in self.bridges if b["id"] != path.split("/")[1]]
        data = json.dumps(out).encode()

        class R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return data
        return R()


WS = []


def ws(msg_type, **fields):
    WS.append((msg_type, fields))
    return {"session": "S3SS"}


st = M.status(M.Hub(Sup(installed=False), ws))
st2 = M.status(M.Hub(Sup(state="stopped"), ws))
check("1 · found by asking the Supervisor; not installed / not running said as such",
      (st, st2["installed"], st2["running"], st2["version"], st2["bridges"]),
      ({"installed": False, "running": False, "version": None, "bridges": []}, True, False, "2.0.59", []))
srv = HubSrv()
sup = Sup()
hub = M.Hub(sup, ws, "http://supervisor", opener=srv)
M.status(hub)
check("2 · reached from inside the box: the PLATFORM asks for the session; Core calls the Supervisor's ingress route",
      (WS[0], srv.seen[0], ("POST", "/ingress/session") in sup.calls),
      (("supervisor/api", {"endpoint": "/ingress/session", "method": "post"}),
       ("GET", "http://supervisor/ingress/ENTRY/api/matter/bridges", "ingress_session=S3SS"), False))
req = M.test_bridge_request("office")
check("3 · the test bridge is one room's lights — area AND light — named ProOS Bridge",
      req, {"name": "ProOS Bridge", "filter": {"include": [{"type": "area", "value": "office"},
                                                           {"type": "domain", "value": "light"}],
                                               "exclude": [], "includeMode": "all"}})
bad = 0
for a, d in (("", ("light",)), ("office", ("light", "switch")), ("office", ())):
    try:
        M.test_bridge_request(a, d)
    except ValueError:
        bad += 1
check("   …refused without a room, or with other than one kind of device", bad, 3)
made = M.make_test_bridge(hub, "office")
check("4 · Pro shows the hub's own reading: status, devices, what failed, paired, the pairing code",
      (made["status"], made["devices"], made["failed"], made["paired"], made["code"], made["proos"]),
      ("running", 2, [{"entity_id": "light.office_strip", "reason": "unsupported color mode"}], True, "3497-011-2332", True))
check("5 · paired-with from the controller's own vendor id; an unknown one shown as its number",
      made["controllers"], ["Google Home", "Amazon Alexa", "Vendor 4999"])
try:
    M.make_test_bridge(hub, "kitchen")
    twice = True
except ValueError:
    twice = False
try:
    M.remove_bridge(hub, "b-other")
    other = True
except ValueError:
    other = False
M.remove_bridge(hub, "b-proos")
check("6 · one ProOS Bridge at a time; remove takes back only ProOS's own",
      (twice, other, [b["id"] for b in srv.bridges]), (False, False, ["b-other"]))
srv_src = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
g = srv_src.split('if parts == ["matterhub"]:')[1][:400]
p = srv_src.split('if parts and parts[0] == "matterhub":')[1][:400]
z = open(os.path.join(CORE, "zipfiles.txt"), encoding="utf-8").read().split()
check("7 · (SOURCE) both routes installer-only; the module rides in the zip",
      ("installer access required" in g and "installer access required" in p and "proos/matterhub.py" in z))
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
