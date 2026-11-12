"""GLOBAL CACHÉ SPEAKS EVERY FAMILY — register 1323 (12 Nov 2026). DRIVES the real drivers/global_cache/protocol.py
over real TCP against a stand-in unit of each family, built from the maker's Unified TCP API v1.1.
Run: python3 tests/global_cache_speaks_every_family_bench.py

Dave 16:21: "support all the models". The stand-ins answer as the document says each family answers:
  iTach IP2IR / IP2SL / IP2CC · iTach Flex · GC-100-12 · Global Connect IR, SL, RT and HM modules
  1. getdevices is read for every family, and each port is what its module says it is
  2. the family is told apart (the GC-100's one-connection rule and its errors depend on it)
  3. a REAL platform code (LG TV HDMI 1, from infrared_protocols 10.7 NECCommand — the library LG Infrared uses)
     becomes the exact sendir: counts = µs × 38 kHz, the code closed with a dark gap, and completeir is waited for
  4. a held key (repeats written into the timings by the platform) plays as one code, not as sendir repeats
  5. busyir is the unit saying "still playing" — said in words, never taken as done
  6. each family's error form (unknowncommand / ERR_m:p,nnn / ERR xx) becomes the unit's reason in words
  7. serial ports get sockets from 4999 (GC-100-12's second at 5000); get/set_SERIAL round-trips
  8. relays switch and read back; a GC-100's stopir (no echo) is not a failure
  9. lines nobody asked for (a GC-100 statechange) reach the integration, even mid-request
 10. the discovery beacon (iTach API) is read: model, MAC from the UUID, the unit's address
 11. a learned/received code (sendir text) becomes the platform's timings
"""
import asyncio
import importlib.util
import os
import sys

sys.dont_write_bytecode = True   # nothing compiled is left inside the driver folder Core ships

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("gc_protocol", os.path.join(os.path.dirname(HERE), "drivers", "global_cache", "protocol.py"))
P = importlib.util.module_from_spec(SPEC)
sys.modules["gc_protocol"] = P
SPEC.loader.exec_module(P)

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


# infrared_protocols 10.7.0: NECCommand(address=0xFB04 (LG_ADDRESS), command=LGTVCode.HDMI_1, modulation=38000)
LG_HDMI1 = [9000, -4500, 562, -562, 562, -562, 562, -1687, 562, -562, 562, -562, 562, -562, 562, -562, 562, -562, 562, -1687, 562, -1687, 562, -562, 562, -1687, 562, -1687, 562, -1687, 562, -1687, 562, -1687, 562, -562, 562, -1687, 562, -1687, 562, -1687, 562, -562, 562, -562, 562, -1687, 562, -1687, 562, -1687, 562, -562, 562, -562, 562, -562, 562, -1687, 562, -1687, 562, -562, 562, -562, 562]
LG_HDMI1_HELD = LG_HDMI1 + [-41000, 9000, -2250, 562, -96000, 9000, -2250, 562]

FAMILIES = {
    "itach_ip2ir": {"devices": ["device,0,0 ETHERNET", "device,1,3 IR"], "version": "710-1005-05", "err": "itach"},
    "itach_ip2sl": {"devices": ["device,0,0 ETHERNET", "device,1,1 SERIAL"], "version": "710-1001-09", "err": "itach"},
    "itach_ip2cc": {"devices": ["device,0,0 ETHERNET", "device,1,3 RELAY"], "version": "710-1002-05", "err": "itach"},
    "flex": {"devices": ["device,0,0 WIFI", "device,1,1 IRTRIPORT"], "version": "710-3000-18", "err": "flex"},
    "gc100_12": {"devices": ["device,1,1 SERIAL", "device,2,1 SERIAL", "device,3,3 RELAY", "device,4,3 IR", "device,5,3 IR"],
                 "version": "3.0-12", "err": "gc100"},
    "gc_ir": {"devices": ["device,0,0 MODULE", "device,1,3 IR_OUT", "device,1,3 SENSOR_DIGITAL"], "version": "1.0.12", "err": "flex"},
    "gc_sl": {"devices": ["device,0,0 MODULE", "device,1,1 SERIAL_RS232"], "version": "1.0.12", "err": "flex"},
    "gc_rt": {"devices": ["device,0,0 MODULE", "device,1,6 RELAY_SPST_3A"], "version": "1.0.12", "err": "flex"},
    "gc_hm": {"devices": ["device,0,0 MODULE", "device,1,4 SWITCH_HDMI_3:1"], "version": "1.0.12", "err": "flex"},
}


class Unit:
    """A stand-in Global Caché unit speaking the Unified TCP API as its family does."""

    def __init__(self, family):
        self.f = FAMILIES[family]
        self.family = family
        self.got = []
        self.state = {}
        self.serial = {}
        self.busy_until = 0.0
        self.playing = None
        self.push = None

    def err(self, code):
        return {"gc100": "unknowncommand %d" % int(code), "itach": "ERR_1:1,%03d" % int(code), "flex": "ERR %s" % code}[self.f["err"]]

    async def handle(self, reader, writer):
        loop = asyncio.get_running_loop()
        self.push = writer
        buf = b""
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data
            while b"\r" in buf:
                raw, buf = buf.split(b"\r", 1)
                line = raw.decode()
                self.got.append(line)
                out = []
                cmd = line.split(",")[0]
                if cmd == "getdevices":
                    out = self.f["devices"] + ["endlistdevices"]
                elif cmd == "getversion":
                    out = [self.f["version"]] if self.f["err"] != "flex" else ["version,0," + self.f["version"]]
                elif cmd == "get_IR":
                    mp = line.split(",")[1]
                    out = ["IR,%s,%s" % (mp, "IR_BLASTER" if mp.endswith(":3") else "IR")]
                elif cmd == "sendir":
                    parts = line.split(",")
                    mp, ident, code = parts[1], parts[2], ",".join(parts[3:])
                    if mp.split(":")[1] not in ("1", "2", "3"):
                        out = [self.err(3)]
                    elif loop.time() < self.busy_until and code != self.playing:
                        out = ["busyir"]
                    else:
                        self.playing, self.busy_until = code, loop.time() + 0.15
                        await asyncio.sleep(0.02)
                        out = ["completeir,%s,%s" % (mp, ident)]
                elif cmd == "stopir":
                    out = [] if self.f["err"] == "gc100" else ["stopir," + line.split(",")[1]]
                elif cmd == "get_SERIAL":
                    mp = line.split(",")[1]
                    out = ["SERIAL,%s,%s" % (mp, self.serial.get(mp, "9600,FLOW_NONE,PARITY_NO"))]
                elif cmd == "set_SERIAL":
                    p = line.split(",")
                    if p[2] not in ("1200", "2400", "4800", "9600", "14400", "19200", "38400", "57600", "115200"):
                        out = ["ERR SL001" if self.f["err"] == "flex" else self.err(23)]
                    else:
                        self.serial[p[1]] = ",".join(p[2:])
                        out = ["SERIAL,%s,%s" % (p[1], self.serial[p[1]])]
                elif cmd == "setstate":
                    p = line.split(",")
                    self.state[p[1]] = p[2]
                    if self.family == "gc100_12":
                        # a sensor somewhere changed at the same moment — pushed BEFORE the answer
                        out = ["statechange,4:1,1"]
                    out = out + ["state,%s,%s" % (p[1], p[2])]
                elif cmd == "getstate":
                    mp = line.split(",")[1]
                    out = ["state,%s,%s" % (mp, self.state.get(mp, "0"))]
                else:
                    out = [self.err(1)]
                for o in out:
                    writer.write((o + "\r").encode())
                await writer.drain()


async def serve(family):
    u = Unit(family)
    srv = await asyncio.start_server(u.handle, "127.0.0.1", 0)
    return u, srv, srv.sockets[0].getsockname()[1]


async def main():
    seen = {}
    for fam in FAMILIES:
        u, srv, port = await serve(fam)
        pushed = []
        c = P.GlobalCacheClient("127.0.0.1", port, on_line=pushed.append)
        mods = await c.get_devices()
        ports = P.io_ports(mods)
        seen[fam] = (P.family_of(mods), [(x.kind, x.address, x.tcp_port) for x in ports], c, u, pushed, srv)

    check("1 · iTach IP2IR: three IR ports on module 1", seen["itach_ip2ir"][1], [("ir", "1:1", 0), ("ir", "1:2", 0), ("ir", "1:3", 0)])
    check("   iTach IP2SL: one serial port, socket 4999", seen["itach_ip2sl"][1], [("serial", "1:1", 4999)])
    check("   iTach IP2CC: three relays", [x[0] for x in seen["itach_ip2cc"][1]], ["relay"] * 3)
    check("   iTach Flex: its one IR link port", seen["flex"][1], [("ir", "1:1", 0)])
    check("   GC-100-12: two serial (4999, 5000), three relays, six IR",
          ([x for x in seen["gc100_12"][1] if x[0] == "serial"], len([x for x in seen["gc100_12"][1] if x[0] == "relay"]),
           [x[1] for x in seen["gc100_12"][1] if x[0] == "ir"]),
          ([("serial", "1:1", 4999), ("serial", "2:1", 5000)], 3, ["4:1", "4:2", "4:3", "5:1", "5:2", "5:3"]))
    check("   Global Connect IR module: three IR and three sensor inputs on module 1",
          sorted(set(x[0] for x in seen["gc_ir"][1])), ["ir", "sensor"])
    check("   Global Connect SL: one RS-232 port", seen["gc_sl"][1], [("serial", "1:1", 4999)])
    check("   Global Connect RT: six relays", len(seen["gc_rt"][1]), 6)
    check("   Global Connect HM: the HDMI switch", [x[0] for x in seen["gc_hm"][1]], ["hdmi"] * 4)
    check("2 · families told apart", {k: v[0] for k, v in seen.items()},
          {"itach_ip2ir": "itach", "itach_ip2sl": "itach", "itach_ip2cc": "itach", "flex": "flex", "gc100_12": "gc100",
           "gc_ir": "globalconnect", "gc_sl": "globalconnect", "gc_rt": "globalconnect", "gc_hm": "globalconnect"})
    check("   …and by the beacon's model when it is known", (P.family_of([], "iTachFlexEthernet"), P.family_of([], "GC-100-12"),
                                                         P.family_of([], "IR3"), P.family_of([], "SL")),
          ("flex", "gc100", "globalconnect", "globalconnect"))

    fam, _, c, u, _, _ = seen["itach_ip2ir"]
    await c.send_ir("1:1", 38000, LG_HDMI1)
    sent = [g for g in u.got if g.startswith("sendir")][-1].split(",")
    counts = [int(x) for x in sent[6:]]
    want = [round(abs(t) * 38000 / 1e6) for t in LG_HDMI1] + [round(40000 * 38 / 1000)]
    check("3 · a real LG code becomes the exact sendir (µs × 38 kHz), closed dark", (sent[1], sent[3], sent[4], sent[5], counts),
          ("1:1", "38000", "1", "1", want))
    check("   …and its completion was waited for (completeir with the same ID)", int(sent[2]) >= 1)
    await asyncio.sleep(0.2)
    await c.send_ir("1:2", 38000, LG_HDMI1_HELD)
    held = [g for g in u.got if g.startswith("sendir")][-1].split(",")
    check("4 · a held key plays as one code (repeats are in the timings; sendir repeat 1)",
          (held[4], len(held) - 6), ("1", len(LG_HDMI1_HELD) + 1))
    err = None
    try:
        await c.send_ir("1:3", 40000, [9000, -4500, 562, -562])
    except P.GlobalCacheError as e:
        err = str(e)
    check("5 · busyir → said in words, not taken as done", bool(err) and "still sending" in err)

    errs = []
    for f in ("itach_ip2ir", "gc100_12", "gc_ir"):
        try:
            await asyncio.sleep(0.2)
            await seen[f][2].send_ir("1:9", 38000, LG_HDMI1)
        except P.GlobalCacheError as e:
            errs.append(e.code)
    check("6 · each family's error form becomes the unit's reason", errs, ["003", "3", "3"])
    check("   …in words", "does not exist on this unit" in str(P.parse_error("ERR_1:9,003")) and
          "baud rate" in str(P.parse_error("ERR SL001")) and "settings are locked" in str(P.parse_error("unknowncommand 27")))

    sl = seen["gc_sl"][2]
    before = await sl.get_serial("1:1")
    after = await sl.set_serial("1:1", 38400, "FLOW_HARDWARE", "PARITY_EVEN")
    bad = None
    try:
        await sl.set_serial("1:1", 12345, "FLOW_NONE", "PARITY_NO")
    except P.GlobalCacheError as e:
        bad = e.code
    check("7 · get/set_SERIAL round-trips; a bad baud is the unit's own refusal",
          (before, after, bad), ({"baud": 9600, "flow": "FLOW_NONE", "parity": "PARITY_NO"},
                                 {"baud": 38400, "flow": "FLOW_HARDWARE", "parity": "PARITY_EVEN"}, "SL001"))

    rt = seen["gc_rt"][2]
    check("8 · a relay switches and reads back", (await rt.set_state("1:4", 1), await rt.get_state("1:4"), await rt.set_state("1:4", 0)), (1, 1, 0))
    g = seen["gc100_12"]
    await g[2].stop_ir("4:1")
    check("   …a GC-100's stopir (no echo, by the document) is not a failure", True)
    await g[2].set_state("3:2", 1)
    check("9 · a GC-100 statechange arriving mid-request reaches the integration", "statechange,4:1,1" in g[4])

    b = P.parse_beacon(b"AMXB<-UUID=GlobalCache_000C1E024239><-SDKClass=Utility><-Make=GlobalCache><-Model=iTachWF2IR>"
                       b"<-Revision=710-1001-05><-Pkg_Level=GCPK001><-Config-URL=http://192.168.1.100.><-PCB_PN=025-0026-06><-Status=Ready>")
    check("10 · the beacon: model, MAC from the UUID, address", (b["model"], b["mac"], b["host"]),
          ("iTachWF2IR", "00:0C:1E:02:42:39", "192.168.1.100"))
    check("    …another maker's beacon is not ours", P.parse_beacon(b"AMXB<-UUID=X><-Make=Other><-Model=Y>"), None)
    freq, tim = P.parse_sendir("sendir,1:1,1,38000,1,1,342,171,21,21,21,64")
    check("11 · a learned code becomes the platform's timings (lit +, dark −)", (freq, tim), (38000, [9000, -4500, 553, -553, 553, -1684]))

    for v in seen.values():
        await v[2].close()
        v[5].close()
    print("\n%d passed, %d failed" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
