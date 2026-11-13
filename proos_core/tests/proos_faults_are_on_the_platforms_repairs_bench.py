"""PROOS FAULTS ARE ON THE PLATFORM'S REPAIRS LIST — register 1326 (12 Nov 2026, plan item D18). DRIVES the real
proos/repairspush.py, the real door file repairs.py (door build 10) against a stand-in of the platform's issue registry
(its 2026.11.1 calls: issues dict, async_create_issue, async_delete_issue, IssueSeverity), and the real
platformfaults.cards.
Run: python3 tests/proos_faults_are_on_the_platforms_repairs_bench.py

  1. Health's open faults are sent whole, each with its title, cause, severity and where the fix is (Pro › Health)
  2. cards that CAME from the platform's lists (platform_*) are not sent back to it
  3. sent when the list changes, and again every few minutes regardless — not on every check
  4. the door makes the platform's ProOS repairs EXACTLY Health's list: new ones raised, closed ones removed
  5. severity carried (critical → critical; anything unknown → warning); tied to the door's entry
  6. another integration's repairs are never touched
  7. Health's reader skips ProOS's own repairs — a fault is never on Health twice
  8. the door's repairs speak in the wording file it ships (translations/en.json), and the installer writes subfolders
"""
import json
import os
import sys
import tempfile
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


from proos import repairspush as RP  # noqa: E402

H = [
    {"id": "offline:office_tv", "kind": "device_offline", "title": "Office TV is not answering", "cause": "No reading for 12 minutes",
     "severity": "critical", "actions": [{"label": "Recover It"}, {"label": "Ask Assist"}]},
    {"id": "low_battery:door", "kind": "battery", "title": "Front door sensor battery low", "cause": "8%", "severity": "info", "actions": []},
    {"id": "platform_repair:x", "kind": "platform_repair", "title": "The platform says…", "severity": "warning"},
]
items = RP.payload(H)
check("1 · Health's faults are sent with title, cause, severity and where the fix is",
      items[1], {"id": "offline:office_tv", "title": "Office TV is not answering", "cause": "No reading for 12 minutes",
                 "severity": "critical", "fix": "In ProOS Pro, Health: Recover It or Ask Assist."})
check("2 · a card that came from the platform's own list is not sent back", [i["id"] for i in items], ["low_battery:door", "offline:office_tv"])


class Client:
    def __init__(self):
        self.sent = []

    def _req(self, m, path, body=None):
        self.sent.append((m, path, body))


c, p = Client(), RP.Pusher()
t0 = 1_800_000_000.0
r = [p.tick(c, H, t0), p.tick(c, H, t0 + 30), p.tick(c, H[:1], t0 + 60), p.tick(c, H[:1], t0 + 60 + RP.PUSH_EVERY + 1)]
check("3 · sent on change and every few minutes — not on every check", (r, c.sent[0][:2]),
      ([True, False, True, True], ("POST", "/api/events/proos_incidents")))

# ── the door's repairs.py against the platform's issue registry ──
issues = {}


class Sev:
    CRITICAL, ERROR, WARNING = "critical", "error", "warning"


def create(hass, domain, issue_id, **kw):
    issues[(domain, issue_id)] = kw


def delete(hass, domain, issue_id):
    issues.pop((domain, issue_id), None)


ir = types.ModuleType("homeassistant.helpers.issue_registry")
ir.IssueSeverity = Sev
ir.async_create_issue, ir.async_delete_issue = create, delete
ir.async_get = lambda hass: types.SimpleNamespace(issues=issues)
core_mod = types.ModuleType("homeassistant.core")
core_mod.callback = lambda f: f
helpers = types.ModuleType("homeassistant.helpers")
helpers.issue_registry = ir
sys.modules.update({"homeassistant": types.ModuleType("homeassistant"), "homeassistant.core": core_mod,
                    "homeassistant.helpers": helpers, "homeassistant.helpers.issue_registry": ir})
from proos import door as D  # noqa: E402

src = D.files("http://core:8770")["repairs.py"].decode()
REP = types.ModuleType("custom_components.proos.repairs")
sys.modules[REP.__name__] = REP
exec(compile(src, "repairs.py", "exec"), REP.__dict__)
entry = types.SimpleNamespace(entry_id="door-entry")
issues[("sonos", "unsupported_speaker")] = {"keep": True}
issues[("proos", "incident_old_fault")] = {"stale": True}
REP.sync(None, entry, RP.payload(H))
mine = {k[1]: v for k, v in issues.items() if k[0] == "proos"}
check("4 · the platform's ProOS repairs are exactly Health's list (old one removed, new ones raised)",
      sorted(mine), ["incident_low_battery_door", "incident_offline_office_tv"])
o = mine["incident_offline_office_tv"]
check("5 · severity carried, unknown → warning; tied to the door's entry; worded from Health",
      (o["severity"], mine["incident_low_battery_door"]["severity"], o["config_entry_id"], o["translation_key"],
       o["translation_placeholders"]["fix"], o["is_fixable"]),
      ("critical", "warning", "door-entry", "incident", "In ProOS Pro, Health: Recover It or Ask Assist.", False))
REP.sync(None, entry, [])
check("   …and when Health is clear, so is the platform's list", sorted(k for k in issues if k[0] == "proos"), [])
check("6 · another integration's repair is never touched", issues.get(("sonos", "unsupported_speaker")), {"keep": True})

from proos import platformfaults as PF  # noqa: E402

PF._snap.update({"at": t0, "entries": [], "resolution": {}, "repairs": [
    {"domain": "proos", "issue_id": "incident_offline_office_tv", "title": "ProOS: Office TV is not answering"},
    {"domain": "sonos", "issue_id": "unsupported_speaker", "title": "Unsupported speaker"}]})
PF._load_seen = lambda: {}
kinds = [(c.get("kind"), c.get("title")) for c in PF.cards()]
check("7 · Health's reader skips ProOS's own repairs (never twice) and still reads everyone else's",
      [k for k in kinds if k[0] == "platform_repair"], [("platform_repair", "Unsupported speaker")])

tr = json.loads(D.files()["translations/en.json"])
check("8 · the repair speaks in the door's own wording file", tr["issues"]["incident"]["title"], "ProOS: {title}")
tmp = tempfile.mkdtemp()
res = D.install(tmp, "http://core:8770")
check("   …and the installer writes the subfolder file", (os.path.exists(os.path.join(tmp, "custom_components", "proos", "translations", "en.json")),
                                                        res["errors"]), (True, []))
src_srv = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
check("   (SOURCE) Core runs the push and the door starts the listener, build 10",
      'threading.Thread(target=_repairs_loop, name="proos-repairs"' in src_srv and "await repairs.async_start(hass, entry)"
      in D.files()["__init__.py"].decode() and D.DOOR_BUILD in ("10", "11", "12") and "repairs.py" in D.DOOR_FILES)   # 1330d/e: builds 11 and 12 keep the Repairs listener
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
