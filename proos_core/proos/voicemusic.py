"""MUSIC BY NAME AS THE PLATFORM'S OWN SENTENCE — "play adele in the office" from ANY voice door (register 866; 14 Oct 2026).

SEEN ON THE BOX (14 Oct 12:19, run by Core's own test through the platform's assistant): "play adele in the office" →
"An unexpected error occurred" — the platform's built-in media intent asked the Sonos to search, which it cannot
("No results found for adele"). The dashboard's mic never saw this because Core's chat takes the sentence first; a
satellite, the platform's own app, or anything else speaking to the platform's assistant got the error.

THE PLATFORM FIRST. The platform's conversation trigger takes a wildcard sentence ("play {music}") and can WAIT for an
event before it answers (wait_for_trigger + set_conversation_response). So Core writes ONE automation, "ProOS · Voice ·
Music": the sentence fires proos_voice {kind: music, music, device_id, cid}; Core does the one music road
(assist.t_music_search_play — every service at once, the rank, the check after) and fires proos_voice_reply {cid, say};
the automation waits up to REPLY_WAIT_S for it and answers with Core's own words ("Office, Adele on Spotify.",
"nothing found for X on Spotify or Sonos Radio"). The room is read from the words ("… in the office") or, when the
words name none, from the device that heard it (the platform's device_id → its room); neither → "Which room?".

NOT TAKEN HERE: the names the apps and favourites automations already own (853, 855) — a condition keeps this sentence
out of their way, so "play netflix in the bedroom" and "play bec's top 100 in the office" stay instant on their own
roads and nothing fires twice. Several kinds fitting one name: this road plays the first in the rank (the dashboard's
chat asks, because it can hear the answer; a satellite's sentence cannot).
"""
from __future__ import annotations

import re

from proos import project as _project

VOICE_MUSIC_ID = "proos_voice_music"
EVENT = "proos_voice"
REPLY_EVENT = "proos_voice_reply"
REPLY_WAIT_S = 12            # plumbing: how long the platform waits for Core's own words before answering "on it"
VERBS = "(play|put on|listen to|play some|play me)"


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9+' ]", " ", str(s or "").lower().replace("’", "'")).split())


def taken_names(pairs: list) -> list:
    """The spoken forms other sentence automations own — pairs of (room name, thing name) from the apps and favourites
    automations — as this sentence's slot would hear them ("netflix in the bedroom", "bec's top 100 in the office"), so
    this sentence leaves them alone and nothing fires twice."""
    out = []
    for room, name in pairs or []:
        r = _project._speakable(room)
        for n in (_project._speakable(name), _norm(name)):
            if not n:
                continue
            forms = [n] if not r else [n + " in the " + r, n + " in " + r, n + " on the " + r + " tv", n + " on " + r + " tv"]
            for f in forms:
                if f not in out:
                    out.append(f)
    return out


def taken_from_automations(client) -> list:
    """The (room, name) pairs the apps (853) and favourites (855) automations hold, read from the platform."""
    pairs = []
    for aid, key in (("proos_voice_apps", "app"), ("proos_voice_favourites", "name")):
        try:
            cfg = client._req("GET", "/api/config/automation/config/%s" % aid) or {}
        except Exception:                                        # noqa: BLE001
            continue
        for v in ((cfg.get("variables") or {}).get("said") or {}).values():
            if isinstance(v, dict) and v.get(key):
                pairs.append((v.get("room") or "", v.get(key)))
    return pairs


def split_room(words: str, rooms: list):
    """("adele", "office") from "adele in the office" when a known room ends the words; (words, None) otherwise.
    rooms: [(area_id, name)]."""
    w = _norm(words)
    best = None
    for aid, name in rooms or []:
        r = _norm(name)
        if not r:
            continue
        for tail in (" in the " + r, " in " + r, " on the " + r + " speaker", " in my " + r):
            if w.endswith(tail):
                cand = (w[: -len(tail)].strip(), aid)
                if best is None or len(tail) > best[2]:
                    best = (cand[0], cand[1], len(tail))
    if best:
        return best[0], best[1]
    return w, None


def voice_music_config(taken: list):
    """The automation. `taken` — names the apps/favourites sentences own; the condition keeps this sentence off them."""
    sentences = [VERBS + " {music}"]
    cfg = {"id": VOICE_MUSIC_ID, "alias": "ProOS · Voice · Music",
           "description": "Written by ProOS Core: music by name, from any voice door, through ProOS's one search across the "
                          "speaker's services. Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10,
           "triggers": [{"trigger": "conversation", "command": sentences, "id": "music"}],
           "conditions": [{"condition": "template",
                           "value_template": "{{ (trigger.slots.music | lower | trim) not in taken }}"}],
           "actions": [
               {"variables": {"cid": "{{ (now().timestamp() * 1000000) | int }}"}},
               {"event": EVENT, "event_data": {"kind": "music", "music": "{{ trigger.slots.music }}",
                                               "device_id": "{{ trigger.device_id }}", "cid": "{{ cid }}"}},
               {"wait_for_trigger": [{"trigger": "event", "event_type": REPLY_EVENT, "event_data": {"cid": "{{ cid }}"}}],
                "timeout": {"seconds": REPLY_WAIT_S}, "continue_on_timeout": True},
               {"set_conversation_response": "{{ wait.trigger.event.data.say if wait.trigger else 'On it — I will tell you if it did not start.' }}"}],
           "variables": {"taken": list(taken or [])}}
    cfg["variables"]["proos_hash"] = _project._voice_hash(cfg)
    return cfg


def voice_music(client) -> dict:
    want = voice_music_config(taken_names(taken_from_automations(client)))
    return _project.voice_write(client, VOICE_MUSIC_ID, want)


def room_of_device(client, device_id: str):
    """The room of the device that heard the words (a satellite), from the platform's registry; None when unknown."""
    if not device_id or str(device_id) in ("None", "none", "null"):
        return None
    try:
        for d in client.device_registry() or []:
            if d.get("id") == device_id:
                return d.get("area_id") or None
    except Exception:                                            # noqa: BLE001
        return None
    return None


def reply(client, cid: str, say: str):
    """Core's own words back to the waiting automation."""
    try:
        client._req("POST", "/api/events/%s" % REPLY_EVENT, {"cid": str(cid or ""), "say": str(say or "")})
        return True
    except Exception:                                            # noqa: BLE001
        return False


def handle(client, runner, rooms: list, data: dict) -> dict:
    """The event's act: the room from the words or the device, then the assistant's own music road. Returns the road's
    answer with `say` set; the caller sends `say` back with reply()."""
    data = data or {}
    if data.get("kind") != "music":
        return {"ignored": True}
    music, area = split_room(data.get("music") or "", rooms)
    if not area:
        area = room_of_device(client, data.get("device_id"))
    if not music:
        return {"error": "nothing to play was named", "say": "What would you like to play?"}
    if not area:
        return {"error": "no room", "say": "Which room?"}
    try:
        res = runner.t_music_search_play({"area_id": area, "query": music}) or {}
    except Exception as e:                                       # noqa: BLE001
        res = {"error": str(e)[:160]}
    if res.get("asked") and res.get("options"):
        # a satellite can't take the answer: the first in the rank plays, said as what it is
        first = res["options"][0]
        try:
            res = runner._play_option(area, first) or {}
        except Exception as e:                                   # noqa: BLE001
            res = {"error": str(e)[:160]}
    if not res.get("say"):
        res["say"] = ("I couldn't do that — %s." % str(res.get("error") or "it couldn't be done").rstrip(".")) if not res.get("ok") else "Done."
    res["area"] = area
    res["music"] = music
    return res
