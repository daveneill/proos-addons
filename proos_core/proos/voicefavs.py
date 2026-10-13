"""FAVOURITES AND THE SPEAKER'S OWN PLAYLISTS AS INSTANT SENTENCES — "play bec's top 100 in the office" — the Assist plan,
build 3 (register 855; 14 Oct 2026).

THE PLATFORM FIRST. The platform already lists every speaker's favourites and a Sonos's own playlists through its media
browser, and plays any of them with its own play_media (the dashboard's Favourites tabs, 831, run on exactly that). What the
platform can't do is know their NAMES as sentences. So, like the apps (853), Core writes ONE automation, "ProOS · Voice ·
Favourites", a trigger per (room, favourite), from what the browser lists for each speaker in the room's record; its action
fires proos_voice {kind: favourite, area, entity_id, media_content_id, media_content_type, name} and sets the reply at once
("Office, Bec's Top 100."); Core hears the event and asks the platform to play it — the same play the Favourites tab makes.
Nothing of ProOS's own: no search, no account, no service road; the speaker plays what it already holds.

NAMES. A favourite's name is spoken as the service wrote it, template characters out ("Chill House 2026 - Summer Vibes 2026 |
Chill Hits" also as its short form "chill house 2026", the part before " - ", " | " or " ("). The bare "[the] <room> <name>"
form is NOT written for favourites — it would collide with an app of the same name ("the bedroom apple tv", 853).
"""
from __future__ import annotations

import re

from proos import project as _project

VOICE_FAVS_ID = "proos_voice_favourites"
EVENT = "proos_voice"
_SPLIT = re.compile(r"\s+[-|–—]\s+|\s*\(")
FOLDER_KINDS = ("favorites_folder",)
SONOS_PLAYLISTS = ("playlist", "SQ:")
MAX_PER_SPEAKER = 200       # plumbing: how many favourites one speaker contributes (the platform's list can be long)


def speakable_name(name: str) -> list:
    """The spoken forms of a favourite's name: the whole name, and its short form when the name has a tail."""
    full = _project._speakable(str(name or "").replace("’", "'"))      # a curly apostrophe is spoken as a plain one
    full = re.sub(r"[^\w\s'+&.-]", " ", full)
    full = " ".join(full.split())
    if not full:
        return []
    out = [full]
    head = _SPLIT.split(str(name or "").replace("’", "'"), 1)[0]
    short = " ".join(re.sub(r"[^\w\s'+&.-]", " ", _project._speakable(head)).split())
    if short and short != full and len(short) >= 3:
        out.append(short)
    return out


def fav_sentences(room: str, name: str) -> list:
    r = _project._speakable(room)
    if not r:
        return []
    out = []
    for n in speakable_name(name):
        out += ["(play|put on) %s in [the] %s" % (n, r), "(play|put on) %s on [the] %s speaker[s]" % (n, r)]
    return out


def fav_said(room: str, name: str) -> str:
    return "%s, %s." % (str(room or "").strip(), str(name or "").strip())


def _browse(ws, entity_id, mtype=None, mid=None):
    f = {"entity_id": entity_id}
    if mtype is not None:
        f.update(media_content_type=mtype, media_content_id=mid or "")
    try:
        return ws("media_player/browse_media", **f) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def read_favourites(ws, entity_id: str, platform: str | None = None) -> list:
    """What the platform lists for this speaker: its favourites (one level of folders opened) and, on a Sonos, its own
    playlists. Each: {name, media_content_id, media_content_type}. Only what can be played."""
    out, seen = [], set()

    def take(c, mtype=None, mid=None, name=None):
        nm = name or c.get("title")
        cid = mid or c.get("media_content_id")
        ct = mtype or c.get("media_content_type")
        if not nm or not cid or c.get("can_play") is False:
            return
        key = (ct, cid)
        if key in seen or len(out) >= MAX_PER_SPEAKER:
            return
        seen.add(key)
        out.append({"name": str(nm), "media_content_id": str(cid), "media_content_type": str(ct)})

    root = _browse(ws, entity_id, "favorites", "")
    kids = list(root.get("children") or [])
    if not kids:
        top = _browse(ws, entity_id)
        fav = next((c for c in (top.get("children") or [])
                    if re.search(r"favou?rite|preset", str(c.get("title", "")) + " " + str(c.get("media_content_type", "")), re.I)), None)
        if fav:
            kids = list(_browse(ws, entity_id, fav.get("media_content_type"), fav.get("media_content_id")).get("children") or [])
    for c in kids:
        if c.get("can_expand") and c.get("can_play") is False:
            for x in (_browse(ws, entity_id, c.get("media_content_type"), c.get("media_content_id")).get("children") or []):
                take(x)
        else:
            take(c)
    if platform == "sonos":
        # the speaker's own playlists: the platform plays one BY ITS NAME with type "playlist" (831)
        for c in (_browse(ws, entity_id, *SONOS_PLAYLISTS).get("children") or []):
            take(c, mtype="playlist", mid=c.get("title"), name=c.get("title"))
    return out


def voice_favs_config(rooms: list):
    """rooms: [(area_id, room name, [(entity_id, favourites)])] -> the automation, or None."""
    triggers, said, seen = [], {}, set()
    for area, room, speakers in rooms:
        n = 0
        for eid, favs in speakers or []:
            for f in favs or []:
                cmds = [s for s in fav_sentences(room, f["name"]) if s not in seen]
                if not cmds:
                    continue
                seen.update(cmds)
                n += 1
                tid = "%s|%d" % (area, n)
                triggers.append({"trigger": "conversation", "command": cmds, "id": tid})
                said[tid] = {"say": fav_said(room, f["name"]), "area": area, "entity_id": eid, "name": f["name"],
                             "media_content_id": f["media_content_id"], "media_content_type": f["media_content_type"], "room": room}
    if not triggers:
        return None
    cfg = {"id": VOICE_FAVS_ID, "alias": "ProOS · Voice · Favourites",
           "description": "Written by ProOS Core from each room speaker's own favourites and playlists. Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10,
           "triggers": triggers, "conditions": [],
           "actions": [{"event": EVENT,
                        "event_data": {"kind": "favourite", "area": "{{ said[trigger.id].area }}", "entity_id": "{{ said[trigger.id].entity_id }}",
                                       "media_content_id": "{{ said[trigger.id].media_content_id }}",
                                       "media_content_type": "{{ said[trigger.id].media_content_type }}",
                                       "name": "{{ said[trigger.id].name }}", "room": "{{ said[trigger.id].room }}"}},
                       {"set_conversation_response": "{{ said[trigger.id].say }}"}],
           "variables": {"said": said}}
    cfg["variables"]["proos_hash"] = _project._voice_hash(cfg)
    return cfg


def voice_favs(client, ws, project: dict, platform_of=None) -> dict:
    names = {}
    try:
        names = {a.get("area_id"): a.get("name") for a in (client.area_registry() or [])}
    except Exception:                                            # noqa: BLE001
        pass
    rooms = []
    for key, rec in sorted(((project or {}).get("areas") or {}).items()):
        if not rec:
            continue
        speakers = [e for e in (rec.get("audio") or rec.get("speakers") or []) if str(e).startswith("media_player.")]
        if not speakers:
            continue
        area = rec.get("area_id") or key
        room = names.get(area) or rec.get("name") or key
        rows = []
        for eid in speakers:
            plat = platform_of(eid) if platform_of else None
            rows.append((eid, read_favourites(ws, eid, plat)))
        rooms.append((area, room, rows))
    want = voice_favs_config(rooms)
    out = _project.voice_write(client, VOICE_FAVS_ID, want)
    if out.get("ok") and want:
        out["favourites"] = len(want["triggers"])
    return out


def handle(client, data: dict) -> dict:
    """The event's act: the platform's own play of what it listed."""
    data = data or {}
    if data.get("kind") != "favourite":
        return {"ignored": True}
    eid, cid, ct = str(data.get("entity_id") or ""), str(data.get("media_content_id") or ""), str(data.get("media_content_type") or "")
    if not (eid and cid and ct):
        return {"error": "the event named no speaker or item"}
    try:
        client.call_service("media_player", "play_media", eid, {"media_content_id": cid, "media_content_type": ct})
        return {"ok": True, "played": data.get("name"), "entity_id": eid}
    except Exception as e:                                       # noqa: BLE001
        return {"error": str(e)[:200]}
