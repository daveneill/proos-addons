"""THE DIRECT PATH -- what a language model is not needed for.

Register 486, 17 Sep 2026. Dave: "this needs to be like Josh.ai on steroids,
speed is crucial."

JOSH IS FAST BECAUSE THE COMMON COMMANDS NEVER REACH A LANGUAGE MODEL. They are
matched against a grammar built from the home's OWN names and executed. ProOS
already holds every name that grammar needs -- the committed rooms, their area
ids, the scenes -- and was sending "kitchen off" on a round trip to a model
carrying a hundred and thirty tool schemas to find out that it meant the
kitchen, and off.

5 OCT 2026 (Dave's ruling: "replace it now"): the hand-typed grammar that stood here
is GONE. The sentence is recognised by the platform's own maintained sentence library
(resolve_platform, below); what remains of this module is ProOS's mapping from the
platform's intent to one of Assist's own tools, and the phrasebook.

This module does not control anything and it holds no
knowledge of how a room is turned off: it RESOLVES A SENTENCE TO ONE OF ASSIST'S
OWN TOOLS, and Core runs that tool through the same ToolRunner as always. So the
audit, the journal, the verification, the tier checks and the room choreography
are all exactly what they were -- there is no second implementation of anything,
which is the only way this could be safe to add.

THE LAW OF THIS MODULE IS CERTAINTY OR NOTHING.

A fast wrong answer is worse than a slow right one, in a product that turns
things off in people's houses. So:

  - the WHOLE utterance must be consumed. A sentence with anything left over
    that this module does not understand is not a match, it is a miss;
  - the room must be NAMED, or come from the room the person is standing in;
  - two actions in one sentence ("and", "then", a comma) are never matched;
  - a question is never matched;
  - a scene is matched only by its exact name, never by something near it;
  - and a miss is SILENT -- it returns None and the full Assist answers, exactly
    as it does today. Nothing is refused here, only declined.

WHAT IS DELIBERATELY NOT HERE, AND WHY:

  - MUSIC. "Play ABC in the study" needs music_search before music_play -- two
    hops, and the second depends on what the first found. That is a judgement,
    which is what Assist is for.
  - THE WHOLE HOUSE. "Everything off" is many rooms, and there is no one tool
    for it. Getting that wrong turns off someone's house.
  - ANYTHING WITH A QUESTION IN IT. Questions are the thing Assist is good at.
"""
from __future__ import annotations

import random
import re
import time

# Politeness and wake words. These do not change an instruction, so they come
# off the front before anything is matched -- otherwise "can you turn the
# office down" takes the slow road and "turn the office down" does not, which
# is the kind of inconsistency that makes a product feel broken.
_LEAD = (
    "hey proos", "ok proos", "okay proos", "proos",
    "could you please", "can you please", "would you please", "will you please",
    "i would like you to", "i'd like you to", "i want you to",
    "could you", "can you", "would you", "will you",
    "please", "just",
)
_TAIL = ("please", "thanks", "thank you", "for me", "now", "mate")
# Two instructions in one breath are a judgement, not a command.
_COMPOUND = (" and ", " then ", " after ", " but ", ";", ",")

def _norm(text: str) -> str:
    t = " ".join(str(text or "").lower().split())
    t = t.strip(" .!")
    changed = True
    while changed:
        changed = False
        for p in _LEAD:
            if t == p:
                return ""
            if t.startswith(p + " "):
                t = t[len(p) + 1:].strip()
                changed = True
        for p in _TAIL:
            if t.endswith(" " + p):
                t = t[: -len(p) - 1].strip()
                changed = True
    return t.strip(" .,!")




# ── THE PLATFORM'S OWN SENTENCE LIBRARY (Dave's ruling, 5 Oct 2026) ──────────
# "Replace it now." Register 495 promised it; 512 and 515 widened the hand-typed
# grammar instead. From today the SENTENCE is recognised by the platform's own
# maintained, local, instant recogniser — the same one its Assist uses, in every
# language it ships — asked through its debug command, which RECOGNISES WITHOUT
# EXECUTING. ProOS then maps the platform's intent onto ONE of Assist's own tools,
# so the room choreography, the audit and the verification stay exactly ProOS's.
# What only ProOS knows stays ProOS's: which rooms are commissioned, and what
# "the Kitchen off" means in a room with activities.
#
# DAVE'S RULING ON THE ROOM (5 Oct 2026): it acts at once only when the room is
# CERTAIN — named in the sentence, or the open room page the person is standing
# on. A guessed room is a miss, and Assist asks which room.

def platform_recognize(ws_call, text, language=None):
    """The platform's own recognition of one sentence, or None. Never executes."""
    msg = {"sentences": [str(text or "")]}
    if language:
        msg["language"] = language
    try:
        res = ws_call("conversation/agent/homeassistant/debug", **msg) or {}
    except Exception:                                            # noqa: BLE001
        return None
    rows = res.get("results") if isinstance(res, dict) else None
    return (rows or [None])[0]


def _slot(rec, key):
    d = ((rec or {}).get("details") or {}).get(key) or {}
    v = d.get("value")
    if v is None:
        v = ((rec or {}).get("slots") or {}).get(key)
    return v


def resolve_platform(rec, text, where=None, rooms=(), scenes=()):
    """The platform's recognition -> one of Assist's own tools, or None.

    rec    : one result of the platform's debug recogniser (platform_recognize)
    rooms  : (area_id, name[, aliases]) for COMMITTED rooms only
    where  : the room the person is in; used ONLY when where["confidence"] is
             "certain" (Dave's ruling, 5 Oct 2026)
    Same law as before: certainty or nothing; a miss returns None and the full
    Assist answers.
    """
    if not rec or not rec.get("match") or rec.get("fuzzy_match"):
        return None
    if rec.get("source") not in (None, "builtin"):
        return None                       # a custom sentence or trigger is not ours to map
    if "?" in str(text or ""):
        return None
    padded = " " + _norm(text) + " "
    if any(c in padded for c in _COMPOUND):
        return None
    intent = str(((rec.get("intent") or {}).get("name")) or "")
    targets = list((rec.get("targets") or {}).keys())

    # THE ROOM: named by the platform's area slot, or the certain room the person is in
    names = {}
    for row in (rooms or []):
        aid, nm = row[0], row[1]
        if aid and nm:
            names[str(nm).lower()] = (aid, nm)
            for al in (row[2] if len(row) > 2 and row[2] else ()):
                names[str(al).lower()] = (aid, nm)
    area_txt = _slot(rec, "area")
    # REGISTER 734: A ROOM'S NAME HEARD AS A DEVICE'S NAME. Read on Dave's box: the Family
    # Room's Sonos is itself called "Family Room", so the platform hears "turn off the family
    # room" as {name: Family Room} on that one speaker, not {area: Family Room}. When the
    # name the platform heard IS a room's name (and no room was named besides), the person
    # meant the room — ProOS's room, the same as the area slot.
    _nm = _slot(rec, "name")
    room_by_name = (not area_txt and _nm and not (rec.get("targets") and all(
        str(t).startswith("scene.") for t in rec.get("targets")))
        and names.get(str(_nm).lower()))
    if room_by_name:
        area_txt = _nm
    if area_txt:
        hit = names.get(str(area_txt).lower())
        if not hit:
            return None                   # a room ProOS has not commissioned: Assist decides
        area_id, area_name = hit
    elif where and where.get("area_id") and where.get("confidence") == "certain":
        area_id, area_name = where.get("area_id"), where.get("area_name") or "this room"
    else:
        area_id = area_name = None

    def out(tool, args, matched, scene=None):
        return {"tool": tool, "args": args, "matched": "platform:" + matched,
                "say": say_for(tool, args, area_name, scene,
                               brief=brief_for(args.get("area_id")) if args.get("area_id") else False)}

    name_slot = None if room_by_name else _slot(rec, "name")
    domain = _slot(rec, "domain")
    domains = set(domain if isinstance(domain, (list, tuple)) else ([domain] if domain else []))

    # A SCENE, by the platform's own target: every target is a scene the home has
    if intent == "HassTurnOn" and name_slot and targets and all(t.startswith("scene.") for t in targets):
        sc = dict((eid, nm) for nm, eid in (scenes() if callable(scenes) else (scenes or ())))
        if len(targets) == 1 and targets[0] in sc:
            return out("scene_apply", {"scene_entity_id": targets[0]}, "scene", sc[targets[0]])
        return None
    if name_slot:
        return None                       # a named device is a judgement: Assist's job
    if not area_id:
        return None                       # no certain room: Assist asks which (ruling 5 Oct)

    # REGISTER 735: "play <something> in <room>" — the platform recognised it; ProOS's
    # music_search_play does it in one step on the room's music speaker and proves it plays
    # (or hands it back to the platform's own search-and-play when the room has no engine).
    if intent == "HassMediaSearchAndPlay":
        q = _slot(rec, "search_query")
        if q and str(q).strip():
            return {"tool": "music_search_play", "args": {"area_id": area_id, "query": str(q).strip()},
                    "matched": "platform:music", "say": None}
        return None
    if intent in ("HassTurnOn", "HassTurnOff"):
        act = "turn_on" if intent == "HassTurnOn" else "turn_off"
        if domains == {"light"}:
            return out("area_control", {"area_id": area_id, "domain": "light", "action": act},
                       "lights_" + act[5:])
        if not domains and not _slot(rec, "device_class"):
            return out("room_on" if act == "turn_on" else "room_off", {"area_id": area_id},
                       "room_" + act[5:])
        return None
    if intent == "HassSetVolume":
        lvl = _slot(rec, "volume_level")
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            return None
        if 0 <= lvl <= 100:
            return out("room_volume", {"area_id": area_id, "action": "set", "level": lvl}, "volume_set")
        return None
    if intent == "HassSetVolumeRelative":
        step = _slot(rec, "volume_step")
        if step in ("up", "down"):
            return out("room_volume", {"area_id": area_id, "action": step}, "volume_" + step)
        return None
    simple = {"HassMediaPlayerMute": ("room_volume", "mute"),
              "HassMediaPlayerUnmute": ("room_volume", "unmute"),
              "HassMediaPause": ("room_media", "pause"),
              "HassMediaUnpause": ("room_media", "play"),
              "HassMediaNext": ("room_media", "next"),
              "HassMediaPrevious": ("room_media", "previous")}
    if intent in simple:
        tool, action = simple[intent]
        return out(tool, {"area_id": area_id, "action": action},
                   ("media_" if tool == "room_media" else "") + action)
    return None


# ── EVERYTHING ELSE THE PLATFORM UNDERSTOOD: ITS OWN AGENT DOES IT (register 731) ──
# Dave, 9 Oct 2026 (design, stage A): "everything the installer commissions in Pro
# automatically becomes an instant voice command, like Josh", and the law: never
# rebuild what the platform does. resolve_platform maps only the verbs ProOS has its
# own meaning for (a room on/off, a room's lights, volume, transport, a ProOS scene).
# EVERY OTHER sentence the platform's library recognised -- the blinds, the heating, a
# fan, a named lamp, "what's the temperature in the office", the weather -- was being
# handed to the cloud model, which took several rounds to do what the platform's own
# agent does in one step. From this build the platform's own agent does it, over
# exactly what register 730 told it about (rooms only, never security), and ProOS
# speaks its answer. Same law as the rest of this module: certainty or nothing.
#
# NOT HERE, deliberately:
#   - HassMediaSearchAndPlay: "play something relaxing" is A5's (the committed speaker
#     and a check that it is actually playing), not the platform's guess of a player;
#   - timers and broadcast: they act on the speaker you are talking TO, and a turn
#     typed or spoken in the app has no such speaker -- the platform would refuse;
#   - two instructions in one sentence, an unknown room, or one sentence that would
#     move several things with no room or name to say which.
PLATFORM_LATER = {"HassMediaSearchAndPlay"}   # ProOS's music_search_play (735); the agent only on its handoff
PLATFORM_NEEDS_A_SPEAKER = {"HassStartTimer", "HassCancelTimer", "HassCancelAllTimers",
                            "HassIncreaseTimer", "HassDecreaseTimer", "HassPauseTimer",
                            "HassUnpauseTimer", "HassTimerStatus", "HassBroadcast",
                            "HassNevermind", "HassRespond"}
# A READING moves nothing: the platform answers it from the live state, so a question
# that is one of these is safe to answer at once (the rest of a question is Assist's).
PLATFORM_READINGS = {"HassGetState", "HassGetWeather", "HassGetCurrentTime",
                     "HassGetCurrentDate", "HassClimateGetTemperature"}


def platform_can_take(rec, text, rooms=()):
    """The platform's intent name when its own agent may do this sentence now, else None.

    Called only after resolve_platform declined. Never acts; the caller hands the SAME
    sentence to the platform's own agent (conversation/process, agent
    conversation.home_assistant), which recognises it the same way and does it.
    """
    if not rec or not rec.get("match") or rec.get("fuzzy_match"):
        return None
    # REGISTER 733: A SENTENCE THE HOME WAS GIVEN. The platform reports a sentence trigger
    # as source "trigger" — an automation's own sentence (ProOS writes one per activity:
    # "watch apple tv in the bedroom"). It matched the WHOLE sentence as written, so it is
    # certain by construction, and the platform's agent runs it.
    if rec.get("source") == "trigger":
        return "trigger"
    if rec.get("source") not in (None, "builtin"):
        return None
    intent = str(((rec.get("intent") or {}).get("name")) or "")
    if not intent or intent in PLATFORM_LATER or intent in PLATFORM_NEEDS_A_SPEAKER:
        return None
    padded = " " + _norm(text) + " "
    if any(c in padded for c in _COMPOUND):
        return None
    if intent in PLATFORM_READINGS:
        return intent
    if "?" in str(text or ""):
        return None                       # a question that is not a reading: Assist's
    # REGISTER 732: A TV OR SPEAKER IS NEVER POWERED RAW. "Turn off the bedroom TV"
    # names a media player, and the platform's agent would switch that one box off --
    # leaving the Apple TV, the soundbar and the input where they were. A room's AV
    # power is choreographed by its activities (TV Off, Watch …), so it goes to Assist,
    # which runs the activity. (Register 733: the room's TV Off now has its own sentences,
    # "turn off the bedroom tv", which the platform checks before this one is ever reached.)
    if intent in ("HassTurnOn", "HassTurnOff"):
        _dom = _slot(rec, "domain")
        _doms = set(_dom if isinstance(_dom, (list, tuple)) else ([_dom] if _dom else []))
        if "media_player" in _doms or any(
                str(t).startswith("media_player.") for t in (rec.get("targets") or {})):
            return None
    area_txt = _slot(rec, "area")
    if area_txt:
        known = set()
        for row in (rooms or []):
            if row and row[1]:
                known.add(str(row[1]).lower())
                for al in (row[2] if len(row) > 2 and row[2] else ()):
                    known.add(str(al).lower())
        if str(area_txt).lower() not in known:
            return None                   # a room ProOS has not been given: Assist decides
        return intent
    targets = list((rec.get("targets") or {}).keys())
    if len(targets) == 1:
        return intent                     # one thing, by its name: certain
    return None                           # several things and no room said which: Assist asks


# ── ONE PHRASEBOOK, USED BY BOTH ROADS (register 488) ────────────────────────
# The direct path composes these for the commands it resolves. The MODEL takes
# the same five verbs when it reasons its way to them, and when it does, the
# home should say the same words -- a product that confirms "the Kitchen is
# off" one way and something else the other way is two products.
#
# It returns None for anything it does not have words for, which is most of
# what Assist can do. Silence here is not a failure; it means the model's own
# answer is the confirmation, as it always was.
#
# REGISTER 512 -- IT STOPS SAYING THE SAME WORDS EVERY TIME. Dave: "native HA is
# boring and no personality... short and varied... not closest to Josh, BETTER
# than Josh." The table below WAS one fixed sentence per verb, said identically
# forever, which is why the product sounded like a lookup table: it was one.
#
# TWO CHANGES, AND THE SECOND IS THE ONE JOSH CANNOT DO.
#   1. each verb now has a small set of full forms, and one is chosen per
#      utterance, so the home does not repeat itself word for word;
#   2. when the SAME ROOM was the last one spoken about, moments ago, the room
#      name is DROPPED -- "On.", "Louder.", "Lights off." A person standing in
#      their office does not need to be told which office. Josh names the room
#      every single time, and that is the difference you can hear.
#
# WHAT IS NOT HERE, DELIBERATELY: nothing that reads state. "Already on." and
# "One of the two didn't come on." are register 513's work and they need the
# room's live contents at the moment the sentence is composed. A confirmation
# said BEFORE the act -- which is what these are -- may only describe the call
# itself. That law is untouched by giving the call better words.
_SAY = {
    "room_off": ["%s, off.", "%s is off."],
    "room_on": ["%s, on.", "%s is on."],
}
_SAY_BRIEF = {"room_off": "Off.", "room_on": "On."}
_SAY_SCENE = ["%s.", "%s, on."]
_SAY_LIGHT = {"turn_on": ["%s lights, on.", "Lights on in the %s."],
              "turn_off": ["%s lights, off.", "Lights off in the %s."]}
_SAY_LIGHT_BRIEF = {"turn_on": "Lights on.", "turn_off": "Lights off."}
_SAY_VOL = {"up": ["%s, louder.", "Turned the %s up."],
            "down": ["%s, quieter.", "Turned the %s down."],
            "mute": ["%s, muted."], "unmute": ["%s, back on."]}
_SAY_VOL_BRIEF = {"up": "Louder.", "down": "Quieter.",
                  "mute": "Muted.", "unmute": "Back on."}
# The transport words were already right: short, and they never name a room,
# so there is nothing for repetition to wear out. Untouched.
_SAY_MEDIA = {"pause": "Paused.", "stop": "Stopped.", "play": "Playing.",
              "next": "Next.", "previous": "Back."}

# HOW LONG "STILL IN THE SAME ROOM" LASTS. Register 474's lesson -- a number
# typed against a behaviour cannot follow that behaviour -- so this number has
# ONE job and it is stated plainly: it is how long after speaking about a room
# a further command is treated as the same conversation rather than a new one.
# 30 seconds covers "turn it up" then "a bit more"; a command an hour later
# names the room again, which is right, because by then you may have moved.
BRIEF_WINDOW = 30.0
_LAST = {"area": None, "ts": 0.0}


def brief_for(area_id, now=None):
    """Was this same room spoken about moments ago? Then drop its name.

    DECIDES ONLY -- it remembers nothing. spoke_of() does the remembering, and
    the two are separate for a reason found while writing the bench: a sentence
    is COMPOSED before the act runs and may never be said at all (the tool
    fails, or the box cannot name the room). A single function that decided and
    recorded in one breath would have claimed rooms the home never mentioned,
    and the NEXT genuine sentence would have dropped a name it had not earned.

    Kept OUT of say_for so that say_for stays a pure function of its arguments
    (sentence.py's precedent: pure functions over a snapshot are the only kind
    a bench can hold still). This one owns the clock, and takes `now` so a
    bench can own it instead.
    """
    t = float(now if now is not None else time.time())
    # A DIFFERENT ROOM ALWAYS GETS ITS NAME. The whole point is that a person
    # is not told which office they are standing in; being told "On." about a
    # room you are not in is the opposite of that, and worse than a repetition.
    return bool(area_id and _LAST["area"] == area_id
                and (t - _LAST["ts"]) < BRIEF_WINDOW)


def spoke_of(area_id, now=None):
    """The home just SAID this room's name out loud. Called where the sentence
    is actually used, never where it is merely composed."""
    if area_id:
        _LAST["area"] = area_id
        _LAST["ts"] = float(now if now is not None else time.time())


def _pick(options):
    """One of the forms, chosen per utterance. Random rather than a rotation:
    a rotation is a pattern, and a pattern is what a person starts to hear."""
    return options[0] if len(options) == 1 else random.choice(options)


def say_for(tool, args, room_name=None, scene_name=None, brief=False):
    """What ProOS says when this tool has just succeeded, or None.

    `brief` drops the room's name -- see brief_for(), which decides it. It is
    passed IN rather than worked out here so that this stays a pure function:
    the same arguments give the same set of possible sentences, every time,
    which is what lets a bench pin every one of them.

    Deliberately narrow: it speaks only for acts whose outcome is completely
    described by the call itself. Anything whose result depends on what was
    FOUND -- music, health, recovery, anything with a reading in it -- has no
    entry here, because a confirmation that cannot be wrong is the only kind
    worth saying before the answer arrives.
    """
    args = args or {}
    # A ROOM THIS BOX CANNOT NAME GETS NO SENTENCE. An earlier draft fell back
    # to the word "room" and produced "The room is off." -- which is not
    # something a person can check, and checkable is the whole reason these
    # sentences are allowed to be said before the answer exists. The tools that
    # speak about a room need its name or they say nothing; the transport ones
    # ("Paused.") never name one and are unaffected.
    n = room_name
    if not n and tool in ("room_off", "room_on", "room_volume", "area_control"):
        return None
    if tool in _SAY:
        if brief:
            return _SAY_BRIEF[tool]
        return _pick(_SAY[tool]) % n
    if tool == "scene_apply":
        return (_pick(_SAY_SCENE) % scene_name) if scene_name else None
    if tool == "room_volume":
        act = str(args.get("action") or "")
        if act == "set":
            try:
                lvl = int(args.get("level"))
            except (TypeError, ValueError):
                return None
            return ("%d%%." % lvl) if brief else ("%s, %d%%." % (n, lvl))
        if act not in _SAY_VOL:
            return None
        return _SAY_VOL_BRIEF[act] if brief else (_pick(_SAY_VOL[act]) % n)
    if tool == "room_media":
        return _SAY_MEDIA.get(str(args.get("action") or ""))
    if tool == "area_control":
        if str(args.get("domain")) != "light":
            return None
        a = str(args.get("action") or "")
        if a not in _SAY_LIGHT:
            return None
        return _SAY_LIGHT_BRIEF[a] if brief else (_pick(_SAY_LIGHT[a]) % n)
    return None
