"""WHAT THE DASHBOARD'S MOTION FOLLOWS — register 1365 (15 Nov 2026). Run: python3 core/tests/motion_readings_bench.py

DRIVEN (proos/motion.py):
  1. an activity's expectations (the room proof's own, activitytest.expectations) become ONE row per device, in the order
     the activity first touches it, each with every reading it must show; the room's verdict check is not a device row;
     a device with no name says what it is from its id
  2. a scene's stored set-up becomes the devices and the plain state each is set to; a stored value the page cannot read
     back (a number, a colour list) is left out and counted, never guessed
SOURCE:
  3. the two routes are reads only (no service call), take the activity's script / the scene from the query, and answer
     with a reason rather than an error page when they cannot read
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "..")
sys.path.insert(0, CORE)
from proos import motion as M  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


exp = [{"entity_id": "switch.frame_art_mode", "what": "the TV is out of Art Mode", "state_in": ["off"]},
       {"entity_id": "media_player.frame", "what": "the TV is on", "state_in": ["on"]},
       {"entity_id": "media_player.atv", "what": "the source is awake", "not_in": ["off", "standby"]},
       {"entity_id": "media_player.frame", "what": "input HDMI 2", "attr": "source", "equals": "HDMI 2"},
       {"entity_id": "sensor.proos_activity_family_room", "what": "ProOS shows the room watching it",
        "verdict": "watch_source", "source": "media_player.atv"}]
rows = M.activity_rows(exp, {"media_player.frame": "Family Room TV", "media_player.atv": "Apple TV"})
check("1 · one row per device, in the order the activity first touches it",
      [r["entity_id"] for r in rows], ["switch.frame_art_mode", "media_player.frame", "media_player.atv"])
check("  …the TV's row carries both of its readings (on, and the input)",
      rows[1]["checks"], [{"what": "the TV is on", "state_in": ["on"]},
                          {"what": "input HDMI 2", "attr": "source", "equals": "HDMI 2"}])
check("  …each device by its own name", [r["name"] for r in rows][1:], ["Family Room TV", "Apple TV"])
check("  …a device with no name says what it is from its id", rows[0]["name"], "Frame Art Mode")
check("  …the room's own verdict is not a device row", any(r["entity_id"].startswith("sensor.") for r in rows), False)
check("  …nothing given, nothing shown", M.activity_rows(None), [])

cfg = {"name": "Good Night", "entities": {"light.kitchen": {"state": "off"}, "light.lamp": {"state": "on", "brightness": 40},
                                          "cover.blinds": {"state": "closed"}, "input_number.x": {"state": 4},
                                          "lock.front": "locked"}}
out = M.scene_rows(cfg, {"light.lamp": "Bedside Lamp"})
check("2 · each device and the plain state the scene sets it to",
      [(e["entity_id"], e["state"]) for e in out["entities"]],
      [("light.kitchen", "off"), ("light.lamp", "on"), ("cover.blinds", "closed"), ("lock.front", "locked")])
check("  …a level is not the test (the platform can round it) — the lamp is checked as on", out["entities"][1]["state"], "on")
check("  …a stored value the page cannot read back is left out and counted", out["not_read"], 1)
check("  …the scene's own name", out["name"], "Good Night")

SRC = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
a = SRC[SRC.index('if len(parts) == 4 and parts[0] == "rooms" and parts[2] == "activities" and parts[3] == "expect":'):][:2600]
b = SRC[SRC.index('if parts == ["scenes", "expect"]:'):][:1400]
check("3 · (SOURCE) the activity route reads the proof's own expectations for the room's display",
      "_at_x.expectations(_client, cfg, disp)" in a and "_mx.activity_rows(exp, names)" in a)
check("  …(SOURCE) the scene route reads the platform's own scene config with Core's token",
      '"/api/config/scene/config/%s" % sid' in b and "_mx.scene_rows(cfg)" in b)
check("  …(SOURCE) both are reads — no service call in either",
      "call_service" not in a and "call_service" not in b and '"POST"' not in a and '"POST"' not in b)
check("  …(SOURCE) a read that fails answers with its reason, not an error page",
      '"why": str(e)' in a and '"why": str(e)' in b)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
