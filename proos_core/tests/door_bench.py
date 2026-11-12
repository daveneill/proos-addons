"""THE REMOTE DOOR — ProOS Core reachable through the box's own front door
(register 351, Dave's ruling A, 7 Sep 2026).
Run: python3 tests/door_bench.py

Away from home the page loads through the remote address but every Core
call went to that host on Core's port, where nothing answers (350). Now
Core installs a small integration into the platform's config folder that
publishes /api/proos/<anything> on the platform's own web server and
hands it to Core inside the box — so Core is reachable through whatever
address the platform is. Auth stays Core's. The platform is never named.

DRIVEN — the real writer on a real temp folder, the real state machine
with a fake client, and the INTEGRATION'S OWN VIEW imported and driven
with the platform's modules stubbed (the platform is not installed here):
 1. the writer: three files, install when different, leave alone when
    identical (mtime untouched), touch nothing else, errors recorded
 2. the files: valid manifest (config_flow, single entry, the door's
    build), Core's own address inside, the door build stamped, both
    Python files compile
 3. the state machine: open · restart (files newer than the platform
    loaded / flow refused) · opening (entry made, door not yet up) ·
    stale (older door running) · unreachable · closed — each in words,
    never naming the platform; the cache re-asks within DOOR_RECHECK_S
 4. the view: the root answers the door's build; a path is handed to Core
    with method, query, body and headers (hop-by-hop stripped); the reply
    streams back chunk by chunk with its status and content type; Core
    silent → 502 said in words; the mint of the route's shape
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import door as D                                      # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


# 1 · the writer
cfg = tempfile.mkdtemp(prefix="proos_door_")
r = D.install(cfg, core_url="http://b333b432-proos-core:8770")
dest = os.path.join(cfg, "custom_components", "proos")
check("fresh box: the three files are installed",
      sorted(r["installed"]), sorted(D.DOOR_FILES))
check("  …into custom_components/proos", r["dest"], dest)
# re-pinned with cause, register 1326: door build 10 ships translations/en.json in a subfolder — every file under the
# folder is walked, so "nothing else is written" still holds to the file
check("  …and nothing else is written", sorted(os.path.relpath(os.path.join(b, f), dest) for b, _d, fs in os.walk(dest)
                                             for f in fs), sorted(D.DOOR_FILES))
m0 = os.stat(os.path.join(dest, "__init__.py")).st_mtime_ns
r2 = D.install(cfg, core_url="http://b333b432-proos-core:8770")
check("second boot: identical files are left alone", (r2["installed"], sorted(r2["current"])),
      ([], sorted(D.DOOR_FILES)))
check("  …not even rewritten", os.stat(os.path.join(dest, "__init__.py")).st_mtime_ns, m0)
with open(os.path.join(dest, "__init__.py"), "w") as fh:
    fh.write("# an older door\n")
r3 = D.install(cfg, core_url="http://b333b432-proos-core:8770")
check("a differing file is replaced", r3["installed"], ["__init__.py"])
open(os.path.join(dest, "stranger.py"), "w").write("keep me\n")
D.install(cfg, core_url="http://b333b432-proos-core:8770")
check("a neighbour in the folder survives", os.path.exists(os.path.join(dest, "stranger.py")))
r4 = D.install(os.path.join(cfg, "nowhere", "\0bad") if os.name != "nt" else "?:\\bad")
check("an unwritable folder is an error recorded, never raised", bool(r4["errors"]))

# 2 · the files
f = D.files("http://b333b432-proos-core:8770")
man = json.loads(f["manifest.json"])
check("manifest: domain proos, a config flow, one entry, the door's build",
      (man["domain"], man["config_flow"], man["single_config_entry"], man["version"]),
      ("proos", True, True, "1.0." + D.DOOR_BUILD))
# RE-PINNED, register 928 (door build 3): the door now also carries "ProOS Assist",
# a conversation agent, so the platform must load its voice side first. Still
# nothing beyond the platform's own web server and its own conversation component.
# RE-PINNED, register 1259 (door build 6): the door also announces the box as ProOS through the platform's own announcer,
# so the announcer (zeroconf) loads first, and the SSDP announcer when present. Still nothing beyond the platform's own.
check("  …depends on the platform's web server, its own voice side and its own announcer only",
      (man["dependencies"], man.get("after_dependencies")), (["http", "conversation", "zeroconf"], ["ssdp"]))
init = f["__init__.py"].decode()
check("the integration carries Core's own address inside the box",
      'CORE = "http://b333b432-proos-core:8770"' in init)
check("  …and the door's build", 'DOOR_BUILD = "%s"' % D.DOOR_BUILD in init)
check("  …publishes /api/proos/<anything> without its own gate (Core is the gate)",
      'url = "/api/proos/{tail:.*}"' in init and "requires_auth = False" in init)
for name in ("__init__.py", "config_flow.py"):
    try:
        ast.parse(f[name].decode())
        check("  …%s compiles" % name, True)
    except SyntaxError as e:
        check("  …%s compiles" % name, str(e), "")
check("Core's address defaults to this add-on's hostname and port",
      D.core_address(8770).endswith(":8770") and D.core_address(8770).startswith("http://"))
low = (init + f["config_flow.py"].decode()).lower()
check("nothing a person reads in the integration names the platform",
      all(w not in low.replace("homeassistant.", "").replace("from homeassistant", "")
          for w in ("home assistant", "nabu")))


# 3 · the state machine
class FakeClient:
    def __init__(self, door=None, entries=(), flow=None, down=False):
        self.door, self.entries, self.flow, self.down = door, list(entries), flow, down
        self.calls = []

    def config_entries(self, domain=None):
        # REGISTER 448: the door reads entries over the WebSocket call, the
        # same one the rest of Core uses (the REST index answered a
        # Supervisor-token caller with an empty list).
        self.calls.append(("WS", "config_entries/get"))
        if self.down:
            raise RuntimeError("Cannot reach the hub at http://supervisor/core")
        out = [{"domain": d, "entry_id": "e%d" % i} for i, d in enumerate(self.entries)]
        return [e for e in out if not domain or e["domain"] == domain]

    def _req(self, method, path, payload=None):
        self.calls.append((method, path))
        if self.down:
            raise RuntimeError("Cannot reach the hub at http://supervisor/core")
        if path == "/api/proos/":
            if self.door is None:
                raise RuntimeError("GET /api/proos/ -> HTTP 404: not found")
            return {"proos_door": self.door}
        if path == "/api/config/config_entries/flow":
            if isinstance(self.flow, Exception):
                raise self.flow
            if self.flow == "create_entry":
                self.entries.append("proos")
                self.door = D.DOOR_BUILD
            return {"type": self.flow}
        return {}


s = D.ensure_open(FakeClient(door=D.DOOR_BUILD))
check("the door answering with this build → open", (s["state"], s["running"]), ("open", D.DOOR_BUILD))
s = D.ensure_open(FakeClient(door="0"))
check("an older door running → stale, restart asked", s["state"], "stale")
s = D.ensure_open(FakeClient(door=None, entries=["proos"]))
check("entry present, door silent → the files are newer than what runs → restart", s["state"], "restart")
c = FakeClient(door=None, flow=RuntimeError("POST /api/config/config_entries/flow -> HTTP 400: Invalid handler specified"))
s = D.ensure_open(c)
check("no entry, the flow refused (not loaded yet) → restart", s["state"], "restart")
check("  …and Core tried to open it itself first", ("POST", "/api/config/config_entries/flow") in c.calls)
c = FakeClient(door=None, flow="create_entry")
s = D.ensure_open(c)
check("no entry, the flow makes one and the door answers → open, no click by anyone",
      (s["state"], c.entries), ("open", ["proos"]))
s = D.ensure_open(FakeClient(down=True))
check("the box not answering → unreachable, said", s["state"], "unreachable")
s = D.ensure_open(FakeClient(door=None), {"installed": [], "current": [], "errors": ["mkdir failed: read-only"]})
check("files could not be written → closed, with the reason", (s["state"], "read-only" in s["words"]), ("closed", True))
for st, w in D._WORDS.items():
    check("words for %r never name the platform" % st,
          all(x not in w.lower() for x in ("home assistant", "hass", "nabu", "integration")))
# PLAN H1 interim (3 Oct 2026, T5 on Dave's box): away-from-home was never set up, and this
# pin made the box claim it worked. The open state now claims only what the door measures.
check("  …and the open state claims only what it measured — not away from home",
      ("at home and away" not in D._WORDS["open"]) and ("not been proven" in D._WORDS["open"]))
# the cache
D._cache.update(at=0.0, state=None)
c = FakeClient(door=None, entries=["proos"])
D.state(c, force=True)
n = len(c.calls)
D.state(c)
check("within DOOR_RECHECK_S the box is not asked again", len(c.calls), n)
D._cache["at"] -= D.DOOR_RECHECK_S + 1
D.state(c)
check("  …after it, it is", len(c.calls) > n)


# 4 · the view, driven with the platform stubbed
class _Json:
    def __init__(self, obj, status):
        self.obj, self.status = obj, status


class _View:
    def json(self, obj, status_code=200, headers=None):
        return _Json(obj, status_code)


class _Timeout:
    def __init__(self, **kw):
        self.kw = kw


class _Stream:
    def __init__(self, status=200, headers=None):
        self.status, self.headers, self.chunks, self.prepared, self.eof = status, dict(headers or {}), [], False, False

    async def prepare(self, request):
        self.prepared = True

    async def write(self, b):
        self.chunks.append(b)

    async def write_eof(self):
        self.eof = True


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


aiohttp = _stub("aiohttp", ClientTimeout=_Timeout)
_stub("aiohttp.web", StreamResponse=_Stream)
aiohttp.web = sys.modules["aiohttp.web"]
_stub("homeassistant")
_stub("homeassistant.components")
_stub("homeassistant.components.http", HomeAssistantView=_View)
_stub("homeassistant.helpers")
SESSION = {}
_stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: SESSION["s"])
mod = types.ModuleType("proos_door_under_test")
exec(compile(init, "custom_components/proos/__init__.py", "exec"), mod.__dict__)


class _Content:
    def __init__(self, chunks):
        self._c = list(chunks)

    def iter_any(self):
        async def gen():
            for ch in self._c:
                yield ch
        return gen()


class _Up:
    def __init__(self, status, headers, chunks):
        self.status, self.headers, self.content, self.released = status, headers, _Content(chunks), False

    def release(self):
        self.released = True


class _Session:
    def __init__(self, up=None, fail=False):
        self.up, self.fail, self.calls = up, fail, []

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.fail:
            raise ConnectionError("refused")
        return self.up


class _Req:
    def __init__(self, method="GET", headers=None, query="", body=b""):
        self.method, self.headers, self.query_string, self._body = method, dict(headers or {}), query, body

    async def read(self):
        return self._body


view = mod.ProOSDoor(hass=object())
root = asyncio.run(view._through(_Req(), ""))
check("the door itself answers its build at /api/proos/", (root.obj, root.status), ({"proos_door": D.DOOR_BUILD}, 200))
SESSION["s"] = _Session(_Up(200, {"Content-Type": "application/json", "Content-Length": "9", "Transfer-Encoding": "chunked",
                                  "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization"},
                            [b'{"ok":', b'true}']))
out = asyncio.run(view._through(_Req("POST", {"Authorization": "Bearer tok", "Host": "remote.example", "Content-Type": "application/json",
                                             "Content-Length": "2"}, "device=ipad", b"{}"), "health"))
m, url, kw = SESSION["s"].calls[0]
check("a path is handed to Core: method, path, query", (m, url), ("POST", "http://b333b432-proos-core:8770/health?device=ipad"))
check("  …with the sign-in header and content type, hop-by-hop stripped",
      (kw["headers"].get("Authorization"), kw["headers"].get("Content-Type"), "Host" in kw["headers"], "Content-Length" in kw["headers"]),
      ("Bearer tok", "application/json", False, False))
check("  …the body whole, no redirect followed, no read timeout (the live feed never ends)",
      (kw["data"], kw["allow_redirects"], kw["timeout"].kw.get("total"), kw["timeout"].kw.get("sock_read")),
      (b"{}", False, None, None))
check("  …the reply streams back chunk by chunk with Core's status and content type",
      (out.status, out.headers.get("Content-Type"), out.chunks, out.prepared, out.eof),
      (200, "application/json", [b'{"ok":', b'true}'], True, True))
check("  …framing headers are the door's own, never copied", "Content-Length" in out.headers or "Transfer-Encoding" in out.headers, False)
# DOOR BUILD 2 (register 354, read from the box's log): Core's own cross-origin
# headers rode through build 1 and the platform's server REFUSED every such
# reply (aiohttp_cors asserts the origin header is not already there) — every
# POST through the door died with a 500. They are Core's business on its bare
# port and nobody's through the door.
check("  …Core's cross-origin headers never pass the door (the platform's server adds its own and refuses a copy)",
      [k for k in out.headers if k.lower().startswith("access-control-")], [])
check("  …the door's build is 2 or later", int(D.DOOR_BUILD) >= 2)
check("  …and Core's reply is released", SESSION["s"].up.released)
SESSION["s"] = _Session(fail=True)
out = asyncio.run(view._through(_Req(), "health"))
check("Core silent → 502, said in words", (out.status, out.obj), (502, {"error": "ProOS Core is not answering"}))
check("every method has a door", all(hasattr(mod.ProOSDoor, x) for x in ("get", "post", "put", "patch", "delete")))
check("the door sits on the platform's own API prefix (rides its remote service unchanged)", mod.ProOSDoor.url.startswith("/api/"))

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + str(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
