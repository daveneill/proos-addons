"""PROTECT → DETECTIONS — register 1232 (Dave, 6 Nov: "yes all of it please"). Run: python3 tests/the_detections_bench.py

DRIVEN: the REAL proos.protectevents view over events shaped as Dave's console answered them (6 Nov readout), and the
REAL uniconsole.get_image over a stand-in console.
  1. each detection: what it was in words (smart types first: Person, Vehicle …; else its own type), the device by NAME
     (the ONE device-name mechanism, id or MAC), when, how long, and whether it has a picture — newest first
  2. the console's own words stay out of sight (smartDetectZone, alrmBark …); nothing but named fields leaves Core
  3. the filter chips count from the view (Person, Vehicle incl. Number Plate, Motion, Sensor …); a filter keeps only its kinds
  4. an event whose id doesn't look like an id is dropped, and the picture route refuses it (no path tricks)
  5. get_image only takes a picture — a web page or JSON answer is a why-not, never bytes
  6. the routes read only, through ProOS's own console user, and send the view (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import protectevents as pe                              # noqa: E402
from proos import uniconsole as uc                                 # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


CAM, SEN = "65f0aa11bb22cc33dd44ee55", "65f0aa11bb22cc33dd44ee66"
BOOT = {"accessKey": "SECRET-KEY", "cameras": [{"id": CAM, "mac": "F4E2C6A1B2C3", "name": "Driveway"}],
        "sensors": [{"id": SEN, "name": "Garage Door"}]}
EV = [
    {"id": "690c11aa22bb33cc44dd55e1", "type": "smartDetectZone", "smartDetectTypes": ["person", "vehicle"], "camera": CAM,
     "start": 1762400000000, "end": 1762400012000, "score": 88, "thumbnail": "e-1", "metadata": {"x": "SECRET-META"}},
    {"id": "690c11aa22bb33cc44dd55e2", "type": "sensorOpened", "sensor": SEN, "start": 1762400100000, "end": 1762400100000},
    {"id": "690c11aa22bb33cc44dd55e3", "type": "smartAudioDetect", "smartDetectTypes": ["alrmBark"], "camera": "F4:E2:C6:A1:B2:C3",
     "start": 1762399000000, "end": 1762399005000},
    {"id": "690c11aa22bb33cc44dd55e4", "type": "smartDetectZone", "smartDetectTypes": ["licensePlate"], "camera": CAM,
     "start": 1762398000000, "end": 1762398003000, "thumbnail": "e-4"},
    {"id": "../../api/users", "type": "motion", "camera": CAM, "start": 1, "end": 2},
]
v = pe.events_view(EV, BOOT)
by = {x["id"][-2:]: x for x in v}
ok([x["id"][-2:] for x in v] == ["e2", "e1", "e3", "e4"] and by["e1"]["kinds"] == ["Person", "Vehicle"] and by["e1"]["device"] == "Driveway"
   and by["e1"]["seconds"] == 12 and by["e1"]["score"] == 88 and by["e1"]["picture"] and by["e2"]["kinds"] == ["Opened"]
   and by["e2"]["device"] == "Garage Door" and by["e3"]["device"] == "Driveway" and by["e4"]["kinds"] == ["Number Plate"],
   "1 · what it was in words, device by name (id or MAC), when, how long, picture — newest first")
blob = json.dumps(v)
ok(not any(s in blob for s in ("smartDetectZone", "alrmBark", "SECRET", "metadata", "licensePlate")) and by["e3"]["kinds"] == ["Barking"],
   "2 · the console's own words stay out of sight; nothing but named fields leaves Core")
c = pe.counts(v)
only = pe.events_view(EV, BOOT, only="vehicle")
ok(c["person"] == 1 and c["vehicle"] == 2 and c["sensor"] == 1 and c["motion"] == 0
   and [x["id"][-2:] for x in only] == ["e1", "e4"],
   "3 · chips count from the view (Vehicle includes Number Plate); a filter keeps only its kinds")
ok(len(v) == 4 and not pe.safe_id("../../api/users") and not pe.safe_id("") and pe.safe_id("690c11aa22bb33cc44dd55e1"),
   "4 · an id that doesn't look like an id is dropped and refused")


class Creds:
    def meta(self, k):
        return {"host": "192.168.8.1"}

    def get(self, k):
        return None


R = []


def http(method, url, body=None, headers=None, timeout=None):
    return R.pop(0)


orig = uc._session
uc._session = lambda creds, h, http=None: ({"cookie": "TOKEN=x", "x-csrf-token": "c"}, None)
R[:] = [(200, {"content-type": "image/jpeg"}, b"\xff\xd8JPEG")]
a = uc.get_image(Creds(), None, "/proxy/protect/api/events/e1/thumbnail", http=http)
R[:] = [(200, {"content-type": "text/html"}, b"<html>")]
b = uc.get_image(Creds(), None, "/p", http=http)
R[:] = [(200, {"content-type": "application/json"}, b"{}")]
c2 = uc.get_image(Creds(), None, "/p", http=http)
uc._session = orig
ok(a == (b"\xff\xd8JPEG", "image/jpeg", None) and b[0] is None and b[2] and c2[0] is None and c2[2],
   "5 · get_image takes only a picture; a web page or JSON answer is a why-not")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "detections" and sub2 == "thumb":')
route = src[i:src.index('if method == "GET" and sub == "network":', i)]
ok("_pev.safe_id(eid)" in route and '_uc.get_image(_creds, ph, "/proxy/protect/api/events/" + eid + "/thumbnail")' in route
   and '_uc.get_json(_creds, ph, "/proxy/protect/api/events?start=%d&end=%d&limit=300"' in route and "_pev.events_view(" in route
   and "send_json" not in route and "call_service" not in route and '"detections": ev' not in route,
   "6 · the routes read only, through ProOS's own console user, and send the view")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
