"""EACH PERSON'S PRIMARY DEVICE DECIDES THEIR HOME AND AWAY — register 1358 (15 Nov 2026).
Run: python3 core/tests/primary_device_bench.py

Dave 16:13 (UniFi's Geofencing: Primary Device), 16:14 / 16:15 "yes". Read on his box 15 Nov: "ProOS Developer"
followed the iPad AND the Pro app on his MacBook — the platform links every device a person signs in on.

DRIVEN (proos/presence.py on records shaped like Dave's box):
  1. a person's app devices are their own sign-ins (by user), oldest first; an iPhone is a phone, a Mac running the
     iPad app (an iPad model) is not
  2. no choice yet → the first iPhone they signed in on; never a Mac or an iPad for them
  3. a choice → that device, even an iPad (theirs to make); a choice that no longer exists → back to the first iPhone
  4. keep(): the record follows exactly the primary among the app trackers; a tracker that is not an app sign-in's
     (a network presence tracker an installer added) is kept; a person with no possible primary is left alone;
     nothing written when already right
  5. the platform's person/update gets every field back unchanged but the trackers (SOURCE)
  6. routes: anyone signed in reads only themselves unless tech/owner; a choice must be one of that person's own
     devices; the radius goes to the platform's own home zone, 50–2000 m (SOURCE); a minute's loop keeps it (SOURCE)
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "..")
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="pres_")
sys.path.insert(0, CORE)
from proos import presence as P  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


def entry(eid, user, model, created, name):
    return {"entry_id": eid, "domain": "mobile_app", "created_at": created,
            "data": {"user_id": user, "model": model, "device_name": name, "app_id": "au.com.protechsystems.ProOS"}}


DAVE = "u-dave"
entries = [entry("ipad", DAVE, "iPad13,8", "2026-11-15T03:08", "Dave's iPad Pro"),
           entry("mac", DAVE, "iPad8,6", "2026-11-15T03:17", "ProOS's Macbook (Pro)"),
           entry("phone", DAVE, "iPhone19,7", "2026-11-15T03:18", "Dave's iPhone 18 Pro Max"),
           entry("sarah", "u-sarah", "iPhone17,1", "2026-11-15T04:00", "Sarah's iPhone"),
           {"entry_id": "hue", "domain": "hue", "data": {}}]
trackers = {"ipad": "device_tracker.ipad", "mac": "device_tracker.ipad_2", "phone": "device_tracker.iphone_6",
            "sarah": "device_tracker.iphone_7"}
persons = [{"id": "p-dave", "name": "ProOS Developer", "user_id": DAVE,
            "device_trackers": ["device_tracker.ipad", "device_tracker.ipad_2"]},
           {"id": "p-sarah", "name": "Sarah", "user_id": "u-sarah",
            "device_trackers": ["device_tracker.iphone_7", "device_tracker.unifi_sarah_phone", "device_tracker.ipad_2"]},
           {"id": "p-guest", "name": "Guest", "user_id": "u-guest", "device_trackers": ["device_tracker.ipad"]}]
states = {"device_tracker.iphone_6": "not_home", "person:p-dave": "home"}

# 1, 2
ppl = {p["person_id"]: p for p in P.people(persons, entries, trackers, states)}
d = ppl["p-dave"]
check("1 · a person's devices are their own sign-ins, oldest first",
      [x["entry_id"] for x in d["devices"]], ["ipad", "mac", "phone"])
check("  …an iPhone is a phone; the Mac running the iPad app is not", [x["phone"] for x in d["devices"]], [False, False, True])
check("2 · no choice yet → the first iPhone they signed in on (not the older iPad, never the Mac)",
      (d["primary"], d["why"]), ("phone", "first iPhone"))
check("  …the person's own state is shown", d["state"], "home")

# 3
ppl = {p["person_id"]: p for p in P.people(persons, entries, trackers, states, {"p-dave": "ipad"})}
check("3 · a choice is theirs to make — an iPad if they choose it", (ppl["p-dave"]["primary"], ppl["p-dave"]["why"]),
      ("ipad", "chosen"))
ppl = {p["person_id"]: p for p in P.people(persons, entries, trackers, states, {"p-dave": "gone"})}
check("  …a choice that no longer exists → back to the first iPhone", ppl["p-dave"]["primary"], "phone")
check("  …a person with no sign-in of their own has no primary", ppl["p-guest"]["primary"], None)

# 4
W = []
read = lambda: (persons, entries, trackers, states)
write = lambda person, ts: W.append((person["id"], ts))
changed = P.keep(read, write)
check("4 · the record follows exactly the primary among the app trackers (the Mac and the old iPad come off)",
      dict(W)["p-dave"], ["device_tracker.iphone_6"])
check("  …a tracker that is not an app sign-in's is kept", dict(W)["p-sarah"],
      ["device_tracker.unifi_sarah_phone", "device_tracker.iphone_7"])
check("  …1360: a person never follows another login's device — Guest's record had Dave's iPad; with no device of their own it follows none",
      dict(W)["p-guest"], [])
W.clear()
persons[0]["device_trackers"] = ["device_tracker.iphone_6"]
persons[1]["device_trackers"] = ["device_tracker.unifi_sarah_phone", "device_tracker.iphone_7"]
persons[2]["device_trackers"] = []
check("  …nothing written when already right", (P.keep(read, write), W), ([], []))
P.choose("p-dave", "ipad")
P.keep(read, write)
check("  …a choice is kept on the box and applied", W, [("p-dave", ["device_tracker.ipad"])])

# 4b · register 1360 — a login with an app and no person gets one (read on Dave's box: the homeowner login had none)
users = [{"id": DAVE, "name": "ProOS Developer"}, {"id": "u-home", "name": "Home", "is_active": True},
         {"id": "u-sys", "name": "Supervisor", "system_generated": True}, {"id": "u-panel", "name": "Panel"},
         {"id": "u-off", "name": "Old", "is_active": False}]
entries += [entry("hphone", "u-home", "iPhone19,7", "2026-11-15T03:50", "Dave's iPhone 18 Pro Max"),
            entry("hsys", "u-sys", "iPhone1,1", "2026-11-15T03:51", "x"), entry("hoff", "u-off", "iPhone1,1", "2026-11-15T03:52", "y")]
trackers["hphone"] = "device_tracker.iphone_8"
check("4b · a login with an app sign-in and no person needs one — never a system login, an inactive one, or one with no app",
      P.missing_persons(users, persons, entries), [("u-home", "Home")])
MADE = []


def create(uid, name):
    MADE.append(uid)
    persons.append({"id": "p-home", "name": name, "user_id": uid, "device_trackers": []})


W.clear()
P.keep(read, write, lambda: users, create)
check("  …made through the platform's person/create, then put on its first iPhone in the same pass",
      (MADE, dict(W).get("p-home")), (["u-home"], ["device_tracker.iphone_8"]))

# 5, 6 SOURCE
SRC = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
check("5 · (SOURCE) person/update passes every field back but the trackers",
      '_ws_call("person/update", person_id=person["id"], name=person.get("name"), user_id=person.get("user_id"),'
      in SRC and 'picture=person.get("picture"))' in SRC)
g = SRC[SRC.index('if parts == ["people", "homeaway"]:'):][:1400]
check("6 · (SOURCE) a person who is not tech/owner reads only themselves",
      'ppl = [x for x in ppl if x.get("user_id") == u.get("id")]' in g and 'x["me"] = x.get("user_id") == u.get("id")' in g)
p_ = SRC[SRC.index('if parts == ["people", "homeaway", "primary"]:'):][:1800]
check("  …(SOURCE) a choice: one of that person's own devices; only tech/owner for someone else",
      "only your own Primary Device" in p_ and "that is not one of this person's devices" in p_)
h_ = SRC[SRC.index('if parts == ["people", "homeaway", "home"]:'):][:1000]
check("  …(SOURCE) the radius goes to the platform's own home zone, 50–2000 m",
      '_ws_call("config/core/update", radius=r_)' in h_ and "50 <= r_ <= 2000" in h_)
check("  …(SOURCE) a minute's loop keeps every record on its primary, and gives every app login a person first",
      "_pr_boot.keep(_presence_read, _presence_write, _presence_users, _presence_create)" in SRC
      and '_ws_call("person/create", name=name, user_id=user_id, device_trackers=[])' in SRC and "_PRESENCE_EVERY_S = 60" in SRC)
check("  …(SOURCE) the existing screen-presence route is untouched",
      SRC.count('if parts == ["presence"]:') == 2)

PRO = open(os.path.join(CORE, "apps", "pro.html"), encoding="utf-8").read()
check("7 · (SOURCE) Pro's People page shows each person's Primary Device and the Home Radius, through Core",
      "/people/homeaway'" in PRO and "/people/homeaway/primary'" in PRO and "/people/homeaway/home'" in PRO
      and "A Mac or a wall panel never counts unless it is chosen here." in PRO)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
