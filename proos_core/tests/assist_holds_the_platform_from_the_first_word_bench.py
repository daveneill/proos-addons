"""ASSIST HOLDS THE PLATFORM FROM THE FIRST WORD — AND NOTHING ELSE OF PROOS'S BUT THE GUARDED DOORS — register 1267/1267d.
Run: python3 tests/assist_holds_the_platform_from_the_first_word_bench.py

Dave, 9 Nov 2026, 13:04: "Assist was only ever meant to add whatever the platform didnt already support"; 13:45 "go".
1.0.1384 (1267) kept 46 ProOS tools and a short rule sheet: the same three requests went 149 s → 60 s. Then 14:29: "I need
it 100% back to platform then we can really find out what still needs to be added by assist as it's impossible when it's
still in control". So (1267d):

DRIVEN (Assist's real _active_tools, _system_doctrine, realtime.instructions; the platform's tools stood in):
 1. a person holding the platform's acts gets the platform's tools on the FIRST round — no "load more" door
 2. nothing of ProOS's own tools — not even the lock/alarm doors (1273, Dave 18:43 "no restrictions for anyone")
 3. the installer tier, a server that does not say which tools only read, and the unattended engine keep the old road
 4. the instruction sheet is bare: the house's name, the white label and Dave's own 16 Aug method (1278) — no yes (1273)
 5. Talk Live's voice preface loses the ProOS-activity sentence on the platform road
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_pf1st_")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import assist as A                                    # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


PLATFORM = [{"name": "ha_search", "description": "find", "input_schema": {}, "read_only": True},
            {"name": "ha_get_state", "description": "read", "input_schema": {}, "read_only": True},
            {"name": "ha_call_service", "description": "act", "input_schema": {}, "read_only": False},
            {"name": "ha_config_set_script", "description": "make a script", "input_schema": {}, "read_only": False}]
SILENT = [dict(t, read_only=False) for t in PLATFORM]


def first_round(user, mirror=PLATFORM):
    r = object.__new__(A.ToolRunner)
    r.user, r.ma, r.actions, r.trace, r.project = user, None, [], [], None
    r.home_domains = lambda: {"lock", "camera", "alarm_control_panel", "weather", "light", "media_player"}
    r._mcp_list = lambda: list(mirror)
    r.opened_tools = set()                       # the FIRST round: nothing asked for yet
    return [t["name"] for t in A._active_tools(r)]


DEV = {"id": "o", "is_owner": True, "is_admin": True}
HOME = {"id": "h"}
INSTALLER = {"id": "i", "is_admin": True}
DOORS = {"lock_control", "security_arm"}

for who, u in (("the Developer", DEV), ("a homeowner", HOME)):
    got = first_round(u)
    check("1 · %s holds the platform's tools on the first round, with no 'load more' door" % who,
          ({"ha_search", "ha_call_service", "ha_config_set_script"} <= set(got), "more_tools" in got), (True, False))
    # RE-PINNED 1273 (Dave 9 Nov 18:43: "I stated a few days ago that no restrictions for anyone until we know system is fully functional"): not even the doors
    # RE-PINNED 1279 (Dave 10 Nov 08:33/08:47: Assist "said it added it to my dashboard but didnt show"; "1. yes"): the first thing Assist adds back
    # RE-PINNED 1337 (Dave 13 Nov 18:56, the Family Room review: Assist rebuilt an activity from a guess): + the rooms and their activities
    check("2 · %s holds the platform's tools and, of ProOS's, only the Scenes page and the rooms' activities (1279, 1337)" % who,
          sorted(t for t in got if not t.startswith("ha_")), ["room_activity", "room_off", "rooms_overview", "scene_dashboard"])

inst = first_round(INSTALLER)
check("3 · the installer (the platform's reads only) keeps the old road — no platform acts, ProOS's tools still there",
      ("ha_call_service" in inst, A._platform_first(INSTALLER, PLATFORM), "room_activity" in inst), (False, False, True))
sil = first_round(DEV, SILENT)
check("3 · a server that does not say which of its tools only read: the old road (fail closed)",
      (A._platform_first(DEV, SILENT), "more_tools" in sil), (False, True))
check("3 · the unattended engine keeps its own road", A._platform_first(dict(DEV, engine=True), PLATFORM), False)

A.MIRROR_STATE = {"connected": True}
short = A._system_doctrine(DEV, "Home")
home_sheet = A._system_doctrine(HOME, "Home")
A.MIRROR_STATE = {}
long_ = A._system_doctrine(DEV, "Home")
check("4 · a platform holder's sheet is bare (under 600 characters); everyone else's is the full one, unchanged",
      (len(short) < 600, A._system_doctrine(INSTALLER, "Home") == A._system_doctrine_full(INSTALLER, "Home"),
       len(long_) > 5000), (True, True, True))
check("4 · (WORDING) it keeps the white label, and asks for no yes anywhere (1273)",
      ("Never say an entity id" in short, [w for w in ("yes", "lock_control", "security_arm") if w in short]), (True, []))
# RE-PINNED, register 1278, with cause (Dave 21:16 "Fine just put it back"): his own 16 Aug method is back, word for word —
# and it is the ONLY working line: no scene/script rule, no ProOS tool, no question rule.
check("4 · (WORDING) Dave's 16 Aug method is there word for word (1278)",
      "Build it the way you would by hand — READ, WRITE, RUN, READ BACK, FIX, RUN AGAIN — and a saved thing you have "
      "not run is NOT tested." in short, True)
check("4 · (WORDING) and nothing else of ProOS's — no scene/script rule, no tool, no question rule",
      [w for w in ("scene", "script", "music_search_play", "room_activity", "ONE short question") if w in short], [])
check("4 · a homeowner's sheet is the Developer's — no yes line (1273)", home_sheet == short, True)

from proos import realtime as RT                                  # noqa: E402
A.MIRROR_STATE = {"connected": True}
pf_voice = RT.instructions(A, DEV, "Home", {})
A.MIRROR_STATE = {}
old_voice = RT.instructions(A, INSTALLER, "Home", {})
check("5 · Talk Live on the platform road: no ProOS-activity sentence; the old road keeps it",
      (RT._ACTIVITY_LINE in pf_voice, RT._ACTIVITY_LINE in old_voice), (False, True))

# 6 · REGISTER 1272 — Dave 18:36, Talk Live on his phone: "Turn lights on." → "…if you say yes." Everybody-the-same was on;
# the tools held him as the developer but the instructions read his login raw. The words now see the person as the tools do.
_om = A.open_mode
A.MIRROR_STATE = {"connected": True}
A.open_mode = lambda *a, **k: True
typed_on, voice_on = A._system_prompt(HOME, "Home"), RT.instructions(A, HOME, "Home", {})
eng_on = A._system_prompt(dict(HOME, engine=True), "Home")
A.open_mode = lambda *a, **k: False
typed_off = A._system_prompt(HOME, "Home")
A.open_mode = _om
A.MIRROR_STATE = {}
check("6 · Everybody-the-same ON: a homeowner login is not told to wait for a yes — typed or Talk Live — nor called a homeowner",
      ("wait for their yes" in typed_on, "wait for their yes" in voice_on, "role: homeowner" in typed_on), (False, False, False))
check("6 · Everybody-the-same OFF: no yes line either, on the platform road (1273)", "wait for their yes" in typed_off, False)
check("6 · the unattended engine is never opened by it", A._as_assist_sees({"id": "proos-engine", "engine": True}).get("is_owner"), None)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
