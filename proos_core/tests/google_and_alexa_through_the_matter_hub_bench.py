"""GOOGLE AND ALEXA THROUGH THE MATTER HUB — register 1330 (13 Nov 2026, plan item 24). DRIVES the real
proos/matterhub.py against a stand-in Supervisor and a stand-in hub (its 2.0.59 API: GET/POST /api/matter/bridges,
GET/DELETE /api/matter/bridges/<id>, the BridgeDataWithMetadata shape).
Run: python3 tests/google_and_alexa_through_the_matter_hub_bench.py

  1. the hub is found by asking the Supervisor (its slug ends _hamh) — not installed / not running is said, not guessed
  2. Core reaches it from INSIDE the box through the DOOR (Core asking the Supervisor got 401, the platform's websocket
     'unauthorized' — 1330b, 1330c); Core holds no session
  8. the door: answers only a call Core vouches for (a one-time word, asked back of Core — 1330e: the address check
     refused Core itself on the box); carries the hub's bridge paths only; makes the session for its panel
     user and calls the Supervisor's ingress route; ProOS's front door never passes the path
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
        self.vouchers = []
        self.bridges, self.seen = [{"id": "b-other", "name": "Garage things", "status": "running", "deviceCount": 2}], []

    def __call__(self, req, timeout=None):
        self.seen.append((req.get_method(), req.full_url, req.get_header("Cookie")))
        self.vouchers.append(req.get_header("X-proos-voucher"))
        path = req.full_url.split("/matterhub/", 1)[1].split("?")[0]
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


BASE = lambda: "http://homeassistant:8123"  # noqa: E731
st = M.status(M.Hub(Sup(installed=False), BASE))
st2 = M.status(M.Hub(Sup(state="stopped"), BASE))
check("1 · found by asking the Supervisor; not installed / not running said as such",
      (st, st2["installed"], st2["running"], st2["version"], st2["bridges"]),
      ({"installed": False, "running": False, "version": None, "bridges": []}, True, False, "2.0.59", []))
srv = HubSrv()
sup = Sup()
hub = M.Hub(sup, BASE, opener=srv)
M.status(hub)
check("2 · Core asks the DOOR, on the platform from inside the box, with the hub's app and address — no session of its own",
      (srv.seen[0], ("POST", "/ingress/session") in sup.calls),
      (("GET", "http://homeassistant:8123/api/proos_core_only/matterhub/bridges?slug=3fd9e6b0_hamh&token=ENTRY", None), False))
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

# ── 8 · the door file (door build 11) against a stand-in platform ──
import asyncio  # noqa: E402
import types  # noqa: E402

made_sessions, outbound = [], []


class View:
    def json(self, d, status_code=200):
        return ("json", status_code, d)


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self.body


class Sess:
    def get(self, url, params=None, timeout=None):
        assert url == "http://abc-proos:8770/door/vouch"
        return VouchResp(M.vouch(params["word"]))

    def request(self, method, url, data=None, headers=None, cookies=None, timeout=None):
        outbound.append((method, url, cookies))
        return Resp(200, b"[]")


class VouchResp:
    def __init__(self, ok):
        self.ok = ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return {"vouched": self.ok}


class Ingress:
    async def create_session(self, opts):
        made_sessions.append(opts.user_id)
        return "SESSION"


web = types.ModuleType("aiohttp.web")
web.Response = lambda status, body, content_type: ("resp", status, body)
aio = types.ModuleType("aiohttp")
aio.web, aio.ClientTimeout = web, (lambda total=None: total)
http = types.ModuleType("homeassistant.components.http")
http.HomeAssistantView = View
cs = types.ModuleType("homeassistant.helpers.aiohttp_client")
cs.async_get_clientsession = lambda hass: Sess()
models = types.ModuleType("aiohasupervisor.models")
models.CreateSessionOptions = lambda user_id: types.SimpleNamespace(user_id=user_id)
handler = types.ModuleType("homeassistant.components.hassio.handler")
handler.get_supervisor_client = lambda hass: types.SimpleNamespace(ingress=Ingress())
const = types.ModuleType("homeassistant.components.hassio.const")
const.DATA_COMPONENT = "hassio"
for n, m in {"aiohttp": aio, "aiohttp.web": web, "homeassistant": types.ModuleType("homeassistant"),
             "homeassistant.components": types.ModuleType("homeassistant.components"),
             "homeassistant.components.http": http, "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
             "homeassistant.helpers.aiohttp_client": cs, "aiohasupervisor": types.ModuleType("aiohasupervisor"),
             "aiohasupervisor.models": models, "homeassistant.components.hassio": types.ModuleType("homeassistant.components.hassio"),
             "homeassistant.components.hassio.handler": handler, "homeassistant.components.hassio.const": const}.items():
    sys.modules[n] = m
from proos import door as D  # noqa: E402
from proos import frontdoor as FD  # noqa: E402

DM = types.ModuleType("custom_components.proos.matterhub")
sys.modules[DM.__name__] = DM
exec(compile(D.files("http://abc-proos:8770")["matterhub.py"].decode(), "matterhub.py", "exec"), DM.__dict__)


hass = types.SimpleNamespace(data={"hassio": types.SimpleNamespace(base_url="http://172.30.32.2")})
v = DM.MatterHubDoor(hass, types.SimpleNamespace(data={"panel_user": "u-panel"}))


def rq(word, method="GET", q=None):
    async def read():
        return b""
    return types.SimpleNamespace(remote="172.30.32.1", method=method, headers={"X-ProOS-Voucher": word} if word else {},
                                 query=q or {"slug": "3fd9e6b0_hamh", "token": "ENTRY"}, read=read)


async def door_runs():
    none = await v.get(rq(None), "bridges")
    made_up = await v.get(rq("guessed-word"), "bridges")
    w = M.issue_voucher()
    first = await v.get(rq(w), "../addons/other")
    w2 = M.issue_voucher()
    c = await v.get(rq(w2), "bridges")
    reused = await v.get(rq(w2), "bridges")
    old = M.issue_voucher(now=1.0)
    stale = await v.get(rq(old), "bridges")
    return none, made_up, first, c, reused, stale


none, made_up, first, c, reused, stale = asyncio.run(door_runs())
check("8 · the door answers only a call Core vouches for — none, made up, used twice or stale is refused",
      (none[1], made_up[1], reused[1], stale[1], none[2]["error"]), (403, 403, 403, 403, "for ProOS Core only"))
check("   …and carries the hub's bridge paths only", first[1], 404)
check("   …makes the session for its panel user inside the platform and calls the Supervisor's ingress route",
      (made_sessions, outbound, c[:2]),
      (["u-panel"], [("GET", "http://172.30.32.2/ingress/ENTRY/api/matter/bridges", {"ingress_session": "SESSION"})], ("resp", 200)))
srvsrc = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
check("   (SOURCE) Core's /door/vouch answers the voucher and nothing else; Core sends one with every hub call",
      ('parts == ["door", "vouch"]' in srvsrc and '_mhv.vouch(_w)' in srvsrc.split('parts == ["door", "vouch"]')[1][:600],
       len(srv.vouchers) > 3 and all(v and len(v) >= 32 for v in srv.vouchers) and len(set(srv.vouchers)) == len(srv.vouchers)), (True, True))
check("   …ProOS's front door never passes the path; the door is build 11 and installs the file",
      (FD.is_platform_path("api/proos_core_only/matterhub/bridges"), FD.is_platform_path("api/states"), D.DOOR_BUILD,
       "matterhub.py" in D.DOOR_FILES, "matterhub.async_register(hass, entry)" in D.files()["__init__.py"].decode()),
      (False, True, "12", True, True))
_authsrc = srvsrc.split("    def _auth(self")[1].split("    def _body(self")[0]
check("   (SOURCE) the door's question passes Core's sign-in gate, GET only (1330g: on the box it met a 401)",
      'if path == "door/vouch" and self.command == "GET":\n                return True' in _authsrc
      and _authsrc.index('"door/vouch"') < _authsrc.index("if path not in auth.PUBLIC_PATHS"))
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
