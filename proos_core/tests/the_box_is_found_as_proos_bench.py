"""THE BOX IS FOUND AS PROOS — register 1259 (step 1 of 3).
Run: python3 tests/the_box_is_found_as_proos_bench.py

Dave, 8 Nov 2026 21:08: "my app is discovered by my app no other app will see it at all"; 21:22 "Yes go". MEASURED that
evening: the Home Assistant app and another app built for the platform found his box on their own and signed in, because
the box announces itself as the platform (_home-assistant._tcp, and SSDP) and ProOS's front door passes the platform's
sign-in for any app. ProOS's own iOS app finds the box by that same announcement.

STEP 1 (this build): the box ALSO announces itself as ProOS (_proos._tcp), through the platform's own announcer, carrying
the same facts the platform announces. The platform's announcing of itself is withdrawn only while Core has written the
quiet file — step 3, after ProOS's own apps look for ProOS. The platform keeps LISTENING for devices throughout.

DRIVEN — the door's real presence.py (as Core writes it) over the REAL announcement library (python-zeroconf's
AsyncZeroconf on this machine's loopback; its own registry is read back), the platform's own pieces stood in (the
platform's announcement built the way its 2026.10.2 code builds it, read before writing this):
 1. started: the box is announced as ProOS — same name, same server, same addresses, same address-for-the-apps, a
    ProOS mark — and the platform's own announcement is LEFT ALONE (no quiet file)
 2. quiet file written: the platform's own announcement is withdrawn and its SSDP announcer stopped; ProOS's stays
 3. quiet file removed: the platform's announcement comes back
 4. (SOURCE) the door ships presence.py at build 6 and starts it with the entry; nothing in it names a product a
    person reads; the quiet file is the one switch
"""
import asyncio
import os
import socket
import sys
import tempfile
import threading
import types

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import door as D                                     # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


try:
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
except Exception:                                                # noqa: BLE001
    print("FAIL  NOT RUN — the announcement library (python-zeroconf) is not installed here")
    sys.exit(1)

CFG = tempfile.mkdtemp(prefix="proos_cfg_")
PLATFORM_TYPE = "_home-assistant._tcp.local."
STATE = {"aio": None, "ssdp_stopped": 0, "ssdp_started": 0, "tick": None, "loop_thread": None}


def stub_platform():
    """The platform's own pieces, as its 2026.10.2 code has them (read: components/zeroconf/__init__.py lines 73, 246;
    components/ssdp/server.py 133, 219; ssdp/const.py UPNP_SERVER = "server")."""
    ha = types.ModuleType("homeassistant")
    comps = types.ModuleType("homeassistant.components")
    zc = types.ModuleType("homeassistant.components.zeroconf")

    async def async_get_async_instance(hass):
        return STATE["aio"]

    async def _async_get_local_service_info(hass):
        return AsyncServiceInfo(PLATFORM_TYPE, name="Home." + PLATFORM_TYPE, server="abc123.local.",
                                parsed_addresses=["192.168.8.117"], port=8123,
                                properties={"location_name": "Home", "uuid": "abc123", "version": "2026.10.2",
                                            "internal_url": "http://192.168.8.117", "external_url": "",
                                            "base_url": "http://192.168.8.117"})
    zc.async_get_async_instance = async_get_async_instance
    zc._async_get_local_service_info = _async_get_local_service_info
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    core = types.ModuleType("homeassistant.core")

    class CoreState:
        running = "RUNNING"
    core.CoreState = CoreState
    helpers = types.ModuleType("homeassistant.helpers")
    ev = types.ModuleType("homeassistant.helpers.event")
    def async_track_time_interval(hass, fn, every):
        # REGISTER 1262 — as the platform's 2026.10.2 HassJob does: a coroutine function runs on the loop; a plain
        # function runs in a WORKER THREAD (the measured fault). The bench keeps the action and fires it as a tick.
        STATE["tick"] = fn
        return lambda: None
    ev.async_track_time_interval = async_track_time_interval
    for n, m in (("homeassistant", ha), ("homeassistant.components", comps), ("homeassistant.components.zeroconf", zc),
                 ("homeassistant.const", const), ("homeassistant.core", core), ("homeassistant.helpers", helpers),
                 ("homeassistant.helpers.event", ev)):
        sys.modules[n] = m
    comps.zeroconf = zc
    return CoreState


class SSDP:
    async def async_stop(self, *_):
        STATE["ssdp_stopped"] += 1

    async def _async_start_upnp_servers(self, event):
        STATE["ssdp_started"] += 1


class Hass:
    def __init__(self, CoreState):
        self.state = CoreState.running
        self.data = {"ssdp": {"server": SSDP()}}
        self.config = types.SimpleNamespace(path=lambda *p: os.path.join(CFG, *p))
        self.bus = types.SimpleNamespace(async_listen_once=lambda *a: None)

    def async_create_task(self, coro):
        # the platform refuses this from any thread but its loop's (helpers/frame.py: report_non_thread_safe_operation)
        if threading.get_ident() != STATE["loop_thread"]:
            coro.close()
            raise RuntimeError("Detected that custom integration 'proos' calls hass.async_create_task from a thread "
                               "other than the event loop")
        return asyncio.ensure_future(coro)


async def fire_tick(hass):
    """One minute passes: the platform runs the timer's action — on its loop if it is a coroutine function, otherwise in
    a worker thread (HassJob, 2026.10.2)."""
    fn = STATE["tick"]
    if asyncio.iscoroutinefunction(fn):
        await fn(None)
        return "ran"
    try:
        await asyncio.get_running_loop().run_in_executor(None, fn, None)
    except RuntimeError as e:
        return str(e)[:60]
    await asyncio.sleep(0.05)
    return "ran"


async def main():
    STATE["loop_thread"] = threading.get_ident()
    CoreState = stub_platform()
    aio = AsyncZeroconf(interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only)
    STATE["aio"] = aio
    try:
        base = await sys.modules["homeassistant.components.zeroconf"]._async_get_local_service_info(None)
        await aio.async_register_service(base)                  # the platform announces itself, as it does at start
        src = D.files("http://core:8770")["presence.py"].decode()
        mod = types.ModuleType("proos_presence_under_test")
        exec(compile(src, "presence.py", "exec"), mod.__dict__)
        hass = Hass(CoreState)
        await mod.async_start(hass, None)
        reg = aio.zeroconf.registry                              # the library keys its registry by lower-cased name
        pro = reg.async_get_info_name("home._proos._tcp.local.")
        plat = reg.async_get_info_name("home." + PLATFORM_TYPE)
        check("1 · the box is announced as ProOS, with the platform's own facts and a ProOS mark",
              (pro is not None, pro and pro.server, pro and pro.parsed_addresses(), pro and pro.properties.get(b"internal_url"),
               pro and pro.properties.get(b"proos")),
              (True, "abc123.local.", ["192.168.8.117"], b"http://192.168.8.117", b"1"))
        check("1 · …and the platform's own announcement is left alone (no quiet file)", (plat is not None, STATE["ssdp_stopped"]), (True, 0))
        open(os.path.join(CFG, D.QUIET_FILE), "w").write("1")
        check("2 · the minute check runs on the platform's own loop (1262: it crashed in a worker thread)",
              await fire_tick(hass), "ran")
        check("2 · quiet: the platform's announcement withdrawn, its SSDP announcer stopped, ProOS's kept",
              (reg.async_get_info_name("home." + PLATFORM_TYPE) is None, STATE["ssdp_stopped"],
               reg.async_get_info_name("home._proos._tcp.local.") is not None), (True, 1, True))
        os.remove(os.path.join(CFG, D.QUIET_FILE))
        await fire_tick(hass)
        # (the library may announce it under a new name — "Home-2" — when the old one is still remembered on the network;
        #  so it is looked up by its type, as presence.py itself does)
        check("3 · not quiet again: the platform's announcement comes back",
              (len(reg.async_get_infos_type(PLATFORM_TYPE)), STATE["ssdp_started"]), (1, 1))
        open(os.path.join(CFG, D.QUIET_FILE), "w").write("1")
        await fire_tick(hass)
        check("3 · …and quiet again withdraws it even under its new name",
              len(reg.async_get_infos_type(PLATFORM_TYPE)), 0)
        os.remove(os.path.join(CFG, D.QUIET_FILE))
        await mod.async_stop(hass)
        check("3 · …and stopping the door withdraws ProOS's", reg.async_get_info_name("home._proos._tcp.local.") is None, True)
    finally:
        await aio.async_close()


asyncio.run(main())

src = open(os.path.join(HERE, "..", "proos", "door.py"), encoding="utf-8").read()
pres = D.files("http://core:8770")["presence.py"].decode()
check("4 · build 7 ships presence.py (kept in build 8, 1306, build 9, 1325, build 10, 1326, and build 11, 1330d), started with the entry; the quiet file is the one switch; no product named to a person",
      D.DOOR_BUILD in ("7", "8", "9", "10", "11") and "presence.py" in D.DOOR_FILES and "await presence.async_start(hass, entry)" in src
      and "hass.config.path(QUIET_FILE)" in pres and "Home Assistant" not in pres, True)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
