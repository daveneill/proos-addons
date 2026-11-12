"""OUR CERTIFIED DRIVERS RIDE INSIDE CORE (register 969a, 20 Oct 2026).

Dave, 12:55: the shipped G-Flip image had none of our drivers ("I'm missing the Samsung TV, Shelly, Coolink"); his
ruling: "Ship inside Core". DRIVES proos/drivers.py against a temporary config folder and a fake platform socket:
install when different, leave alone when identical, touch nothing else; the state asks the platform what it LOADED.
"""
import os, sys, json, shutil, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
from proos import drivers as D

passed = 0; failed = []
def check(name, cond):
    global passed
    if cond: passed += 1; print("  ok    " + name)
    else: failed.append(name); print("  FAIL  " + name)

# RE-PINNED, register 1185 (Dave 3 Nov 09:30: a WattBox outlet must be choosable in Device Recovery; "yes" to a ProOS
# WattBox driver): Core carries 14 — the WattBox community driver joins, labelled not certified.
# RE-PINNED with cause, register 1323 (Dave 12 Nov 16:20: "If you can build it then I will order one to test with"):
# the Global Caché driver joins — carried, and NOT certified until it is seen on his unit (its NOT_CERTIFIED file).
want = {"aegis_ajax", "apple_tv", "coollink", "global_cache", "samsungtv_smart", "savant_ha", "wattbox"}   # 1298/1299: the eight sensor-only copies handed back to the platform
sh = D.shipped()
check("Core carries 7 drivers — Apple TV, Samsung, Coollink, Savant, the interim Ajax driver (971), WattBox (1185) and Global Caché (1323); the eight sensor-only copies are handed back (1298, 1299) (%s)" % ", ".join(sorted(sh)), {k.split("@")[0] for k in sh} == want)   # 1303: apple_tv@<release> builds group under apple_tv
check("1323 · Global Caché rides NOT certified (its NOT_CERTIFIED file), and Core's catalog skips a driver that carries one",
      "NOT_CERTIFIED" in sh["global_cache"]["files"] and 'if "NOT_CERTIFIED" in (_dv.get("files") or {}):' in
      open(os.path.join(CORE, "server.py"), encoding="utf-8").read().split('if parts == ["catalog"]:')[1].split('_e["tier"] = "certified"')[0])
_wb = sh["wattbox"]; _wm = json.loads(_wb["files"]["manifest.json"])
# RE-PINNED with cause, registers 1186–1188 (3 Nov): the driver moved to 1.3.0+proos.4 (?PowerStatus optional, !OutletSet as
# the protocol document spells it, no made-up power on a 250). The rule pinned is unchanged: eseglem's driver, ours to
# answer for, domain wattbox, the upstream licence kept, labelled NOT certified (re-pinned again 4 Nov, cloud copy lost).
check("…WattBox is eseglem 1.3.0+proos.4, named WattBox, ours to answer for, domain wattbox, MIT upstream licence kept, labelled NOT certified",
      (_wb["version"], _wm["name"], _wm["codeowners"], _wm["domain"]) == ("1.3.0+proos.4", "WattBox", ["@protech-proos"], "wattbox")
      and b"MIT License" in _wb["files"]["LICENSE.upstream"] and b"NOT certified" in _wb["files"]["PROOS_BUILD.md"])
# register 971 (24 Oct): the interim Ajax driver is Aegis for Ajax v1.27.2, byte-for-byte but its manifest's name,
# codeowner and version; domain unchanged; its licence and our note ride with it.
_aj = sh["aegis_ajax"]; _am = json.loads(_aj["files"]["manifest.json"])
check("…Ajax is Aegis 1.27.2+proos.1, named Ajax, ours to answer for, domain aegis_ajax",
      (_aj["version"], _am["name"], _am["codeowners"], _am["domain"]) == ("1.27.2+proos.1", "Ajax", ["@protech-proos"], "aegis_ajax"))
check("…carries the upstream licence and says it is the interim, disposable driver",
      b"MIT License" in _aj["files"]["LICENSE.upstream"] and b"DISPOSABLE" in _aj["files"]["PROOS_BUILD.md"])
check("…the remote door's own `proos` component is not among them (door.py writes it)", "proos" not in sh)
check("…Samsung TV is the author's 9.3.0 + the ProOS patch (re-pinned with cause, 1310: was our frozen 8.0.0+proos.9), Cool Automation is coollink 1.0.0",
      sh["samsungtv_smart"]["version"] == "9.3.0+proos.10" and sh["coollink"]["version"] == "1.0.0")
check("…no cache files are shipped", not any("__pycache__" in r or r.endswith(".pyc") or r.endswith(".DS_Store") for d in sh.values() for r in d["files"]))
tmp = tempfile.mkdtemp()
try:
    os.makedirs(os.path.join(tmp, "custom_components", "hacs")); open(os.path.join(tmp, "custom_components", "hacs", "keep.txt"), "w").write("x")
    r1 = D.install(config_dir=tmp)
    check("a fresh box: every driver installed into custom_components", set(r1["installed"]) == want and not r1["errors"])
    check("…files byte-for-byte", open(os.path.join(tmp, "custom_components", "coollink", "manifest.json"), "rb").read() == sh["coollink"]["files"]["manifest.json"])
    check("…and nothing else touched (another integration's files stay)", os.path.exists(os.path.join(tmp, "custom_components", "hacs", "keep.txt")))
    r2 = D.install(config_dir=tmp)
    check("the next boot: identical, so left alone", not r2["installed"] and set(r2["current"]) == want)
    p = os.path.join(tmp, "custom_components", "wattbox", "binary_sensor.py"); open(p, "w").write("edited on the box")   # 1299: was shelly (handed back)
    r3 = D.install(config_dir=tmp)
    check("a driver changed on the box is put back to what Core carries", r3["installed"] == ["wattbox"])
    def ws_loaded(t, integration):
        if t != "manifest/get": raise AssertionError(t)
        if integration == "coollink": raise RuntimeError("Integration not found")
        if integration == "wattbox": raise ConnectionError("no answer")   # 1299: was shelly
        return {"version": sh[integration]["version"]}
    s = D.state(ws_loaded, force=True)
    check("the state asks the platform: loaded where its manifest is our version", "samsungtv_smart" in s["loaded"] and "coollink" not in s["loaded"])
    check("…waiting where the platform has not loaded it (restart once)", s["waiting"] == ["coollink"] and s["restart_needed"] is True and "restart the home engine once" in s["words"])
    check("…and 'can't tell' where the platform did not answer — never a claim", s["unknown"] == ["wattbox"])
finally:
    shutil.rmtree(tmp)
# 969b: LIFX's first ProOS build (2026.6.3, config entries VERSION 1) could not read the entries the platform's own
# 2026.10.2 LIFX had made (VERSION 2) — both showed migration_error on Dave's box. It is rebuilt on 2026.10.2.
# RE-PINNED WITH CAUSE (1299, 11 Nov 2026): LIFX is handed back to the platform's own — the copy added only a Connection
# sensor, which Health no longer uses (1297) — so the 969b rebuild check becomes: Core no longer carries it at all.
check("LIFX is handed back: Core no longer carries a LIFX copy (1299)", "lifx" not in sh and "lifx" in D.RETIRED)
dock = open(os.path.join(CORE, "Dockerfile")).read(); zl = open(os.path.join(CORE, "zipfiles.txt")).read().split()
check("the image copies them in (Dockerfile COPY drivers)", "COPY drivers /app/drivers" in dock)
check("…the zip carries every driver file and the installer", "proos/drivers.py" in zl and all(("drivers/%s/%s" % (d, r)) in zl for d, v in sh.items() for r in v["files"]))
srv = open(os.path.join(CORE, "server.py")).read()
check("Core installs them at boot (with the platform's version, 1301 — re-pinned with cause) and /health reports them",
      "_dv = _drivers.install(platform_version=_pv)" in srv and '"drivers": _drv,' in srv)
check("Core's catalog calls every driver it carries certified (the central catalog still said native for eight)",
      '_e["tier"] = "certified"' in srv and "_drivers.shipped().items()" in srv.split('if parts == ["catalog"]:')[1].split("return self._send(200, _cat)")[0])
from proos import discovery as _disc, watcher as _w  # noqa: E402
check("971 · the Ajax panel is classed security, so it lands where Ness does and is watched as an alarm",
      _disc.device_class("aegis_ajax") == "security" and "aegis_ajax" in _w._SECURITY_PLATFORMS)
check("971 · …and is NOT certified by this edit (certification = on-box validation + Dave's approval)",
      "aegis_ajax" not in _disc.CERTIFIED_INTEGRATIONS)
print("\n%d checks passed, %d failed" % (passed, len(failed)))
sys.exit(1 if failed else 0)
