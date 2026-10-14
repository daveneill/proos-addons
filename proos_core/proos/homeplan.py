"""THE HOME CATALOGUE ROAD — one AI call, one plan, held to what the home has — the Assist plan, build 6 (register 858;
14 Oct 2026). Rewritten: the first copy (739, 10 Oct) was paused the same morning and its only copy is gone.

WHERE IT SITS. After the instant road has declined (no platform sentence matched) and before the full assistant (the
step-by-step agent with its hundred tools). The room's CATALOGUE — what the home actually has, by id — goes to the AI once,
with the request, and the AI answers ONE plan from a CLOSED SET of acts. Every id in the plan must be in the catalogue or the
plan is refused; nothing is invented. The plan is done at once, said at once, and (build 4) the device's own state is
checked after. A plan the AI can't make ("assist") hands the turn to the full assistant exactly as before. A plan that
WORKED is saved as an instant sentence for the exact words said, so the second time costs ~150 ms and no AI (the
platform can't tell who spoke, so a learned sentence is the household's — stated in 739).

THE ACTS (closed set): scene <id> · activity <id> · app <id> · favourite <id> · lights on|off · covers open|close ·
volume <0-100>|up|down · pause · search_play <words> · ask <question> · assist. Several acts in one plan is how
"lights off and play jazz" is done (build 7's compound), in the catalogue's own order.
"""
from __future__ import annotations

import json
import re
import time

MAX_STEPS = 4
LEARN_AFTER = 1          # plumbing: a plan is saved as an instant sentence after this many successes (decision 3 of 9 Oct: one)
VOICE_LEARNED_ID = "proos_voice_learned"
EVENT = "proos_voice"
_SAID_IDS = ("proos_voice_activities", "proos_voice_apps", "proos_voice_favourites")


def _speakable(text):
    from proos import project as _p
    return _p._speakable(text)


def catalogue(client, project_mod, area_id, rooms, scenes, has_lights=None, has_covers=None):
    """What the home has for THIS request, by id: compact, the AI's whole world for the plan.
    rooms: (area_id, name) pairs; scenes: (name, entity_id); the rest read from the sentence automations ProOS already
    wrote (their 'said' tables are the catalogue of activities, apps and favourites — one record, not a second read)."""
    names = {a: n for a, n in rooms}
    room = names.get(area_id) if area_id else None
    cat = {"room": {"id": area_id, "name": room} if area_id else None,
           "rooms": [{"id": a, "name": n} for a, n in rooms],
           "scenes": [{"id": e, "name": n} for n, e in scenes],
           "activities": [], "apps": [], "favourites": []}
    for aid in _SAID_IDS:
        try:
            cfg = client._req("GET", "/api/config/automation/config/%s" % aid)
        except Exception:                                        # noqa: BLE001
            cfg = None
        said = ((cfg or {}).get("variables") or {}).get("said") or {}
        for tid, v in said.items():
            if not isinstance(v, dict):
                # activities: {trigger id (script) : words}
                cat["activities"].append({"id": tid, "room": _room_of_script(tid, names), "say": v})
                continue
            if area_id and v.get("area") and v.get("area") != area_id:
                continue
            if v.get("app"):
                cat["apps"].append({"id": tid, "room": v.get("area"), "app": v.get("app"), "device": v.get("device")})
            elif v.get("media_content_id"):
                cat["favourites"].append({"id": tid, "room": v.get("area"), "name": v.get("name")})
                cat.setdefault("_favs", {})[tid] = dict(v, kind="favourite")
    if area_id:
        cat["activities"] = [a for a in cat["activities"] if a.get("room") in (None, area_id)]
    cat["lights"] = bool(has_lights) if has_lights is not None else None
    cat["covers"] = bool(has_covers) if has_covers is not None else None
    return cat


def _room_of_script(script_id, names):
    m = re.match(r"^script\.proos_([a-z0-9_]+?)_(watch|listen|tv_off|off|on)", str(script_id))
    if not m:
        return None
    return m.group(1) if m.group(1) in names else None


def prompt(text, cat, earlier=None):
    """earlier: the last exchange ((what they said, what the home answered)) — a follow-up ("louder", "and the blinds")
    is read against it (register 859, build 7)."""
    shown = json.dumps({k: v for k, v in cat.items() if not k.startswith("_")}, ensure_ascii=False)
    lines = ["You run a home. Someone said: %r." % text]
    if earlier and earlier[0]:
        lines.append("Just before this they said %r and the home answered %r — a short follow-up refers to that."
                     % (earlier[0], earlier[1] or ""))
    lines += ["Here is EVERYTHING this home has for it, as JSON (ids are exact; never invent one):", shown,
              'Answer ONLY with JSON: {"do": [steps], "say": "<one short sentence, the room first>"}.',
              'Each step is one of: {"scene": id} {"activity": id} {"app": id} {"favourite": id} '
              '{"lights": "on"|"off"} {"covers": "open"|"close"} {"volume": 0-100|"up"|"down"} {"pause": true} '
              '{"search_play": "<words to search a music service for>"} {"ask": "<one question>"} {"assist": true}.',
              "Rules: at most %d steps, in the order to do them. Use \"search_play\" for music by mood, style, artist, song or "
              "playlist not in the favourites. Use \"ask\" when the room is unknown and it matters. Use \"assist\" when this "
              "needs a conversation, a reading, a check or a fix — not an act. If the request names a room other than the "
              'current one, every step gets {"room": id} too.' % MAX_STEPS]
    return "\n".join(lines)


def parse(answer, cat):
    """The plan, held to the catalogue; None when the answer isn't a plan or names anything the home hasn't got."""
    m = re.search(r"\{.*\}", str(answer or ""), re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:                                            # noqa: BLE001
        return None
    steps = d.get("do") if isinstance(d, dict) else None
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return None
    ids = {"scene": {s["id"] for s in cat.get("scenes") or []}, "activity": {a["id"] for a in cat.get("activities") or []},
           "app": {a["id"] for a in cat.get("apps") or []}, "favourite": {f["id"] for f in cat.get("favourites") or []}}
    rooms = {r["id"] for r in cat.get("rooms") or []}
    out = []
    for st in steps:
        if not isinstance(st, dict) or len([k for k in st if k != "room"]) != 1:
            return None
        room = st.get("room")
        if room is not None and room not in rooms:
            return None
        k = next(k for k in st if k != "room")
        v = st[k]
        if k in ids:
            if v not in ids[k]:
                return None
        elif k == "lights" and v not in ("on", "off"):
            return None
        elif k == "covers" and v not in ("open", "close"):
            return None
        elif k == "volume" and not (v in ("up", "down") or (isinstance(v, (int, float)) and 0 <= v <= 100)):
            return None
        elif k == "pause" and v is not True:
            return None
        elif k in ("search_play", "ask") and not (isinstance(v, str) and v.strip()):
            return None
        elif k == "assist" and v is not True:
            return None
        elif k not in ("scene", "activity", "app", "favourite", "lights", "covers", "volume", "pause", "search_play", "ask", "assist"):
            return None
        out.append({k: v} | ({"room": room} if room else {}))
    say = d.get("say") if isinstance(d.get("say"), str) else ""
    return {"do": out, "say": " ".join(say.split())[:160]}


def calls_for(plan, cat):
    """The plan as the exact tool calls it means — (tool, args) each, a favourite as ("favourite", its play data) — or
    ("assist"|"ask", …) where the plan stops. What a learned sentence replays, so no catalogue is needed later."""
    out, room0 = [], (cat.get("room") or {}).get("id")
    for st in plan["do"]:
        k = next(k for k in st if k != "room")
        v, room = st[k], st.get("room") or room0
        if k == "assist":
            out.append(("assist", None))
        elif k == "ask":
            out.append(("ask", v))
        elif k == "scene":
            out.append(("scene_apply", {"scene_entity_id": v}))
        elif k == "activity":
            out.append(("room_activity", {"script_entity_id": v}))
        elif k == "app":
            a = next((x for x in cat.get("apps") or [] if x["id"] == v), {})
            # register 863: the device rides along (the instant sentence's own pick) — a launch with no device came back
            # "which device?" and was counted as done (seen on the box 14 Oct 10:34)
            out.append(("app_launch", {"area_id": a.get("room") or room, "app": a.get("app")}
                        | ({"device": a["device"]} if a.get("device") else {})))
        elif k == "favourite":
            out.append(("favourite", (cat.get("_favs") or {}).get(v) or {}))
        elif k == "lights":
            out.append(("area_control", {"area_id": room, "domain": "light", "action": "turn_on" if v == "on" else "turn_off"}))
        elif k == "covers":
            out.append(("area_control", {"area_id": room, "domain": "cover", "action": "open_cover" if v == "open" else "close_cover"}))
        elif k == "volume":
            out.append(("room_volume", {"area_id": room, "action": "set", "level": int(v)} if v not in ("up", "down")
                        else {"area_id": room, "action": v}))
        elif k == "pause":
            out.append(("area_control", {"area_id": room, "domain": "media_player", "action": "media_pause"}))
        else:
            out.append(("music_search_play", {"area_id": room, "query": v}))
    return out


def run_calls(runner, calls, play_favourite):
    """Do the calls through the SAME tools the assistant uses (one mechanism). Returns (ok, results, needs)."""
    results = []
    for tool, args in calls:
        if tool == "assist":
            return False, results, "assist"
        if tool == "ask":
            return False, results, ("ask", args)
        try:
            r = play_favourite(args) if tool == "favourite" else runner.run(tool, args)
        except Exception as e:                                   # noqa: BLE001
            r = {"error": str(e)[:160]}
        results.append({"tool": tool, "args": args, "result": r})
        if not isinstance(r, dict) or r.get("error"):
            return False, results, None
        if not done(r):
            # register 863: only a result that SAYS done counts — "which device?", a hand-off or an empty answer is not
            r["error"] = r.get("error") or _not_done_words(tool, r)
            return False, results, None
    return True, results, None


def done(r):
    """A tool's answer that counts as done: ok, or a question asked, or a handoff the assistant will take up."""
    if not isinstance(r, dict):
        return False
    return bool(r.get("ok") or r.get("asked") or r.get("handoff") or r.get("launched") or r.get("applied") or r.get("done"))


def _not_done_words(tool, r):
    if r.get("needs_choice"):
        names = [o.get("name") or o.get("entity_id") for o in r.get("options") or []]
        return "%s is on %s — which one?" % (r.get("app") or "it", " and ".join(names) or "several devices")
    return "%s didn't say it was done" % tool.replace("_", " ")


def complete_step(tool, args):
    """A learned step that can be replayed as it is: an app launch names its device (register 863 — the dud learned on
    14 Oct was an app launch with no device, which asks "which?" and does nothing)."""
    if tool == "app_launch":
        return bool((args or {}).get("device"))
    return True


def prune(client):
    """Drop learned sentences whose steps can't be replayed (see complete_step). Returns what was done."""
    from proos import project as _p
    path = "/api/config/automation/config/%s" % VOICE_LEARNED_ID
    try:
        cur = client._req("GET", path)
    except Exception:                                            # noqa: BLE001
        return {"ok": False, "action": "unreachable"}
    if not isinstance(cur, dict):
        return {"ok": True, "action": "none"}
    stored = (cur.get("variables") or {}).get("proos_hash")
    if stored and stored != _p._voice_hash(cur):
        return {"ok": True, "action": "kept_edited"}
    kept, dropped = [], []
    for v in ((cur.get("variables") or {}).get("said") or {}).values():
        if not (isinstance(v, dict) and v.get("text")):
            continue
        steps = v.get("steps") or []
        if all(complete_step(st[0], st[1] if len(st) > 1 else {}) for st in steps if isinstance(st, (list, tuple)) and st):
            kept.append({"text": v["text"], "steps": steps, "say": v.get("say")})
        else:
            dropped.append(v["text"])
    if not dropped:
        return {"ok": True, "action": "unchanged", "kept": len(kept)}
    want = learned_config(kept)
    out = _p.voice_write(client, VOICE_LEARNED_ID, want)
    return dict(out, dropped=dropped, kept=len(kept))


def learned_config(entries):
    """entries: [{text, steps, say}] -> the automation of learned sentences, or None."""
    from proos import project as _p
    triggers, said = [], {}
    for n, e in enumerate(entries, 1):
        cmd = _speakable(e["text"])
        if not cmd:
            continue
        tid = "learned|%d" % n
        triggers.append({"trigger": "conversation", "command": [cmd], "id": tid})
        said[tid] = {"say": e["say"], "steps": e["steps"], "text": e["text"]}
    if not triggers:
        return None
    cfg = {"id": VOICE_LEARNED_ID, "alias": "ProOS · Voice · Learned",
           "description": "Sentences the home learned: a request the assistant worked out once, done instantly since. Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10, "triggers": triggers, "conditions": [],
           "actions": [{"event": EVENT, "event_data": {"kind": "plan", "steps": "{{ said[trigger.id].steps | to_json }}",
                                                       "say": "{{ said[trigger.id].say }}", "text": "{{ said[trigger.id].text }}"}},
                       {"set_conversation_response": "{{ said[trigger.id].say }}"}],
           "variables": {"said": said}}
    cfg["variables"]["proos_hash"] = _p._voice_hash(cfg)
    return cfg


def learn(client, text, steps, say):
    """Add one learned sentence — the exact calls that worked — replacing an older one for the same words."""
    from proos import project as _p
    path = "/api/config/automation/config/%s" % VOICE_LEARNED_ID
    try:
        cur = client._req("GET", path)
    except Exception:                                            # noqa: BLE001
        cur = None
    entries = []
    if isinstance(cur, dict):
        stored = (cur.get("variables") or {}).get("proos_hash")
        if stored and stored != _p._voice_hash(cur):
            return {"ok": True, "action": "kept_edited"}
        for v in ((cur.get("variables") or {}).get("said") or {}).values():
            if isinstance(v, dict) and v.get("text") and _speakable(v["text"]) != _speakable(text):
                entries.append({"text": v["text"], "steps": v.get("steps"), "say": v.get("say")})
    entries.append({"text": text, "steps": steps, "say": say})
    want = learned_config(entries[-200:])
    return _p.voice_write(client, VOICE_LEARNED_ID, want)
