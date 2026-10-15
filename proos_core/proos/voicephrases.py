"""TRIGGER PHRASES FOR SCENES AND ACTIVITIES — Josh's three ways (stage 4 of the planned build, 16 Oct 2026, register 894).

Josh: a scene has several trigger phrases ("movie time", "movie night"); it can be built in the portal, made by the
assistant, or an existing control-system scene can be given phrases. On the platform:

  - THE PHRASE IS THE PLATFORM'S OWN ALIAS on the scene or script (the entity registry's `aliases`, the field the
    platform's own Assist reads). Pro and Assist write it there; ProOS keeps no copy (the mirror ruling). So the
    platform's built-in "activate <phrase>" / "turn on <phrase>" already works.
  - WHAT ONLY PROOS ADDS: the phrase said ON ITS OWN ("movie time" — the platform's library has no sentence for a bare
    name), answered in a few words, and for a scene the ProOS companions (its music, its activity) — which a plain
    scene.turn_on would leave behind. So ONE automation, ProOS's own (id proos_voice_phrases), written by the same
    writer as every other sentence automation (project.voice_write: differences only, an installer's edit kept, nothing
    to say removes it). A scene's phrase fires the proos_voice event and Core applies the scene through the ONE apply
    path (assist.apply_scene, the same as a dashboard tap); a script's phrase runs the script, exactly as a tap does.

Only phrases the installer or the household chose become bare sentences — never a scene's NAME on its own (a scene
called "Kitchen" must not answer every "kitchen"); the name keeps working through the platform's own verbs.
A phrase two things share is refused in Pro and Assist; if one reaches here anyway the first keeps it and the second is
reported, never silently doubled.
"""
from proos import project as _project

VOICE_PHRASES_ID = "proos_voice_phrases"
EVENT = "proos_voice"
_VERBS = "(turn on|activate|start|run|play|set)"


def phrase_sentences(phrase: str) -> list:
    p = _project._speakable(phrase)
    if not p:
        return []
    return [p, "[please] %s [the] %s [scene]" % (_VERBS, p)]


def phrase_said(name: str) -> str:
    """A few words, the room first: a ProOS activity is named "ProOS · Living Room · TV Off" → "Living Room, TV Off."
    (the same shape every activity sentence answers in); a scene says its own name."""
    parts = [p.strip() for p in str(name or "").split("\u00b7") if p.strip()]
    if parts and parts[0].lower() == "proos":
        parts = parts[1:]
    if len(parts) >= 2:
        return "%s, %s." % (parts[0], " ".join(parts[1:]))
    n = " ".join(" ".join(parts).split())
    return ("%s." % n) if n else "Done."


def collect(client) -> list:
    """[(entity_id, name, [phrases])] for every scene and script that has phrases (aliases) on the platform."""
    try:
        ents = client.entity_registry() or []
    except Exception:                                            # noqa: BLE001
        return []
    names = {}
    try:
        for s in client._req("GET", "/api/states") or []:
            names[s.get("entity_id")] = (s.get("attributes") or {}).get("friendly_name")
    except Exception:                                            # noqa: BLE001
        pass
    out = []
    for e in ents:
        eid = e.get("entity_id") or ""
        if eid.split(".")[0] not in ("scene", "script") or e.get("disabled_by"):
            continue
        al = [a for a in (e.get("aliases") or []) if isinstance(a, str) and a.strip()]
        if not al:
            continue
        out.append((eid, e.get("name") or names.get(eid) or e.get("original_name") or eid, sorted(al, key=str.lower)))
    return sorted(out)


def voice_phrases_config(items: list):
    """items: [(entity_id, name, [phrases])]. None when nothing has a phrase."""
    triggers, said, seen, clashes = [], {}, {}, []
    for eid, name, phrases in items or []:
        cmds = []
        for ph in phrases or []:
            key = _project._speakable(ph)
            if not key:
                continue
            if key in seen and seen[key] != eid:
                clashes.append({"phrase": key, "kept": seen[key], "skipped": eid})
                continue
            seen[key] = eid
            cmds += [s for s in phrase_sentences(ph) if s not in cmds]
        if not cmds:
            continue
        triggers.append({"trigger": "conversation", "command": cmds, "id": eid})
        said[eid] = {"say": phrase_said(name), "name": name}
    if not triggers:
        return None
    cfg = {"id": VOICE_PHRASES_ID, "alias": "ProOS · Voice · Phrases",
           "description": "Written by ProOS Core from the phrases given to scenes and activities (their aliases). "
                          "Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10,
           "triggers": triggers, "conditions": [],
           "actions": [{"choose": [{"conditions": [{"condition": "template",
                                                    "value_template": "{{ trigger.id.startswith('scene.') }}"}],
                                    "sequence": [{"event": EVENT,
                                                  "event_data": {"kind": "scene", "entity_id": "{{ trigger.id }}",
                                                                 "name": "{{ said[trigger.id].name }}",
                                                                 "device_id": "{{ trigger.device_id }}"}}]}],
                        "default": [{"action": "script.turn_on", "target": {"entity_id": "{{ trigger.id }}"}}]},
                       {"set_conversation_response": "{{ said[trigger.id].say }}"}],
           "variables": {"said": said}}
    cfg["variables"]["proos_hash"] = _project._voice_hash(cfg)
    if clashes:
        cfg["_clashes"] = clashes
    return cfg


def voice_phrases(client) -> dict:
    """Write every scene's and activity's phrases into the platform (the rules above)."""
    want = voice_phrases_config(collect(client))
    clashes = (want or {}).pop("_clashes", []) if want else []
    out = _project.voice_write(client, VOICE_PHRASES_ID, want)
    if want and out.get("ok"):
        out["phrases"] = {t["id"]: len(t["command"]) // 2 for t in want["triggers"]}
    if clashes:
        out["clashes"] = clashes
    return out


def clash(client, phrase: str, entity_id: str):
    """Who else already answers to this phrase — another scene/script's phrase, a room's name, or a device's name or
    alias. None when it is free. Josh refuses duplicates of room, device and scene phrases; so does ProOS."""
    key = _project._speakable(phrase)
    if not key:
        return "a phrase needs words"
    try:
        for a in client.area_registry() or []:
            if _project._speakable(a.get("name")) == key or key in [_project._speakable(x) for x in (a.get("aliases") or [])]:
                return "that is the %s room's name — saying it would act on the whole room" % a.get("name")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        for e in client.entity_registry() or []:
            if e.get("entity_id") == entity_id:
                continue
            if key in [_project._speakable(x) for x in (e.get("aliases") or [])] or \
                    (e.get("name") and _project._speakable(e.get("name")) == key):
                return "%s already answers to \"%s\"" % (e.get("name") or e.get("entity_id"), key)
    except Exception:                                            # noqa: BLE001
        pass
    return None


def set_phrases(client, ws_call, entity_id=None, phrases=None):
    """Set one scene's/script's phrases (its platform aliases), then rewrite the phrases automation; with no entity,
    only rewrite. Every NEW phrase is checked (clash) first. Returns (http status, body)."""
    clean = None
    if entity_id:
        eid = str(entity_id).strip()
        if eid.split(".")[0] not in ("scene", "script"):
            return 400, {"error": "phrases are for scenes and activities (scene.* or script.*)"}
        if not isinstance(phrases, list):
            return 400, {"error": "phrases must be a list"}
        try:
            cur = next((e for e in (client.entity_registry() or []) if e.get("entity_id") == eid), None) or {}
        except Exception:                                        # noqa: BLE001
            cur = {}
        if not cur:
            return 404, {"error": "%s is not on this box" % eid}
        have = {_project._speakable(a) for a in (cur.get("aliases") or [])}
        clean, seen = [], set()
        for ph in phrases:
            k = _project._speakable(ph)
            if not k or k in seen:
                continue
            if k not in have:
                why = clash(client, k, eid)
                if why:
                    return 409, {"error": why, "phrase": k}
            seen.add(k)
            clean.append(" ".join(str(ph).split()))
        try:
            ws_call("config/entity_registry/update", entity_id=eid, aliases=clean)
        except Exception as e:                                   # noqa: BLE001
            return 502, {"error": "the platform did not take the phrases: %s" % e}
    try:
        res = voice_phrases(client)
    except Exception as e:                                       # noqa: BLE001
        return 500, {"error": "phrases saved but the sentences were not written: %s" % e}
    return (200 if res.get("ok") else 500), dict(res, entity_id=entity_id, phrases=clean)
