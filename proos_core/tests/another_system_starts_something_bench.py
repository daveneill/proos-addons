"""ANOTHER SYSTEM STARTS SOMETHING — OUTSIDE TRIGGERS (register 1328, 13 Nov 2026, plan F23). DRIVES the real
proos/triggers.py against a stand-in platform (its automation config API, its states, its webhook endpoint).
Run: python3 tests/another_system_starts_something_bench.py

  1. an Outside Trigger is ONE platform automation: a webhook trigger, one action that starts the activity or scene
  2. the address is a long random key (never the name), and two triggers never share one
  3. it starts only an activity (script) or a scene — never a lock, an alarm, a light switched raw
  4. the box's ProOS automations are listed back with name, target, address and "last used"; others are not
  5. both addresses are built from the box's own (home) and ProOS Connect's (away) — none invented when unknown
  6. remove takes back only ProOS's own; test calls the trigger exactly as the other system will
  7. (1328a) Make answers once the platform is RUNNING the trigger, and says "not ready" when it never started
"""
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from proos import triggers as T  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


class Platform:
    def __init__(self):
        self.autos, self.calls = {}, []
        self.autos["other_one"] = {"id": "other_one", "alias": "Garden lights at dusk", "triggers": [], "actions": []}

    def _req(self, m, path, body=None):
        self.calls.append((m, path, body))
        if path == "/api/states":
            out = [{"entity_id": "automation." + a, "state": "on", "attributes": {"id": a, "friendly_name": c.get("alias"),
                    "last_triggered": "2026-11-13T07:40:00+11:00" if a.startswith(T.AUTO_PREFIX) else None}}
                   for a, c in self.autos.items()]
            return out
        if path.startswith("/api/config/automation/config/"):
            aid = path.rsplit("/", 1)[1]
            if m == "POST":
                self.autos[aid] = body
            elif m == "DELETE":
                self.autos.pop(aid, None)
            return self.autos.get(aid) or {}
        return {}


P = Platform()
made = T.create(P, "Doorbell Pressed", "script.front_door_welcome")
auto = P.autos[made["id"]]
trig, act = auto["triggers"][0], auto["actions"][0]
check("1 · ONE platform automation: a webhook trigger, one action starting the activity",
      (auto["alias"], trig["trigger"], act), ("ProOS Outside Trigger: Doorbell Pressed", "webhook",
                                              {"action": "script.turn_on", "target": {"entity_id": "script.front_door_welcome"}}))
other = T.create(P, "Doorbell Pressed", "scene.evening")
check("2 · the address is a long random key, never the name; two never share one",
      (len(trig["webhook_id"]) >= 32, "doorbell" not in trig["webhook_id"].lower(),
       trig["webhook_id"] != P.autos[other["id"]]["triggers"][0]["webhook_id"], made["id"] != other["id"]), (True, True, True, True))
bad = []
for t in ("lock.front_door", "alarm_control_panel.home", "light.kitchen", "script", ""):
    try:
        T.create(P, "X", t)
    except ValueError:
        bad.append(t)
check("3 · it starts only an activity or a scene — never a lock, an alarm, a raw light", len(bad), 5)
try:
    T.create(P, "  ", "script.x")
    noname = False
except ValueError:
    noname = True
check("   …and it has a name the other system's installer will see", noname)
L = T.listing(P)
check("4 · the box's ProOS triggers are listed back — name, target, address, last used; others are not",
      ([t["name"] for t in L], [t["target"] for t in L], all(t["webhook_id"] and t["last_used"] for t in L),
       any(t["id"] == "other_one" for t in L)), (["Doorbell Pressed", "Doorbell Pressed"], ["script.front_door_welcome", "scene.evening"], True, False))
a = T.addresses("abc", "http://192.168.8.117", "https://home.example:21003")
n = T.addresses("abc", None, None)
check("5 · addresses from the box (home) and ProOS Connect (away); none invented when unknown",
      (a, n), ({"home": "http://192.168.8.117/api/webhook/abc", "away": "https://home.example:21003/api/webhook/abc"},
               {"home": None, "away": None}))
try:
    T.remove(P, "other_one")
    took_other = True
except ValueError:
    took_other = False
T.test(P, trig["webhook_id"])
T.remove(P, made["id"])
check("6 · remove takes back only ProOS's own; test calls the trigger as the other system will",
      (took_other, "other_one" in P.autos, made["id"] in P.autos, P.calls[-2][:2]),
      (False, True, False, ("POST", "/api/webhook/" + trig["webhook_id"])))

# 7 · (1328a, read on the box 13 Nov) the platform saves the automation, then starts it a moment later: Make answers
#     only once the platform is running it — and says so honestly when it never started
class SlowPlatform(Platform):
    def __init__(self, starts_after):
        super().__init__()
        self.reads, self.starts_after = 0, starts_after

    def _req(self, m, path, body=None):
        if path == "/api/states":
            self.reads += 1
            out = Platform._req(self, m, path, body)
            return [s for s in out if not s["attributes"]["id"].startswith(T.AUTO_PREFIX) or self.reads > self.starts_after]
        return Platform._req(self, m, path, body)


slept = []
SP = SlowPlatform(3)
m1 = T.create(SP, "Gate Opened", "scene.evening", sleep=slept.append)
check("7 · Make waits until the platform is running it, then says ready", (m1["ready"], m1["on"], len(slept)), (True, True, 3))
slept.clear()
m2 = T.create(SlowPlatform(10 ** 6), "Never Starts", "scene.evening", sleep=slept.append)
check("   …and when it never starts, it waits a bounded time and says NOT ready (never claimed)",
      (m2["ready"], sum(slept) >= T.READY_WAIT_S, len(slept) < 40), (False, True, True))
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
