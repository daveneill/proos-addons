"""PROTECT → VIEWPORTS · RECORDINGS — register 1235 (Dave, 6 Nov: "yes all of it please").
Run: python3 tests/the_viewports_recordings_bench.py

  1. the recorder's storage comes from the PLATFORM's own recorder sensors joined by its MAC, in words (Storage Used 41 %,
     Disk 1 Healthy / a disk problem flagged) — DRIVEN over the real protectcams.readings_for
  2. get_media takes only the type asked for: a clip must be video, a picture an image — DRIVEN over a stand-in console
  3. the clip route: a safe id only; reads the event for its camera and times; at most 60 s, padded 2 s; only a video
     answer is sent (SOURCE)
  4. the viewports and recordings routes only read (SOURCE)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import protectcams as pc                                # noqa: E402
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


N = "AABBCCDDEEFF"
ENTS = [{"platform": "unifiprotect", "unique_id": N + "_storage_utilization", "entity_id": "sensor.nvr_storage"},
        {"platform": "unifiprotect", "unique_id": N + "_disk_health_0", "entity_id": "binary_sensor.nvr_disk_0"},
        {"platform": "unifiprotect", "unique_id": N + "_record_capacity", "entity_id": "sensor.nvr_capacity"}]
STATES = [{"entity_id": "sensor.nvr_storage", "state": "41.2", "attributes": {"unit_of_measurement": "%"}},
          {"entity_id": "binary_sensor.nvr_disk_0", "state": "off"},
          {"entity_id": "sensor.nvr_capacity", "state": "1209600", "attributes": {"unit_of_measurement": "s"}}]
r = {x["label"]: x for x in pc.readings_for(N, ENTS, STATES)}
ok(r["Recording Capacity"]["value"] == "14 days", "1b · (1238, measured \"3303119 s\") the recorder's capacity reads in days")
STATES[1]["state"] = "on"
r2 = {x["label"]: x for x in pc.readings_for(N, ENTS, STATES)}
ok(r["Storage Used"]["value"] == "41.2 %" and r["Disk 1"]["value"] == "Healthy" and not r["Disk 1"]["alert"]
   and r2["Disk 1"]["value"] == "Problem" and r2["Disk 1"]["alert"],
   "1 · the recorder's storage from the platform's own sensors, in words; a disk problem flagged")


class Creds:
    def meta(self, k):
        return {"host": "192.168.8.1"}

    def get(self, k):
        return None


R = []
orig = uc._session
uc._session = lambda creds, h, http=None: ({"cookie": "TOKEN=x", "x-csrf-token": "c"}, None)
R[:] = [(200, {"content-type": "video/mp4"}, b"MP4")]
a = uc.get_media(Creds(), None, "/v", "video/", http=lambda *x, **k: R.pop(0))
R[:] = [(200, {"content-type": "image/jpeg"}, b"JPG")]
b = uc.get_media(Creds(), None, "/v", "video/", http=lambda *x, **k: R.pop(0))
R[:] = [(200, {"content-type": "image/jpeg"}, b"JPG")]
c = uc.get_image(Creds(), None, "/i", http=lambda *x, **k: R.pop(0))
uc._session = orig
ok(a == (b"MP4", "video/mp4", None) and b[0] is None and c == (b"JPG", "image/jpeg", None),
   "2 · get_media takes only the type asked for (a clip must be video)")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "clip" and not sub2:')
clip = src[i:src.index('if method == "POST" and sub == "cameras" and sub2 == "set":', i)]
ok("_pev.safe_id(eid)" in clip and '_uc.get_json(_creds, ph, "/proxy/protect/api/events/" + eid)' in clip
   and "_pev.safe_id(ev.get(\"camera\"))" in clip and "t0 + 60000" in clip and '- 2000' in clip and '+ 2000' in clip
   and '"video/")' in clip and "send_json" not in clip,
   "3 · the clip: a safe id, the event's camera and times, at most 60 s padded 2 s, only a video answer")
j = src.index('if method == "GET" and sub == "viewports" and not sub2:')
vr = src[j:i]
ok('collection("viewers")' in vr and 'collection("liveviews")' in vr and 'collection("nvrs")' in vr and "_pc.readings_for(" in vr
   and "call_service" not in vr and "send_json" not in vr,
   "4 · the viewports and recordings routes only read")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
