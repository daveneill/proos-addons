"""Global Caché — the Unified TCP API v1.1, for every family (register 1323, 12 Nov 2026).

Written to the maker's own document — "Global Caché Unified TCP API Version 1.1" (API-GC-UnifiedTCPv1.1.pdf), with
the iTach API 1.5 (the discovery beacon) and the Global Connect TCP API 1.0 (module numbering) — as Dave's
certification standard asks (3 Nov 2026). Nothing here imports the platform: this file is the unit's language only,
so it is tested on its own against a stand-in unit of each family.

Families (Unified §1): GC-100 (-06, -12, -18), iTach (IP2IR, WF2IR, IP2SL, WF2SL, IP2CC, WF2CC), iTach Flex, and
Global Connect (each chassis module is its own network device with its own address).

The rules this client keeps, each from the document:
  - commands on TCP 4998, one printable line ending in CR; responses end in CR (§1)
  - a GC-100 takes ONE command connection; the others take eight (§1) — the integration holds one connection per unit
  - sendir's completion is "completeir,<m>:<p>,<ID>"; a different code already playing answers "busyir" (§4, §9)
  - errors: GC-100 "unknowncommand <n>", iTach "ERR_<m>:<p>,<nnn>", Flex/Global Connect "ERR <code>" (§10)
  - each serial port is its own raw TCP socket from 4999 (GC-100-12's second at 5000) (§1, §5)
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable

COMMAND_PORT = 4998
SERIAL_BASE_PORT = 4999
BEACON_GROUP = "239.255.250.250"
BEACON_PORT = 9131
REPLY_TIMEOUT = 5.0          # plumbing: how long a reply may take before the unit counts as not answering
IR_TIMEOUT = 15.0            # plumbing: a long code with repeats can play for seconds before completeir
TRAILING_GAP_US = 40000      # plumbing: sendir needs on/off PAIRS; a code that ends lit gets this closing gap

# GC-100 relays answer on module 3 for backward compatibility (Global Connect §relay); not used to address anything.
_ERR = re.compile(r"^(?:unknowncommand\s*(?P<gc100>\d+)|ERR_(?P<mp>\d+:\d+),(?P<itach>\d+)|ERR\s+(?P<flex>[A-Z]{0,2}\d+))\s*$")

# §10, the common codes and the class-specific ones, in the installer's words
ERROR_WORDS = {
    "001": "the unit did not recognise the command", "1": "the unit did not recognise the command",
    "002": "the command's syntax was wrong", "2": "the command's syntax was wrong",
    "003": "that module or port does not exist on this unit", "3": "that module or port does not exist on this unit",
    "004": "the IR code's ID is out of range", "005": "the IR frequency is not supported",
    "006": "the IR repeat is out of range", "007": "the IR offset is out of range",
    "008": "an IR pulse count is out of range", "010": "the IR code has an odd number of pulses",
    "014": "the port is in the wrong mode for this command", "016": "the command had no carriage return",
    "020": "the IR code is too long", "021": "that port is not an IR port", "023": "a parameter is not valid",
    "027": "the unit's settings are locked",
    "IR008": "the IR output is busy", "IR020": "the IR input is busy", "IR021": "the IR input overflowed",
    "SL001": "that baud rate is not supported", "SL002": "that flow control is not supported",
    "SL003": "that parity is not supported", "SL004": "that stop bit setting is not supported",
    "SL006": "that duplex setting is not supported",
    "RO001": "that relay type is not supported", "RO002": "that relay state is not valid",
    "RO003": "that relay setting is not supported", "RO004": "that relay is disabled or in use by another",
    "SI001": "that sensor is not available", "SI002": "that notify port is not valid", "SI003": "that notify time is not valid",
    "SW001": "that input or output selection is not valid", "SW010": "the CEC frame is too large",
    "SW011": "the CEC message could not be sent",
}


class GlobalCacheError(Exception):
    """The unit refused a command (its own error code) or did not answer."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def parse_error(line: str) -> GlobalCacheError | None:
    m = _ERR.match(line.strip())
    if not m:
        return None
    code = m.group("gc100") or m.group("itach") or m.group("flex")
    # a GC-100 says "27" where the others say "027" (§10): one table, the number padded
    words = ERROR_WORDS.get(code) or (ERROR_WORDS.get(code.zfill(3)) if code.isdigit() else None) or "error " + code
    return GlobalCacheError("Global Caché refused it: %s (%s)" % (words, line.strip()), code)


# ── what is on the unit ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Module:
    number: int
    ports: int
    kind: str           # ETHERNET, WIFI, MODULE, IR, SERIAL, RELAY, SENSOR, RELAYSENSOR, IR_BLASTER, IRTRIPORT, …
    subtype: str = ""   # DIGITAL, RS232, SPST_3A, HDMI_3:1 … — information only (§3)


_DEV = re.compile(r"^device,(\d+),(\d+)\s+([A-Z0-9_:]+)\s*$")
# a type is the first word of "<type>[_subtype]"; the ones with an underscore in the type itself come first
_KINDS = ("IRTRIPORT_BLASTER", "IR_BLASTER", "IRTRIPORT", "RELAYSENSOR", "ETHERNET", "MODULE", "SERIAL", "SENSOR",
          "RELAY", "SWITCH", "WIFI", "IR")


def parse_devices(lines: list[str]) -> list[Module]:
    out = []
    for line in lines:
        m = _DEV.match(line.strip())
        if not m:
            continue
        num, ports, word = int(m.group(1)), int(m.group(2)), m.group(3)
        kind = next((k for k in _KINDS if word == k or word.startswith(k + "_")), word)
        sub = word[len(kind) + 1:] if word.startswith(kind + "_") else ""
        out.append(Module(num, ports, kind, sub))
    return out


def family_of(modules: list[Module], model: str = "") -> str:
    """'gc100', 'itach', 'flex' or 'globalconnect' — from the beacon's Model when known, else from the module list."""
    m = (model or "").lower().replace("-", "")
    if m.startswith("gc100"):
        return "gc100"
    if m.startswith("itachflex") or m.startswith("flex"):
        return "flex"
    if m.startswith("itach"):
        return "itach"
    if m in ("sl232", "rl3a", "hmx3", "ir3", "sl", "rt", "hm", "ir") or m.startswith("globalconnect"):
        return "globalconnect"
    kinds = {x.kind for x in modules if x.number == 0}
    io = {x.number for x in modules if x.number > 0}
    if "MODULE" in kinds:
        return "globalconnect"
    if any(x.kind in ("IRTRIPORT", "IRTRIPORT_BLASTER") for x in modules):
        return "flex"                       # the Flex Link's tri-port is the Flex's own (§3)
    if len(io) > 1:
        return "gc100"                      # a GC-100 lists several I/O modules from 1 up, with no network line (§2)
    return "itach"                          # an iTach: its network module at 0, one I/O module at 1


@dataclass(frozen=True)
class IOPort:
    kind: str          # "ir", "serial", "relay", "sensor", "hdmi"
    module: int
    port: int
    tcp_port: int = 0  # serial only: the raw socket for this serial port
    subtype: str = ""

    @property
    def address(self) -> str:
        return "%d:%d" % (self.module, self.port)


def io_ports(modules: list[Module]) -> list[IOPort]:
    """Every addressable port. IR modules' ports may be IR or sensor by mode (§4) — listed as IR here; the integration
    reads each port's mode (get_IR) to decide what it is. Serial ports take sockets from 4999 in order (§1)."""
    out, serial_n, seen = [], 0, set()
    for mod in modules:
        if mod.number == 0:
            continue
        for p in range(1, max(mod.ports, 1) + 1):
            kind = {"IR": "ir", "IR_BLASTER": "ir", "IRTRIPORT": "ir", "IRTRIPORT_BLASTER": "ir", "SERIAL": "serial",
                    "RELAY": "relay", "RELAYSENSOR": "relay", "SENSOR": "sensor", "SWITCH": "hdmi"}.get(mod.kind)
            if kind is None or (kind, mod.number, p) in seen:
                continue
            seen.add((kind, mod.number, p))
            tcp = 0
            if kind == "serial":
                tcp = SERIAL_BASE_PORT + serial_n
                serial_n += 1
            out.append(IOPort(kind, mod.number, p, tcp, mod.subtype))
    return out


# ── the discovery beacon (iTach API §beacon; Global Connect TCP API) ────────────────────────────────────────────────

_BEACON_FIELD = re.compile(r"<-([A-Za-z_-]+)=([^>]*)>")


def parse_beacon(data: bytes | str) -> dict | None:
    """'AMXB<-UUID=GlobalCache_000C1E024239><-Make=GlobalCache><-Model=iTachWF2IR>…' → {uuid, make, model, mac,
    config_url, revision, status}; None when it is not a Global Caché beacon."""
    text = data.decode("ascii", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    if not text.startswith("AMXB"):
        return None
    f = {k.lower(): v.strip() for k, v in _BEACON_FIELD.findall(text)}
    if (f.get("make") or "").replace(" ", "").lower() not in ("globalcache", "globalcaché"):
        return None
    uuid = f.get("uuid", "")
    mac = ""
    m = re.search(r"([0-9A-Fa-f]{12})$", uuid)
    if m:
        h = m.group(1).upper()
        mac = ":".join(h[i:i + 2] for i in range(0, 12, 2))
    url = (f.get("config-url") or "").rstrip(".")
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    return {"uuid": uuid, "make": f.get("make"), "model": f.get("model", ""), "mac": mac, "host": host,
            "revision": f.get("revision", ""), "status": f.get("status", "")}


# ── IR: the platform's timings ↔ sendir (Unified §4) ───────────────────────────────────────────────────────────────

def timings_to_counts(timings: list[int], frequency: int) -> list[int]:
    """The platform's raw timings (µs; positive lit, negative dark — infrared_protocols.Command.get_raw_timings)
    → Global Caché on/off counts of carrier periods. Neighbours of one sign are one pulse; a code starts lit and
    ends dark (sendir is on/off pairs)."""
    merged: list[int] = []
    for t in timings or []:
        t = int(t)
        if t == 0:
            continue
        if merged and (merged[-1] > 0) == (t > 0):
            merged[-1] += t
        else:
            merged.append(t)
    while merged and merged[0] < 0:
        merged.pop(0)
    if not merged:
        raise GlobalCacheError("the IR code has no pulses")
    if merged[-1] > 0:
        merged.append(-TRAILING_GAP_US)
    return [max(1, round(abs(t) * frequency / 1_000_000)) for t in merged]


def build_sendir(address: str, ident: int, frequency: int, counts: list[int], repeat: int = 1, offset: int = 1) -> str:
    if len(counts) % 2:
        raise GlobalCacheError("the IR code has an odd number of pulses")
    if offset % 2 == 0:
        raise GlobalCacheError("the IR offset must be odd")
    return "sendir,%s,%d,%d,%d,%d,%s" % (address, ident, int(frequency), repeat, offset, ",".join(str(c) for c in counts))


def parse_sendir(line: str) -> tuple[int, list[int]] | None:
    """A learned or received code ('sendir,1:1,<id>,<freq>,<repeat>,<offset>,on,off,…') → (frequency, timings in µs,
    lit positive / dark negative) — the platform's receiver format."""
    parts = line.strip().split(",")
    if len(parts) < 8 or parts[0] != "sendir":
        return None
    try:
        freq = int(parts[3])
        counts = [int(x) for x in parts[6:] if x.strip()]
    except ValueError:
        return None
    if freq <= 0:
        return None
    timings = [round(c * 1_000_000 / freq) * (1 if i % 2 == 0 else -1) for i, c in enumerate(counts)]
    return freq, timings


# ── the connection ─────────────────────────────────────────────────────────────────────────────────────────────────

SERIAL_BAUDS = (1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200)
SERIAL_FLOW = ("FLOW_NONE", "FLOW_HARDWARE")
SERIAL_PARITY = ("PARITY_NO", "PARITY_ODD", "PARITY_EVEN")
IR_MODES = ("IR", "IR_BLASTER", "IR_NOCARRIER", "BL2_BLASTER", "IRTRIPORT", "IRTRIPORT_BLASTER", "SENSOR",
            "SENSOR_NOTIFY", "SERIAL", "RECEIVER", "LED_LIGHTING")


@dataclass
class _Wait:
    match: Callable[[str], bool]
    future: asyncio.Future
    lines: list = field(default_factory=list)
    until: Callable[[str], bool] | None = None   # multi-line answers (getdevices … endlistdevices)


class GlobalCacheClient:
    """One command connection to one unit (a Global Connect module is its own unit). Requests go one at a time — the
    GC-100 allows a single connection and the API answers in order — and lines nobody asked for (a sensor change, a
    received IR code, a learned code) go to `on_line`."""

    def __init__(self, host: str, port: int = COMMAND_PORT, on_line: Callable[[str], None] | None = None):
        self.host, self.port = host, port
        self.on_line = on_line
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._wait: _Wait | None = None
        self._ir_id = 0
        self.connected = False

    async def connect(self) -> None:
        if self.connected:
            return
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), REPLY_TIMEOUT)
        self.connected = True
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    async def close(self) -> None:
        self.connected = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writer = self._reader = None

    async def _read_loop(self) -> None:
        buf = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buf += chunk
                # the API ends lines with CR; some firmware adds LF — either ends a line
                while True:
                    i = min([x for x in (buf.find(b"\r"), buf.find(b"\n")) if x >= 0], default=-1)
                    if i < 0:
                        break
                    line, buf = buf[:i].decode("ascii", "replace").strip(), buf[i + 1:]
                    if line:
                        self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        self.connected = False
        w = self._wait
        if w and not w.future.done():
            w.future.set_exception(GlobalCacheError("the unit closed the connection"))

    def _dispatch(self, line: str) -> None:
        w = self._wait
        if w and not w.future.done():
            err = parse_error(line)
            if err is not None:
                w.future.set_exception(err)
                return
            if w.until is not None and (line.startswith("device,") or w.until(line)):
                w.lines.append(line)
                if w.until(line):
                    w.future.set_result(list(w.lines))
                return
            if w.match(line):
                w.future.set_result([line])
                return
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001
                pass

    async def request(self, command: str, match: Callable[[str], bool] | None = None, *,
                      until: Callable[[str], bool] | None = None, timeout: float = REPLY_TIMEOUT) -> list[str]:
        async with self._lock:
            await self.connect()
            fut = asyncio.get_running_loop().create_future()
            self._wait = _Wait(match or (lambda _l: True), fut, until=until)
            try:
                self._writer.write((command + "\r").encode("ascii"))
                await self._writer.drain()
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError as e:
                raise GlobalCacheError("the unit at %s did not answer %r" % (self.host, command.split(",")[0])) from e
            except (OSError, ConnectionError) as e:
                await self.close()
                raise GlobalCacheError("the unit at %s is not reachable: %s" % (self.host, e)) from e
            finally:
                self._wait = None

    # — the commands —
    async def get_devices(self) -> list[Module]:
        lines = await self.request("getdevices", until=lambda l: l.startswith("endlistdevices"))
        return parse_devices(lines)

    async def get_version(self, module: int | None = None) -> str:
        cmd = "getversion" if module is None else "getversion,%d" % module
        line = (await self.request(cmd))[0]
        return line.split(",", 2)[2] if line.startswith("version,") else line

    async def get_ir_mode(self, address: str) -> str:
        line = (await self.request("get_IR," + address, lambda l: l.startswith("IR," + address)))[0]
        return line.split(",")[2]

    async def set_ir_mode(self, address: str, mode: str) -> str:
        if mode not in IR_MODES:
            raise GlobalCacheError("%s is not an IR port mode" % mode)
        line = (await self.request("set_IR,%s,%s" % (address, mode), lambda l: l.startswith("IR," + address)))[0]
        return line.split(",")[2]

    async def send_ir(self, address: str, frequency: int, timings: list[int]) -> None:
        """Play one platform IR command on one port and wait for the unit to say it finished (completeir)."""
        counts = timings_to_counts(timings, frequency)
        self._ir_id = self._ir_id % 65535 + 1
        ident = self._ir_id
        done = "completeir,%s,%d" % (address, ident)
        line = (await self.request(build_sendir(address, ident, frequency, counts),
                                   lambda l: l.startswith(done) or l.startswith("busyir"), timeout=IR_TIMEOUT))[0]
        if line.startswith("busyir"):
            raise GlobalCacheError("the IR port %s is still sending another code — try again in a moment" % address)

    async def stop_ir(self, address: str) -> None:
        # the GC-100 does not echo stopir (§4): no answer is not a failure there
        try:
            await self.request("stopir," + address, lambda l: l.startswith("stopir," + address), timeout=1.0)
        except GlobalCacheError as e:
            if e.code:
                raise

    async def get_state(self, address: str) -> int:
        line = (await self.request("getstate," + address, lambda l: l.startswith("state," + address + ",")))[0]
        return int(line.rsplit(",", 1)[1])

    async def set_state(self, address: str, state: int) -> int:
        line = (await self.request("setstate,%s,%d" % (address, state),
                                   lambda l: l.startswith("state," + address + ",")))[0]
        return int(line.rsplit(",", 1)[1])

    async def get_serial(self, address: str) -> dict:
        line = (await self.request("get_SERIAL," + address, lambda l: l.startswith("SERIAL," + address)))[0]
        return parse_serial(line)

    async def set_serial(self, address: str, baud: int, flow: str, parity: str, stopbits: str | None = None) -> dict:
        cmd = "set_SERIAL,%s,%d,%s,%s" % (address, baud, flow, parity) + ("," + stopbits if stopbits else "")
        line = (await self.request(cmd, lambda l: l.startswith("SERIAL," + address)))[0]
        return parse_serial(line)

    async def receive_ir(self, address: str, enabled: bool) -> None:
        await self.request("receiveIR,%s,%s" % (address, "enabled" if enabled else "disabled"),
                           lambda l: l.lower().startswith("receiveir"), timeout=REPLY_TIMEOUT)


def parse_serial(line: str) -> dict:
    p = line.strip().split(",")
    out = {"baud": int(p[2]) if len(p) > 2 and p[2].isdigit() else None,
           "flow": p[3] if len(p) > 3 else "", "parity": p[4] if len(p) > 4 else ""}
    if len(p) > 5:
        out["stopbits"] = p[5]
    return out
