"""THE PLATFORM'S COMMANDS OBEY PROOS; TALK LIVE AND HOMEOWNERS HOLD EVERYTHING — register 1256.
Run: python3 tests/the_platforms_commands_obey_proos_bench.py

Dave, 8 Nov 2026: 15:22 "3. yes but platform tools first" (ProOS's rules checked on top of the platform's own commands);
"7. Homeowners get everything … everything is by concent … we might just change the terms assist will use when talking to
a homeowner"; 18:04 "go" (one build: the rules on top, Talk Live the same tools, homeowners everything). MEASURED on his
box that morning: loading the platform's set made ProOS's device control step aside, and the platform's service call then
skipped "hidden from Assist", the TV-via-activities rule, the media-power rule, the port guard and the read-back; Talk
Live was handed 44 tools and never the platform's or the fault-finding set; a homeowner held none of the platform's tools.

DRIVEN — the real ToolRunner (_run_mcp, run, _device_guard, _active_tools) and the real realtime.tools, over a Bedroom
built like Dave's (the bench rig of register 1143) and a stand-in platform tool server:
 1. the platform's call to a TV's own Power switch is REFUSED (activities only) — nothing sent
 2. …a media player's power is refused (choreographed per room); a device hidden from Assist is refused
 3. …a port that feeds other gear is refused by the port guard
 4. a lamp through the platform's call is SENT, and read back (ProOS's read-back on the platform's word)
 5. reloading an integration is not device control — not stopped by the device rules
 6. a bulk call by room is resolved to its devices and each one is checked; a whole floor is refused unread
 7. a homeowner is OFFERED the platform's tools and the professional's ProOS tools
 8. a homeowner's platform act WITHOUT their proven yes is refused; reading needs none; with the yes it is sent
 9. a homeowner's professional ProOS act (an automation) without their yes is refused
10. Talk Live opens with the fault-finding set and the platform's tools — and without the load-more door
11. (SOURCE) Talk Live's tool calls go to the service record and the heard words to history
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_platobey_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import roomdevices as rd, assist as A, project as PJ, realtime as RT  # noqa: E402

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


PROJECT = {"areas": {"bedroom": {"area_id": "bedroom", "committed": True, "display": "media_player.bedroom_tv",
                                 "sources": ["media_player.bedroom_apple_tv"], "speakers": [], "audio": [], "tvaudio": []}}}
ENTS = [
    {"entity_id": "media_player.bedroom_tv", "device_id": "dev_tv", "platform": "samsungtv_smart"},
    {"entity_id": "switch.bedroom_tv_power", "device_id": "dev_tv", "platform": "samsungtv_smart", "name": "Power"},
    {"entity_id": "media_player.bedroom_apple_tv", "device_id": "dev_atv", "platform": "apple_tv"},
    {"entity_id": "light.bedroom_becs_lamp", "device_id": "dev_l1", "platform": "lifx", "name": "Bec's Lamp"},
    {"entity_id": "light.bedroom_daves_lamp", "device_id": "dev_l2", "platform": "lifx", "name": "Dave's Lamp"},
    {"entity_id": "switch.av_rack_usw_48_port_4", "device_id": "dev_sw", "platform": "unifi", "name": "Port 4"},
]
DEVS = [{"id": "dev_tv", "name": "Bedroom TV", "area_id": "bedroom"}, {"id": "dev_atv", "name": "Bedroom Apple TV", "area_id": "bedroom"},
        {"id": "dev_l1", "name": "Bec's Lamp", "area_id": "bedroom"}, {"id": "dev_l2", "name": "Dave's Lamp", "area_id": "bedroom"},
        {"id": "dev_sw", "name": "AV-Rack", "area_id": "bedroom"}]
STATE = {e["entity_id"]: "off" for e in ENTS}


class Client:
    def entity_registry(self):
        return [dict(e) for e in ENTS]

    def device_registry(self):
        return [dict(d) for d in DEVS]

    def area_registry(self):
        return [{"area_id": "bedroom", "name": "Bedroom"}]

    def snapshot(self, ids):
        return {i: {"state": STATE.get(i, "off"), "attributes": {"friendly_name": i}} for i in ids}

    def state(self, eid):
        return {"state": STATE.get(eid, "off"), "attributes": {}}

    def _req(self, m, p, b=None):
        return {}


class Proj:
    @staticmethod
    def load():
        return PROJECT


SENT = []


class MCP:
    def call(self, name, args):
        SENT.append((name, args))
        if name == "ha_call_service" and str(args.get("service")) == "turn_on":
            for e in str(args.get("entity_id") or "").split(","):
                if e:
                    STATE[e.strip()] = "on"
        return {"success": True}


MIRROR = [{"name": "ha_call_service", "description": "x", "input_schema": {"type": "object"}, "read_only": False, "destructive": False},
          {"name": "ha_bulk_control", "description": "x", "input_schema": {"type": "object"}, "read_only": False, "destructive": False},
          {"name": "ha_get_logs", "description": "x", "input_schema": {"type": "object"}, "read_only": True}]


def runner(user):
    r = A.ToolRunner(Client(), None, Proj(), user, mcp=None)
    r.mcp = MCP()
    r._mcp_list = lambda: MIRROR
    r.awareness = {"port_guard": lambda eid: "it feeds AV-Rack's uplink" if eid == "switch.av_rack_usw_48_port_4" else None}
    r.consent_ok = False
    return r


DAVE = {"id": "dave", "name": "Dave", "is_owner": True, "tech": True}
HOME = {"id": "bec", "name": "Bec"}
CALL = next(t for t in MIRROR if t["name"] == "ha_call_service")
BULK = next(t for t in MIRROR if t["name"] == "ha_bulk_control")
_pjload, _hidden = PJ.load, rd.assist_hidden
PJ.load = lambda *a, **k: PROJECT
rd.assist_hidden = lambda client: {"light.bedroom_daves_lamp"}
try:
    r = runner(DAVE)
    SENT[:] = []
    o = r._run_mcp(CALL, {"domain": "switch", "service": "turn_on", "entity_id": "switch.bedroom_tv_power"})
    ok("activities" in str(o.get("error")) and not SENT, "1 · the platform's call to the TV's own Power switch is refused (%s), nothing sent" % o.get("error"))
    o = r._run_mcp(CALL, {"domain": "media_player", "service": "turn_off", "entity_id": "media_player.bedroom_tv"})
    o2 = r._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_daves_lamp"})
    ok("choreographed" in str(o.get("error")) and "hidden from Assist" in str(o2.get("error")) and not SENT,
       "2 · a media player's power is refused; a device hidden from Assist is refused")
    o = r._run_mcp(CALL, {"domain": "switch", "service": "turn_off", "entity_id": "switch.av_rack_usw_48_port_4"})
    ok("uplink" in str(o.get("error")) and not SENT, "3 · a port that feeds other gear is refused by the port guard (%s)" % o.get("error"))
    o = r._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_becs_lamp"})
    rb = (o.get("proos_read_back") or {}).get("light.bedroom_becs_lamp") or {}
    ok(SENT and SENT[-1][0] == "ha_call_service" and o.get("confirmed") is True and rb.get("confirmed") is True,
       "4 · a lamp through the platform's call is sent and READ BACK (confirmed %s)" % o.get("confirmed"))
    SENT[:] = []
    o = r._run_mcp(CALL, {"domain": "homeassistant", "service": "reload_config_entry", "entity_id": "media_player.bedroom_tv"})
    ok(not o.get("error") and SENT, "5 · reloading an integration is not device control — sent")
    SENT[:] = []
    o = r._run_mcp(BULK, {"selector": {"domain": "switch", "area_ids": ["bedroom"]}, "action": "off"})
    o2 = r._run_mcp(BULK, {"selector": {"domain": "light", "floor_ids": ["ground"]}, "action": "off"})
    ok(o.get("entity_id") in ("switch.bedroom_tv_power", "switch.av_rack_usw_48_port_4") and o2.get("error") and not SENT,
       "6 · a bulk call by room is checked device by device (refused at %s); a whole floor is refused unread" % o.get("entity_id"))

    h = runner(HOME)
    h.opened_tools = {"platform", "diagnostics"}
    have = {t["name"] for t in A._active_tools(h)}
    # RE-PINNED, register 1267d, with cause (Dave 9 Nov 14:29 "I need it 100% back to platform"): a homeowner holding the
    # platform is offered the platform's tools and, of ProOS's, only the two guarded doors — nothing else in control.
    ok({"ha_call_service", "ha_get_logs"} <= have
       and not [n for n in have if not n.startswith("ha_") and n not in ("scene_dashboard", "rooms_overview", "room_activity", "room_off")],  # 1273; 1279; 1337 the rooms' activities
       "7 · a homeowner is offered the platform's tools and only ProOS's guarded doors (1267d)")
    SENT[:] = []
    o = h._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_becs_lamp"})
    rd_ = h._run_mcp(MIRROR[2], {})
    ok(o.get("error") == A._CONSENT_UNPROVEN and not [s for s in SENT if s[0] == "ha_call_service"] and not rd_.get("error"),
       "8 · a homeowner's platform act without their proven yes is refused; reading needs none")
    h.consent_ok = True
    o = h._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_becs_lamp"})
    ok(not o.get("error") and any(s[0] == "ha_call_service" for s in SENT), "8 · …with their yes it is sent")
    h.consent_ok = False
    o = h.run("automation_create", {"alias": "x", "trigger": [], "action": []})
    ok(o.get("error") == A._CONSENT_UNPROVEN, "9 · a homeowner's automation without their yes is refused")

    lv = runner(DAVE)
    lt = {t["name"] for t in RT.tools(A, lv)}
    # RE-PINNED, register 1267d, with cause (as 7): Talk Live opens with the platform's tools and the guarded doors only.
    ok({"ha_call_service", "ha_get_logs"} <= lt
       and not [n for n in lt if not n.startswith("ha_") and n not in ("web_search", "scene_dashboard", "rooms_overview", "room_activity", "room_off")],  # 1273; 1279; 1337
       "10 · Talk Live opens with the platform's tools and the guarded doors, without the load-more door (%d tools, 1267d)" % len(lt))
    # 12 · REGISTER 1267d — with the platform's tools connected, ProOS's ROOM rules are not laid over the platform's
    # commands, but its SAFETY checks still are.
    A.MIRROR_STATE = {"connected": True}
    try:
        p = runner(DAVE)
        SENT[:] = []
        o = p._run_mcp(CALL, {"domain": "media_player", "service": "turn_off", "entity_id": "media_player.bedroom_tv"})
        ok(not o.get("error") and SENT, "12 · platform road: a TV's own power goes through the platform's call (%s)" % o.get("error"))
        SENT[:] = []
        # RE-PINNED, register 1285, with cause (Dave 10 Nov 12:24, Talk Live: "The TV's not on" → "hidden from Assist in Pro";
        # 12:24 "you said assist was disabled it was only the platform"). On the platform road a TV's own power switch and a
        # device marked hidden go through as the platform would send them; a port feeding other gear and a restart are
        # still refused.
        o1 = p._run_mcp(CALL, {"domain": "switch", "service": "turn_on", "entity_id": "switch.bedroom_tv_power"})
        o3 = p._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_daves_lamp"})
        ok(not o1.get("error") and not o3.get("error") and len(SENT) == 2,
           "12 · …a TV's own power switch and a device marked hidden go through on the platform road (1285)")
        SENT[:] = []
        o2 = p._run_mcp(CALL, {"domain": "switch", "service": "turn_off", "entity_id": "switch.av_rack_usw_48_port_4"})
        o4 = p._run_mcp(CALL, {"domain": "homeassistant", "service": "restart"})
        ok("uplink" in str(o2.get("error")) and o4.get("error") and not SENT,
           "12 · …while a port feeding other gear and a restart are still refused")
        # RE-PINNED, register 1273, with cause (Dave 9 Nov 18:43: "I stated a few days ago that no restrictions for anyone until we know system is fully functional"): on the platform road an unlock goes through the platform's own call.
        o5 = p._run_mcp(CALL, {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})
        ok(not o5.get("error") and SENT and SENT[-1][0] == "ha_call_service", "12 · …and an unlock goes through the platform's own call, no yes (1273)")
        SENT[:] = []
        hp = runner(HOME)
        o6 = hp._run_mcp(CALL, {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom_becs_lamp"})
        ok(not o6.get("error") and SENT, "12 · …and a homeowner's lights go on when asked, no yes (1273; the old road's yes is check 8)")
    finally:
        A.MIRROR_STATE = {}
finally:
    PJ.load, rd.assist_hidden = _pjload, _hidden

src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
ok('_assist_journal_turn(uinfo, {"trace": list(getattr(rnr, "trace", None) or [])})' in src
   and '_assist._hist_append(uinfo, "talk-live", _heard,' in src,
   "11 · (SOURCE) Talk Live's tool calls go to the service record and the heard words to history")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
