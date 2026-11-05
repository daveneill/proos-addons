"""PROTECT → CAMERAS / DOORBELL — register 1233 (Dave, 6 Nov: "yes all of it please"). Run: python3 tests/the_camera_controls_bench.py

PLATFORM FIRST: every camera setting is the platform's own UniFi Protect control ("<MAC>_<key>", measured on Dave's box).
DRIVEN: the REAL proos.protectcams over registry rows and states shaped as the platform keeps them.
  1. each camera is joined to ITS OWN controls by MAC (any spelling); another camera's, another platform's, or a key ProOS
     doesn't offer never joins; a disabled control is counted, never offered
  2. each control carries its state now, its choices (select) or range (number), its group and what changing it does
  3. plan(): a switch needs on/off; a choice must be one the platform offers; a number within its range; anything not
     ProOS's to offer, disabled, or on another platform is refused with a reason
  4. landed(): "confirmed" only when the platform's state shows the value asked for
  5. the doorbell is the camera that has doorbell controls
  6. the routes: read joins the official list to the platform's controls; set runs only what plan() allows, then reads the
     platform's state back (SOURCE)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import protectcams as pc                                # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


A, B = "F4E2C6A1B2C3", "F4E2C6A1B2C4"
ENTS = [
    {"platform": "unifiprotect", "unique_id": A + "_status_light", "entity_id": "switch.drive_status_light"},
    {"platform": "unifiprotect", "unique_id": A + "_recording_mode", "entity_id": "select.drive_recording_mode"},
    {"platform": "unifiprotect", "unique_id": A + "_mic_level", "entity_id": "number.drive_mic_level"},
    {"platform": "unifiprotect", "unique_id": A + "_hdr_mode", "entity_id": "switch.drive_hdr_mode", "disabled_by": "integration"},
    {"platform": "unifiprotect", "unique_id": A + "_privacy_mode", "entity_id": "switch.drive_privacy_mode", "disabled_by": "user"},
    {"platform": "unifiprotect", "unique_id": A + "_ssh", "entity_id": "switch.drive_ssh"},
    {"platform": "unifiprotect", "unique_id": A + "_reboot", "entity_id": "button.drive_reboot"},
    {"platform": "other", "unique_id": A + "_privacy_mode", "entity_id": "switch.not_protect"},
    {"platform": "unifiprotect", "unique_id": B + "_status_light", "entity_id": "switch.bell_status_light"},
    {"platform": "unifiprotect", "unique_id": B + "_doorbell_text", "entity_id": "select.bell_doorbell_text"},
]
STATES = [
    {"entity_id": "switch.drive_status_light", "state": "on"},
    {"entity_id": "select.drive_recording_mode", "state": "Always", "attributes": {"options": ["Always", "Never", "Detections"]}},
    {"entity_id": "number.drive_mic_level", "state": "80", "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}},
    {"entity_id": "switch.bell_status_light", "state": "unavailable"},
    {"entity_id": "select.bell_doorbell_text", "state": "Welcome", "attributes": {"options": ["Welcome", "Leave package at door"]}},
]
CAMS = [{"id": "c1", "name": "Driveway", "type": "G5 Bullet", "state": "CONNECTED", "mac": "f4:e2:c6:a1:b2:c3", "featureFlags": {"hasMic": True}},
        {"id": "c2", "name": "Front Door", "type": "G4 Doorbell Pro", "state": "CONNECTED", "mac": B, "featureFlags": {}}]
v = {c["name"]: c for c in pc.cameras_view(CAMS, ENTS, STATES)}
dk = [r["key"] for r in v["Driveway"]["controls"]]
ok(dk == ["recording_mode", "status_light", "mic_level"] and v["Driveway"]["controls_disabled"] == 1
   and [r["key"] for r in v["Front Door"]["controls"]] == ["status_light", "doorbell_text"],
   "1 · each camera joins its own controls by MAC; others, other platforms, keys ProOS doesn't offer and a control of the wrong kind (the platform's disabled HDR switch, as on Dave's box) never join; a disabled one ProOS offers is counted")
rm = {r["key"]: r for r in v["Driveway"]["controls"]}
ok(rm["recording_mode"]["options"] == ["Always", "Never", "Detections"] and rm["recording_mode"]["state"] == "Always"
   and rm["mic_level"]["max"] == 100 and rm["mic_level"]["unit"] == "%" and rm["status_light"]["group"] == "On the Camera"
   and "records" in rm["recording_mode"]["does"] and not {r["key"]: r for r in v["Front Door"]["controls"]}["status_light"]["available"],
   "2 · each control carries its state, choices or range, group and what changing it does (unavailable shown as such)")
E = {e["entity_id"]: e for e in ENTS}
S = {s["entity_id"]: s for s in STATES}
p1 = pc.plan(E["switch.drive_status_light"], S["switch.drive_status_light"], False)
p2 = pc.plan(E["select.drive_recording_mode"], S["select.drive_recording_mode"], "Never")
p3 = pc.plan(E["select.drive_recording_mode"], S["select.drive_recording_mode"], "Sometimes")
p4 = pc.plan(E["number.drive_mic_level"], S["number.drive_mic_level"], 150)
p5 = pc.plan(E["number.drive_mic_level"], S["number.drive_mic_level"], 40)
p6 = pc.plan(E["switch.drive_ssh"], None, True)
p7 = pc.plan(E["switch.drive_hdr_mode"], None, True)
p8 = pc.plan(E["switch.not_protect"], None, True)
p9 = pc.plan(E["switch.drive_status_light"], None, "yes")
ok(p1 == ("switch", "turn_off", {}) and p2 == ("select", "select_option", {"option": "Never"}) and p3[0] is None
   and p4[0] is None and p5 == ("number", "set_value", {"value": 40.0}) and p6[0] is None and p7[0] is None and p8[0] is None and p9[0] is None,
   "3 · plan(): on/off, an offered choice, a number in range; anything else refused with a reason")
ok(pc.landed("switch", {"state": "off"}, False) and not pc.landed("switch", {"state": "on"}, False)
   and pc.landed("select", {"state": "Never"}, "Never") and pc.landed("number", {"state": "40.0"}, 40) and not pc.landed("number", {"state": "unavailable"}, 40),
   "4 · landed() only when the platform's state shows the value asked for")
ok(v["Front Door"]["doorbell"] and not v["Driveway"]["doorbell"], "5 · the doorbell is the camera with doorbell controls")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "cameras" and not sub2:')
route = src[i:src.index('if method == "GET" and sub == "detections" and sub2 == "thumb":', i)]
ok("_pc.cameras_view(" in route and "ProtectClient(_creds).cameras()" in route and "dom, svc, data = _pc.plan(ent, st, val)" in route
   and "if dom is None:" in route and "_client.call_service(dom, svc, eid, data or None)" in route and route.count("call_service(") == 1
   and "_pc.landed(dom, st2, val)" in route and '"confirmed": False' in route,
   "6 · read joins the official list to the platform's controls; set runs only what plan() allows and reads back")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
