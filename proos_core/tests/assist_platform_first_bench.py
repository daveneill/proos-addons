"""The platform is the MAIN surface; ProOS tools are the EXTRA.
Run: python3 tests/assist_platform_first_bench.py

DAVE, 18 Aug 2026, watching Assist fail a job he had already watched a
Developer session do by hand: "Create a work scene that just plays
Triple M 80s at low volume in the office." Assist called music_search
(the OPTIONAL engine's lane — not linked on his box), called
area_entities (which filters to light/cover/climate/fan/switch and so
returned NOTHING for a room whose devices are speakers), and answered:
"the office area exists but has no committed devices… music isn't
connected yet — you'll need to run Connect Music first."

Both statements came from OUR tools. The box itself said otherwise:

    media_player.office_office   media_title "Triple M 80s"
                                 media_content_id x-sonosapi-hls:…
    sensor.proos_activity_office members = [office, HomePod]

The platform's own ha_search("office", media_player) answers it in one
call. The model never got there because our tools answered first and
answered wrong.

TWO LAWS, DRIVEN HERE — neither is new, both are the SAME law already
in this file ("remove the last lock and the lock tools vanish"):
 1. A TOOL THAT CANNOT WORK IS NOT OFFERED. No music engine linked →
    the engine's tools do not exist this turn, and the doctrine tells
    the model the hand method instead of naming a tool it hasn't got.
 2. A TOOL THAT ONLY RE-ASKS THE PLATFORM STEPS ASIDE for a caller
    holding the platform's own tool that answers it. Every one of them
    is a READ, so the test is whether this caller holds the platform's
    READS. The Developer and the installer/tech do; a homeowner holds
    none of it and keeps every one. No tier name is written in the test
    — it follows what the caller is actually holding (register 229,
    completing Dave's staging: "100% for developer first, then a filter
    for installer and Homeowner").

KEPT ON PURPOSE (my audit filed these under 'retire' — reading them
proved the audit wrong, so they are pinned KEPT here): scenes_list
carries the companion record and the Scenes-page flag; device_powerlog
reads through ProOS's own power record; get_states carries the
missing-entity honesty shaper. (REGISTER 1267d: all of this is now the OLD road —
see the note under the imports.)
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_pf_")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import assist                                        # noqa: E402

# REGISTER 1267d (Dave 9 Nov 14:29 "I need it 100% back to platform"): a person holding the platform's acts now gets the
# platform's tools and ONLY ProOS's two guarded doors — pinned in assist_holds_the_platform_from_the_first_word_bench and
# at the end of this file. EVERY RULE BELOW IS THE OLD ROAD, which still exists — the installer tier, the unattended
# engine, a box whose platform tools are not connected — and is pinned here unchanged, on that road.
_REAL_PLATFORM_FIRST = assist._platform_first
assist._platform_first = lambda *a, **k: False

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


WHOLE = [
    {"name": "ha_search", "description": "find things",
     "input_schema": {}, "read_only": True, "destructive": False},
    {"name": "ha_get_state", "description": "read a state",
     "input_schema": {}, "read_only": True, "destructive": False},
    {"name": "ha_call_service", "description": "act",
     "input_schema": {}, "read_only": False, "destructive": True},
]


def runner_for(user, ma=None, mirror=True):
    r = object.__new__(assist.ToolRunner)
    r.user = user
    r.ma = ma
    r.actions, r.trace = [], []
    r.project = None
    r.home_domains = lambda: {"lock", "camera", "alarm_control_panel",
                              "weather", "light", "media_player"}
    r._mcp_list = (lambda: list(WHOLE)) if mirror else (lambda: [])
    return r


def offered(user, ma=None, mirror=True):
    r = runner_for(user, ma, mirror)
    # RE-PINNED WITH CAUSE, REGISTER 489. The tool list is handed over in TWO
    # TIERS now: the everyday surface always, the specialist sets (including
    # the platform mirror, the biggest block of all) one ask away via
    # more_tools. THE TIER RULES THIS BENCH EXISTS FOR ARE UNTOUCHED -- a tier
    # gate is about PERMISSION and deferring is about SPEED -- so every set is
    # opened before the question is asked, which is exactly the state the model
    # is in the moment it asks for them. That the sets are NOT carried up front
    # is pinned on its own, in assist_tools_two_tiers_bench.
    if hasattr(assist, "_TOOL_GROUPS"):
        r.opened_tools = set(assist._TOOL_GROUPS)
    return [t["name"] for t in assist._active_tools(r)]


DEV = {"id": "o", "is_owner": True, "is_admin": True}
INSTALLER = {"id": "i", "is_admin": True}
HOME = {"id": "h"}
MUSIC = ("music_search", "music_play", "music_playlist_create")
DUPES = ("weather", "locks_status", "cameras_status", "security_status",
         "area_entities")


class FakeEngine:
    """Stands in for a linked music engine."""


# 1 · a tool that cannot work is not offered
no_engine = offered(DEV, ma=None)
check("with NO music engine, the engine's tools are not offered at all",
      [m for m in MUSIC if m in no_engine], [])
with_engine = offered(DEV, ma=FakeEngine())
check("with an engine linked, they ARE offered",
      sorted(m for m in MUSIC if m in with_engine), sorted(MUSIC))

# 2 · the duplicates step aside for whoever holds the WHOLE platform
check("the Developer (whole toolset in hand) is not offered our "
      "re-asks of the platform",
      [d for d in DUPES if d in no_engine], [])
check("  …and DOES hold the platform's own tools",
      [t for t in ("ha_search", "ha_get_state", "ha_call_service")
       if t in no_engine],
      ["ha_search", "ha_get_state", "ha_call_service"])

# THE FILTER IS NOW BUILT (19 Aug 2026, register 229). Every tool in
# DUPES is a READ, and the census (register 223) measured what an
# installer actually holds on this box: 33 read-only platform tools.
# They hold the thing that answers these, so ours step aside for them
# too. The staging note this pin used to carry is spent.
inst = offered(INSTALLER)
check("an installer holding the platform's READS is not offered our "
      "re-asks of them either",
      [d for d in DUPES if d in inst], [])
check("  …and still holds the platform's reads to answer with",
      sorted(t for t in inst if t.startswith("ha_")),
      ["ha_get_state", "ha_search"])
home = offered(HOME, mirror=True)
# RE-PINNED, register 1256 (Dave 8 Nov 15:22: "Homeowners get everything … everything is by concent"; 18:04 "go"):
# a homeowner now holds the platform's tools, so ProOS's re-asks step aside for them exactly as for everyone else.
check("a homeowner, who now holds the platform's reads, is no longer handed ProOS's re-asks of them",
      [d for d in DUPES if d in home], [])
check("  …and a homeowner is offered the platform's tools",
      bool([t for t in home if t.startswith("ha_")]), True)

# A BOX WHOSE SERVER STATES NOTHING. The census (register 223) found
# Dave's server annotates well, but another site's may not: then no
# platform tool is KNOWN to be a read, and stepping our re-asks aside
# would leave the caller with nothing that answers them. Fail closed —
# we only stand down when we can SEE the replacement.
# (A mutation survived without this: "holds any mirror tool at all" and
# "holds the platform's READS" are indistinguishable on a well-annotated
# box, so the pin could not tell them apart. This is the box that does.)
SILENT = [dict(t, read_only=False, destructive=False) for t in WHOLE]


def offered_silent(user):
    r = runner_for(user)
    r._mcp_list = lambda: list(SILENT)
    # RE-PINNED WITH CAUSE, REGISTER 489. The tool list is handed over in TWO
    # TIERS now: the everyday surface always, the specialist sets (including
    # the platform mirror, the biggest block of all) one ask away via
    # more_tools. THE TIER RULES THIS BENCH EXISTS FOR ARE UNTOUCHED -- a tier
    # gate is about PERMISSION and deferring is about SPEED -- so every set is
    # opened before the question is asked, which is exactly the state the model
    # is in the moment it asks for them. That the sets are NOT carried up front
    # is pinned on its own, in assist_tools_two_tiers_bench.
    if hasattr(assist, "_TOOL_GROUPS"):
        r.opened_tools = set(assist._TOOL_GROUPS)
    return [t["name"] for t in assist._active_tools(r)]


sil = offered_silent(DEV)
check("a server that states no reads: our re-asks STAY (fail closed)",
      sorted(d for d in DUPES if d in sil), sorted(DUPES))
check("  …while the platform's tools are still offered",
      len([t for t in sil if t.startswith("ha_")]), 3)

# 3 · nothing that carries ProOS's own knowledge was swept up with them
# (1267 re-pinned this for the platform road; 1267d moved the platform road out of this file, so it is the original again)
check("scenes_list, device_powerlog and get_states are KEPT for the "
      "Developer — they carry ProOS's own record, not a re-ask",
      sorted(t for t in ("scenes_list", "device_powerlog", "get_states")
             if t in no_engine),
      ["device_powerlog", "get_states", "scenes_list"])
# A DECISION THAT CHANGED, RECORDED CHANGING (23 Aug, reg 265): on
# 18 Aug lock_control was pinned KEPT for the Developer as "the law".
# Ruling B's own test says otherwise: for a caller already holding the
# platform's acts, our copy is a second tool for one job — the law it
# carries binds the TIERS THAT KEEP IT, and an act-holder can reach the
# lock through the platform either way. Same for security_arm. The pin
# below moved them out of this list the day the rule changed.
check("  …as are the rooms and the law",
      all(t in no_engine for t in ("rooms_overview", "room_status",
                                   "scene_apply", "verify")), True)

# ── A DECISION OF HIS THAT CHANGED, AND IS RECORDED CHANGING ────────────
# On 18 Aug scene_create was KEPT for the Developer, on the reasoning that
# it carries ProOS's own record. On 19 Aug he watched Pro Assist fail a job
# that a plain Claude session holding the platform's MCP had done without
# trouble, and said: "what I asked for was to have Pro Assist working
# exactly the same as this chat in Claude."
#
# The reason the other session worked is that it had NO ProOS tools in the
# way. The model reached for OUR scene_create — familiar-sounding, and
# worse: the platform's own tool has optimistic locking and surgical edits
# ProOS has never had.
#
# So scene_create now steps aside for a caller holding the platform's acts.
# It is only SAFE to stand down because the one genuinely ProOS half of it —
# the music and activity companions — moved to scene_companions in the same
# build. If that tool ever disappears, this check fails and the stand-down
# must be reconsidered, not quietly kept.
check("the Developer is no longer handed TWO tools for making a scene",
      "scene_create" in no_engine, False)
# (as above: the original pin again, on the old road)
check("  …and what the platform cannot do is still in his hand",
      "scene_companions" in no_engine, True)
# ── WHAT THE CULL MUST NOT LOSE (register 247) ──────────────────────────
# Dave found this the same night 1.0.494 shipped. scene_create's answer used
# to end with "then ASK the user if they'd like it on their dashboard
# (scene_dashboard)". Standing that tool down took the sentence with it —
# so the platform made his Work scene, nobody attached the `dashboard_scene`
# label, and it could not reach the homeowner's Scenes page at all.
# Read off his box: scene.work, labels: [].
#
# THE PUBLISH IS A PROOS CONCEPT and now lives with the other ProOS-only
# halves. REPORTED, NEVER DECIDED — putting a client's scene on their screen
# is their call, so this names the tool and stops.
_A = open(os.path.join(os.path.dirname(__file__), "..", "proos", "assist.py"),
          encoding="utf-8").read()
_SC_BODY = _A[_A.find("def t_scene_companions"):]
_SC_BODY = _SC_BODY[:_SC_BODY.find("\n    def ", 10)]
check("attaching a moment also READS whether it is on the homeowner's page",
      '"dashboard_scene" in ((reg.get(seid) or {}).get("labels") or [])'
      in _SC_BODY, True)
check("  …and when it is NOT, it names the tool that publishes it",
      _SC_BODY.count("scene_dashboard"), 3)
check("  …on BOTH answers — the one that changed something and the one "
      "that did not", _SC_BODY.count('"on_scenes_page"'), 2)
check("  …but it never publishes on its own — that is the client's call",
      "scene_dashboard" in _SC_BODY and "_pub = True" not in _SC_BODY, True)
check("the publisher itself is still in the Developer's hands",
      "scene_dashboard" in no_engine, True)
check("  …and scenes_list still says which scenes are published",
      '"on_scenes_page"' in _A[_A.find("def t_scenes_list"):][:2000], True)
check("a registry we cannot read never blocks attaching a moment",
      "except Exception:                                    # noqa: BLE001\n            pass\n        note, changed, rec" in _A, True)

# THE INTERLOCK. scene_create may only stand down while the companions have
# somewhere else to live. If scene_companions is ever dropped, this build's
# safety argument evaporates and the stand-down must be reconsidered — so it
# is checked here rather than remembered.
_NAMES = {t["name"] for t in assist.TOOLS}
check("scene_companions exists, which is the ONLY reason scene_create may go",
      "scene_companions" in _NAMES
      and "scene_create" in assist._MIRROR_WRITE_DUPLICATES, True)
# A mutation survived the first spelling: it searched the STRINGIFIED tool
# for "music", and "remove_music" contains "music", so renaming the music
# property changed nothing the check could see. Read the schema's KEYS.
_SC = next(t for t in assist.TOOLS if t["name"] == "scene_companions")
_props = set((_SC.get("input_schema") or {}).get("properties") or {})
check("  …and it can actually attach BOTH halves of a moment",
      sorted(_props & {"music", "activity_script",
                       "remove_music", "remove_activity"}),
      ["activity_script", "music", "remove_activity", "remove_music"])
check("  …and it is told WHICH scene, or it could attach to nothing",
      "scene_entity_id" in _props
      and "scene_entity_id" in ((_SC.get("input_schema") or {}).get("required") or []),
      True)
# Nothing that carries a ProOS half was swept onto the write list.
for _k in ("scene_apply", "scene_delete", "scenes_list", "get_states",
           "rooms_overview", "room_status", "verify", "scene_companions"):
    check("  …%s was NOT swept onto the write list" % _k,
          _k in assist._MIRROR_WRITE_DUPLICATES, False)
# THE CASE THAT MATTERS MOST, and my first spelling of this check had the
# wrong premise: a "silent" server still hands the Developer acts, so our
# tools rightly stand down. The box to worry about is one with NO MCP
# SERVER AT ALL — there is nothing to stand aside FOR, and standing down
# would leave Assist unable to make a scene, control a device or write an
# automation. It must keep every one of them.
_none = offered(DEV, mirror=False)
check("a box with NO platform MCP keeps every ProOS write tool",
      sorted(t for t in assist._MIRROR_WRITE_DUPLICATES if t in _none),
      sorted(assist._MIRROR_WRITE_DUPLICATES))
check("  …and the read-side re-asks come back too",
      sorted(d for d in DUPES if d in _none), sorted(DUPES))

check("  …installer and tech, who hold no platform ACTS, keep theirs",
      all(t in offered(INSTALLER) for t in ("scene_create", "device_control",
                                       "area_control", "automation_create")),
      True)

# 4 · the doctrine matches what the model is actually holding.
# Built through a guard so a missing flag runs this bench RED instead of
# crashing it — and every check below carries a length assertion, so an
# empty string can never pass one by absence.
def doctrine(engine):
    try:
        return assist._system_doctrine(DEV, "Home", engine=engine)
    except TypeError:
        return ""


d_off, d_on = doctrine(False), doctrine(True)
check("the doctrine is built for both cases",
      (len(d_off) > 2000, len(d_on) > 2000), (True, True))
check("with no engine the doctrine never names a music tool the model "
      "hasn't got",
      [m for m in MUSIC if m in d_off] or (["EMPTY"] if not d_off else []),
      [])
check("  …and it carries the hand method instead (read · run · read back)",
      ("read the speaker" in d_off.lower()
       and "read the speaker back" in d_off.lower()), True)
check("  …and forbids blaming an optional add-on for music being "
      "impossible",
      "optional" in d_off.lower() and "isn't linked" in d_off.lower(), True)
check("with an engine the doctrine names its lane",
      all(m in d_on for m in MUSIC), True)
check("  …and STILL says the room's own speaker is read-write-run-read",
      "read the speaker" in d_on.lower(), True)

# 5 · the leftovers that pointed at a tool the Developer no longer holds
SC = next(t for t in assist.TOOLS if t["name"] == "scene_create")
check("scene_create's own text never orders area_entities",
      "area_entities" in str(SC), False)
src = open(os.path.join(HERE, "..", "proos", "assist.py"),
           encoding="utf-8").read()
_body = src.split("def t_scene_create", 1)[1].split("\n    def ", 1)[0]
_code = "\n".join(ln for ln in _body.split("\n")
                  if not ln.strip().startswith("#"))
check("  …nor does any message it can return",
      "area_entities" in _code, False)


# ── RULING B, ITEMS 1d AND 1a — THREE DECISIONS, THREE DATES (reg 265→266)
# 18 Aug: security_arm and lock_control KEPT for the Developer as "the
# law". 23 Aug morning (1d, reg 265): stood down as pure re-asks for an
# act-holder. 23 Aug afternoon (1a, reg 266): Dave's consent ruling made
# them the GUARDED DOORS — unlock and disarm now require the user's
# explicit yes, the mirror refuses those services for everyone, so these
# two are the ONLY way through and are back in every hand. Each reversal
# is a dated ruling, never drift. app_launch was read too and is NOT a
# re-ask — it carries the ProOS app catalogue and the register-109
# which-witness-to-believe fact; the platform's select_source loses both.
check("the guarded doors are in the act-holder's hand too — the mirror "
      "refuses the risky services, so these are the only way through",
      all(t in no_engine for t in ("security_arm", "lock_control")), True)
_inst = offered(INSTALLER)
check("  …and in the installer's",
      all(t in _inst for t in ("security_arm", "lock_control")), True)
_home = offered(HOME)
check("  …and the homeowner's",
      all(t in _home for t in ("security_arm", "lock_control")), True)

# The law still binds — CONSENT now, not "never" (driven fully in
# guarded_acts_bench; the door's own ask is pinned here too).
_hr = runner_for(HOME)
out = _hr.t_lock_control({"entity_id": "lock.front", "action": "unlock"})
check("unlock without the user's explicit yes ASKS and fires nothing",
      bool(out.get("needs_confirmation")))
out = runner_for(INSTALLER).t_security_arm({"entity_id":
                                            "alarm_control_panel.h",
                                            "mode": "disarm"})
check("disarm without the user's explicit yes ASKS and fires nothing",
      bool(out.get("needs_confirmation")))

# app_launch: read before ruling — it is knowledge, not a re-ask.
check("app_launch is NOT stood down for anyone — it carries the app "
      "catalogue and the witness fact",
      "app_launch" in no_engine and "app_launch" in _inst
      and "app_launch" in _home, True)
_al = src_al = open(os.path.join(HERE, "..", "proos", "assist.py"),
                    encoding="utf-8").read()
_al = _al[_al.find("def t_app_launch"):]
_al = _al[:_al.find("\n    def ", 10)]
check("  …the witness fact is really in its body (verify_by), so the "
      "keep is measured, not remembered",
      'res["verify_by"] = "sensor.proos_activity_' in _al, True)


# ── THE PLATFORM ROAD (register 1267d) ───────────────────────────────────
assist._platform_first = _REAL_PLATFORM_FIRST
pr = offered(DEV)
# RE-PINNED, register 1273, with cause (Dave 9 Nov 18:43: "I stated a few days ago that no restrictions for anyone until we know system is fully functional"): not even the guarded doors.
# RE-PINNED, register 1279, with cause (Dave 10 Nov 08:33/08:47: Assist "said it added it to my dashboard but didnt show"; "1. yes").
check("1279 · the Developer holding the platform: the platform's tools and, of ProOS's, only scene_dashboard and the rooms' activities (1337)",
      sorted(t for t in pr if not t.startswith("ha_")), ["room_activity", "room_off", "rooms_overview", "scene_dashboard"])   # re-pinned 1337: + the rooms' activities

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + str(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
