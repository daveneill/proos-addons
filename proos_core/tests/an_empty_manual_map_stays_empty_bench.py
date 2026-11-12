"""AN EMPTY MANUAL MAP STAYS EMPTY — register 1327 (13 Nov 2026). DRIVES the real apply_auto_reachability from
server.py (extracted and run with stand-ins for the platform reads).
Run: python3 tests/an_empty_manual_map_stays_empty_bench.py

Read on Dave's box: 27 watched devices with a UniFi tracker found by MAC were held on the retired copies' dead
Connection sensors, because a half-built first map had been frozen as the installer's "manual" settings.
  1. with no manual settings, a second run does NOT turn the first run's map into "manual"
  2. a first run made before the UniFi trackers are readable is healed by the next run: the tracker wins
  3. a real manual setting (from the add-on options) still wins over the automatic map, run after run
"""
import ast
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SRV = open(os.path.join(os.path.dirname(HERE), "server.py"), encoding="utf-8").read()
PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


tree = ast.parse(SRV)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_auto_reachability")
code = ast.get_source_segment(SRV, fn)


sys.path.insert(0, os.path.dirname(HERE))      # the function imports Core's own proos.reachability


class Watcher:
    reach_map = {}

    def reach_specs(self):
        return []


class NetMap:
    trackers_ready = False

    def harvest(self, client=None):
        return {"light.kitchen_dining": {"ip": "192.168.8.215", "via": "config_entry"}}

    def harvest_unifi_trackers(self, client, auto):
        if not self.trackers_ready:
            return {}
        return {"light.kitchen_dining": {"sensor": "device_tracker.dining_main_shelly_mini_g4", "via": "unifi_tracker_mac",
                                         "ip": "192.168.8.215"}}


def run_env(cfg):
    g = {"_cfg": cfg, "netmap": NetMap(), "_client": object(), "_watcher": Watcher(), "_controllers": {},
         "print": lambda *a, **k: None}
    exec(code, g)
    return g


# a fresh Core, no manual settings in the add-on options; the first run happens before the trackers are readable
cfg = {"reachability": {}}
g = run_env(cfg)
g["apply_auto_reachability"]()
g["netmap"].trackers_ready = True
g["apply_auto_reachability"]()
check("1 · with no manual settings, the first map is NOT frozen as manual", cfg["_reach_manual"], {})
check("2 · the next run heals it: Dining's second check is its UniFi tracker",
      g["_watcher"].reach_map.get("light.kitchen_dining", {}).get("sensor"), "device_tracker.dining_main_shelly_mini_g4")
g["apply_auto_reachability"]()
check("   …and stays so run after run", (cfg["_reach_manual"], g["_watcher"].reach_map["light.kitchen_dining"]["via"]),
      ({}, "unifi_tracker_mac"))
from proos import reachability as R  # noqa: E402 — Core's own rule for what counts as a second check
check("   …and by Core's own rule that is a witness again (the frozen entry, an address alone, was not)",
      (R.is_witness(g["_watcher"].reach_map["light.kitchen_dining"]),
       R.is_witness({"ip": "192.168.8.215", "via": "config_entry"})), (True, False))

cfg2 = {"reachability": {"light.kitchen_dining": {"ip": "10.0.0.9"}}}
g2 = run_env(cfg2)
g2["netmap"].trackers_ready = True
g2["apply_auto_reachability"]()
g2["apply_auto_reachability"]()
check("3 · a real manual setting still wins over the automatic map, run after run",
      (cfg2["_reach_manual"], g2["_watcher"].reach_map["light.kitchen_dining"]), ({"light.kitchen_dining": {"ip": "10.0.0.9"}},
                                                                                   {"ip": "10.0.0.9"}))
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
