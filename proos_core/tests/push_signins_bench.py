"""EVERY APP SIGNED IN, AND A CLEAN-UP THAT REMOVES ONLY THE SAME APP — register 1354 (15 Nov 2026).
Run: python3 core/tests/push_signins_bench.py

Apple's Delivery Log (15 Nov 12:45): the test to the iPhone 17 Pro Max was "discarded as device was offline" a
millisecond after Apple received it — the box sent to a sign-in no phone listens on. Read on Dave's box: four "iPad"
and two "iPhone" sign-ins; the iPad's alert went to the PRO app's sign-in. Dave 13:22 "It has to not be a work around".

DRIVEN:
  1. _push_prune_ghosts, EXTRACTED AND RUN against a fake platform store: an older sign-in of the SAME app on the same
     device is removed; the Pro app and the homeowner app on one device (same Apple device id) are NOT each other's
     ghost (the ping-pong that multiplied sign-ins); a newer one, another device, the official app — never touched
  2. pushsignins.signins on a store shaped like Dave's: which app (from the bundle id), the channel the platform sends
     by (the name's slug), how many share it, when the app was last heard (its Last Update Trigger sensor) or "not
     heard" when that sensor is unavailable — never a time made up; the sign-in the last alert went to (by the box's
     own record) carries what Apple said and its receipt; no push token or webhook id in what Pro is given
  3. SOURCE: the routes are tech/owner only; removal only for a sign-in in that list, by the platform's own delete;
     each alert's record names its sign-in; Apple holds an alert up to an hour on BOTH send paths
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "..")
sys.path.insert(0, CORE)
SRC = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


# 1 · the clean-up, extracted and run
i = SRC.index("def _push_prune_ghosts(")
j = SRC.index("\ndef _push_forget(", i)
D = tempfile.mkdtemp(prefix="psi_")
STORE = os.path.join(D, "core.config_entries")
P = "au.com.protechsystems.ProOS"


def entry(eid, app, dev, created, name="iPad"):
    return {"entry_id": eid, "domain": "mobile_app", "created_at": created,
            "data": {"app_id": app, "device_id": dev, "device_name": name, "webhook_id": "wh" + eid,
                     "app_data": {"push_token": "tok" + eid}}}


entries = [entry("home_old", P, "DEV-IPAD", "2026-11-01"), entry("pro", P + "Pro", "DEV-IPAD", "2026-11-05"),
           entry("home_new", P, "DEV-IPAD", "2026-11-10"), entry("home_newer", P, "DEV-IPAD", "2026-11-12"),
           entry("other_dev", P, "DEV-PHONE", "2026-10-01", "iPhone"),
           entry("official", "io.robbie.HomeAssistant", "DEV-IPAD", "2026-09-01")]
json.dump({"data": {"entries": entries}}, open(STORE, "w"))
DELETED = []


class _C:
    def _req(self, method, path):
        DELETED.append(path.rsplit("/", 1)[-1])


ns = {"json": json, "_HA_STORAGE_ENTRIES": STORE, "_PUSH_APP_PREFIX": P, "_push_pruned": set(), "_client": _C()}
exec(SRC[i:j], ns)
reg = {"entry_id": "home_new", "device_id": "DEV-IPAD", "app_id": P, "created_at": "2026-11-10", "device_name": "iPad"}
n = ns["_push_prune_ghosts"](reg)
check("1 · an older sign-in of the SAME app on the same device is removed — and only that", (n, DELETED), (1, ["home_old"]))
check("  …the Pro app on the same iPad is not the homeowner app's ghost (same Apple device id, different app)",
      "pro" not in DELETED)
check("  …a newer sign-in, another device, the official app: never touched",
      not ({"home_newer", "other_dev", "official"} & set(DELETED)))
DELETED.clear()
ns["_push_pruned"] = set()
n2 = ns["_push_prune_ghosts"]({"entry_id": "pro", "device_id": "DEV-IPAD", "app_id": P + "Pro", "created_at": "2026-11-05"})
check("  …and an alert to the Pro app removes none of the homeowner app's sign-ins", (n2, DELETED), (0, []))
check("  …a sign-in with no app named removes nothing",
      ns["_push_prune_ghosts"]({"entry_id": "x", "device_id": "DEV-IPAD", "app_id": "", "created_at": "2027"}), 0)

# 2 · the list Pro shows
from proos import pushsignins as PS  # noqa: E402

store = [entry("a", P, "D1", "2026-11-01"), entry("b", P + "Pro", "D1", "2026-11-05"),
         entry("c", P + ".dev", "D2", "2026-11-03", "iPhone 17 Pro Max"), entry("d", P, "D3", "2026-11-04", "iPhone")]
for e in store:
    e["data"].update(model="iPad14,1", os_name="iPadOS", os_version="26.1")
# shaped like Dave's box (read 15 Nov 13:4x): the platform restored every reading at its start (14 Nov 02:14:02);
# the apps' reports since moved last_reported; the box's own notify.* stamps are not the app
ereg = [{"entity_id": "sensor.ipad_battery_level", "config_entry_id": "a"},
        {"entity_id": "sensor.ipad_app_version", "config_entry_id": "a"},
        {"entity_id": "sensor.ipad_battery_level_2", "config_entry_id": "b"},
        {"entity_id": "sensor.iphone_17_pro_max_battery_level", "config_entry_id": "c"},
        {"entity_id": "sensor.iphone_17_pro_max_last_update_trigger", "config_entry_id": "c"},
        {"entity_id": "notify.iphone_17_pro_max", "config_entry_id": "c"},
        {"entity_id": "sensor.iphone_battery_level", "config_entry_id": "d"},
        {"entity_id": "light.kitchen", "config_entry_id": "hue"}]
T0 = "2026-11-14T02:14:02.169882+00:00"
STATES = {"sensor.ipad_battery_level": {"state": "90", "last_updated": "2026-11-15T01:12:06+00:00",
                                        "last_reported": "2026-11-15T02:42:08+00:00"},
          "sensor.ipad_app_version": {"state": "x", "last_updated": T0, "last_reported": "2026-11-15T02:42:08+00:00"},
          "sensor.ipad_battery_level_2": {"state": "90", "last_updated": T0, "last_reported": "2026-11-15T02:16:35+00:00"},
          "sensor.iphone_17_pro_max_battery_level": {"state": "100", "last_updated": T0, "last_reported": T0},
          "sensor.iphone_17_pro_max_last_update_trigger": {"state": "unavailable", "last_updated": T0, "last_reported": T0},
          "notify.iphone_17_pro_max": {"state": "2026-11-15T01:45:20", "last_updated": "2026-11-15T01:45:20+00:00",
                                       "last_reported": "2026-11-15T01:45:20+00:00"},
          "sensor.iphone_battery_level": {"state": "unavailable", "last_updated": T0,
                                          "last_reported": "2026-11-15T03:00:00+00:00"}}
pst = {"iPad": {"entry_id": "b", "ok": True, "words": "delivered to Apple", "at": 1794700000,
                "receipt": {"apns-id": "R1"}},
       "iPhone 17 Pro Max": {"ok": True, "at": 1}}
rows = {r["entry_id"]: r for r in PS.signins(store, ereg, STATES.get, pst)}
check("2 · which app, from the app's own bundle id",
      [rows[k]["app"] for k in "abcd"], ["Homeowner", "Pro", "Homeowner · developer build", "Homeowner"])
check("  …the channel the platform sends by, and how many sign-ins share it",
      [(rows[k]["channel"], rows[k]["shares"]) for k in "abcd"],
      [("mobile_app_ipad", 1), ("mobile_app_ipad", 1), ("mobile_app_iphone_17_pro_max", 0), ("mobile_app_iphone", 0)])
check("  …last heard = the latest report on the app's own readings, after the box started; the start-up restore is "
      "not a report, the box's own alert stamp is not the app, an unavailable reading is not a report",
      [rows[k]["heard"] for k in "abcd"], ["2026-11-15T02:42:08+00:00", "2026-11-15T02:16:35+00:00", None, None])
_calls = []
PS.signins(store, ereg, lambda eid: (_calls.append(eid), STATES.get(eid))[1], {})
check("  …only the apps' own readings are read — never the rest of the house", "light.kitchen" not in _calls)
check("  …the last alert for 'iPad' is shown on the sign-in it went to (the box's own record), not on the other",
      (rows["b"]["last_alert_here"], rows["b"]["last_alert"]["receipt"], rows["a"]["last_alert_here"], rows["a"]["last_alert"]),
      (True, {"apns-id": "R1"}, False, None))
check("  …a record that names no sign-in is shown on none", rows["c"]["last_alert_here"], False)
blob = json.dumps(list(rows.values()))
check("  …no push token or webhook id in what Pro is given", "tok" not in blob and "wh" not in blob)
check("  …oldest first", [r["entry_id"] for r in PS.signins(store, ereg, STATES.get, {})], ["a", "c", "d", "b"])
check("  …the slug is the platform's (the same rule as Core's)", PS.slug("Dave’s iPhone 17 Pro Max"), "daves_iphone_17_pro_max")

# 3 · SOURCE
g = SRC[SRC.index('if parts == ["push", "signins"]:'):][:700]
check("3 · (SOURCE) the list is tech/owner only", 'users.is_tech(u.get("id"))' in g and "_push_signins()" in g)
r_ = SRC[SRC.index('if parts == ["push", "signins", "remove"]:'):][:1300]
check("  …removal: tech/owner only, only a sign-in in that list, by the platform's own delete",
      'users.is_tech(u.get("id"))' in r_ and 'any(r["entry_id"] == eid for r in _push_signins())' in r_
      and '"/api/config/config_entries/entry/%s" % eid' in r_)
check("  …(SOURCE) the stamps come through the platform's template engine (its REST answer was read stale on Dave's box)",
      "_client.render_template(_TPL)" in SRC and "s.last_reported.isoformat()" in SRC
      and "integration_entities('mobile_app')" in SRC)
check("  …each alert's record names the sign-in it went to", '"entry_id": (reg or {}).get("entry_id"),' in SRC)
GW = open(os.path.join(CORE, "..", "connect", "relay", "gateway.py"), encoding="utf-8").read()
PU = open(os.path.join(CORE, "proos", "push.py"), encoding="utf-8").read()
check("  …Apple holds an alert up to an hour, on Connect's send and the box's own",
      '"apns-expiration: %d" % (int(time.time()) + HOLD_S)' in GW and "HOLD_S = 3600" in GW
      and 'f"apns-expiration: {int(time.time()) + 3600}"' in PU)
PRO = open(os.path.join(CORE, "apps", "pro.html"), encoding="utf-8").read()
check("  …Pro lists the sign-ins, says when a name is shared, and removes on a second tap (no browser dialog)",
      "/push/signins'" in PRO and "Shares the name" in PRO and "Tap Again to Remove" in PRO
      and "confirm(" not in PRO[PRO.index("async function notifySigninRemove"):][:900])

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
