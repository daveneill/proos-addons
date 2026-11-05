"""PROTECT → LIGHTS & CHIMES · SENSORS — register 1234 (Dave, 6 Nov: "yes all of it please").
Run: python3 tests/the_lights_sensors_bench.py

PLATFORM FIRST: the platform's own unifiprotect controls and readings, joined by MAC (measured on Dave's box: floodlights
= the light itself (unique id the MAC alone), light_motion, sensitivity, duration, paired_camera, status_light; sensors =
mount_type, sensitivity, reading switches, clear_tamper, and door / leak / battery / temperature … readings).
  1. a floodlight joins its light (the bare-MAC id), when it turns on, how long, sensitivity, paired camera, status light
  2. a sensor's readings in words: door Open/Closed, leak Wet/Dry, battery %, temperature with its unit; an alert flagged;
     a silent reading says so; readings are never offered as controls
  3. plan(): the light is on/off through the light service; a button is only pressed (True), nothing else
  4. a button is confirmed only when the platform records a new press time; the devices route reads only (SOURCE)
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


L, S = "AABBCCDDEE01", "AABBCCDDEE02"
ENTS = [
    {"platform": "unifiprotect", "unique_id": L, "entity_id": "light.flood"},
    {"platform": "unifiprotect", "unique_id": L + "_light_motion", "entity_id": "select.flood_light_motion"},
    {"platform": "unifiprotect", "unique_id": L + "_duration", "entity_id": "number.flood_duration"},
    {"platform": "unifiprotect", "unique_id": L + "_sensitivity", "entity_id": "number.flood_sensitivity"},
    {"platform": "unifiprotect", "unique_id": L + "_status_light", "entity_id": "switch.flood_status_light"},
    {"platform": "unifiprotect", "unique_id": L + "_dark", "entity_id": "binary_sensor.flood_dark"},
    {"platform": "unifiprotect", "unique_id": S + "_door", "entity_id": "binary_sensor.pantry_door"},
    {"platform": "unifiprotect", "unique_id": S + "_leak", "entity_id": "binary_sensor.pantry_leak"},
    {"platform": "unifiprotect", "unique_id": S + "_battery_level", "entity_id": "sensor.pantry_battery"},
    {"platform": "unifiprotect", "unique_id": S + "_temperature_level", "entity_id": "sensor.pantry_temp"},
    {"platform": "unifiprotect", "unique_id": S + "_humidity_level", "entity_id": "sensor.pantry_hum"},
    {"platform": "unifiprotect", "unique_id": S + "_mount_type", "entity_id": "select.pantry_mount"},
    {"platform": "unifiprotect", "unique_id": S + "_clear_tamper", "entity_id": "button.pantry_clear_tamper"},
    {"platform": "unifiprotect", "unique_id": S + "_temperature", "entity_id": "switch.pantry_temperature"},
]
STATES = [
    {"entity_id": "light.flood", "state": "off"},
    {"entity_id": "select.flood_light_motion", "state": "Motion", "attributes": {"options": ["Manual", "Motion", "When Dark"]}},
    {"entity_id": "number.flood_duration", "state": "60", "attributes": {"min": 15, "max": 900, "unit_of_measurement": "s"}},
    {"entity_id": "number.flood_sensitivity", "state": "50", "attributes": {"min": 0, "max": 100}},
    {"entity_id": "switch.flood_status_light", "state": "on"},
    {"entity_id": "binary_sensor.flood_dark", "state": "off"},
    {"entity_id": "binary_sensor.pantry_door", "state": "on"},
    {"entity_id": "binary_sensor.pantry_leak", "state": "off"},
    {"entity_id": "sensor.pantry_battery", "state": "96"},
    {"entity_id": "sensor.pantry_temp", "state": "21.5", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "sensor.pantry_hum", "state": "unavailable"},
    {"entity_id": "select.pantry_mount", "state": "door", "attributes": {"options": ["door", "window", "garage", "none"]}},
    {"entity_id": "button.pantry_clear_tamper", "state": "2026-11-06T01:00:00+00:00"},
    {"entity_id": "switch.pantry_temperature", "state": "on"},
]
devs = [{"id": "l1", "name": "FloodLight - Sideyard", "type": "UP FloodLight", "state": "CONNECTED", "mac": L},
        {"id": "s1", "name": "Pantry Door", "type": "UP Sense", "state": "CONNECTED", "mac": S}]
v = {d["name"]: d for d in pc.devices_view(devs, ENTS, STATES)}
fl = v["FloodLight - Sideyard"]
ok([r["key"] for r in fl["controls"]] == ["status_light", "_main_light", "light_motion", "duration", "sensitivity"]
   and {r["key"]: r for r in fl["controls"]}["_main_light"]["domain"] == "light" and fl["readings"] == [{"key": "dark", "label": "Dark", "value": "No", "alert": False}],
   "1 · a floodlight joins its light (bare-MAC id), when it turns on, how long, sensitivity, status light; Dark is a reading")
pd = v["Pantry Door"]
rd = {r["label"]: r for r in pd["readings"]}
ok(rd["Door"]["value"] == "Open" and rd["Door"]["alert"] and rd["Leak"]["value"] == "Dry" and rd["Battery"]["value"] == "96%"
   and rd["Temperature"]["value"] == "21.5 °C" and rd["Humidity"]["value"] == "Not answering"
   and not any(r["key"] in ("door", "leak", "battery_level") for r in pd["controls"])
   and [r["key"] for r in pd["controls"]] == ["mount_type", "temperature", "clear_tamper"],
   "2 · readings in words (Open/Dry/96%/21.5 °C), an alert flagged, a silent one says so; readings never offered as controls")
E = {e["entity_id"]: e for e in ENTS}
S2 = {s["entity_id"]: s for s in STATES}
ok(pc.plan(E["light.flood"], S2["light.flood"], True) == ("light", "turn_on", {})
   and pc.plan(E["button.pantry_clear_tamper"], S2["button.pantry_clear_tamper"], True) == ("button", "press", {})
   and pc.plan(E["button.pantry_clear_tamper"], None, False)[0] is None and pc.plan(E["binary_sensor.pantry_door"], None, True)[0] is None
   and pc.landed("light", {"state": "on"}, True),
   "3 · the light on/off through the light service; a button only pressed; a reading can never be set")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "devices" and not sub2:')
route = src[i:src.index('if method == "POST" and sub == "cameras" and sub2 == "set":', i)]
j = src.index('if method == "POST" and sub == "cameras" and sub2 == "set":')
setr = src[j:src.index('if method == "GET" and sub == "detections" and sub2 == "thumb":', j)]
ok('kind not in ("lights", "sensors", "chimes")' in route and "_pc.devices_view(" in route and "call_service" not in route
   and 'before = (st or {}).get("state")' in setr and 'dom == "button" and (st2 or {}).get("state") not in (None, before' in setr,
   "4 · the devices route only reads; a button is confirmed only by a new press time")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
