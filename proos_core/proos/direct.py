"""THE DIRECT PATH -- what a language model is not needed for.

Register 486, 17 Sep 2026. Dave: "this needs to be like Josh.ai on steroids,
speed is crucial."

JOSH IS FAST BECAUSE THE COMMON COMMANDS NEVER REACH A LANGUAGE MODEL. They are
matched against a grammar built from the home's OWN names and executed. ProOS
already holds every name that grammar needs -- the committed rooms, their area
ids, the scenes -- and was sending "kitchen off" on a round trip to a model
carrying a hundred and thirty tool schemas to find out that it meant the
kitchen, and off.

This module is that grammar. It does not control anything and it holds no
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
# Intensifiers on a step change. up is up: the tools take no magnitude, so
# these carry no meaning that could be lost by dropping them.
_DEGREE = ("a bit more", "a little more", "a bit", "a little", "a touch",
           "slightly", "some", "a lot", "right", "way", "a fraction")
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


# "on", "at" and "to" are NOT in this list, and that is the whole point of it
# being written down. They read like connectives and they are not: "on" is a
# VERB here. An earlier draft stripped a trailing "on" and silently disabled
# every on-command in the grammar -- "bedroom lights on" became "bedroom
# lights", which matches nothing, so it fell through to the model and looked
# for all the world like the direct path simply had not been reached.
_FILLER_LEAD = ("the", "in", "for", "of", "my", "our")
_FILLER_TAIL = ("the", "in", "for", "of", "my", "our", "it")


def _strip_filler(t: str) -> str:
    """Connective words left behind once a room name is lifted out."""
    words = t.split()
    while words and words[0] in _FILLER_LEAD:
        words.pop(0)
    while words and words[-1] in _FILLER_TAIL:
        words.pop()
    return " ".join(words)


def _drop_degree(t: str) -> str:
    for d in _DEGREE:
        if t.endswith(" " + d):
            return t[: -len(d) - 1].strip()
        if t == d:
            return ""
    return t


def _find_room(t: str, rooms):
    """The room named in the sentence, longest name first so 'family room'
    is never read as 'room'. Returns (area_id, name, remaining text) or None.

    A name must sit on WORD boundaries: a room called 'Den' must not be found
    inside 'garden'.
    """
    best = None
    for aid, name in rooms:
        n = " ".join(str(name or "").lower().split())
        if not n:
            continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t)
        if not m:
            continue
        if best is None or len(n) > len(best[1]):
            best = (aid, n, m.start(), m.end(), name)
    if not best:
        return None
    aid, n, a, b, disp = best
    head, tail = t[:a], t[b:]
    # The connective that introduced the room leaves with it: "lights off in
    # the kitchen" must not leave "lights off in the" behind.
    head = re.sub(r"\b(?:in|for|of|to|at|on)\s+(?:the\s+)?$", " ", head)
    head = re.sub(r"\bthe\s+$", " ", head)
    return aid, disp, " ".join((head + " " + tail).split())


# ── THE VERBS ────────────────────────────────────────────────────────────────
# Each entry is: the exact phrases that mean it, the tool, its arguments, and
# the sentence ProOS says. Nothing here is a prefix match or a "contains" --
# the residue after the room is lifted out must EQUAL one of these phrases.

_OFF = ("off", "turn off", "turn it off", "shut down", "shut off",
        "power off", "switch off", "turn everything off", "everything off",
        "all off")
_ON = ("on", "turn on", "turn it on", "power on", "switch on")

_LIGHTS_OFF = ("lights off", "lights out", "turn the lights off",
               "turn lights off", "light off", "kill the lights")
_LIGHTS_ON = ("lights on", "turn the lights on", "turn lights on", "light on")

_VOL_UP = ("volume up", "turn up", "turn it up", "up", "louder",
           "turn the volume up", "volume higher")
_VOL_DOWN = ("volume down", "turn down", "turn it down", "down", "quieter",
             "softer", "turn the volume down", "volume lower")
_MUTE = ("mute", "mute it", "silence", "shush")
_UNMUTE = ("unmute", "unmute it", "sound on")

_PAUSE = ("pause", "pause it", "hold")
_STOP = ("stop", "stop it")
_PLAY = ("play", "resume", "unpause", "keep playing", "play it", "continue")
_NEXT = ("next", "skip", "next track", "next song", "skip it", "skip this")
_PREV = ("previous", "back", "last track", "previous track", "go back",
         "previous song")

def _volume_level(t: str):
    """A volume the person named, 0-100, or None. A number outside that range
    is NOT clamped into range -- it is a miss, and Assist can ask."""
    m = re.match(r"^(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})\s*(?:%|percent)?$", t)
    if not m:
        m = re.match(r"^(\d{1,3})\s*(?:%|percent)$", t)
    if not m:
        return None
    n = int(m.group(1))
    return n if 0 <= n <= 100 else None


def resolve(text, where=None, rooms=(), scenes=()):
    """One sentence -> one of Assist's own tools, or None.

    rooms  : iterable of (area_id, name) -- COMMITTED rooms only
    scenes : iterable of (name, scene_entity_id), OR a callable returning one.
             A callable is only called if nothing else matched, because reading
             the home's scenes costs a full state read and "turn the kitchen
             off" must not pay for it.
    where  : the room the person is in, {"area_id":..., "area_name":...}

    Returns {"tool", "args", "say", "matched"} or None. `say` is what ProOS
    says when the tool SUCCEEDS; a tool that fails speaks for itself.
    """
    t = _norm(text)
    if not t:
        return None
    # A question is Assist's job, and two instructions are a judgement.
    if "?" in str(text or ""):
        return None
    padded = " " + t + " "
    if any(c in padded for c in _COMPOUND):
        return None

    rooms = [(a, n) for a, n in (rooms or []) if a and n]

    def _scenes():
        try:
            return scenes() if callable(scenes) else scenes
        except Exception:                                        # noqa: BLE001
            return ()

    # ── THE ROOM ─────────────────────────────────────────────────────────────
    found = _find_room(t, rooms)
    if found:
        area_id, area_name, rest = found
    elif where and where.get("area_id"):
        area_id, area_name, rest = (where.get("area_id"),
                                    where.get("area_name") or "this room", t)
    else:
        return _scene_hit(t, _scenes())
    if not area_id:
        return _scene_hit(t, _scenes())

    r = _drop_degree(_strip_filler(rest))
    if not r:
        return _scene_hit(t, _scenes())

    def out(tool, args, matched, scene=None):
        # ONE PHRASEBOOK (register 488). This used to carry its own sentences,
        # beside an identical set the model's road would need. Two copies of
        # the words a product says are two products.
        return {"tool": tool, "args": args, "matched": matched,
                "say": say_for(tool, args, area_name, scene,
                               brief=brief_for(area_id))}

    # ── LIGHTS BEFORE THE ROOM, because "lights off" contains "off" ─────────
    if r in _LIGHTS_OFF:
        return out("area_control",
                   {"area_id": area_id, "domain": "light", "action": "turn_off"},
                   "lights_off")
    if r in _LIGHTS_ON:
        return out("area_control",
                   {"area_id": area_id, "domain": "light", "action": "turn_on"},
                   "lights_on")

    # ── THE WHOLE ROOM ───────────────────────────────────────────────────────
    if r in _OFF:
        return out("room_off", {"area_id": area_id}, "room_off")
    if r in _ON:
        return out("room_on", {"area_id": area_id}, "room_on")

    # ── VOLUME ───────────────────────────────────────────────────────────────
    lvl = _volume_level(r)
    if lvl is not None:
        return out("room_volume",
                   {"area_id": area_id, "action": "set", "level": lvl}, "volume_set")
    if r in _VOL_UP:
        return out("room_volume", {"area_id": area_id, "action": "up"}, "volume_up")
    if r in _VOL_DOWN:
        return out("room_volume", {"area_id": area_id, "action": "down"}, "volume_down")
    if r in _MUTE:
        return out("room_volume", {"area_id": area_id, "action": "mute"}, "mute")
    if r in _UNMUTE:
        return out("room_volume", {"area_id": area_id, "action": "unmute"}, "unmute")

    # ── TRANSPORT ────────────────────────────────────────────────────────────
    for phrases, action in ((_PAUSE, "pause"), (_STOP, "stop"), (_PLAY, "play"),
                            (_NEXT, "next"), (_PREV, "previous")):
        if r in phrases:
            return out("room_media", {"area_id": area_id, "action": action},
                       "media_" + action)

    return _scene_hit(t, _scenes())


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


def _scene_hit(t, scenes):
    """A SCENE, BY ITS EXACT NAME -- the name itself, or the name behind a word
    that plainly means "run it". NEVER "movie time" for a scene called Movie:
    that is a guess, and this module does not guess.

    Tried LAST and its list fetched LAZILY, because reading the home's scenes
    costs a full state read and a room command must not pay for it.
    """
    for name, ent in (scenes or []):
        n = " ".join(str(name or "").lower().split())
        if not n or not ent:
            continue
        for lead in ("", "run ", "start ", "activate ", "apply ", "set "):
            if t == lead + n:
                args = {"scene_entity_id": ent}
                return {"tool": "scene_apply", "args": args, "matched": "scene",
                        "say": say_for("scene_apply", args, None, name)}
    return None
