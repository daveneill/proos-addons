"""GLOBAL CACHÉ HANDS ITS PORTS TO THE PLATFORM — register 1323 (12 Nov 2026). DRIVES the real driver files (hub.py,
infrared.py, switch.py, binary_sensor.py, select.py, the serial port scanner in __init__.py) against stand-in units
over real TCP, with the platform's classes stood in by their 2026.11.1 shapes (InfraredEmitterEntity.async_send_command,
InfraredReceiverEntity._handle_received_signal, usb.SerialDevice, DataUpdateCoordinator …). The platform itself is not
installed here; its own behaviour is the box's to show.
Run: python3 tests/global_cache_hands_its_ports_to_the_platform_bench.py

  1. what each IR port IS comes from the unit's own get_IR: IR → emitter, port 3 IR_BLASTER → blaster, SENSOR → input,
     RECEIVER → the platform's receiver
  2. a platform IR command (a real LG code's timings + carrier) reaches the unit as sendir through the emitter
  3. the unit refusing (busyir / its error) reaches the platform as an error with the unit's reason, not as done
  4. serial ports join the platform's port list as socket://<unit>:4999, :5000 — named for the unit and its speed
  5. relays: on/off goes to the unit; the switch shows the unit's ANSWER
  6. a Global Connect pushes a sensor change on the open connection → the input changes with no poll
  7. a received IR code (a remote pointed at a RECEIVER port) reaches the platform's receiver as timings
  8. serial speed/parity/flow are written to the unit and show what the unit answered
  9. an IR port's use is changed on the unit, then the unit's entities are reloaded
"""
import asyncio
import importlib
import os
import sys
import types

sys.dont_write_bytecode = True   # nothing compiled is left inside the driver folder Core ships

HERE = os.path.dirname(os.path.abspath(__file__))
DRV = os.path.join(os.path.dirname(HERE), "drivers")
PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


# ── the platform's classes, in their 2026.11.1 shapes (read in the platform's source) ──────────────────────────────
def mod(name, **kw):
    m = types.ModuleType(name)
    m.__dict__.update(kw)
    sys.modules[name] = m
    return m


def callback(f):
    return f


class HomeAssistantError(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class UpdateFailed(Exception):
    pass


class DataUpdateCoordinator:
    def __class_getitem__(cls, _):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name="", update_interval=None):
        self.hass, self.config_entry, self.data, self._listeners = hass, config_entry, None, []

    def async_set_updated_data(self, data):
        self.data = data
        for l in self._listeners:
            l()

    async def async_refresh(self):
        self.data = await self._async_update_data()


class CoordinatorEntity:
    def __class_getitem__(cls, _):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.written = 0

    def async_write_ha_state(self):
        self.written += 1


class InfraredEmitterEntity:
    pass


class InfraredReceiverEntity:
    def _handle_received_signal(self, signal):
        self.heard = signal


class InfraredReceivedSignal:
    def __init__(self, timings, modulation=None):
        self.timings, self.modulation = timings, modulation


class SerialDevice:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Entity:
    pass


mod("homeassistant")
mod("homeassistant.core", HomeAssistant=object, callback=callback)
mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError, ConfigEntryNotReady=ConfigEntryNotReady)
mod("homeassistant.const", CONF_HOST="host", EVENT_HOMEASSISTANT_STOP="stop", EntityCategory=types.SimpleNamespace(CONFIG="config"))
mod("homeassistant.helpers")
mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed,
    CoordinatorEntity=CoordinatorEntity)
mod("homeassistant.helpers.device_registry", DeviceInfo=dict, CONNECTION_NETWORK_MAC="mac", format_mac=lambda m: m.lower(),
    async_get=lambda h: None)
mod("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
mod("homeassistant.helpers.discovery_flow", async_create_flow=lambda *a, **k: None)
mod("homeassistant.helpers.typing", ConfigType=dict)
mod("homeassistant.config_entries", SOURCE_INTEGRATION_DISCOVERY="integration_discovery", ConfigEntry=object)
mod("homeassistant.components")
mod("homeassistant.components.infrared", InfraredCommand=object, InfraredEmitterEntity=InfraredEmitterEntity,
    InfraredReceiverEntity=InfraredReceiverEntity, InfraredReceivedSignal=InfraredReceivedSignal)
mod("homeassistant.components.switch", SwitchEntity=Entity)
mod("homeassistant.components.binary_sensor", BinarySensorEntity=Entity)
mod("homeassistant.components.select", SelectEntity=Entity)
mod("homeassistant.components.usb", SerialDevice=SerialDevice)
sys.modules["homeassistant.helpers"].device_registry = sys.modules["homeassistant.helpers.device_registry"]
sys.modules["homeassistant.helpers"].discovery_flow = sys.modules["homeassistant.helpers.discovery_flow"]
sys.modules["homeassistant.components"].usb = sys.modules["homeassistant.components.usb"]

sys.path.insert(0, DRV)
GC = importlib.import_module("global_cache")
hubm = importlib.import_module("global_cache.hub")
irm = importlib.import_module("global_cache.infrared")
swm = importlib.import_module("global_cache.switch")
bsm = importlib.import_module("global_cache.binary_sensor")
selm = importlib.import_module("global_cache.select")

LG_HDMI1 = [9000, -4500] + [562, -562, 562, -1687] * 16 + [562]


class Cmd:
    modulation = 38000

    def get_raw_timings(self):
        return list(LG_HDMI1)


class Unit:
    """A stand-in unit; `modes` says what each IR port is, `devices` what getdevices lists."""

    def __init__(self, devices, modes=None, family="itach"):
        self.devices, self.modes, self.family = devices, dict(modes or {}), family
        self.got, self.state, self.serial, self.writer, self.busy = [], {}, {}, None, False

    async def handle(self, reader, writer):
        self.writer = writer
        buf = b""
        while True:
            d = await reader.read(4096)
            if not d:
                return
            buf += d
            while b"\r" in buf:
                raw, buf = buf.split(b"\r", 1)
                line = raw.decode()
                self.got.append(line)
                c, out = line.split(",")[0], []
                p = line.split(",")
                if c == "getdevices":
                    out = self.devices + ["endlistdevices"]
                elif c == "getversion":
                    out = ["710-1005-05"]
                elif c == "get_IR":
                    out = ["IR,%s,%s" % (p[1], self.modes.get(p[1], "IR"))]
                elif c == "set_IR":
                    self.modes[p[1]] = p[2]
                    out = ["IR,%s,%s" % (p[1], p[2])]
                elif c == "sendir":
                    out = ["busyir"] if self.busy else ["completeir,%s,%s" % (p[1], p[2])]
                elif c == "get_SERIAL":
                    out = ["SERIAL,%s,%s" % (p[1], self.serial.get(p[1], "9600,FLOW_NONE,PARITY_NO"))]
                elif c == "set_SERIAL":
                    self.serial[p[1]] = ",".join(p[2:])
                    out = ["SERIAL,%s,%s" % (p[1], self.serial[p[1]])]
                elif c == "setstate":
                    self.state[p[1]] = p[2]
                    out = ["state,%s,%s" % (p[1], p[2])]
                elif c == "getstate":
                    out = ["state,%s,%s" % (p[1], self.state.get(p[1], "0"))]
                elif c == "receiveIR":
                    out = ["receiveIR,%s,%s" % (p[1], p[2])]
                else:
                    out = ["ERR_1:1,001"]
                for o in out:
                    writer.write((o + "\r").encode())
                await writer.drain()

    async def push(self, line):
        self.writer.write((line + "\r").encode())
        await self.writer.drain()


class Entry:
    def __init__(self, title, uid):
        self.title, self.unique_id, self.entry_id, self.runtime_data = title, uid, uid, None


async def bring_up(unit, title, uid, model=""):
    srv = await asyncio.start_server(unit.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    entry = Entry(title, uid)
    hub = hubm.GlobalCacheHub(object(), entry, "127.0.0.1", "", model)
    hub.client.port = port
    await hub.async_read_unit()
    await hub.async_refresh()
    entry.runtime_data = hub
    ents = []
    for m in (irm, swm, bsm, selm):
        await m.async_setup_entry(None, entry, ents.extend)
    return hub, entry, ents, srv


def by(ents, cls):
    return [e for e in ents if isinstance(e, cls)]


async def main():
    # an iTach IP2IR: port 1 IR, port 2 SENSOR, port 3 IR_BLASTER
    u1 = Unit(["device,0,0 ETHERNET", "device,1,3 IR"], {"1:1": "IR", "1:2": "SENSOR", "1:3": "IR_BLASTER"})
    hub1, e1, ents1, s1 = await bring_up(u1, "iTach at 127.0.0.1", "itach1")
    em = by(ents1, irm.GlobalCacheEmitter)
    check("1 · port modes come from the unit: IR → emitter, port 3 → blaster, SENSOR → input",
          ([e.port.address for e in em], [e._attr_name for e in em], [e.port.address for e in by(ents1, bsm.GlobalCacheInput)]),
          (["1:1", "1:3"], ["IR 1:1", "IR 1:3 blaster"], ["1:2"]))
    await em[0].async_send_command(Cmd())
    sent = [g for g in u1.got if g.startswith("sendir,1:1,")]
    check("2 · a platform IR command (real LG timings + carrier) reaches the unit as sendir",
          bool(sent) and sent[-1].split(",")[3] == "38000" and sent[-1].split(",")[6:8] == ["342", "171"])
    u1.busy = True
    err = None
    try:
        await em[1].async_send_command(Cmd())
    except HomeAssistantError as e:
        err = str(e)
    u1.busy = False
    check("3 · the unit's refusal reaches the platform as an error with the reason", bool(err) and "still sending" in err)

    # a GC-100-12: two serial ports, three relays
    u2 = Unit(["device,1,1 SERIAL", "device,2,1 SERIAL", "device,3,3 RELAY", "device,4,3 IR", "device,5,3 IR"], family="gc100")
    hub2, e2, ents2, s2 = await bring_up(u2, "GC-100 at 127.0.0.1", "gc100a")
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_loaded_entries=lambda d: [e1, e2]))
    ports = GC._scan_serial_ports(hass)
    check("4 · serial ports join the platform's port list as socket://unit:4999, :5000",
          [p.device for p in ports], ["socket://127.0.0.1:4999", "socket://127.0.0.1:5000"])
    check("   …named for the unit and its speed", "GC-100 at 127.0.0.1 serial 1 (1:1) · 9600 baud" == ports[0].description
          and ports[0].manufacturer == "Global Caché")
    rel = by(ents2, swm.GlobalCacheRelay)
    await rel[1].async_turn_on()
    check("5 · a relay switches on the unit, and shows the unit's answer", (u2.state.get("3:2"), rel[1].is_on, len(rel)), ("1", True, 3))
    await rel[1].async_turn_off()
    check("   …and off", (u2.state.get("3:2"), rel[1].is_on), ("0", False))

    # a Global Connect IR module: port 1 SENSOR (pushes), port 2 RECEIVER
    u3 = Unit(["device,0,0 MODULE", "device,1,3 IR_OUT", "device,1,3 SENSOR_DIGITAL"], {"1:1": "SENSOR", "1:2": "RECEIVER", "1:3": "IR"})
    hub3, e3, ents3, s3 = await bring_up(u3, "Global Connect at 127.0.0.1", "gcir")
    inp = by(ents3, bsm.GlobalCacheInput)
    check("   (a Global Connect lists its IR ports twice — as IR and as sensor; one input, not two)", [i.port.address for i in inp], ["1:1"])
    check("6 · the Global Connect is asked to push (getstate … notify) and a pushed change lands with no poll",
          any(g == "getstate,1:1,notify" for g in u3.got), True)
    await u3.push("state,1:1,1")
    await asyncio.sleep(0.05)
    check("   …the input reads closed", inp[0].is_on, True)
    rx = by(ents3, irm.GlobalCacheReceiver)
    check("7 · a RECEIVER port is the platform's receiver, switched on at the unit", ([r.port.address for r in rx], "receiveIR,1:2,enabled" in u3.got),
          (["1:2"], True))
    await u3.push("sendir,1:1,0,38000,1,1,342,171,21,21")
    await asyncio.sleep(0.05)
    check("   …a code from a remote reaches it as the platform's timings", (rx[0].heard.modulation, rx[0].heard.timings),
          (38000, [9000, -4500, 553, -553]))

    sel = [e for e in by(ents2, selm.SerialSetting) if e.port.address == "1:1"]
    speed = [e for e in sel if e.what == "baud"][0]
    parity = [e for e in sel if e.what == "parity"][0]
    await speed.async_select_option("38400")
    await parity.async_select_option("Even")
    check("8 · serial speed and parity are written to the unit and show its answer",
          (u2.serial.get("1:1"), speed.current_option, parity.current_option), ("38400,FLOW_NONE,PARITY_EVEN", "38400", "Even"))

    reloads = []
    use = [e for e in by(ents1, selm.IRPortUse) if e.port.address == "1:1"][0]
    use.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_schedule_reload=reloads.append))
    use._entry = e1
    check("   (port 3 offers the blaster; ports 1–2 do not)", ("IR blaster" in use._attr_options,
          "IR blaster" in [e for e in by(ents1, selm.IRPortUse) if e.port.address == "1:3"][0]._attr_options), (False, True))
    await use.async_select_option("Sensor input")
    check("9 · an IR port's use is set on the unit, then the unit's entities reload", (u1.modes["1:1"], reloads), ("SENSOR", ["itach1"]))

    for h, s in ((hub1, s1), (hub2, s2), (hub3, s3)):
        await h.async_close()
        s.close()
    print("\n%d passed, %d failed" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
