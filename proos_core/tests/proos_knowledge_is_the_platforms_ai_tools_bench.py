"""PROOS'S KNOWLEDGE IS THE PLATFORM'S OWN AI TOOLS — register 1325 (12 Nov 2026, plan item D17). DRIVES the real
door file Core writes (llm_tools.py, door build 9) with the platform's llm classes stood in by their 2026.11.1 shapes
(API is a kw_only dataclass; Tool, ToolResult, ToolAnnotations, APIInstance, async_register_api), and the real
assist.platform_tools / run_platform_tool.
Run: python3 tests/proos_knowledge_is_the_platforms_ai_tools_bench.py

  1. the tool set is "ProOS", registered by the door, taken back on unload
  2. its tools are Pro Assist's OWN, read from Core: the name, Pro Assist's description, its parameters
  3. every tool is declared read-only to the platform (its tool server forwards that)
  4. a call goes to Core's /assist/tool with the panel's sign-in, and Core's answer comes back as the tool's result
  5. Core silent → the AI is told "ProOS is not answering", never a crash, never silence
  6. Core runs ONLY the read-only list: room_volume (an act) is refused there, whatever asks
  7. the list is kept a while (Core not asked on every turn), and a Core that was down is asked again
  8. (SOURCE) the two routes: GET /assist/platform_tools, POST /assist/tool, both signed-in only
"""
import asyncio
import dataclasses
import os
import sys
import types

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


# ── the platform's llm helper, in its 2026.11.1 shapes ──────────────────────────────────────────────────────────────
REGISTERED = []


@dataclasses.dataclass(frozen=True)
class ToolAnnotations:
    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = True


@dataclasses.dataclass
class ToolResult:
    data: dict
    error: bool = False


class Tool:
    name = ""
    parameters = None


@dataclasses.dataclass(slots=True, kw_only=True)
class API:
    hass: object
    id: str
    name: str
    requires_admin: bool = False


@dataclasses.dataclass
class APIInstance:
    api: object
    api_prompt: str
    llm_context: object
    tools: list


def async_register_api(hass, api):
    REGISTERED.append(api)
    return lambda: REGISTERED.remove(api)


class Schema:
    def __init__(self, d):
        self.d = d

    def __call__(self, args):
        for k in self.d:
            if getattr(k, "required", False) and k.key not in args:
                raise ValueError("missing " + k.key)
        return dict(args)


class Mark:
    def __init__(self, key, description=None):
        self.key, self.description = key, description


class Required(Mark):
    required = True


class Optional(Mark):
    required = False


llm = types.ModuleType("homeassistant.helpers.llm")
llm.__dict__.update(ToolAnnotations=ToolAnnotations, ToolResult=ToolResult, Tool=Tool, API=API, APIInstance=APIInstance,
                    async_register_api=async_register_api)
probatio = types.ModuleType("probatio")
probatio.__dict__.update(Schema=Schema, Required=Required, Optional=Optional)


class FakeCore:
    def __init__(self):
        self.calls, self.lists, self.up = [], 0, True


CORE_STUB = FakeCore()


class Resp:
    def __init__(self, data):
        self._d = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._d


class Session:
    def get(self, url, headers=None, timeout=None):
        if not CORE_STUB.up:
            raise OSError("connection refused")
        CORE_STUB.lists += 1
        assert url.endswith("/assist/platform_tools")
        from proos import assist as A
        return Resp({"tools": A.platform_tools()})

    def post(self, url, json=None, headers=None, timeout=None):
        if not CORE_STUB.up:
            raise OSError("connection refused")
        CORE_STUB.calls.append((url, json, headers))
        return Resp({"area_id": json["args"].get("area_id"), "activity": "watching", "endpoints": [{"name": "Office Sonos", "volume": "30%"}]})


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientTimeout = lambda total=None: total
mods = {"homeassistant": types.ModuleType("homeassistant"), "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.llm": llm, "homeassistant.helpers.aiohttp_client": types.ModuleType("homeassistant.helpers.aiohttp_client"),
        "probatio": probatio, "aiohttp": aiohttp}
mods["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: Session()
mods["homeassistant.helpers"].llm = llm
sys.modules.update(mods)

from proos import assist as A  # noqa: E402
from proos import door as D  # noqa: E402

src = D.files("http://127.0.0.1:8770")["llm_tools.py"].decode()
LT = types.ModuleType("custom_components.proos.llm_tools")
sys.modules[LT.__name__] = LT     # as the platform imports it: a named module
exec(compile(src, "llm_tools.py", "exec"), LT.__dict__)


class Auth:
    async def async_get_user(self, uid):
        return types.SimpleNamespace(id="u-panel", refresh_tokens={"r": "rt"})

    def async_create_access_token(self, rt):
        return "panel-token"


class Entry:
    def __init__(self):
        self.data, self.unloads = {"panel_user": "u-panel"}, []

    def async_on_unload(self, f):
        self.unloads.append(f)


async def main():
    hass = types.SimpleNamespace(auth=Auth(), config_entries=None)
    entry = Entry()
    undo = LT.async_register(hass, entry)
    api = REGISTERED[0]
    check("1 · the door registers a \"ProOS\" tool set", (len(REGISTERED), api.id, api.name), (1, "proos", "ProOS"))
    inst = await api.async_get_api_instance(object())
    t = inst.tools[0]
    want = next(x for x in A.TOOLS if x["name"] == "room_status")
    check("2 · its tool is Pro Assist's own room_status, read from Core", ([x.name for x in inst.tools], t.description),
          (["proos_room_status"], want["description"]))
    check("   …with Pro Assist's parameters (area_id, required)", [(k.key, k.required) for k in t.parameters.d], [("area_id", True)])
    check("3 · declared read-only to the platform", (t.annotations.read_only, t.annotations.destructive, t.integration), (True, False, "proos"))
    res = await t.async_call(hass, types.SimpleNamespace(tool_args={"area_id": "office"}), object())
    url, body, hdr = CORE_STUB.calls[-1]
    check("4 · a call goes to Core's /assist/tool with the panel's sign-in", (url, body, hdr),
          ("http://127.0.0.1:8770/assist/tool", {"name": "room_status", "args": {"area_id": "office"}}, {"Authorization": "Bearer panel-token"}))
    check("   …and Core's answer is the tool's result", (res.error, res.data["activity"], res.data["endpoints"][0]["volume"]), (False, "watching", "30%"))
    CORE_STUB.up = False
    res2 = await t.async_call(hass, types.SimpleNamespace(tool_args={"area_id": "office"}), object())
    check("5 · Core silent → \"ProOS is not answering\", as an error result", (res2.error, res2.data["error"].startswith("ProOS is not answering")), (True, True))

    class Runner:
        def __init__(self):
            self.ran = []

        def run(self, name, args):
            self.ran.append(name)
            return {"ok": True}

    r = Runner()
    refused = A.run_platform_tool(r, "room_volume", {"area_id": "office", "action": "up"})
    check("6 · Core runs ONLY the read-only list: room_volume is refused, and never reaches the runner",
          ("error" in refused, r.ran), (True, []))
    check("   …and every tool on the list really is a read (no act among them)",
          all(n in ("room_status",) for n in A.PLATFORM_AI_TOOLS) and A.run_platform_tool(r, "room_status", {"area_id": "x"}) == {"ok": True})
    before = CORE_STUB.lists
    CORE_STUB.up = True
    await api.async_get_api_instance(object())
    check("7 · the list is kept a while — Core not asked again on the next turn", CORE_STUB.lists, before)
    api._read_at = 0.0
    api._tools = []
    await api.async_get_api_instance(object())
    check("   …and asked again once it is stale or was never read", CORE_STUB.lists, before + 1)
    undo()
    check("1b · taken back on unload", REGISTERED, [])

    srv = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
    g = srv.split('if parts == ["assist", "platform_tools"]:')[1].split("if parts ==")[0]
    p = srv.split('if parts == ["assist", "tool"]:')[1].split('if parts == ["assist", "chat"]:')[0]
    check("8 · (SOURCE) GET /assist/platform_tools and POST /assist/tool: signed-in only, through the read-only list",
          ('"sign in required"' in g and "_assist.platform_tools()" in g and '"sign in required"' in p
           and "_assist.run_platform_tool(rnr" in p and srv.index('["assist", "platform_tools"]') < srv.index("def do_POST")
           and srv.index('["assist", "tool"]') > srv.index("def do_POST")))
    check("   (SOURCE) the door registers the tools at setup and takes them back with the entry",
          "entry.async_on_unload(llm_tools.async_register(hass, entry))" in D.files()["__init__.py"].decode()
          and "llm_tools.py" in D.DOOR_FILES and D.DOOR_BUILD == "9")
    print("\n%d passed, %d failed" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
