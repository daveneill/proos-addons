"""The platform's integration log in ProOS's Master Log — word for word for the Developer, never the platform's name for
tech / installer — DRIVEN (register 1027).
Run: python3 tests/platform_log_bench.py

Dave, 27 Oct 10:41: report in ProOS's logs exactly what the platform's logs say from the integrations, before any rule.
11:16: "developer access gets everything word for word .. tech and installer modes logs need to then be filtered to never
show Home Assistant name it is to be replaced with ProOS". 11:18: "I believe you had benches set to make sure Home
Assistant did not break though and show by mistake" — whitelabel_bench checks the words ProOS WRITES; these rows are the
platform's own words arriving at run time, so this bench drives them through the real filter.

The fakes are shaped like Dave's box's own system log (register 1026).
 1. every platform message becomes one row: integration (from the platform's own source path), level, message, count
 2. TECH / INSTALLER: across EVERY field of every row, no spelling of the platform's name survives — "Home Assistant",
    "homeassistant", "home-assistant", "HASS", "hassio" — each reads "ProOS"; no logger or source path is served
 3. …and nothing else in the message is changed
 4. DEVELOPER: every word as the platform wrote it, with its logger and source
 5. the count is carried across a platform restart (the platform starts counting again; ProOS adds the runs)
 6. a row is attached to a room only by identity — an entity id the platform holds, or an integration's own host —
    and only when that names one room; a device NAME in the text attaches nothing
 7. the Master Log route merges these rows for both tiers, choosing by the Developer flag (wiring, SPELLING TEST)
"""
import json
import os
import re
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_plog_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import platformlog as PL                                # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


ROWS = [
    {"name": "homeassistant.components.websocket_api.commands", "level": "ERROR",
     "source": ["components/websocket_api/commands.py", 120],
     "message": ["Refusing to allow ProOS Panel Screen to subscribe to event homeassistant_stop"],
     "count": 638, "first_occurred": 1000.0, "timestamp": 2000.0},
    {"name": "homeassistant.components.mobile_app.notify", "level": "ERROR",
     "source": ["components/mobile_app/notify.py", 140], "message": ["Timeout sending notification to iPad"],
     "count": 170, "first_occurred": 1100.0, "timestamp": 2100.0},
    {"name": "homeassistant.components.lifx.coordinator", "level": "ERROR",
     "source": ["components/lifx/coordinator.py", 200],
     "message": ["Error fetching Dave's Lamp light.bedroom_daves_lamp data: update_failed"],
     "count": 3, "first_occurred": 1200.0, "timestamp": 2200.0},
    {"name": "homeassistant.components.hassio.handler", "level": "ERROR",
     "source": ["components/hassio/handler.py", 99],
     "message": ["Timeout on /store/reload request — Home Assistant Supervisor (HASS / hassio_store / home-assistant)"],
     "count": 5, "first_occurred": 1300.0, "timestamp": 2300.0},
    {"name": "aioesphomeapi.reconnect_logic", "level": "WARNING",
     "source": ["components/esphome/manager.py", 50],
     "message": ["Can't connect to ESPHome API for satellite1-cfeacc @ 192.168.8.77"],
     "count": 1, "first_occurred": 1400.0, "timestamp": 2400.0},
    {"name": "custom_components.samsungtv_smart.media_player", "level": "WARNING",
     "source": ["custom_components/samsungtv_smart/media_player.py", 10],
     "message": ["Updating Living Room TV took longer than the scheduled update interval"],
     "count": 40, "first_occurred": 1500.0, "timestamp": 2500.0},
]
ENT_AREA = {"light.bedroom_daves_lamp": "bedroom", "media_player.living_room_tv": "living_room"}
HOST_AREAS = {"192.168.8.77": {"office"}}

store = PL.ingest(ROWS, now=2600.0, ent_area=ENT_AREA, host_areas=HOST_AREAS)
check("1 · every platform message is one row", len(store), len(ROWS))
inst = PL.for_viewer(False)
dev = PL.for_viewer(True)
by = {e["data"]["message"][:30]: e for e in dev}
lifx = next(e for e in dev if e["data"]["integration"] == "lifx")
check("1 · the integration comes from the platform's own source path (lifx, esphome, samsungtv_smart)",
      sorted({e["data"]["integration"] for e in dev}) == sorted(["websocket_api", "mobile_app", "lifx", "hassio", "esphome", "samsungtv_smart"]))
check("1 · level and count as the platform gave them", (lifx["data"]["level"], lifx["data"]["count"]), ("ERROR", 3))

blob = json.dumps(inst)
check("2 · TECH/INSTALLER: no spelling of the platform's name anywhere in any row",
      re.search(r"home[\s_\-]?assistant|(?<![a-z])hass(?:io)?(?![a-z])", blob, re.I) is None)
check("2 · …each spelling reads ProOS (the panel's 'homeassistant_stop' and the hassio integration)",
      "ProOS_stop" in blob and any(e["data"]["integration"] == "ProOS" for e in inst))
check("2 · …no logger or source path is served to tech/installer",
      all("logger" not in e["data"] and "source" not in e["data"] for e in inst))
ip = next(e for e in inst if "Timeout sending notification" in e["data"]["message"])
check("3 · …and nothing else in a message is changed", ip["data"]["message"], "Timeout sending notification to iPad")
d_hass = next(e for e in dev if e["data"]["integration"] == "hassio")
check("4 · DEVELOPER: every word as the platform wrote it",
      d_hass["data"]["message"], "Timeout on /store/reload request — Home Assistant Supervisor (HASS / hassio_store / home-assistant)")
check("4 · …with its logger and source", (d_hass["data"]["logger"], d_hass["data"]["source"][0]),
      ("homeassistant.components.hassio.handler", "components/hassio/handler.py"))

# 5 · a platform restart: the platform's count starts again from a new first_occurred
r2 = [dict(ROWS[2], count=2, first_occurred=5000.0, timestamp=5100.0)]
PL.ingest(r2, now=5200.0, ent_area=ENT_AREA, host_areas=HOST_AREAS)
lifx2 = next(e for e in PL.for_viewer(True) if e["data"]["integration"] == "lifx")
check("5 · after a platform restart the total carries on (3 before + 2 since = 5)", lifx2["data"]["count"], 5)
PL.ingest([dict(ROWS[2], count=4, first_occurred=5000.0, timestamp=5300.0)], now=5400.0, ent_area=ENT_AREA, host_areas=HOST_AREAS)
lifx3 = next(e for e in PL.for_viewer(True) if e["data"]["integration"] == "lifx")
check("5 · …and within one run the platform's own count is followed (3 + 4 = 7)", lifx3["data"]["count"], 7)

# 6 · rooms by identity only
check("6 · an entity id the platform holds attaches its room (the lamp → bedroom)", lifx3["room"], "bedroom")
esp = next(e for e in dev if e["data"]["integration"] == "esphome")
check("6 · an integration's own host attaches its room (Pro Mic's address → office)", esp["room"], "office")
tv = next(e for e in dev if e["data"]["integration"] == "samsungtv_smart")
check("6 · a device NAME in the text attaches nothing (Living Room TV by name → whole system)", tv["room"], "site")
check("6 · two rooms named → whole system",
      PL.attach("light.bedroom_daves_lamp and media_player.living_room_tv", ENT_AREA, HOST_AREAS), None)

SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
check("7 · (wiring) the Master Log merges the rows and chooses by the Developer flag; the loop reads every minute (SPELLING TEST)",
      "_pl_.for_viewer(bool(u.get(\"is_owner\")))" in SRV and "_platform_log_read()" in SRV, True)

check("7 · (1027a) the page limit cuts ProOS's own events only — every integration row comes along (SPELLING TEST)",
      "_evs_ = _evs_[:lim]" not in SRV and "_journal_mod.merged(lim)" in SRV, True)   # re-pinned 1475: the same call, now passed through for_glass

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
