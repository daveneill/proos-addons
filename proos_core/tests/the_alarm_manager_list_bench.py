"""PROTECT → ALARM MANAGER LIST — register 1230 (Dave, 6 Nov: "yes all of it please").
Run: python3 tests/the_alarm_manager_list_bench.py

DRIVEN: the REAL proos.protectalarms view over records shaped as Dave's console answered them (6 Nov readout:
name, enable, sources[{device,type}], conditions[{condition}], schedules, actions[{type,metadata,order}], cooldown,
status, usedInArmMode …).
  1. nothing secret leaves Core: no web address, user id, access key, user or token — whatever the records carry
  2. each alarm: name, on/off, its devices BY NAME (an id ProOS can't name is counted, never shown), its actions in words
  3. what it watches for is plain words only — an id or address inside a condition is dropped
  4. schedule (Always / N schedules), cooldown in seconds, last fired and how often; deleted alarms are left out
  5. the route reads only, through ProOS's own console user, and sends the view — never the raw records (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import protectalarms as pa                              # noqa: E402

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
BOOT = {"accessKey": "ACCESS-KEY-SECRET", "users": [{"id": "u1", "email": "dave@example.com"}], "authUserId": "u1",
        "cameras": [{"id": CAM, "name": "Front Door", "mac": "AA"}], "sensors": [{"id": SEN, "name": "Garage Door"}]}
AUTO = [
    {"id": "a1", "name": "Person at Front Door", "enable": True, "editable": True, "userId": "USER-ID-SECRET",
     "sources": [{"device": CAM, "type": "include"}, {"device": "ffffffffffffffffffffffff", "type": "include"}],
     "conditions": [{"condition": {"type": "is", "source": "person", "value": ["vehicle", "0123456789abcdef"]}}],
     "schedules": [], "cooldown": {"enable": True, "timeout": 60000},
     "actions": [{"type": "HTTP_REQUEST", "order": 2, "metadata": {"url": "http://192.168.8.117:8770/hook?token=TOKEN-SECRET"}},
                 {"type": "SEND_NOTIFICATION", "order": 1, "metadata": {"userIds": ["USER-ID-SECRET"]}}],
     "status": {"lastExecutedAt": 1762390000000, "total": 14}, "usedInArmMode": True},
    {"id": "a2", "name": "Garage Left Open", "enable": False, "sources": [{"device": SEN, "type": "include"}],
     "conditions": [{"condition": {"type": "is", "source": "sensorOpened"}}], "schedules": [{"x": 1}, {"x": 2}],
     "actions": [{"type": "SEND_NOTIFICATION", "order": 1, "metadata": {}}], "status": {}},
    {"id": "a3", "name": "Old Deleted", "enable": True, "deleted": True},
]
v = pa.alarms_view(AUTO, BOOT)
blob = json.dumps(v)
ok(not any(s in blob for s in ("ACCESS-KEY", "dave@example.com", "USER-ID", "TOKEN", "http", "192.168", "metadata", "userId",
                               "0123456789abcdef", "ffffffff")),
   "1 · no web address, user id, access key, user or token leaves Core")
by = {x["name"]: x for x in v}
p = by["Person at Front Door"]
ok(p["on"] and p["devices"] == ["Front Door"] and p["devices_unknown"] == 1 and p["actions"] == ["Notify", "Web Request"]
   and not by["Garage Left Open"]["on"] and by["Garage Left Open"]["devices"] == ["Garage Door"],
   "2 · on/off, devices by name (an unnamed id counted, not shown), actions in words in their order")
ok(p["watches"] == ["Person", "Vehicle"] and by["Garage Left Open"]["watches"] == ["Sensor Opened"],
   "3 · what it watches for is plain words; an id inside a condition is dropped")
ok(p["schedule"] == "Always" and by["Garage Left Open"]["schedule"] == "2 schedules" and p["cooldown_s"] == 60
   and p["fired_total"] == 14 and p["last_fired"] == 1762390000000 and p["used_in_arm_mode"]
   and "Old Deleted" not in by and [x["name"] for x in v] == ["Garage Left Open", "Person at Front Door"],
   "4 · schedule, cooldown, last fired and count; deleted alarms left out; sorted by name")
# 1230b — MEASURED on Dave's box: a faces/plates alarm's 12 sources matched no device id (he has 12 cameras). A source
# may name a device by its MAC, with or without colons, in either case — matched exactly, never by name.
BOOT2 = {"cameras": [{"id": CAM, "mac": "F4E2C6A1B2C3", "name": "Driveway"}, {"id": "65f0aa11bb22cc33dd44ee77", "mac": "f4:e2:c6:a1:b2:c4", "name": "Office"}]}
A2 = [{"id": "m1", "name": "Plates", "enable": True,
       "sources": [{"device": "f4:e2:c6:a1:b2:c3"}, {"device": "F4E2C6A1B2C4"}, {"device": "Driveway"}, {"device": "aabbccddeeff00112233"}]}]
v2 = pa.alarms_view(A2, BOOT2)[0]
ok(v2["devices"] == ["Driveway", "Office"] and v2["devices_unknown"] == 2 and pa.unnamed_shapes(A2, BOOT2) == [8, 20]
   and "aabbcc" not in json.dumps(pa.unnamed_shapes(A2, BOOT2)),
   "6 · a source naming its device by MAC (any spelling) is named; a name is never an id; unmatched ids report only their length")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "alarms":')
# (pin narrowed, register 1233: the Cameras routes — which do switch — now follow the alarm route; it still only reads)
route = src[i:src.index('if method == "GET" and sub == "cameras" and not sub2:', i)]
ok('_uc.get_json(_creds, ph, "/proxy/protect/api/automations")' in route and "_pa.alarms_view(" in route
   and "call_service" not in route and "POST" not in route and '"alarms": au' not in route and '"bootstrap"' not in route.split("_pa.alarms_view")[1],
   "5 · the route reads only, through ProOS's own console user, and sends the view, never the raw records")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
