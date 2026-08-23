"""
ProOS Pro Assist GATEWAY (phase 1: text, read/control tools).

Natural-language home assistant per ProOS_ProAssist_Gateway_Spec.md. The brain
is a SUPPLIER — `provider` is a config key (claude | openai), one adapter each,
identical tool schema — never a marriage. Everything the model can do goes
through the TOOL REGISTRY below, which operates on IMMUTABLE ids (area_id,
entity_id) per the Identity Architecture Standard, with committed membership as
the world model: the model is told what each room ACTUALLY contains, not the
raw registry.

Hard rules enforced in code (not just prompt):
  * AV power NEVER via raw device calls — media_player turn_on/off/toggle is
    rejected with a pointer to room_activity, so the generated scripts keep
    owning the ordering that makes rooms work (sleep sources first, AVR last).
  * Service + domain whitelists on every control path.
  * Every chat turn runs as the AUTHENTICATED caller; config is tech/owner.

Config lives in /data/assist.json: {"provider","model","api_key"} — written by
Pro's Assist AI card, never echoed back out (status reports everything BUT the
key). Sessions are in-memory per (user, session) with a trimmed rolling window;
pinned long-term memory is phase 2.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from .membership import area_of

_CFG_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "assist.json")
_LOCK = threading.Lock()
_SESSIONS: dict = {}          # (user_id, session) -> [ {role, content}, ... ]
_MAX_TURNS = 24               # rolling window (user+assistant messages kept)


def _safe_trim(msgs, limit):
    """Trim a session's rolling window WITHOUT orphaning tool messages.

    A naive tail-slice can cut between an assistant's tool call and its tool
    responses. Both providers hard-reject that: OpenAI with "messages with role
    'tool' must be a response to a preceding message with 'tool_calls'", Claude
    with the tool_use/tool_result equivalent — and the session is then bricked,
    erroring on EVERY message until it happens to trim past the orphan. After
    slicing, drop leading messages until the head is a clean user turn."""
    out = list(msgs)[-limit:]
    def _is_orphan_head(m):
        if not isinstance(m, dict):
            return True
        if m.get("role") == "tool":                       # OpenAI tool response
            return True
        if m.get("role") == "assistant":
            if m.get("tool_calls"):                       # OpenAI call, replies lost
                return True
            c = m.get("content")
            if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in c):
                return True                               # Claude tool_use head
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return True                               # Claude result, call lost
        return False
    while out and _is_orphan_head(out[0]):
        out.pop(0)
    return out
# ROOM TO FINISH A REAL JOB (19 Aug 2026, register 228). Eight was
# measured against the job Dave watched a Developer session do by hand:
# read the speaker · write the script · run it · read the devices back ·
# fix it · run it again · verify · attach it to a scene · confirm — NINE
# steps, and that is ONE room with everything going right. Eight could
# not finish it, so it stopped mid-repair and said only that it had hit
# a limit. Reads are the cheap half of that count and are exactly what
# this product asks for more of.
_MAX_TOOL_ROUNDS = 24
_HTTP_TIMEOUT = 75

DEFAULT_MODELS = {"claude": "claude-sonnet-4-5", "openai": "gpt-4o"}


# ── config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(_CFG_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(body: dict) -> dict:
    cfg = load_config()
    # fast_model (A5, optional): a cheaper model for short single-clause commands.
    # Blank = off, everything runs on the main model — the safe default.
    for k in ("provider", "model", "fast_model"):
        if k in body:
            cfg[k] = (body.get(k) or "").strip()
    # api_key: absent = keep existing; empty string = clear.
    if "api_key" in body:
        cfg["api_key"] = (body.get("api_key") or "").strip()
    # image_key: optional OpenAI key for AI-generated scene photos. Absent =
    # keep; empty = clear. Lets Claude drive chat while OpenAI makes the images.
    if "image_key" in body:
        cfg["image_key"] = (body.get("image_key") or "").strip()
    if cfg.get("provider") not in ("claude", "openai"):
        cfg["provider"] = cfg.get("provider") or ""
    os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
    tmp = _CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.replace(tmp, _CFG_PATH)
    return status()


def status() -> dict:
    cfg = load_config()
    return {"enabled": bool(cfg.get("provider") and cfg.get("api_key")),
            "provider": cfg.get("provider") or "",
            "model": cfg.get("model") or DEFAULT_MODELS.get(cfg.get("provider") or "", ""),
            "has_key": bool(cfg.get("api_key")),
            "has_image_key": bool(_image_key(cfg))}


def _image_key(cfg: dict) -> str:
    """OpenAI key for AI scene images: the dedicated image_key, else the chat
    key when the chat provider is OpenAI."""
    cfg = cfg or load_config()
    ik = (cfg.get("image_key") or "").strip()
    if ik:
        return ik
    if cfg.get("provider") == "openai":
        return (cfg.get("api_key") or "").strip()
    return ""


def resolve_scene_photo(name: str, mood: str, slug: str):
    """Decide a scene's photo: AI-generate a bespoke image when an OpenAI image
    key is set (saved to /www, persistent), else a curated matched image. Logs
    exactly which path ran so a fallback is never a silent mystery. Returns
    (url_or_path, source) where source is generated|curated|curated_no_key."""
    from . import scenephotos
    ik = _image_key(None)
    if ik:
        png, err = scenephotos.generate(scenephotos.build_prompt(name, mood), ik)
        if png:
            p = scenephotos.save_generated(slug, png)
            if p:
                print("  [assist] scene photo generated for '%s'" % name, flush=True)
                return p, "generated"
        print("  [assist] scene image generation failed (%s) — using curated" % err, flush=True)
        return scenephotos.match(mood or name), "curated_gen_failed"
    print("  [assist] no scene image key — using curated photo", flush=True)
    return scenephotos.match(mood or name), "curated_no_key"


def clear_data() -> None:
    """Factory-reset hook: forget provider config, pinned memory, and sessions."""
    for p in (_CFG_PATH, _MEM_PATH):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    with _LOCK:
        _SESSIONS.clear()


# ── tool registry ────────────────────────────────────────────────────────────
# One schema, served to BOTH providers. Small, well-described, immutable ids.

_MEDIA_POWER = {"turn_on", "turn_off", "toggle"}
_DEVICE_ACTIONS = {
    "turn_on", "turn_off", "toggle",
    "media_play", "media_pause", "media_stop",
    "media_next_track", "media_previous_track",
    "volume_set", "volume_up", "volume_down", "volume_mute",
    "select_source",
    "open_cover", "close_cover", "stop_cover", "set_cover_position",
    "set_temperature", "set_hvac_mode", "set_fan_mode",
    "set_percentage",
}
_AREA_DOMAINS = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off"},
    "fan": {"turn_on", "turn_off"},
    "cover": {"open_cover", "close_cover", "stop_cover"},
    "media_player": {"media_pause", "media_stop"},   # transport only; power is per-room choreography
}

TOOLS = [
    {"name": "rooms_overview",
     "description": "The home's rooms as commissioned: committed members with their roles and "
                    "live states, plus each room's available one-touch activities. Use this FIRST "
                    "to ground yourself — it is the source of truth for what exists and its ids.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "room_activity",
     "description": "Run a room's one-touch activity by its script entity_id (from rooms_overview), "
                    "e.g. watch a source, TV off. This is the ONLY correct way to power AV — the "
                    "scripts own the device ordering that makes it reliable. Judge success by the "
                    "room's verdict sensor (rooms_overview: verdict_sensor), NEVER by raw device "
                    "state: a room whose off_state is 'art' rests its display on artwork — Art "
                    "Mode showing after TV off IS off, not a failure. Report it simply as off "
                    "('Family Room is off'); the rest state is configured, so don't mention it.",
     "input_schema": {"type": "object", "properties": {
         "script_entity_id": {"type": "string", "description": "script.proos_* entity id"}},
         "required": ["script_entity_id"]}},
    {"name": "room_off",
     "description": "Turn a WHOLE room off, deterministically: runs the room's TV-off activity "
                    "(honouring its off_state — an 'art' room rests on artwork and that IS off), "
                    "stops its speakers, and switches off the room's lights, switches and fans — "
                    "skipping every device on the installer's power-protect list (those stay "
                    "powered, e.g. equipment plugs, but remain individually controllable). Use "
                    "for 'turn the bedroom off'. For just the TV, use room_activity.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "room_on",
     "description": "Turn a room's lights, switches and fans ON, skipping the installer's "
                    "power-protect list. AV is NOT started — starting a room means choosing an "
                    "activity, so use room_activity for that.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "device_control",
     "description": "Control ONE device by entity_id with a whitelisted action. Media player POWER "
                    "is rejected here by design — use room_activity. data carries service fields "
                    "(brightness_pct, volume_level 0-1, temperature, source, position...). Lights "
                    "RAMP natively: 'slowly' / 'over N seconds' = ONE turn_on with data "
                    "{brightness_pct, transition: N} — never step brightness manually and never "
                    "claim ramping is unsupported.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "action": {"type": "string", "description": "one of: " + ", ".join(sorted(_DEVICE_ACTIONS))},
         "data": {"type": "object", "description": "optional service data"}},
         "required": ["entity_id", "action"]}},
    {"name": "area_control",
     "description": "Control a whole room at once, live-resolved from the registry: lights / "
                    "switches / fans on-off, covers open-close, pause media. Use for 'turn off the "
                    "office lights' style requests. data passes through — e.g. lights ramp with "
                    "{brightness_pct: 100, transition: 10} (one call, never manual steps). "
                    "Domains: " + ", ".join(sorted(_AREA_DOMAINS)),
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "domain": {"type": "string"},
         "action": {"type": "string"},
         "data": {"type": "object"}},
         "required": ["area_id", "domain", "action"]}},
    {"name": "health_incidents",
     "description": "The OPEN incidents on the Health page, exactly as the "
                    "installer sees them — title, cause, fix wording, room, "
                    "severity. Read this FIRST whenever anyone mentions a "
                    "problem, warning, alarm, or asks 'any issues?' / 'what "
                    "needs attention?': the system has usually already named "
                    "the issue AND its fix, so relay that plainly instead of "
                    "re-diagnosing from scratch. Empty list = nothing open.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "device_powerlog",
     "description": "Every time a TV/media player turned ON or OFF recently, "
                    "each attributed: 'proos' (a room activity did it) or "
                    "'external' (native remote, an app, or another device "
                    "waking the display through CEC — ALL equally "
                    "legitimate). THE tool for 'is this TV turning itself "
                    "on?': read the pattern — external events at times "
                    "nobody was using the room are the DEVICE's own "
                    "behaviour, not a system fault. State the pattern "
                    "plainly; judge nothing; advise disabling nothing.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "hours": {"type": "number",
                   "description": "look-back window, default 48, max 240"}},
         "required": ["entity_id"]}},
    {"name": "get_states",
     "description": "Read the live state + key attributes of up to 40 entities by id.",
     "input_schema": {"type": "object", "properties": {
         "entity_ids": {"type": "array", "items": {"type": "string"}}},
         "required": ["entity_ids"]}},
    {"name": "room_status",
     "description": "The room's live AV situation RIGHT NOW — read this BEFORE you "
                    "answer any question about what's playing or how loud it is, and "
                    "before any volume/mute command, so you CONFIRM instead of assume. "
                    "Returns the room's activity verdict, whether the context is video "
                    "(watching) or audio (music playing), the ACTIVE volume endpoint — "
                    "the speaker actually playing, or the TV-audio owner when watching — "
                    "and each endpoint's REAL volume (a percent, like '40%') and muted flag. This is "
                    "how you know, not guess: never tell anyone a room is 'already muted', "
                    "or say you 'turned it up', without having read this first. Empty "
                    "endpoints = the room has no volume endpoint committed; say so plainly. "
                    "Accepts an area_id or a room name.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "room_volume",
     "description": "Change the volume of a ROOM — you name the room, the endpoint model "
                    "picks the target: the speaker actually playing (music), or the TV-audio "
                    "owner when watching. This is the RIGHT tool for 'turn it up', 'louder in "
                    "the office', 'mute', 'set the volume to 40%' — it follows what's playing, "
                    "so it never moves the wrong device. action: up | down | mute | unmute | "
                    "set. For set, give level as a percent, 0-100 (40 means 40%). mute/unmute SET "
                    "the state deterministically — read room_status first if you need to REPORT "
                    "the mute state, but to mute you just mute. If the room has no volume "
                    "endpoint you get a message saying so — relay it, don't spray devices.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "action": {"type": "string", "description": "up | down | mute | unmute | set"},
         "level": {"type": "number", "description": "for set: a percent, 0-100 (40 means 40%)"}},
         "required": ["area_id", "action"]}},
    {"name": "room_media",
     "description": "Transport control for the ROOM's active player — pause / play / next / "
                    "previous the speaker (or TV-audio owner) that's currently playing, resolved "
                    "from the room's verdict. Use for 'pause the music in here', 'skip this "
                    "track', 'resume'. Best for music; for AV power and sources use room_activity.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "action": {"type": "string", "description": "play | pause | next | previous | stop"}},
         "required": ["area_id", "action"]}},
    {"name": "usage_history",
     "description": "The room's learned USAGE patterns — what it's typically used for, at what "
                    "time of day, on weekdays vs weekends, and how often it's started externally "
                    "(a native remote) — derived from the home's OWN recorded history. This is "
                    "SOFT evidence: use it to reason ('they usually watch Apple TV on weekday "
                    "evenings'), to personalise a suggestion, and to make a CONFIRM question "
                    "smarter — e.g. a TV just came on externally and it's their usual Apple TV "
                    "hour, so ask 'looks like your Apple TV — want the room set up?'. It is NEVER "
                    "proof of what the room is doing right now (call room_status for that) and "
                    "NEVER a reason to act on its own — a habit is a hint, the person's yes is the "
                    "gate. Accepts an area_id or a room name.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "room_read",
     "description": "STACK the evidence about a room that changed on its own — call this when a "
                    "room comes on and no ProOS activity fired (a native remote, a CEC wake), or "
                    "when someone asks 'what's going on in here'. It gathers three things for you "
                    "to reason over: the LIVE state (and whether it was started externally), any "
                    "recent external-start events, and what the room is USUALLY doing at THIS time "
                    "(its habit). When the external change lines up with the habit — the TV just "
                    "came on and it's their usual Apple TV hour — you may say what it looks like "
                    "and offer the setup as a CONFIRM question ('looks like your usual Apple TV — "
                    "want the room set the way you like it?'). The habit is what makes the guess "
                    "good, never what makes it certain: never state it as fact, never act without "
                    "the yes, and if the evidence is thin just report the plain device fact. "
                    "Read-only. Accepts an area_id or room name.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "app_launch",
     "description": "Open a streaming app (Netflix, Disney+, YouTube…) in a room. A room can have "
                    "several devices that run apps — the smart TV, an Apple TV, a Shield. Call with "
                    "just area_id + app first: if only one device has it, it launches; if MORE than "
                    "one does, you get {needs_choice, options:[{entity_id,name}]} — ASK the user "
                    "which device, then call again with `device` set to their chosen entity_id. If "
                    "the app isn't anywhere you get the available list — say so, don't guess. Make "
                    "sure the display is ON first (run the room's watch activity). Judge the OUTCOME "
                    "by the room's verdict sensor (rooms_overview: verdict_sensor), NEVER by the "
                    "device's raw state: many streamers cannot see which app is on screen, so their "
                    "state can sit at 'idle' while Netflix plays — raw 'idle' is not evidence of "
                    "failure. If the verdict and a raw state disagree, ask the user what is ON THE "
                    "SCREEN — their answer settles it.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "app": {"type": "string", "description": "app name, e.g. 'Netflix'"},
         "device": {"type": "string", "description": "entity_id of the chosen device (only after needs_choice)"}},
         "required": ["area_id", "app"]}},
    {"name": "area_entities",
     "description": "List the REAL entities assigned to a room, with their current state — use this "
                    "before building a scene so you use actual light/cover/climate ids and never "
                    "invent one. Accepts an area_id or a room name; domains defaults to "
                    "light/cover/climate/fan/switch.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "domains": {"type": "array", "items": {"type": "string"}}},
         "required": ["area_id"]}},
    {"name": "verify",
     "description": "AFTER acting, verify reality: each check compares an entity's live state to "
                    "what you expect. Report failures honestly to the user — never claim success "
                    "on a command echo. But a failed check is ONE WITNESS, not a verdict: a "
                    "media_player's driver may be UNABLE to see what you asked about (app playback "
                    "especially — some sit at 'idle' while the screen plays). Before reporting an "
                    "AV failure, read the room's verdict sensor; if it says the activity is "
                    "running, believe it — or ask the user what they SEE, which outranks every "
                    "sensor.",
     "input_schema": {"type": "object", "properties": {
         "checks": {"type": "array", "items": {"type": "object", "properties": {
             "entity_id": {"type": "string"},
             "expect_state": {"type": "string"},
             "expect_attr": {"type": "object", "description": "attribute:value pairs"}},
             "required": ["entity_id"]}}},
         "required": ["checks"]}},
    {"name": "music_search",
     "description": "Search the home's music library + streaming services for artists, albums, "
                    "tracks or playlists. Returns items with a `uri` you pass to music_play or "
                    "music_playlist_create. kinds is optional (artist/album/track/playlist).",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "kinds": {"type": "array", "items": {"type": "string"},
                   "description": "optional: artist, album, track, playlist, radio"},
         "limit": {"type": "integer", "description": "per kind, default 6"}},
         "required": ["query"]}},
    {"name": "music_play",
     "description": "Play (or queue) music in a room. area_id is the room; the room's committed "
                    "MA speaker is resolved automatically. media_uri comes from music_search. "
                    "mode: play (now), next (play next), add (end of queue).",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "media_uri": {"type": "string"},
         "mode": {"type": "string", "description": "play | next | add (default play)"}},
         "required": ["area_id", "media_uri"]}},
    {"name": "music_playlist_create",
     "description": "Create a personalised playlist and fill it with tracks. Curate track_uris "
                    "yourself via music_search (search, pick, then create). Returns the new playlist.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "track_uris": {"type": "array", "items": {"type": "string"}}},
         "required": ["name"]}},
    # THE KNOWLEDGE STORE (19 Aug 2026 — Dave: "the only difference
    # should be having all this project documentation"). What the
    # product and this house have LEARNED, read when it is relevant
    # instead of recited before every answer.
    {"name": "knowledge_search",
     "description": "Search what ProOS and this house have learned — how devices present "
                    "themselves, what awareness may claim, how a moment is built and proven, "
                    "who may do what, and the installer's own notes about THIS house. Search "
                    "here BEFORE you conclude anything you are unsure of, and whenever "
                    "something behaves in a way you did not expect: the answer has usually "
                    "been written down already, and it was written because it was learned the "
                    "hard way. Returns each document's own words.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what you want to know about"}},
         "required": ["query"]}},
    {"name": "knowledge_read",
     "description": "Read one knowledge document whole, by the path a search returned.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "knowledge_write",
     "description": "Write a note about THIS house into the installer's record — a quirk, a "
                    "decision, something you found out that the next person would waste an "
                    "hour rediscovering. Ask before writing one, and write what you READ, not "
                    "what you concluded.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "body": {"type": "string", "description": "plain words; what was read, and when"}},
         "required": ["title", "body"]}},
    {"name": "memory_get",
     "description": "Recall what you know about this person — `facts` they TOLD you and `learned` "
                    "preferences you picked up from how they use the home (soft). Call at the start "
                    "of a conversation when personalisation would help.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "memory_set",
     "description": "Remember something about this person for future conversations. Default is a "
                    "TOLD fact they stated ('likes jazz at dinner', 'kids' bedtime is 8pm'). Set "
                    "learned=true for a LEARNED preference you INFERRED from how they use the home "
                    "('prefers the kitchen HomePod', 'watches Apple TV most weekday evenings') — "
                    "it's kept separately and treated as soft. This is how your memory GROWS: pin "
                    "durable, meaningful patterns as you notice them, never one-offs. Keep it short. "
                    "forget=true removes a matching item from both. Per person.",
     "input_schema": {"type": "object", "properties": {
         "fact": {"type": "string"},
         "learned": {"type": "boolean", "description": "true = a preference you inferred (soft), not one they stated"},
         "decline": {"type": "boolean", "description": "true = a suggestion they waved off; record it so you never re-offer (a no sticks)"},
         "forget": {"type": "boolean", "description": "true to remove a previously-pinned item matching `fact` from all streams"}},
         "required": ["fact"]}},
    {"name": "scenes_list",
     "description": "List existing ProOS-created scenes: name, entity_id, which room each lives "
                    "in (area_id) and what devices it contains. ALWAYS call this FIRST when the "
                    "user refers to a scene by name — find it here by name and room, never assume "
                    "which room a scene belongs to. If more than one matches, ask.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "scene_create",
     "description": "Create a NEW scene, or UPDATE one you already made. A scene is a saved MOMENT. "
                    "states is a list of {entity_id, state, attributes} and is stored EXACTLY as "
                    "given — nothing is converted, stripped or added, so send what you have READ "
                    "from the device itself, then apply it and read the devices back to prove it. "
                    "The only thing refused is an entity that is not on this box. "
                    "For a full moment ('dinner scene with some jazz') pass music: {query: 'smooth "
                    "jazz', area_id: the room, volume: 25} — that music then starts EVERY time the "
                    "scene runs, from chat or a dashboard tap. Suggest music that fits the mood when "
                    "the user hasn't named any, and ask before attaching it. To make a new scene, "
                    "omit scene_entity_id — a fresh scene is created even if another room has the "
                    "same name. To change a scene you created, pass its scene_entity_id (do NOT rely "
                    "on the name). photo_query is a short vivid MOOD description (e.g. 'dim cinema "
                    "room, warm glow') matched to a dashboard photo. After creating, apply and "
                    "verify (the test loop). Reuse committed member ids from rooms_overview.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "scene_entity_id": {"type": "string", "description": "ONLY to update an existing ProOS scene; omit for a new one"},
         "states": {"type": "array", "items": {"type": "object", "properties": {
             "entity_id": {"type": "string"},
             "state": {"type": "string"},
             "attributes": {"type": "object", "description": "the attributes to save, stored "
                            "exactly as given. Read the device first and send what IT uses; "
                            "if a scene doesn't restore what you sent, the read-back will "
                            "show it and you fix it there."}},
             "required": ["entity_id", "state"]}},
         "music": {"type": "object", "description": "music that starts with the scene", "properties": {
             "query": {"type": "string", "description": "what to play, e.g. 'smooth jazz playlist'"},
             "area_id": {"type": "string", "description": "room it plays in"},
             "volume": {"type": "number", "description": "1-100, optional"}}},
         "activity_script": {"type": "string",
             "description": "a room activity that fires with the scene — the script entity_id "
                            "from rooms_overview (e.g. the room's Watch Apple TV). THIS is how "
                            "TV/AV belongs in a scene: user wants a scene that turns the TV on "
                            "or starts a source, attach the activity — don't refuse."},
         "remove_entities": {"type": "array", "items": {"type": "string"},
             "description": "UPDATE only: entity_ids to drop from the scene. Naming the "
                            "activity companion's source device also removes the activity."},
         "remove_activity": {"type": "boolean",
             "description": "UPDATE only: detach the activity — 'take the TV/Apple TV out "
                            "of the scene' means THIS, not remove_entities"},
         "remove_music": {"type": "boolean",
             "description": "UPDATE only: detach the music companion"},
         "photo_query": {"type": "string", "description": "vivid mood description to match a photo"}},
         "required": ["name", "states"]}},
    # THE HALF THE PLATFORM HAS NEVER HEARD OF (19 Aug 2026, register 245).
    # Its own scene tool is better at scenes than ours — optimistic locking,
    # surgical edits. Use that. Then attach the MOMENT here.
    {"name": "scene_companions",
     "description": "Attach ProOS's half of a moment to a scene that already exists: the MUSIC "
                    "that should start and the room ACTIVITY (TV/source) that should come up. "
                    "Make the scene itself with the platform's own scene tool, then call this. "
                    "Also removes them: remove_music / remove_activity.",
     "input_schema": {"type": "object", "properties": {
         "scene_entity_id": {"type": "string", "description": "e.g. scene.work"},
         "music": {"type": "object", "description": "{query, area_id, volume} — what to play and where",
                   "properties": {"query": {"type": "string"},
                                  "area_id": {"type": "string"},
                                  "volume": {"type": "number"}}},
         "activity_script": {"type": "string", "description": "script.* proven to start the source"},
         "remove_music": {"type": "boolean"},
         "remove_activity": {"type": "boolean"}},
         "required": ["scene_entity_id"]}},
    {"name": "scene_photo",
     "description": "Match a new photo to a scene from a vivid description (e.g. 'cosy reading nook, "
                    "warm lamp light'). Use when the user wants a different picture. Updates the "
                    "dashboard image.",
     "input_schema": {"type": "object", "properties": {
         "scene_entity_id": {"type": "string"},
         "photo_query": {"type": "string"}},
         "required": ["scene_entity_id", "photo_query"]}},
    {"name": "scene_apply",
     "description": "Activate (fire) a scene by its entity_id to TEST or use it. Follow with verify "
                    "to confirm the devices actually reached the intended states.",
     "input_schema": {"type": "object", "properties": {
         "scene_entity_id": {"type": "string"}}, "required": ["scene_entity_id"]}},
    {"name": "scene_delete",
     "description": "Delete a ProOS-created scene by entity_id. Confirm with the user first.",
     "input_schema": {"type": "object", "properties": {
         "scene_entity_id": {"type": "string"}}, "required": ["scene_entity_id"]}},
    {"name": "scene_dashboard",
     "description": "Show (or hide) a scene on the homeowner's dashboard Scenes page. ALWAYS ask "
                    "the user if they'd like it added before calling. The dashboard auto-picks a "
                    "photo from the scene NAME (e.g. Movie, Dinner, Relax, Night, Party, Away, "
                    "Morning, Work) — so name scenes with one of those words for a fitting picture.",
     "input_schema": {"type": "object", "properties": {
         "scene_entity_id": {"type": "string"},
         "show": {"type": "boolean", "description": "true to add to the dashboard, false to remove"}},
         "required": ["scene_entity_id", "show"]}},
    {"name": "automation_create",
     "description": "Create OR update an automation (installer/tech only). trigger/condition/action "
                    "are lists of standard automation config dicts. Prefer firing a scene or a room activity "
                    "as the action. Pass the same alias to update. Test with automation_trigger.",
     "input_schema": {"type": "object", "properties": {
         "alias": {"type": "string"},
         "trigger": {"type": "array", "items": {"type": "object"}},
         "condition": {"type": "array", "items": {"type": "object"}},
         "action": {"type": "array", "items": {"type": "object"}},
         "mode": {"type": "string", "description": "single | restart | queued (default single)"}},
         "required": ["alias", "trigger", "action"]}},
    {"name": "automation_trigger",
     "description": "Manually run an automation's actions now to TEST it (installer/tech only). "
                    "Follow with verify.",
     "input_schema": {"type": "object", "properties": {
         "automation_entity_id": {"type": "string"}}, "required": ["automation_entity_id"]}},
    {"name": "automation_delete",
     "description": "Delete an automation by entity_id (installer/tech only). Confirm first.",
     "input_schema": {"type": "object", "properties": {
         "automation_entity_id": {"type": "string"}}, "required": ["automation_entity_id"]}},
    # ── awareness: the assistant answers from VERDICTS, not vibes ────────────
    {"name": "home_status",
     "description": "The health of the whole home right now, from ProOS's live device "
                    "watchers and room monitors: every watched device's status (ok / "
                    "standby / amber / fault), what's wrong and the guidance for it. "
                    "ALWAYS call this for 'is everything ok', 'any problems', or any "
                    "question about the state of the home — never guess.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "room_health",
     "description": "Diagnose ONE room: its live health check (issues found + suggested "
                    "actions), device fault verdicts and current activity state. ALWAYS "
                    "call this when something in a room 'isn't working', before "
                    "explaining or attempting anything.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"}}, "required": ["area_id"]}},
    {"name": "recovery_history",
     "description": "Recent awareness events: faults, recoveries and what ProOS did "
                    "about them, newest first. Use for 'what happened', 'did anything "
                    "go wrong overnight', or to check whether a device has been "
                    "flapping before promising it's fine.",
     "input_schema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "events to return (default 30)"}}}},
    {"name": "device_liveness",
     "description": "RAW EVIDENCE per watched device: integration state, whether its "
                    "independent network witness can see it right now, and the current "
                    "verdict. This is how 'everything is normal' gets EARNED — call it "
                    "before making any all-clear claim, and whenever a device's power "
                    "state matters. A device reporting 'off' whose witness says GONE is "
                    "dead, unplugged or cut off — never 'normal'.",
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string",
                     "description": "optional — limit to one room"}}}},
    {"name": "device_recover",
     "description": "Run the configured self-heal for one faulted device — integration "
                    "reload, or the installer-assigned smart-plug/PoE power-cycle when "
                    "one is set. Same executor the automatic recovery uses. Check "
                    "room_health or home_status first and only recover devices that "
                    "actually show a fault. For a HOMEOWNER: explain what you found in "
                    "plain words, ask 'would you like me to try fixing it?', and only "
                    "after a clear yes call this with confirmed=true. Follow with "
                    "verify; if it didn't come back, flag_for_pro.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "confirmed": {"type": "boolean",
                       "description": "the user said yes to attempting the fix"}},
         "required": ["entity_id"]}},
    # ── capability tools: offered ONLY when the home actually has the class ──
    {"name": "security_status",
     "description": "The security system: every alarm panel's state (armed_home / "
                    "armed_away / disarmed / triggered) and any open or faulted zones.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "security_arm",
     "description": "ARM the security system (home or away). Arming only — this tool "
                    "cannot disarm, ever: disarming needs a code at a keypad or the "
                    "security app, say so if asked. Confirm with the user before arming.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "mode": {"type": "string", "description": "home | away"}},
         "required": ["entity_id", "mode"]}},
    {"name": "locks_status",
     "description": "Every door lock's current state (locked / unlocked / jammed).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "lock_control",
     "description": "Lock a door (any resident) or unlock one (installer/tech/owner "
                    "only — never unlock for a homeowner request, offer the lock's "
                    "own app or keypad instead). Confirm before unlocking.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "action": {"type": "string", "description": "lock | unlock"}},
         "required": ["entity_id", "action"]}},
    {"name": "cameras_status",
     "description": "Every camera: recording state and any current motion/doorbell "
                    "activity from its detection sensors. Status only — no video.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "weather",
     "description": "Current conditions and forecast from the home's own weather "
                    "provider. Use for any weather question, and to ADVISE: rain "
                    "coming → offer to close covers; a hot afternoon → suggest "
                    "pre-cooling; a cold snap → suggest adjusting heating times.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "flag_for_pro",
     "description": "Log an issue for the home's installer to look at — with everything "
                    "you've diagnosed attached, so they arrive knowing the story. Use "
                    "when a problem needs hands or parts, when recovery didn't fix it, "
                    "or whenever the user asks you to 'tell the installer'. Tell the "
                    "user it's been passed on.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "one-line issue"},
         "detail": {"type": "string", "description": "what you found: device, room, verdicts, what was tried"},
         "entity_id": {"type": "string"}},
         "required": ["summary"]}},
]

# Which tools exist only under a condition. domain: at least one entity of that
# HA domain is present in the home; pro: only offered to installer/tech/owner.
# Everything else is universal. This is what makes the toolset GROW with the
# home: add an alarm panel and the assistant gains security tools, remove the
# last lock and lock tools vanish — nothing hardcoded per site.
_TOOL_GATES = {
    "security_status": {"domain": "alarm_control_panel"},
    "security_arm":    {"domain": "alarm_control_panel"},
    "locks_status":    {"domain": "lock"},
    "lock_control":    {"domain": "lock"},
    "cameras_status":  {"domain": "camera"},
    "weather":         {"domain": "weather"},
    "automation_create":  {"pro": True},
    "automation_trigger": {"pro": True},
    "automation_delete":  {"pro": True},
    # A TOOL THAT CANNOT WORK IS NOT OFFERED (18 Aug 2026). The music
    # engine is an OPTIONAL add-on. When it isn't linked these tools can
    # only fail — and on Dave's box the model called one, got "not
    # connected", and reported that to him as the answer instead of
    # doing the job with the platform's own tools on the room's own
    # speaker. This is the SAME law as the domain gates above, not a new
    # one: no lock, no lock tools; no engine, no engine tools.
    # A note about THIS house is the professional's record.
    "knowledge_write":       {"pro": True},
    "music_search":          {"engine": True},
    "music_play":            {"engine": True},
    "music_playlist_create": {"engine": True},
    # device_recover, recovery_history and device_liveness are offered to
    # EVERYONE: a homeowner
    # runs recovery through in-chat consent (the tool requires confirmed=true
    # for them), and the history is how "what happened overnight" gets an
    # honest answer. The homeowner-facing wording is the prompt's job.
}


# A ProOS tool that only RE-ASKS THE PLATFORM a question the platform
# answers better steps aside for a caller who is holding the platform's
# whole toolset (18 Aug 2026 — Dave: "get it working 100% for developer
# first and then we can build a filter for installer and Homeowner").
# Today that is the Developer alone; anyone holding only part of the
# platform keeps these, and a homeowner — who holds none of it — keeps
# them all. Nothing is DELETED here: the same code still serves the
# tiers that have nothing to replace it with.
#
# WHY area_entities IS IN THIS LIST: it filters to light/cover/climate/
# fan/switch, so on a room whose devices are SPEAKERS it returns
# nothing, and the model reported "the office has no committed devices"
# to a man looking at two speakers playing Triple M 80s. The platform's
# own search answers it in one call. rooms_overview still carries the
# commissioned view — that is ProOS's own knowledge and is untouched.
#
# NOT IN THIS LIST, having read them: scenes_list (carries the companion
# record and the Scenes-page flag), device_powerlog (reads through
# ProOS's own power record), get_states (the missing-entity honesty
# shaper). The audit filed all three under 'retire'; the audit was wrong.
_MIRROR_DUPLICATES = {"weather", "locks_status", "cameras_status",
                      "security_status", "area_entities"}

# ── THE SAME LAW, EXTENDED FROM READS TO WRITES (19 Aug 2026, register 245)
#
# DAVE, 19 Aug, after watching Pro Assist fail a job a plain Claude session
# with the platform's MCP had done without trouble: "what I asked for was to
# have Pro Assist working exactly the same as this chat in Claude… if you
# look at the transcript I posted using a separate session it has no issues
# creating the scene or any scene or automation."
#
# THE REASON THE OTHER SESSION WORKED IS THAT IT HAD NO PROOS TOOLS IN THE
# WAY. As Developer he was handed 78 platform tools AND 41 of ours, and the
# model reached for `scene_create` — ours, familiar-sounding, and worse: the
# platform's own scene tool has optimistic locking and surgical edits that
# ProOS has never had.
#
# So these step aside for a caller who holds the platform's own ACTS, the
# same way the reads above step aside for a caller who holds its reads. No
# new law: it is ruling B's law, applied to the other half of the tool list,
# and it still follows the server's own testimony rather than a tier name.
#
# WHAT IS NOT ON THIS LIST, AND WHY: room verdicts, health, witnesses,
# recovery, the journal, the knowledge store, music (the engine is not the
# platform), and scene_companions — the music and activity halves of a
# moment, which no platform tool has ever heard of. That is the product.
# FOUR OF THESE I TRIED TO PUT ON THE LIST AND THE GATE STOPPED ME. Read
# before arguing with it, and it was right every time:
#   scenes_list  — lists each scene's COMPANIONS; the platform cannot see them
#   scene_apply  — fires the companions; scene.turn_on would leave the music
#                  and the activity dead, and the person in a silent room
#   scene_delete — cleans the companion sidecar AND the photo; the platform's
#                  remove would orphan both
#   get_states   — the missing-entity honesty shaper (register 220), pinned
#                  by his ruling on 18 Aug and worth more than its ten lines
# A tool is only a re-ask if the platform's version loses NOTHING.
_MIRROR_WRITE_DUPLICATES = {
    "scene_create",       # ha_config_set_scene — and it has the locking and
                          # surgical edits ours never had. Safe to stand down
                          # ONLY because the companions moved to their own
                          # tool in this same build.
    "automation_create",  # ha_config_set_automation
    "automation_delete",  # ha_config_remove_automation
    "automation_trigger",  # ha_call_service automation.trigger
    "device_control",     # ha_call_service / ha_bulk_control
    "area_control",       # ha_bulk_control
    # RULING B, ITEM 1d — the last two re-ask ACTS (23 Aug 2026, reg 265).
    # Both are thin wrappers over one service call. Their law (this tool
    # NEVER disarms; a homeowner never unlocks) binds the tiers that KEEP
    # them — an act-holder reaches the panel and the lock through the
    # platform either way, so for that caller ours is a second tool for
    # one job. app_launch was read at the same time and is NOT here: it
    # carries the ProOS app catalogue and the register-109
    # which-witness-to-believe fact, and the platform's select_source
    # loses both.
    "security_arm",       # ha_call_service alarm_control_panel.alarm_arm_*
    "lock_control",       # ha_call_service lock.lock / lock.unlock
}


def _active_tools(runner) -> list:
    """The tool list for THIS turn: universal tools, plus capability tools for
    device classes the home actually has, minus pro tools below the caller's
    tier. A tool that isn't offered can't be attempted — the model never sees
    it, so a homeowner is never told 'access denied', and the assistant never
    talks about a security system the home doesn't have."""
    doms = runner.home_domains()
    pro = _is_pro(runner.user)
    # WHAT THE CALLER HOLDS OF THE PLATFORM decides whether our re-asks
    # of it are offered at all — see _MIRROR_DUPLICATES. Computed first
    # because it filters the ProOS list below.
    whole = runner._mcp_list() if (runner.user.get("is_owner") or pro) else []
    if runner.user.get("is_owner"):
        mirror = list(whole)
    elif pro:
        mirror = [t for t in whole
                  if t.get("read_only") or not t.get("destructive")]
    else:
        mirror = []
    # THE TIER FILTER (19 Aug 2026, register 229 — Dave: "then we can
    # build a filter for installer and Homeowner"). A ProOS re-ask steps
    # aside for anyone holding the platform's own tool that ANSWERS IT.
    # Every tool in _MIRROR_DUPLICATES is a READ, so the test is whether
    # this caller holds the platform's reads — which the census (register
    # 223) measured them doing: 33 read-only tools on this box, offered
    # to installer and tech alike. A homeowner holds none of it and keeps
    # every one of them. No tier name is written anywhere in this test.
    holds_reads = any(t.get("read_only") for t in mirror)
    # …and the same question for the WRITE side (register 245): does this
    # caller hold acts of the platform's? On this box only the Developer
    # does — the census found ZERO acts the server vouches as safe, so
    # installer and tech keep every ProOS write tool exactly as before.
    # Nothing here loosens a tier; it only stops handing the Developer two
    # tools for one job and letting the model pick the worse one.
    holds_acts = any(not t.get("read_only") for t in mirror)
    out = []
    for t in TOOLS:
        if holds_reads and t["name"] in _MIRROR_DUPLICATES:
            continue
        if holds_acts and t["name"] in _MIRROR_WRITE_DUPLICATES:
            continue
        g = _TOOL_GATES.get(t["name"])
        if g:
            if g.get("pro") and not pro:
                continue
            if g.get("engine") and not getattr(runner, "ma", None):
                continue
            d = g.get("domain")
            if d and d not in doms:
                continue
        out.append(t)
    # THE MIRROR'S TOOLS (Dave, 16 Aug 2026: "our Assist should be using
    # the HA MCP, same as you — it should be a mirror"). His tier ruling,
    # decided by the SERVER'S OWN read-only testimony, never a name rule:
    # the Developer (owner) receives everything; installer and tech
    # receive the tools the server marks read-only (the diagnosing power);
    # a homeowner receives none — the railed ProOS tools above are their
    # surface. A tool with NO annotation is an act, fail-closed. Offered
    # here AND enforced again at dispatch (_run_mcp).
    # DAVE'S RULING B (18 Aug 2026): the platform's tools are the MAIN
    # surface; ProOS adds what only ProOS knows. The tier line follows the
    # SERVER'S OWN TESTIMONY — never a list of tool names written by us:
    #   Developer (owner) — everything.
    #   installer / tech  — every READ, plus the acts the server itself
    #                       marks NON-destructive. An act whose
    #                       destructiveness the server does not state is
    #                       treated as destructive and withheld (fail
    #                       closed). If the server states nothing at all,
    #                       this lands exactly where it stood before —
    #                       reads only — and the boot census SAYS so.
    #   homeowner         — none; the railed ProOS tools are their surface.
    have = {t["name"] for t in out}
    out.extend({"name": t["name"], "description": t["description"],
                "input_schema": t["input_schema"]}
               for t in mirror if t.get("name") and t["name"] not in have)
    return out

_ATTR_KEYS = ("friendly_name", "brightness", "volume_level", "source", "media_title",
              "app_name", "current_temperature", "temperature", "hvac_mode",
              "current_position", "device_class", "supported_color_modes",
              "supported_features", "hvac_modes",
              # THE READING WAS WITHHELD, NOT THE CAPABILITY (Dave, 16 Aug
              # 2026: "go look for an alternative — you're writing rules
              # again"). select_source was always an allowed act, but the
              # model could not SEE a speaker's own source list (where a
              # Sonos publishes its favourites), so the intelligence had
              # nothing to find and the coded music path looked like the
              # only door. The device's own report, visible; no lane, no
              # platform named. (Capped in _slim_state — a cut is said.)
              "source_list",
              # Colour: without these, verify compared every colour against
              # None — lamps turned blue while the assistant reported failure
              # and flagged the installer for a problem that didn't exist.
              "rgb_color", "hs_color", "color_temp", "color_temp_kelvin",
              "color_mode")


def _attr_close(key, got, want) -> bool:
    """Attribute equality with the tolerance devices actually have. Integrations
    quantise AND convert: ask for brightness 77 and a lamp reports 76; ask for
    rgb [0,0,255] and a lamp that thinks in hue/saturation reports back
    [0,0,254]. Exact matching turns working devices into reported failures —
    the lamp that turned blue instantly while the assistant apologised and
    flagged the installer."""
    if got == want:
        return True
    # Colour lists/tuples (rgb_color, hs_color, xy_color): element-wise with
    # per-channel tolerance — colour round-trips through the device's native
    # colour space and comes back a whisker off.
    if isinstance(got, (list, tuple)) and isinstance(want, (list, tuple)):
        if len(got) != len(want):
            return False
        try:
            return all(abs(float(a) - float(b)) <= 5 for a, b in zip(got, want))
        except (TypeError, ValueError):
            return list(got) == list(want)
    try:
        g, w = float(got), float(want)
    except (TypeError, ValueError):
        return False
    if key == "brightness":
        return abs(g - w) <= 5
    if 0 <= w <= 1:
        return abs(g - w) <= 0.02          # unit floats (volume_level, position)
    return abs(g - w) <= 1                 # temperatures, color_temp, percentages


def _light_caps(attrs: dict) -> dict:
    """What a light can actually do, from supported_color_modes. A light whose
    ONLY mode is onoff can't dim — so the assistant must not promise brightness."""
    modes = [str(m).lower() for m in (attrs.get("supported_color_modes") or [])]
    dimmable = any(m not in ("onoff", "unknown") for m in modes) if modes else False
    color = any(m in ("hs", "rgb", "rgbw", "rgbww", "xy") for m in modes)
    color_temp = "color_temp" in modes
    return {"dimmable": dimmable, "color": color, "color_temp": color_temp}

# ── scene music (moments) ────────────────────────────────────────────────────
# An HA scene can only RESTORE states — it cannot start playback. But "dinner
# scene" MEANS warm lights AND the jazz starting. So a scene ProOS Assist
# creates can carry a music companion in a sidecar here, and applying the scene
# — from chat OR a dashboard tap routed through Core — fires both. The scene
# stays a clean HA scene; the moment lives at the ProOS layer.
_SCENE_MUSIC_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"),
                                 "assist_scene_music.json")


def _scene_music_load() -> dict:
    try:
        with open(_SCENE_MUSIC_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _scene_music_save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_SCENE_MUSIC_PATH), exist_ok=True)
        tmp = _SCENE_MUSIC_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, _SCENE_MUSIC_PATH)
    except Exception:
        pass


def _resolve_activity_script(client, project_mod, rec) -> str:
    """The CURRENT script for a stored activity companion, resolved at fire
    time from identity — the source's entity_id + area — never trusted from a
    cached script name. Script object_ids embed the source's LABEL slug, and
    labels are modifiable: an installer renames 'Apple TV' to 'ATV 4K',
    regenerates, and a stored script id points at nothing. The source's
    entity_id is the identity the standard allows, and every generated script
    carries it in variables.proos_source — so we look the script up fresh each
    time. The cached script id is only a last-resort fallback."""
    src = (rec.get("activity_source") or "").strip()
    area = (rec.get("activity_area") or "").strip()
    if src and area and project_mod is not None:
        try:
            proj = project_mod.load()
            acts = (project_mod.activities_status(client, proj, area) or {}).get("activities") or []
            for a in acts:
                if a.get("source_eid") == src and a.get("entity_id"):
                    return a["entity_id"]
        except Exception:
            pass
    return (rec.get("activity_script") or "").strip()


def apply_scene(client, ws_call, project_mod, ma, scene_entity_id: str,
                user: dict | None = None) -> dict:
    """Apply a scene AND its companions. The single apply path: the chat tool
    and the dashboard's scene tap both land here, so a moment behaves
    identically wherever it's triggered.

    A moment can carry an ACTIVITY (the room's generated watch script — TV on,
    right input, proper ordering) and MUSIC. Order matters: the activity fires
    first because AV takes seconds, lights land instantly on top, music last."""
    eid = (scene_entity_id or "").strip()
    if not eid.startswith("scene."):
        return {"error": "scene_entity_id required"}
    out = {"ok": True, "applied": eid}
    rec = _scene_music_load().get(eid) or {}
    act = _resolve_activity_script(client, project_mod, rec)
    if act.startswith("script."):
        try:
            client._req("POST", "/api/services/script/turn_on", {"entity_id": act})
            out["activity"] = act
        except Exception as e:                                   # noqa: BLE001
            out["activity"] = "failed — %s" % e
    client._req("POST", "/api/services/scene/turn_on", {"entity_id": eid})
    if not rec.get("query"):
        return out
    # NO RESOLVER LIVES HERE (deleted 16 Aug 2026, register 222). Yesterday
    # I put one in: it matched the asked-for station against each committed
    # speaker's source_list. Dave's Sonos publishes only ["Line-in", "TV"] —
    # the station it plays is a content id — so that code could never have
    # fired on his box, and its bench passed only because I wrote the fake
    # speaker to contain what my rule needed. A rule, and a wrong one.
    # THE PROVEN THING IS A SCRIPT: whoever builds the moment reads the
    # devices, writes the script, RUNS it and reads the result back — then
    # attaches it as the scene's activity companion (fired above). A tile
    # tap then replays something already proven, with nothing here guessing.
    if not ma:
        out["music"] = ("not played — no music engine is linked. A music "
                        "moment should be a proven script attached as the "
                        "scene's activity (build it, run it, read it back), "
                        "not a phrase resolved at tap time.")
        return out
    runner = ToolRunner(client, ws_call, project_mod, user or {}, ma=ma)
    try:
        uri = (rec.get("uri") or "").strip()
        if not uri:
            found = ma.search((rec.get("query") or "").strip(), limit=3) or {}
            for kind in ("playlists", "radio", "albums", "tracks"):
                items = found.get(kind) or []
                if items and items[0].get("uri"):
                    uri = items[0]["uri"]
                    break
        spk = runner._room_ma_speaker(rec.get("area_id") or "")
        if not (uri and spk):
            out["music"] = "skipped — no %s" % ("music found" if spk else "speaker in that room")
            return out
        vol = rec.get("volume")
        if isinstance(vol, (int, float)) and 0 < vol <= 100:
            try:
                client._req("POST", "/api/services/media_player/volume_set",
                            {"entity_id": spk, "volume_level": round(vol / 100.0, 2)})
            except Exception:
                pass
        client._req("POST", "/api/services/music_assistant/play_media",
                    {"entity_id": spk, "media_id": uri, "enqueue": "play"})
        out["music"] = {"playing": rec.get("query") or uri, "speaker": spk}
    except Exception as e:                                       # noqa: BLE001
        out["music"] = "failed — %s" % e
    return out


# ── pinned memory (phase 2) ──────────────────────────────────────────────────
# Long-term facts the assistant should remember about a user ("Dave likes jazz
# at dinner", "kids' bedtime is 8pm"). Per-user, in a small JSON store; cleared
# by factory reset with the rest of the assist data.
_MEM_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "assist_memory.json")
_MEM_MAX = 40   # facts per user (oldest dropped)


def _mem_load() -> dict:
    try:
        with open(_MEM_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _mem_save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_MEM_PATH), exist_ok=True)
        tmp = _MEM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, _MEM_PATH)
    except Exception:
        pass


def _media_map(client):
    """entity_id -> what each media player IS, read LIVE from the home's
    own registries (C2 slice 1, 23 Aug 2026, reg 261). For every enabled,
    visible media_player: the room it sits in (its own assignment, else
    its device's), the device it belongs to, and — when its DEVICE also
    carries a camera — kind "camera": that speaker is the camera's own,
    a physical read from the registry, never a guess from a name.

    The engine's twin players (music_assistant) are skipped: an engine
    lane of a real speaker is not a device in the home — the same line
    the homeowner app draws. Blind is not broken: any failure returns {}
    and the caller claims nothing."""
    try:
        ents = client.entity_registry() or []
        devs = client.device_registry() or []
    except Exception:  # noqa: BLE001
        return {}
    if not ents or not devs:
        return {}
    dev_area = {d.get("id"): d.get("area_id") for d in devs}
    dev_name = {d.get("id"): (d.get("name_by_user") or d.get("name"))
                for d in devs}
    cam_devs = {e.get("device_id") for e in ents
                if str(e.get("entity_id") or "").startswith("camera.")
                and e.get("device_id")}
    out = {}
    for e in ents:
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("media_player."):
            continue
        if e.get("disabled_by") or e.get("hidden_by"):
            continue
        if e.get("platform") == "music_assistant":
            continue
        did = e.get("device_id")
        row = {"area_id": e.get("area_id") or dev_area.get(did),
               "name": e.get("name") or e.get("original_name") or eid}
        if dev_name.get(did):
            row["device"] = dev_name[did]
        if did in cam_devs:
            row["kind"] = "camera"
        out[eid] = row
    return out


def _tier(user: dict) -> str:
    """Permission tier from the HA user on the request (§4 rails)."""
    u = user or {}
    if u.get("tech"):
        return "tech"
    if u.get("is_owner"):
        return "owner"
    if u.get("is_admin"):
        return "installer"
    return "homeowner"


def _is_pro(user: dict) -> bool:
    return _tier(user) in ("tech", "owner", "installer")


_SOURCE_LIST_CAP = 40    # a Sonos can hold dozens of favourites; the cut is SAID


def _slim_state(snap_val) -> dict:
    st = (snap_val or {})
    a = st.get("attributes") or {}
    # AN ENTITY THAT DOES NOT EXIST IS NOT A BROKEN DEVICE (16 Aug 2026,
    # read live on Dave's box: Assist called a PAUSED Sonos "unavailable"
    # from an id the house never had — one word doing two jobs). The model
    # is the audience that was misled, so its own shaper says the fact in
    # words and never the word that means broken.
    if st.get("missing"):
        return {"state": None, "missing": True,
                "note": "no such entity on this box — the id is wrong or "
                        "stale; find the real one before saying anything "
                        "about it"}
    out = {"state": st.get("state"),
           "attributes": {k: a.get(k) for k in _ATTR_KEYS if a.get(k) is not None}}
    sl = out["attributes"].get("source_list")
    if isinstance(sl, list) and len(sl) > _SOURCE_LIST_CAP:
        out["attributes"]["source_list"] = (
            sl[:_SOURCE_LIST_CAP]
            + ["… (+%d more not shown)" % (len(sl) - _SOURCE_LIST_CAP)])
    # FRIENDLY WORDS BY CONSTRUCTION (Dave, 13 Aug: "friendly names across the
    # board, not entities — when Assist is communicating it is also using
    # friendly words and can't leak by mistake"). Every state payload flows
    # through here — get_states, rooms_overview members, verify's actuals,
    # the area_entities fallback — so promoting the NAME to a first-class
    # field beside the state puts the speakable word in the model's hand at
    # every surface at once, one mechanism. Never invented: a device with no
    # friendly_name simply carries none.
    if a.get("friendly_name"):
        out["name"] = a.get("friendly_name")
    return out


class ToolRunner:
    """Executes tool calls for one chat turn as one authenticated caller."""

    def __init__(self, client, ws_call, project_mod, user: dict, ma=None,
                 awareness=None, mcp=None):
        self.client = client
        self.ws_call = ws_call
        self.project = project_mod
        self.ma = ma               # MaCommissioner (music tools); None if unlinked
        self.mcp = mcp             # HaMcp — the mirror's toolset; None = as before
        self._mcp_cache = None
        # Bridge to the awareness layer: dict of callables supplied by server.py
        # (watchers report, monitor, per-room health, audit trail, recover).
        # The whole point of ProAssist is that it answers from these VERDICTS —
        # a "Pro in the box" who has actually looked, not one who guesses.
        self.awareness = awareness or {}
        self.user = user or {}
        self.actions = []          # audit of every side-effect this turn
        self.trace = []            # EVERY tool call this turn — reads and
                                   # acts alike (Dave, 16 Aug: "how do I
                                   # know it's checking?"). Checking is
                                   # evidence; it is recorded like evidence.

    def _audit(self, tool, **info):
        rec = {"tool": tool, **info}
        self.actions.append(rec)
        # Same log stream the watcher uses, so every side-effect is traceable.
        print("  [assist] %s %s by %s(%s)" % (
            tool, {k: v for k, v in info.items() if k != 'result'},
            self.user.get("name") or "?", _tier(self.user)), flush=True)

    # -- helpers ------------------------------------------------------------
    def home_domains(self) -> set:
        """Entity domains present in this home — the capability scan that
        decides which tools exist this turn. One registry read, cached on the
        runner (one turn = one world)."""
        if getattr(self, "_domains", None) is not None:
            return self._domains
        doms = set()
        try:
            for e in (self.client.entity_registry() or []):
                eid = e.get("entity_id") or ""
                if "." in eid and not e.get("disabled_by"):
                    doms.add(eid.split(".", 1)[0])
        except Exception:
            pass
        self._domains = doms
        return doms

    def _members(self, proj) -> set:
        out = set()
        for rec in (proj or {}).get("areas", {}).values():
            if rec and rec.get("committed"):
                for e in ([rec.get("display")] + list(rec.get("sources") or [])
                          + list(rec.get("audio") or [])):
                    if e:
                        out.add(e)
        return out

    def _mcp_list(self):
        """The mirror's tools — the SERVER'S own list with its own
        read-only testimony — or [] when the mirror isn't connected.
        Fail-open: Assist without the mirror is Assist as before."""
        if self.mcp is None:
            return []
        if self._mcp_cache is None:
            try:
                self._mcp_cache = self.mcp.tools() or []
            except Exception:                                    # noqa: BLE001
                self._mcp_cache = []
        return self._mcp_cache

    def _run_mcp(self, tool, args):
        """One mirror call, under the SAME tier law the offer used —
        enforced AGAIN at dispatch, so a tool name arriving out of thin
        air obeys Dave's ruling too: reads for the trade, acts for the
        Developer alone, and an unannotated tool is an act (fail-closed).
        Acts are audited like ProOS acts; everything is traced."""
        if not _is_pro(self.user):
            out = {"error": "that tool is not available at this access level"}
        elif (not tool.get("read_only") and tool.get("destructive")
                and not self.user.get("is_owner")):
            out = {"error": "that tool can DESTROY or disrupt (the platform "
                            "does not vouch for it as safe) — Developer only"}
        else:
            if not tool.get("read_only"):
                self._audit(tool["name"], mcp=True)
            try:
                out = self.mcp.call(tool["name"], args or {})
            except Exception as e:                               # noqa: BLE001
                out = {"error": str(e)[:300]}
        t = {"tool": tool["name"],
             "kind": "read" if tool.get("read_only") else "act"}
        tgt = ((args or {}).get("entity_id") or (args or {}).get("area_id"))
        if tgt:
            t["target"] = str(tgt)
        if isinstance(out, dict) and out.get("error"):
            t["error"] = True
        self.trace.append(t)
        return out

    def run(self, name: str, args: dict):
        fn = getattr(self, "t_" + name, None)
        if not fn:
            m = next((t for t in self._mcp_list()
                      if t.get("name") == name), None)
            if m is not None:
                return self._run_mcp(m, args)
            return {"error": "unknown tool %s" % name}
        # THE ONE DISPATCH TRACES EVERY CALL (16 Aug 2026 — Dave: "it says
        # the bedroom is reporting healthy, but how do I know it's
        # checking?"). Both providers ride this method, so one record
        # covers them: reads and acts alike, told apart by whether the
        # tool wrote to the side-effect audit, failures carried too. The
        # answer to the model is EXACTLY what it was before.
        before = len(self.actions)
        try:
            out = fn(args or {})
        except Exception as e:  # noqa: BLE001 - the model must see failures, not stack traces
            out = {"error": str(e)}
        t = {"tool": name,
             "kind": "act" if len(self.actions) > before else "read"}
        tgt = ((args or {}).get("entity_id") or (args or {}).get("area_id")
               or (args or {}).get("area"))
        if tgt:
            t["target"] = str(tgt)
        if isinstance(out, dict) and out.get("error"):
            t["error"] = True
        self.trace.append(t)
        return out

    # -- tools --------------------------------------------------------------
    def t_rooms_overview(self, args):
        proj = self.project.load()
        areas = {a.get("area_id"): a.get("name") for a in (self.client.area_registry() or [])}
        # C2 SLICE 1 (23 Aug 2026, reg 261). Dave's box holds
        # media_player.office_speaker — the G3 Instant CAMERA's own
        # speaker, unifiprotect, in NO room. A media_player like the
        # Sonos, found by NAME, and no served answer said what it was
        # FOR — so "what speakers are in the office" could list it as
        # one. Dave refused the rename: "it should be able to TELL it's
        # not a music speaker." Register 246's law again: the reading is
        # handed over, never withheld. What each player IS comes from the
        # home's own registries, live (its device carries a camera);
        # what it is FOR is the committed roles. Both ride below.
        _mm = _media_map(self.client)
        rooms, ents = [], []
        for key, rec in (proj or {}).get("areas", {}).items():
            if not rec:
                continue
            members = []
            # A2/A3 parity with room_devices (B4, 3 Aug): legacy
            # speakers[] and tvaudio are committed members too; first
            # role wins on duplicates (A1).
            for role, ids in (("display", [rec.get("display")]),
                              ("source", rec.get("sources") or []),
                              ("speaker", rec.get("audio") or []),
                              ("speaker", rec.get("speakers") or []),
                              ("tvaudio", [rec.get("tvaudio")])):
                for e in ids:
                    if e and all(m["entity_id"] != e for m in members):
                        m2 = {"entity_id": e, "role": role}
                        # WHAT THE DEVICE ACTUALLY IS (19 Aug 2026, reg 246).
                        # Dave's homeowner app, tonight: "The Office has two
                        # HomePod speakers set up" — one of them is his SONOS.
                        # When he said so, Assist did not go and look; it
                        # agreed with him and invented "HomePod-like
                        # characteristics" to explain the clash.
                        #
                        # It was never told. This tool handed over entity ids
                        # and roles and nothing else, so the only thing left
                        # to go on was a friendly name — and the OTHER
                        # speaker in that room is literally called "HomePod".
                        #
                        # ProOS HAS the answer and was sitting on it: the
                        # committed record's meta carries the integration it
                        # certified. A plain Claude session with the
                        # platform's MCP answered this correctly first time
                        # because ha_get_entity told it `platform: sonos`.
                        # Same reading, handed over instead of withheld.
                        # A HOMEOWNER holds no platform tools at all, so if
                        # ProOS does not say it, nobody can.
                        _ig = ((rec.get("meta") or {}).get(e) or {}).get("integration")
                        if _ig:
                            m2["integration"] = _ig
                        members.append(m2)
                        ents.append(e)
            aid = rec.get("area_id") or key
            room = {"area_id": aid,
                    "name": areas.get(aid) or rec.get("name") or key,
                    "kind": rec.get("kind"),
                    "committed": bool(rec.get("committed")),
                    "members": members,
                    "activities": []}
            # The room's committed OFF policy (Dave, 1 Aug: Assist called a
            # Frame resting in Art Mode a failed power-off). off_state 'art'
            # means the display RESTS ON ARTWORK when the room is off — that
            # IS off; judge by the room's verdict sensor, never raw TV state.
            offs = rec.get("off_state")
            if offs:
                room["off_state"] = offs
                if offs == "art":
                    room["off_note"] = ("display rests in Art Mode when off — "
                                        "artwork showing IS off; judge by the "
                                        "verdict sensor, not the TV state, and "
                                        "report it simply as Off: this is the "
                                        "room's configured behaviour, so never "
                                        "mention artwork or Art Mode unless "
                                        "the user asks")
            room["verdict_sensor"] = "sensor.proos_activity_%s" % aid
            ents.append(room["verdict_sensor"])
            # The room's OTHER media players: in the room by membership
            # (HA's assignment IS the room), outside the committed roles.
            # Served with what they ARE so nothing is left to guess —
            # and never doubled with a member.
            if _mm:
                _other = [dict(_mm[e], entity_id=e) for e in sorted(_mm)
                          if _mm[e].get("area_id") == aid
                          and all(m["entity_id"] != e for m in members)]
                for _o in _other:
                    _o.pop("area_id", None)
                if _other:
                    room["other_media"] = _other
            rooms.append(room)
        # live states for members (one snapshot), activities from stored scripts
        snap = self.client.snapshot(ents) if ents else None
        for r in rooms:
            sv = snap.get(r["verdict_sensor"]) if snap else None
            st = sv if isinstance(sv, dict) else (getattr(sv, "__dict__", {}) or {})
            if st and st.get("state") not in (None, "", "unavailable"):
                _va = st.get("attributes") or {}
                # external + devices{} ride along (B4): Assist must know a
                # lit room is externally driven and WHICH devices are lit,
                # or its diagnosis contradicts what the panel says.
                # TOKEN BUDGET (Dave, 4 Aug: 429s — "Limit 30000 TPM,
                # Requested 4715"). devices{} repeated every member's live
                # state a second time, and the served sentence already says
                # what it meant; members[] below still carries the detail.
                # Every tool round resends this payload, so bloat here costs
                # ~8× per conversation.
                r["verdict"] = {"state": st.get("state"),
                                "label": _va.get("label"),
                                "verified": _va.get("verified"),
                                # THE BASIS REACHES ASSIST TOO (19 Aug 2026,
                                # register 238). Assist was handed the WORD
                                # "verified" and nothing else, so if a client
                                # asked "are you sure?" the only honest answer
                                # available to it was a bare yes. It can now
                                # say what the reading was.
                                #
                                # CARRIED ONLY WHEN THE CLAIM IS MADE — the
                                # key is OMITTED otherwise (see below): an
                                # unverified verdict has no basis to give, and
                                # this payload is resent on EVERY tool round
                                # (the token note below), so a null key would
                                # be paid for eight times to say nothing.
                                "external": _va.get("external"),
                                # Dave, 3 Aug: the on-glass summary IS
                                # Assist's answer — same served words.
                                "sentence": _va.get("sentence"),
                                "env_line": _va.get("env_line")}
                if _va.get("verified") and _va.get("verified_by"):
                    r["verdict"]["verified_by"] = _va["verified_by"]
        for r in rooms:
            for m in r["members"]:
                sv = snap.get(m["entity_id"]) if snap else None
                m.update(_slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {}))
            if r["committed"]:
                try:
                    st = self.project.activities_status(self.client, self.project.load(), r["area_id"])
                    r["activities"] = [{"script_entity_id": a.get("entity_id") or ("script." + a.get("object_id", "")),
                                        "name": a.get("alias") or a.get("kind"),
                                        "kind": a.get("kind"),
                                        # the source device this activity watches —
                                        # the IDENTITY to pin companions to
                                        "source_eid": a.get("source_eid")}
                                       for a in (st.get("activities") or [])
                                       if a.get("entity_id") or a.get("object_id")]
                except Exception:
                    r["activities"] = []
            # Music availability is INDEPENDENT of AV commissioning — a room can
            # play music (MA speaker) without a committed AV display. Surface it
            # so the model knows where music_play will work.
            if self.ma:
                try:
                    spk = self._room_ma_speaker(r["area_id"])
                    if spk:
                        # 262→509 (23 Aug 2026, Dave's ruling): this is
                        # the music ENGINE's handle on the room — the
                        # same physical box as a committed speaker,
                        # reached through the engine — and it used to be
                        # served as "music_speaker". A SINGULAR field
                        # wearing the word "speaker" read as "the room's
                        # one real speaker", and the model demoted the
                        # committed HomePod on Dave's glass because of
                        # it. The fix is the NAME, not a caption: the
                        # word "speaker" stays reserved for the
                        # commissioned members.
                        r["music_engine_player"] = spk
                except Exception:
                    pass
        note = "entity ids and area ids are identity — names are display only."
        # Only advertise the engine's lane when the engine is actually
        # here (18 Aug 2026). Unlinked, this line sent the model to a
        # tool it did not have and a dead end it then read out loud.
        if self.ma:
            note += (" music_play works in any room with a"
                     " music_engine_player — the music engine's own handle"
                     " on that room, not an extra speaker.")
        out = {"rooms": rooms, "note": note}
        if _mm:
            # Players sitting in NO room at all — the Office Speaker's
            # actual case on Dave's box: the camera's own speaker,
            # unassigned, findable only by name. Named here so the model
            # never has to guess what a name-matched player is.
            _nowhere = [dict(_mm[e], entity_id=e) for e in sorted(_mm)
                        if not _mm[e].get("area_id")]
            for _n in _nowhere:
                _n.pop("area_id", None)
            if _nowhere:
                out["media_in_no_room"] = _nowhere
            note += (" other_media and media_in_no_room are the home's media"
                     " players OUTSIDE the committed roles — kind 'camera'"
                     " means a camera's OWN speaker, never a music speaker;"
                     " media_in_no_room sit in NO room.")
            out["note"] = note
        return out

    def t_room_activity(self, args):
        eid = (args.get("script_entity_id") or "").strip()
        if not re.match(r"^script\.proos_[a-z0-9_]+$", eid):
            return {"error": "not a ProOS activity script: %s" % eid}
        self.client.call_service("script", "turn_on", eid)
        self.actions.append({"tool": "room_activity", "target": eid})
        return {"ok": True, "fired": eid,
                "next": "verify the outcome with the verify tool before reporting success"}

    def _room_display(self, area_id):
        """The room's committed display media_player (the TV/AV output), from the
        AV project — the thing an app like Netflix opens on."""
        try:
            proj = self.project.load()
            for key, rec in (proj or {}).get("areas", {}).items():
                if not rec:
                    continue
                if (rec.get("area_id") or key) == area_id:
                    d = rec.get("display")
                    return d if isinstance(d, str) and d.startswith("media_player.") else None
        except Exception:  # noqa: BLE001
            pass
        return None

    def t_app_launch(self, args):
        """Open a named app on a room's app-capable devices (the smart TV, an
        Apple TV, a Shield…). A room can have several — when more than one offers
        the app you'll get needs_choice with the options; ASK the user which,
        then call again with `device` set to their pick. Honest: if the app isn't
        available anywhere we return what IS. The display should already be ON
        (run the room's watch activity first)."""
        aid = self._resolve_area_id(args.get("area_id"))
        app = (args.get("app") or "").strip()
        if not aid or not app:
            return {"error": "area_id and app required"}
        try:
            from . import appctl
        except Exception as e:  # noqa: BLE001
            return {"error": "app launch unavailable: %s" % e}
        res = appctl.launch(self.client, self.project, aid, app, device=(args.get("device") or "").strip() or None)
        if res.get("ok"):
            self._audit("app_launch", app=res.get("launched"), device=res.get("device"), area=aid)
            # The Bedroom bug (12 Aug, register 109): the launch WORKED, the
            # verdict sensor said 'watching Apple TV' — and Assist reported a
            # failure because the streamer's driver sat at 'idle' (it cannot
            # see app playback). The result must carry the fact the model
            # needs at the moment it needs it: which witness to believe.
            res["verify_by"] = "sensor.proos_activity_%s" % aid
            res["note"] = ("launched. Judge the outcome by the room's verdict sensor "
                           "(verify_by above) — this device's raw state may sit at 'idle' "
                           "or 'off' while the app plays, because many streamer drivers "
                           "cannot see what is on screen. Raw state here is NOT evidence "
                           "of failure. If the verdict says the room is watching this "
                           "device, it worked; if you still doubt, ask the user what is "
                           "on the screen — their answer settles it.")
        return res

    def t_device_control(self, args):
        eid = (args.get("entity_id") or "").strip()
        action = (args.get("action") or "").strip()
        data = args.get("data") or {}
        if "." not in eid:
            return {"error": "entity_id required"}
        domain = eid.split(".", 1)[0]
        if action not in _DEVICE_ACTIONS:
            return {"error": "action '%s' not allowed" % action}
        if domain == "media_player" and action in _MEDIA_POWER:
            return {"error": "media power is choreographed per room — use room_activity "
                             "(rooms_overview lists each room's activities)"}
        if domain == "media_player":
            proj = self.project.load()
            if eid not in self._members(proj):
                return {"error": "%s is not a committed member of any room — not controllable" % eid}
        payload = {"entity_id": eid}
        payload.update({k: v for k, v in data.items() if k != "entity_id"})
        self.client._req("POST", "/api/services/%s/%s" % (domain, action), payload)
        self.actions.append({"tool": "device_control", "target": eid, "action": action})
        return {"ok": True, "called": "%s.%s" % (domain, action), "entity_id": eid}

    def t_area_control(self, args):
        aid = (args.get("area_id") or "").strip()
        domain = (args.get("domain") or "").strip()
        action = (args.get("action") or "").strip()
        data = args.get("data") or {}
        allowed = _AREA_DOMAINS.get(domain)
        if not aid:
            return {"error": "area_id required"}
        if not allowed:
            return {"error": "domain '%s' not allowed for area control" % domain}
        if action not in allowed:
            return {"error": "action '%s' not allowed for %s" % (action, domain)}
        # COUNT WHAT IS ACTUALLY THERE BEFORE CLAIMING TO HAVE TOUCHED IT.
        # Home Assistant answers 200 for a service call that matches ZERO
        # entities — a successful no-op. This tool used to relay that as
        # {"ok": True}, so Assist told Dave "the lights in the office are now
        # turned off" in a room that has no lights (12 Aug, register 105).
        # The tool was telling the truth about the HTTP call and a lie about
        # the house. Nothing to act on is not a success; it is an answer.
        #
        # `roomdevices.available()` is the SAME source `area_entities` reads —
        # one mechanism for "what is in this room", per the product's own law.
        area_name = aid
        try:
            for a in (self.client.area_registry() or []):
                if a.get("area_id") == aid:
                    area_name = a.get("name") or aid
                    break
        except Exception:                                        # noqa: BLE001
            pass
        targets = None
        try:
            from . import roomdevices
            avail = roomdevices.available(self.client, aid)
            if avail is not None:
                targets = [d for d in avail if d.get("domain") == domain]
        except Exception:                                        # noqa: BLE001
            targets = None                                       # unknown, not zero
        if targets is not None and not targets:
            # Say the true thing, and say it in the words a person would use.
            return {"ok": False, "affected": 0, "area_id": aid,
                    "area_name": area_name, "domain": domain,
                    "error": "there are no %ss in the %s, so there was nothing "
                             "to %s — tell them that rather than reporting it done"
                             % (domain, area_name, action.replace("_", " "))}
        payload = {"area_id": aid}
        payload.update({k: v for k, v in data.items() if k not in ("entity_id", "area_id")})
        self.client._req("POST", "/api/services/%s/%s" % (domain, action), payload)
        n = len(targets) if targets is not None else None
        self.actions.append({"tool": "area_control", "target": aid,
                             "action": "%s.%s" % (domain, action),
                             "affected": n})
        out = {"ok": True, "called": "%s.%s" % (domain, action), "area_id": aid,
               "area_name": area_name}
        if n is not None:
            out["affected"] = n
            out["targets"] = [d.get("name") or d.get("entity_id") for d in targets]
        else:
            # STAGE 6 BUILD 4 (16 Aug 2026): the register-105 hole. When the
            # room-contents read itself failed, this fell through to a plain
            # ok:true — the exact "done in a room that may hold nothing" lie
            # the 105 fix was built to stop, running again whenever the
            # evidence was missing. The ACTION stays fail-open (a broken
            # registry read must never block control), but the ANSWER now
            # carries the unknowing (assist_area_honesty_bench.py).
            out["unverified"] = True
            out["note"] = ("ProOS could not read what is in this room, so it "
                           "cannot confirm anything was actually affected. "
                           "Report the command as SENT — not as done — unless "
                           "verify or the user confirms the change.")
        return out

    # ── room_off / room_on: deterministic whole-room power (spec, 1 Aug) ────
    # "Room off means room off everything" (Dave) — one tool, same result
    # every time, instead of the model composing its own interpretation.
    # The installer's power-protect list (roomdevices overlay power_exclude)
    # is honoured: protected devices are skipped by BULK power but remain
    # individually controllable through device_control.
    @staticmethod
    def _stop_targets(rec) -> list:
        """True standalone speakers of a room — the ONLY entities room_off may
        media_stop. The record's audio bucket can hold the DISPLAY itself (a
        TV-audio room) and video endpoints, and a media command at a display
        WAKES it — live 1 Aug: room_off's media_stop hit the Family Room
        Frame right after tv_off and pulled it back out of Art Mode. Same
        law as sibling-rest (1.0.270): power/transport never crosses roles.
        Pure; benched."""
        if not isinstance(rec, dict):
            return []
        never = {rec.get("display"), rec.get("tvaudio")}
        for s in (rec.get("sources") or []):
            e = s.get("entity") if isinstance(s, dict) else s
            never.add(e)
        out = []
        for b in ("speakers", "audio"):
            for item in (rec.get(b) or []):
                e = item.get("entity") if isinstance(item, dict) else item
                if isinstance(e, str) and e and e not in never and e not in out:
                    out.append(e)
        return out

    def _room_power(self, args, on: bool):
        aid = self._resolve_area_id(args.get("area_id"))
        if not aid:
            return {"error": "area_id required"}
        did = {"area_id": aid}
        # 1 · AV, off only: the room's TV-off activity owns the choreography
        #     (and its off_state — an 'art' room rests on artwork; that IS off).
        if not on:
            try:
                st = self.project.activities_status(self.client,
                                                    self.project.load(), aid)
                tvoff = next((a for a in (st.get("activities") or [])
                              if a.get("key") == "tv_off"), None)
                if tvoff and tvoff.get("entity_id"):
                    self.client.call_service("script", "turn_on",
                                             tvoff["entity_id"])
                    did["av"] = tvoff["entity_id"]
            except Exception:                                    # noqa: BLE001
                pass
            # 2 · stop the room's committed speakers (presentation off; a
            #     mains speaker has no power to cut)
            try:
                rec = next((r for r in (self.project.load().get("areas") or {})
                            .values()
                            if (r or {}).get("area_id") == aid), None) or {}
                spk = self._stop_targets(rec)
                for e in spk:
                    try:
                        self.client.call_service("media_player", "media_stop", e)
                    except Exception:                            # noqa: BLE001
                        pass
                if spk:
                    did["stopped_speakers"] = spk
            except Exception:                                    # noqa: BLE001
                pass
        # 3 · bulk power for lights/switches/fans, minus the protect list
        try:
            from . import roomdevices
            plan = roomdevices.power_targets(
                roomdevices.discover(self.client, aid).get("devices"))
        except Exception:                                        # noqa: BLE001
            plan = {"targets": {}, "skipped": 0}
        svc = "turn_on" if on else "turn_off"
        for dom, eids in (plan.get("targets") or {}).items():
            for e in eids:
                try:
                    self.client.call_service(dom, svc, e)
                except Exception:                                # noqa: BLE001
                    pass
            did[dom] = len(eids)
        if plan.get("skipped"):
            did["power_protected_skipped"] = plan["skipped"]
        self.actions.append({"tool": "room_off" if not on else "room_on",
                             "target": aid, "action": svc})
        did["ok"] = True
        did["note"] = ("verify with the room's verdict sensor; power-protected "
                       "devices were deliberately left untouched")
        return did

    def t_room_off(self, args):
        return self._room_power(args, on=False)

    def t_room_on(self, args):
        return self._room_power(args, on=True)

    def t_health_incidents(self, args):
        try:
            from . import healthmon as _hm
            out = []
            for i in (_hm.incidents() or []):
                out.append({k: i.get(k) for k in
                            ("id", "kind", "severity", "room", "title",
                             "cause", "subject", "since")})
            return {"incidents": out, "count": len(out)}
        except Exception as e:                                   # noqa: BLE001
            return {"incidents": [], "count": 0, "error": str(e)}

    def t_device_powerlog(self, args):
        eid = (args.get("entity_id") or "").strip()
        if not eid:
            return {"error": "entity_id required"}
        try:
            hours = min(float(args.get("hours") or 48), 240.0)
        except Exception:                                        # noqa: BLE001
            hours = 48.0
        try:
            from . import powerlog as _plog
            return _plog.fetch_log(self.client, self.project.load, eid, hours)
        except Exception as e:                                   # noqa: BLE001
            return {"error": str(e)}

    def t_get_states(self, args):
        ids = [e for e in (args.get("entity_ids") or []) if isinstance(e, str)][:40]
        if not ids:
            return {"error": "entity_ids required"}
        snap = self.client.snapshot(ids)
        out = {}
        for e in ids:
            sv = snap.get(e)
            out[e] = _slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {})
        return {"states": out}

    def t_room_status(self, args):
        """The room's live AV truth for a status question or a volume command —
        the CONFIRM tool (Assist Redesign A1, 6 Aug). It resolves the room's
        ACTIVE volume endpoint the same way control does (_room_vol_targets:
        the verdict's playing speaker, or the TV-audio owner when watching) and
        reads that endpoint's REAL volume + mute — so the agent answers from
        what IS, never from a stale assumption ('already muted' on the wrong
        speaker, Dave 6 Aug). Empty endpoints means no volume endpoint is
        committed; the agent says so rather than pretending."""
        area = self._resolve_area_id(args.get("area_id") or args.get("room"))
        if not area:
            return {"error": "area_id (or room name) required"}
        verdict_eid = "sensor.proos_activity_%s" % area
        tgts, ctx = _room_vol_targets(self, area)
        ids = list(tgts) + [verdict_eid]
        snap = self.client.snapshot(ids) or {}
        v = snap.get(verdict_eid) or {}
        vatt = v.get("attributes") or {}
        endpoints = []
        for e in tgts:
            st = snap.get(e) or {}
            a = st.get("attributes") or {}
            # C1 (22 Aug 2026): ONE dialect for a volume — percent. The raw
            # 0-1 volume_level stops here; the agent is handed "40%", so
            # that is the number it speaks. One reply on Dave's screen gave
            # "volume 0.1", "10%" and "volume 10" for the SAME number
            # because we served it three dialects of one fact.
            _vl = a.get("volume_level")
            endpoints.append({
                "entity_id": e,
                "name": a.get("friendly_name"),
                "state": st.get("state"),
                "volume": ("%d%%" % round(float(_vl) * 100))
                          if isinstance(_vl, (int, float)) else None,
                "muted": a.get("is_volume_muted"),
                "media_title": a.get("media_title"),
                "source": a.get("source")})
        return {
            "area_id": area,
            "activity": v.get("state"),
            "activity_sentence": vatt.get("sentence"),
            "context": ctx,                       # 'video' | 'audio' | None
            "active_endpoint": tgts[0] if tgts else None,
            "endpoints": endpoints,
            "note": ("no volume endpoint is committed in this room — tell the user "
                     "there's nothing to control here" if not tgts else
                     "these are the room's ACTIVE volume endpoint(s); volume is a "
                     "percent and muted is the real mute state — answer from these, "
                     "and act on active_endpoint, never assume")}

    def t_usage_history(self, args):
        """The room's learned usage patterns from its journal (Pro-Assistant H2).
        Reads the history Core already records and returns habits as SOFT evidence
        — for reasoning, personalisation and a smarter confirm question. Never the
        live state (that's room_status), never a licence to act (the yes is the
        gate). Read-only: it never touches the verdict."""
        from . import journal, usage
        area = self._resolve_area_id(args.get("area_id") or args.get("room"))
        if not area:
            return {"error": "area_id (or room name) required"}
        try:
            events = journal.read(area, limit=1000)
        except Exception:                                        # noqa: BLE001
            events = []
        s = usage.summary(events)
        s["area_id"] = area
        return s

    def t_room_read(self, args):
        """Habit-weighted diagnosis (Pro-Assistant H4). Gathers the evidence for a
        room that changed on its own — live state + whether it was started
        externally, recent external-start events, and what the room is USUALLY
        doing at this time — and hands it to the model to reason over and CONFIRM.
        Core gathers deterministically; the judgement is the model's. Read-only:
        it never touches the verdict and never acts."""
        from . import journal, usage
        area = self._resolve_area_id(args.get("area_id") or args.get("room"))
        if not area:
            return {"error": "area_id (or room name) required"}
        now = time.time()
        verdict_eid = "sensor.proos_activity_%s" % area
        try:
            snap = self.client.snapshot([verdict_eid]) or {}
        except Exception:                                        # noqa: BLE001
            snap = {}
        v = snap.get(verdict_eid) or {}
        vatt = v.get("attributes") or {}
        try:
            events = journal.read(area, limit=1000)
        except Exception:                                        # noqa: BLE001
            events = []
        # external-start events in the last ~15 minutes — "did this just happen"
        recent_external = [
            {"to": (e.get("data") or {}).get("to"), "ts": e.get("ts"),
             "note": (e.get("data") or {}).get("note")}
            for e in events
            if e.get("type") == "external_control"
            and (now - float(e.get("ts") or 0)) <= 900]
        return {
            "area_id": area,
            "live": {"activity": v.get("state"),
                     "external": vatt.get("external"),
                     "source": vatt.get("source"),
                     "sentence": vatt.get("sentence")},
            "recent_external": recent_external,
            "expected_now": usage.expectation(events, now),
            "note": "stacked evidence to reason over — the live state, whether the "
                    "room was just started externally, and what it's USUALLY doing "
                    "at this time. If the external change matches the habit, offer "
                    "the personalised setup as a CONFIRM question; the habit is a "
                    "hint, never proof, and the yes is the gate — never act on this "
                    "alone.",
        }

    def t_room_volume(self, args):
        """Volume for a ROOM, endpoint-resolved (Assist Redesign A2, 6 Aug). The
        agent names a room; _room_vol_targets picks the target that FOLLOWS what's
        playing (music -> the playing speaker; watching -> the TV-audio owner), so
        'turn it up' can't move the wrong device. mute/unmute SET the flag — never
        a stale 'already muted'. No endpoint -> a plain message, and nothing fires."""
        area = self._resolve_area_id(args.get("area_id") or args.get("room"))
        if not area:
            return {"error": "area_id (or room name) required"}
        action = str(args.get("action") or "").strip().lower()
        if action not in ("up", "down", "mute", "unmute", "set"):
            return {"error": "action must be up | down | mute | unmute | set"}
        tgts, ctx = _room_vol_targets(self, area)
        if not tgts:
            return {"message": "There's no volume control set up in this room."}
        done = []
        for e in tgts:
            try:
                if action == "up":
                    self.client.call_service("media_player", "volume_up", e, None)
                elif action == "down":
                    self.client.call_service("media_player", "volume_down", e, None)
                elif action in ("mute", "unmute"):
                    self.client.call_service("media_player", "volume_mute", e,
                                             {"is_volume_muted": action == "mute"})
                elif action == "set":
                    # C1 (22 Aug 2026): level is a PERCENT, nothing else.
                    # "0-1 or a percentage" made this code guess: "set it to
                    # 1" read as 1.0 on the machine scale — FULL VOLUME. The
                    # judgement about what a person MEANT belongs to the
                    # agent; this pipe carries one unambiguous unit.
                    lv = args.get("level")
                    if lv is None:
                        return {"error": "set needs a level in percent (0-100)"}
                    lv = max(0.0, min(100.0, float(lv))) / 100.0
                    self.client.call_service("media_player", "volume_set", e,
                                             {"volume_level": lv})
                done.append(e)
            except Exception:                       # noqa: BLE001
                pass
        self.actions.append({"tool": "room_volume", "area_id": area,
                             "action": action, "targets": done, "context": ctx})
        return {"ok": True, "action": action, "targets": done, "context": ctx}

    def t_room_media(self, args):
        """Transport (play/pause/next/previous/stop) for the room's ACTIVE player,
        resolved from the verdict the same way volume is (A2). Best for music."""
        area = self._resolve_area_id(args.get("area_id") or args.get("room"))
        if not area:
            return {"error": "area_id (or room name) required"}
        svc = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
               "next": "media_next_track", "previous": "media_previous_track",
               "prev": "media_previous_track"}.get(str(args.get("action") or "").strip().lower())
        if not svc:
            return {"error": "action must be play | pause | next | previous | stop"}
        tgts, ctx = _room_vol_targets(self, area)
        if not tgts:
            return {"message": "There's nothing playing to control in this room."}
        done = []
        for e in tgts:
            try:
                self.client.call_service("media_player", svc, e, None)
                done.append(e)
            except Exception:                       # noqa: BLE001
                pass
        self.actions.append({"tool": "room_media", "area_id": area,
                             "service": svc, "targets": done})
        return {"ok": True, "service": svc, "targets": done}

    # Devices whose state REPORTS slowly. A TV or AVR obeys the command within
    # a second, but its integration may only confirm on the next poll — up to
    # ~15s for the slowest. Reading instantly and announcing "still on" is a
    # false alarm that makes a working system look broken, so verify WAITS for
    # these domains before it's allowed to conclude a mismatch.
    _SLOW_DOMAINS = ("media_player", "remote", "climate", "cover", "fan", "switch")
    _VERIFY_WAIT = 15          # max seconds to wait for a slow domain to settle
    _VERIFY_STEP = 2           # REST fallback grid: each re-check is a full dump
    # STAGE 6 BUILD 3 (16 Aug 2026): with the HA event stream healthy,
    # client.snapshot() is a free read of the verbatim push cache — no HTTP
    # at all (Stage 1). So verify re-checks every 0.25s on the stream and a
    # command that lands in a second CONFIRMS in about a second, instead of
    # holding the whole turn on a 2s grid of full-house REST dumps (the
    # audit's "~17s, nine dumps per call"). The REST fallback keeps the 2s
    # grid so its dump count can never grow; the total patience
    # (_VERIFY_WAIT) is unchanged and honest either way
    # (assist_verify_stream_bench.py).
    _VERIFY_STEP_STREAM = 0.25

    def _verify_step(self):
        try:
            s = getattr(self.client, "stream", None)
            if s is not None and s.healthy():
                return self._VERIFY_STEP_STREAM
        except Exception:                                        # noqa: BLE001
            pass
        return self._VERIFY_STEP

    def _check_once(self, checks, ids):
        snap = self.client.snapshot(ids) if ids else {}
        results = []
        for c in checks:
            e = c.get("entity_id")
            sv = snap.get(e)
            cur = _slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {})
            ok = True
            why = []
            if c.get("expect_state") is not None and cur.get("state") != c["expect_state"]:
                # off/standby are the same outcome for AV gear: "off" to the
                # user, standby to the driver. Never fail one against the other.
                pair = {c["expect_state"], cur.get("state")}
                if not pair <= {"off", "standby"}:
                    ok = False
                    why.append("state is %s, expected %s" % (cur.get("state"), c["expect_state"]))
            for k, v in (c.get("expect_attr") or {}).items():
                got = (cur.get("attributes") or {}).get(k)
                if not _attr_close(k, got, v):
                    ok = False
                    why.append("%s is %s, expected %s" % (k, got, v))
            results.append({"entity_id": e, "pass": ok,
                            # The NAME rides beside the id (Dave, 13 Aug): a
                            # verify sentence must name the device the way the
                            # home does, never read its entity_id to the user.
                            "name": cur.get("name"),
                            "actual": cur, "why": "; ".join(why) or "as expected"})
        return results

    def t_verify(self, args):
        checks = args.get("checks") or []
        ids = [c.get("entity_id") for c in checks if c.get("entity_id")][:40]
        results = self._check_once(checks, ids)
        # Lights answer fast but TRANSITION: a colour/brightness read a moment
        # after the command can catch the fade mid-flight. One short re-check —
        # not the AV-length wait — before any light is called a failure.
        _step = self._verify_step()
        if (any(not r["pass"] and (r["entity_id"] or "").startswith("light.")
                for r in results)):
            time.sleep(min(2, _step * 2) if _step < 2 else 2)
            results = self._check_once(checks, ids)
        # Failures on slow-reporting domains get patience, not a verdict:
        # re-check until they settle or time runs out — every 0.25s when the
        # stream makes reads free, every 2s on the REST fallback (build 3).
        deadline = time.time() + self._VERIFY_WAIT
        while (time.time() < deadline
               and any((not r["pass"]) and (r["entity_id"] or "").split(".")[0] in self._SLOW_DOMAINS
                       for r in results)):
            time.sleep(_step)
            results = self._check_once(checks, ids)
            if all(r["pass"] for r in results):
                break
        out = {"results": results,
               "all_pass": all(r["pass"] for r in results) if results else False}
        slow_fails = [r for r in results
                      if not r["pass"] and (r["entity_id"] or "").split(".")[0] in self._SLOW_DOMAINS]
        if slow_fails:
            out["note"] = ("these devices still hadn't confirmed after %ds of waiting — "
                           "genuinely investigate before reporting a failure. A raw state "
                           "is ONE witness, and for media players it may be a witness that "
                           "CANNOT SEE the answer: some streamer drivers sit at 'idle' while "
                           "an app plays. Investigate means: read the room's verdict sensor "
                           "(sensor.proos_activity_<area_id>, listed in rooms_overview) — if "
                           "it shows the activity running, the room is working and this state "
                           "is the driver's blindness, not a failure — and when witnesses "
                           "disagree, ask the user what they SEE on the screen; their answer "
                           "outranks every sensor." % self._VERIFY_WAIT)
        return out

    # -- music (phase 2) ----------------------------------------------------
    def _resolve_area_id(self, area):
        """Accept an area_id OR a room name and return the canonical area_id."""
        area = (area or "").strip()
        if not area:
            return None
        try:
            for a in (self.client.area_registry() or []):
                if a.get("area_id") == area:
                    return area
                if (a.get("name") or "").lower() == area.lower():
                    return a.get("area_id")
        except Exception:
            pass
        return area

    def _area_entities(self, area_id, domains):
        """Real entity_ids assigned to an area (entity override, else its
        device's area), filtered to the given domains. Live from the registry —
        so the model gets ACTUAL ids to build scenes from, never a guess."""
        try:
            dev_area = {d.get("id"): d.get("area_id") for d in (self.client.device_registry() or [])}
            ents = self.client.entity_registry() or []
        except Exception:
            return []
        doms = set(domains or [])
        out = []
        for e in ents:
            eid = e.get("entity_id") or ""
            if "." not in eid:
                continue
            if doms and eid.split(".", 1)[0] not in doms:
                continue
            if e.get("disabled_by") or e.get("hidden_by"):
                continue
            ea = area_of(e, dev_area)
            if ea == area_id:
                out.append(eid)
        return sorted(set(out))

    def t_area_entities(self, args):
        aid = self._resolve_area_id(args.get("area_id"))
        if not aid:
            return {"error": "area_id required"}
        # Prefer the installer's committed room-device list (auto-discovered,
        # minus excluded, with roles/names). Access follows that list. Fall back
        # to raw area membership if the module/store isn't available.
        want = set(args.get("domains") or [])
        try:
            from . import roomdevices
            avail = roomdevices.available(self.client, aid)
        except Exception:  # noqa: BLE001
            avail = None
        if avail is not None:
            out = []
            for d in avail:
                if want and d.get("domain") not in want:
                    continue
                rec = {"entity_id": d["entity_id"], "name": d.get("name") or d["entity_id"],
                       "state": d.get("state")}
                if d.get("role"):
                    rec["role"] = d["role"]
                caps = d.get("caps") or {}
                if d.get("domain") == "light":
                    rec["caps"] = {"dimmable": bool(caps.get("dimmable")),
                                   "color": bool(caps.get("color")),
                                   "color_temp": bool(caps.get("color_temp"))}
                elif d.get("domain") == "climate":
                    rec["hvac_modes"] = caps.get("hvac_modes")
                elif d.get("domain") == "cover":
                    rec["supports_position"] = bool(caps.get("position"))
                if d.get("offline"):
                    rec["offline"] = True
                out.append(rec)
            return {"area_id": aid, "entities": out,
                    "note": "these are the room's available devices (installer-committed), read "
                            "from the devices themselves — caps is what each one publishes about "
                            "itself, not a rule. Use these EXACT entity_ids; never invent one."}
        # Fallback path (no roomdevices module).
        domains = args.get("domains") or ["light", "cover", "climate", "fan", "switch"]
        eids = self._area_entities(aid, domains)
        if not eids:
            return {"area_id": aid, "entities": [],
                    "note": "no matching entities are assigned to this area in the registry"}
        snap = self.client.snapshot(eids) or {}
        out = []
        for e in eids:
            sv = snap.get(e)
            s = _slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {})
            attrs = s.get("attributes") or {}
            rec = {"entity_id": e, "name": attrs.get("friendly_name") or e,
                   "state": s.get("state")}
            dom = e.split(".", 1)[0]
            if dom == "light":
                rec["caps"] = _light_caps(attrs)   # {dimmable, color, color_temp}
            elif dom == "climate":
                rec["hvac_modes"] = attrs.get("hvac_modes")
            elif dom == "cover":
                rec["supports_position"] = attrs.get("current_position") is not None
            if s.get("state") == "unavailable":
                rec["offline"] = True
            out.append(rec)
        return {"area_id": aid, "entities": out,
                "note": "read from the devices themselves — caps is what each one publishes "
                        "about itself, not a rule. Use these EXACT entity_ids; never invent one."}

    def _entity_exists(self, eid):
        try:
            s = self.client._req("GET", "/api/states/%s" % eid)
            return bool(s and s.get("entity_id") == eid)
        except Exception:
            return False

    def _room_ma_speaker(self, area):
        """The Music Assistant player that plays in a room. Music lives on the MA
        engine, enabled per-speaker in Pro → Room speakers — NOT on the AV
        project's committed audio (which may be the native twin, e.g. the
        apple_tv HomePod entity). So resolve directly: a music_assistant-platform
        media_player whose area (entity override, else its device's area) is this
        room. Prefer one that's available; skip obvious group players."""
        area_id = self._resolve_area_id(area)
        if not area_id:
            return None
        try:
            dev_area = {d.get("id"): d.get("area_id") for d in (self.client.device_registry() or [])}
            ents = self.client.entity_registry() or []
        except Exception:
            return None
        cands = []
        for e in ents:
            eid = e.get("entity_id") or ""
            if not eid.startswith("media_player.") or e.get("platform") != "music_assistant":
                continue
            ea = area_of(e, dev_area)
            if ea == area_id:
                cands.append(eid)
        if not cands:
            return None
        # Prefer an available (non-'unavailable') player; keep deterministic order.
        try:
            snap = self.client.snapshot(cands)
            live = [e for e in cands if (snap.get(e) or {}).get("state") not in (None, "unavailable")]
            if live:
                return sorted(live)[0]
        except Exception:
            pass
        return sorted(cands)[0]

    def _slim_search(self, res: dict) -> dict:
        out = {}
        for kind in ("artists", "albums", "tracks", "playlists", "radio"):
            items = (res or {}).get(kind) or []
            slim = []
            for it in items[:8]:
                if not isinstance(it, dict):
                    continue
                artist = ""
                a = it.get("artists") or []
                if a and isinstance(a[0], dict):
                    artist = a[0].get("name", "")
                slim.append({"uri": it.get("uri"), "name": it.get("name"),
                             "artist": artist or None})
            if slim:
                out[kind] = slim
        return out or {"note": "no results"}

    def t_music_search(self, args):
        if not self.ma:
            return {"error": "the OPTIONAL music engine (ProOS Music) isn't linked", "note": "committed speakers report their own playable sources (source_list, via get_states) and select_source plays one — no engine needed for a speaker's own favourites"}
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "query required"}
        limit = int(args.get("limit") or 6)
        res = self.ma.search(q, media_types=args.get("kinds") or None, limit=limit)
        return {"results": self._slim_search(res),
                "note": "pass a result's uri to music_play or music_playlist_create"}

    def t_music_play(self, args):
        if not self.ma:
            return {"error": "the OPTIONAL music engine (ProOS Music) isn't linked", "note": "committed speakers report their own playable sources (source_list, via get_states) and select_source plays one — no engine needed for a speaker's own favourites"}
        area = (args.get("area_id") or "").strip()
        uri = (args.get("media_uri") or "").strip()
        if not area or not uri:
            return {"error": "area_id and media_uri required"}
        eid = self._room_ma_speaker(area)
        if not eid:
            return {"error": "no committed music speaker in that room — commission one in Pro first"}
        mode = (args.get("mode") or "play").lower()
        enqueue = {"play": "play", "next": "next", "add": "add"}.get(mode, "play")
        self.client._req("POST", "/api/services/music_assistant/play_media",
                         {"entity_id": eid, "media_id": uri, "enqueue": enqueue})
        self._audit("music_play", area=area, entity=eid, uri=uri, mode=enqueue)
        return {"ok": True, "playing_on": eid, "mode": enqueue,
                "next": "verify with get_states on %s if the user asked to confirm" % eid}

    def t_music_playlist_create(self, args):
        if not self.ma:
            return {"error": "the OPTIONAL music engine (ProOS Music) isn't linked", "note": "committed speakers report their own playable sources (source_list, via get_states) and select_source plays one — no engine needed for a speaker's own favourites"}
        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name required"}
        pl = self.ma.create_playlist(name)
        pid = pl.get("item_id") if isinstance(pl, dict) else None
        added = 0
        uris = [u for u in (args.get("track_uris") or []) if isinstance(u, str)]
        if pid and uris:
            try:
                self.ma.playlist_add(pid, uris)
                added = len(uris)
            except Exception as e:  # noqa: BLE001
                return {"ok": True, "playlist": name, "item_id": pid,
                        "added": 0, "warning": "created but couldn't add tracks: %s" % e}
        self._audit("music_playlist_create", name=name, item_id=pid, tracks=added)
        return {"ok": True, "playlist": name, "item_id": pid, "added": added}

    # -- scenes & automations (phase 3) — create / test / modify ------------
    # Everything the assistant makes gets a  proos_assist_  id prefix, so it only
    # ever lists/edits/deletes its OWN objects — never an installer's hand-built
    # scenes/automations. (HA scenes carry no custom attributes, so the id
    # prefix is the identity, checked on list and delete.)
    def _slugify(self, s, prefix):
        base = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "x"
        return "%s_%s" % (prefix, base)

    def _scene_cfg_id(self, eid):
        """The scene's config id (== unique_id, carried as attributes.id). This —
        NOT the entity_id — is where our proos_assist_ marker lives, because HA
        derives the scene ENTITY_ID from the name (scene.relax_in_office), while
        our id becomes attributes.id (proos_assist_relax_in_office)."""
        try:
            s = self.client._req("GET", "/api/states/%s" % eid) or {}
            return (s.get("attributes") or {}).get("id")
        except Exception:
            return None

    def _owns_scene(self, eid):
        return str(self._scene_cfg_id(eid) or "").startswith("proos_assist_")

    def _scene_eid_for_cfg(self, sid):
        """Find the live scene entity_id whose config id == sid (HA slugs the
        entity_id from the NAME, so it isn't 'scene.<sid>')."""
        try:
            for s in (self.client._req("GET", "/api/states") or []):
                e = s.get("entity_id", "")
                if e.startswith("scene.") and (s.get("attributes") or {}).get("id") == sid:
                    return e
        except Exception:
            pass
        return None

    def _used_scene_ids(self):
        """Config ids already taken by ProOS scenes (so a new scene never clobbers
        an existing one that happens to share a name)."""
        used = set()
        try:
            for s in (self.client._req("GET", "/api/states") or []):
                if s.get("entity_id", "").startswith("scene."):
                    cid = (s.get("attributes") or {}).get("id")
                    if cid:
                        used.add(str(cid))
        except Exception:
            pass
        return used

    def _free_scene_id(self, base):
        """base slug → a config id not already in use. Same name in two rooms
        yields relaxed_evening, relaxed_evening_2, … — distinct scenes, no
        overwrite. (Display name stays short; only the hidden id is suffixed.)"""
        used = self._used_scene_ids()
        if base not in used:
            return base
        n = 2
        while "%s_%d" % (base, n) in used:
            n += 1
        return "%s_%d" % (base, n)

    def t_scenes_list(self, args):
        try:
            states = self.client._req("GET", "/api/states") or []
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        out = []
        for s in states:
            eid = s.get("entity_id", "")
            # ours = config id (attributes.id) starts proos_assist_ — the entity_id
            # itself is name-derived and carries no marker.
            if eid.startswith("scene.") and str((s.get("attributes") or {}).get("id") or "").startswith("proos_assist_"):
                rec = {"entity_id": eid,
                       "name": (s.get("attributes") or {}).get("friendly_name") or eid}
                try:
                    ereg = getattr(self, "_ereg_cache", None)
                    if ereg is None:
                        ereg = {x.get("entity_id"): x for x in (self.client.entity_registry() or [])}
                        self._ereg_cache = ereg
                    rec["on_scenes_page"] = "dashboard_scene" in ((ereg.get(eid) or {}).get("labels") or [])
                except Exception:
                    pass
                # WHAT the scene touches and WHERE it lives — so "the watch tv
                # scene" is found by looking, never by guessing a room.
                members = ((s.get("attributes") or {}).get("entity_id")) or []
                if members:
                    rec["contains"] = members
                    areas = {}
                    try:
                        dev_area = {d.get("id"): d.get("area_id")
                                    for d in (self.client.device_registry() or [])}
                        for e2 in (self.client.entity_registry() or []):
                            if e2.get("entity_id") in members:
                                a2 = area_of(e2, dev_area)
                                if a2:
                                    areas[a2] = areas.get(a2, 0) + 1
                    except Exception:
                        pass
                    if areas:
                        rec["area_id"] = max(areas, key=areas.get)
                m = _scene_music_load().get(eid) or {}
                if m.get("query"):
                    rec["music"] = m.get("query")
                if m.get("activity_script"):
                    rec["activity"] = m.get("activity_script")
                out.append(rec)
        return {"scenes": out}

    def t_scene_create(self, args):
        name = (args.get("name") or "").strip()
        states = args.get("states") or []
        # A pure removal (states, activity or music) is a legal update with no
        # new states. Everything else needs name + states.
        removal_only = bool((args.get("scene_entity_id") or "").strip()
                            and (args.get("remove_entities")
                                 or args.get("remove_activity")
                                 or args.get("remove_music")
                                 or args.get("music")
                                 or args.get("activity_script")))
        # A MOMENT MAY BE MUSIC ONLY, OR ACTIVITY ONLY (16 Aug 2026). Dave
        # asked for "a work scene that just starts Triple M 80s, no
        # switches"; this tool demanded states, so the only way to obey was
        # to put something — anything — in it, and the model captured the
        # speaker's own feature switches. The INVENTION WAS FORCED BY THIS
        # LINE. A scene with a companion and no states is a legitimate
        # moment; only a scene with nothing at all is an error.
        has_companion = bool((args.get("music") or {}).get("query")
                             if isinstance(args.get("music"), dict) else False) \
            or bool((args.get("activity_script") or "").strip())
        if not name or (not states and not removal_only and not has_companion):
            return {"error": "name, and states or a music/activity companion, "
                             "are required"}
        entities = {}
        unknown = []
        for st in states:
            e = (st.get("entity_id") or "").strip()
            if "." not in e:
                continue
            # A READING, NOT A RULE (18 Aug 2026): the entity must actually
            # be on this box. A scene saved against an invented id does
            # nothing, forever, and nothing downstream would ever say so.
            # This asks the box one question — does this exist — and does
            # NOT judge what KIND of thing may be in a scene. The
            # media-player refusal that used to live here is DELETED: it is
            # what forced the invention of 16 Aug (asked for a music-only
            # scene, this tool demanded states, so switches got captured).
            live = None
            try:
                live = self.client._req("GET", "/api/states/%s" % e)
            except Exception:
                live = None
            if not (live and live.get("entity_id") == e):
                unknown.append(e)
                continue
            # STORED VERBATIM. No conversion, no capability stripping. This
            # tool does not decide how a device works — that is the
            # platform's business and the caller's. Whoever builds the
            # scene reads the device, sends what that device takes, RUNS
            # it, and READS IT BACK; the loop is what proves it, never
            # maths in here. (Deleted with this: brightness_pct -> 0-255,
            # the light-capability strip, and the 'adjusted' report that
            # existed only to announce our own guessing.)
            ent = {"state": st.get("state")}
            ent.update(dict(st.get("attributes") or {}))
            entities[e] = ent
        if unknown:
            return {"error": ("these entity_ids are not on this box: %s. Read the room's "
                              "real ids from the devices themselves and use those exact "
                              "ids — never invent an entity_id." % ", ".join(unknown))}
        if not entities and not removal_only and not has_companion:
            return {"error": ("no valid entities. Read the room's real devices and "
                              "capture those exact ids.")}
        # UPDATE in place only when the caller names the scene to change; otherwise
        # a NEW scene gets a fresh, non-colliding id so it never overwrites another
        # room's same-named scene (the display name can stay short).
        upd_eid = (args.get("scene_entity_id") or "").strip()
        merged_from = 0
        if upd_eid:
            cid = self._scene_cfg_id(upd_eid)
            if not str(cid or "").startswith("proos_assist_"):
                return {"error": "can only update scenes ProOS Assist created; omit scene_entity_id to make a new one"}
            sid = cid
            # An update MERGES: start from what the scene already holds and
            # overlay only what was sent. "Set the lamps to 40%" must never
            # silently delete the TV that was also in the scene — partial
            # updates are safe by construction, not by trusting the caller to
            # resend everything. remove_entities is the explicit way to drop.
            try:
                cur = self.client._req("GET", "/api/config/scene/config/%s" % sid) or {}
                existing = dict(cur.get("entities") or {})
            except Exception:
                existing = {}
            merged_from = len(existing)
            for rm in (args.get("remove_entities") or []):
                existing.pop((rm or "").strip(), None)
            # ATTRIBUTE-level merge, not record replacement. A caller updating
            # "just the activity" often re-lists the lamps as a bare
            # {state: on} to be safe — replacing the stored record with that
            # silently erased brightness 40%, and the next apply blasted the
            # lamps to full. What isn't re-specified is kept, per attribute.
            for e2, rec_new in entities.items():
                old = existing.get(e2)
                if isinstance(old, dict):
                    merged2 = dict(old)
                    merged2.update(rec_new)
                    entities[e2] = merged2
            existing.update(entities)
            entities = existing
        else:
            sid = self._free_scene_id(self._slugify(name, "proos_assist"))
        cfg = {"id": sid, "name": name, "entities": entities}
        try:
            self.client._req("POST", "/api/config/scene/config/%s" % sid, cfg)
            self.client._req("POST", "/api/services/scene/reload", {})
        except Exception as e:  # noqa: BLE001
            return {"error": "couldn't save scene: %s" % e}
        # Resolve the REAL entity_id (name-derived), retrying briefly for reload.
        seid = None
        for _ in range(6):
            seid = self._scene_eid_for_cfg(sid)
            if seid:
                break
            time.sleep(0.4)
        seid = seid or ("scene.%s" % sid)
        # A bespoke photo for the scene: AI-generated from its mood when an image
        # key is set, else matched. Keyed to the scene's config id (stable file).
        photo_source = None
        try:
            from . import scenephotos
            photo, photo_source = resolve_scene_photo(name, args.get("photo_query"), sid)
            if photo:
                scenephotos.set_photo(seid, photo=photo)
        except Exception:  # noqa: BLE001 - photo is cosmetic, never fail the scene
            pass
        # THE COMPANIONS NOW LIVE IN ONE PLACE (register 245), so this tool
        # can step aside for the platform's own scene tool without taking
        # them with it. Same code, same behaviour, one caller more.
        music_note, changed_side, rec2 = self._apply_companions(seid, args)
        self._audit("scene_create", name=name, entity=seid, entities=len(entities))
        out = {"ok": True, "scene_entity_id": seid, "name": name,
               "photo_source": photo_source,
               "next": "apply it with scene_apply, verify the entities reached these states, "
                       "then ASK the user if they'd like it on their dashboard (scene_dashboard)"}
        # Whether it's already on the homeowner's Scenes page — so the model
        # offers to add it only when it ISN'T, and never re-asks on an update.
        try:
            reg = {x.get("entity_id"): x for x in (self.client.entity_registry() or [])}
            out["on_scenes_page"] = "dashboard_scene" in ((reg.get(seid) or {}).get("labels") or [])
        except Exception:
            pass
        # Companion-only change (no device states touched): NOTHING physical
        # moved, so there is nothing to apply or verify — applying would fire
        # the whole scene at the room the user didn't ask to change. The edit
        # takes effect the next time the scene runs.
        companion_only = upd_eid and not (args.get("states") or [])
        if companion_only:
            out["next"] = ("done — do NOT apply or verify; nothing physical changed. "
                           "The edit takes effect next time the scene runs.")
        elif out.get("on_scenes_page"):
            out["next"] = ("apply it with scene_apply and verify — it's already on their "
                           "Scenes page, so DON'T ask about adding it")
        elif not upd_eid:
            out["next"] = ("apply it with scene_apply, verify, then ASK if they'd like it "
                           "added to their Scenes page (scene_dashboard)")
        if upd_eid:
            out["updated"] = True
            out["kept"] = ("merged with the scene's existing %d device(s) — everything not "
                           "mentioned was kept unchanged" % merged_from)
        if music_note:
            out["music"] = music_note
        # GROUND TRUTH for the reply: what the scene holds NOW. The model must
        # confirm from this, never from what it intended.
        out["scene_now"] = {
            "devices": sorted(entities),
            "activity": rec2.get("activity_script") or None,
            "music": rec2.get("query") or None}
        # A CLAIM NEEDS PROOF (16 Aug 2026 — Dave asked "did you test if it
        # worked?" and was told "Yes — the scene works perfectly", when the
        # music half had never played a note and verify only ever reads
        # device states). Creating a scene NEVER plays its music, so the
        # result says so in words the model must relay — and when NOTHING
        # in the house can play it, that is said HERE, not discovered by
        # the person standing in a silent room.
        if rec2.get("query"):
            out["music_proven"] = False
            if not self.ma:
                out["music_note"] = (
                    "SAVED, BUT NOTHING WILL PLAY IT: no music engine is "
                    "linked. Build the moment as a script instead — read the "
                    "speaker, write the calls, RUN it, read the speaker back "
                    "to prove it, then attach that script to the scene "
                    "(activity_script). Say this plainly; do not call the "
                    "scene working.")
            else:
                out["music_note"] = (
                    "the music has NOT been played yet — creating a scene never "
                    "plays it, and verify only reads device states. Never say it "
                    "is tested or working: apply the scene to prove it.")
        if photo_source == "curated_no_key":
            out["photo_note"] = ("used a curated photo — AI-generated scene images need an OpenAI "
                                 "image key (Pro › Tech Tools › Assist AI). Mention this if the user "
                                 "expected a custom picture.")
        return out

    # ── THE ONE HALF OF A SCENE THAT IS GENUINELY OURS (register 245) ────
    # Extracted from t_scene_create UNCHANGED — this is a move, not a
    # rewrite. Dave, 19 Aug: "I asked for Pro Assist working exactly the
    # same as this chat in Claude." The platform already creates scenes,
    # with locking and surgical edits ProOS does not have. What it does NOT
    # know about is the MOMENT: the music and the room's watch activity,
    # the halves an HA scene cannot hold. Those live here, so ProOS's
    # scene tool can step aside without taking them with it.
    def _apply_companions(self, seid, args):
        """(music_note, changed, record). The record is what the sidecar
        holds for this scene AFTER the change — ground truth for the reply,
        never what the caller intended."""
        # Companions: the halves of a "moment" an HA scene can't hold — music
        # and the room's watch ACTIVITY. Stored in the sidecar keyed by the
        # scene's entity_id; fired by the shared apply path so chat and
        # dashboard taps behave identically. On update, existing companions
        # survive unless explicitly changed (same merge principle as states).
        sm = _scene_music_load()
        rec2 = dict(sm.get(seid) or {})
        changed_side = False
        music_note = None
        # ── Companion REMOVAL ────────────────────────────────────────────────
        # "Take the Apple TV out of the scene" means the ACTIVITY companion —
        # that's how TV/AV lives in a scene. Without an explicit removal path
        # the model had no way to honour the request, and reported success on
        # an update that couldn't touch it. remove_entities naming the
        # companion's source device counts too: that's what a caller will
        # naturally reach for.
        rm_ents = [str(r or "").strip() for r in (args.get("remove_entities") or [])]
        if args.get("remove_activity") or (
                rec2.get("activity_source") and rec2["activity_source"] in rm_ents):
            _popped = [rec2.pop(k, None) is not None
                       for k in ("activity_script", "activity_source", "activity_area")]
            if any(_popped):
                changed_side = True
                music_note = "activity removed — the scene no longer starts the TV/source"
        if args.get("remove_music"):
            _popped = [rec2.pop(k, None) is not None
                       for k in ("query", "uri", "volume", "area_id")]
            if any(_popped):
                changed_side = True
                music_note = ((music_note + "; ") if music_note else "") + "music removed"
        music = args.get("music") or {}
        if isinstance(music, dict) and (music.get("query") or "").strip():
            rec2["area_id"] = (music.get("area_id") or rec2.get("area_id") or "").strip()
            rec2["query"] = music.get("query").strip()
            v = music.get("volume")
            if isinstance(v, (int, float)) and 0 < v <= 100:
                rec2["volume"] = v
            changed_side = True
            if not rec2["area_id"]:
                rec2.pop("query", None)
                music_note = "music skipped — give music.area_id (the room it should play in)"
            else:
                music_note = "music attached: '%s' will start whenever the scene runs" % rec2["query"]
        act = (args.get("activity_script") or "").strip()
        if act:
            if act.startswith("script."):
                # Store the activity by IDENTITY (source entity_id + area),
                # never by script name alone: script ids embed the source's
                # modifiable label, so a rename + regenerate would silently
                # break a name-keyed companion. Resolved back to the current
                # script at every apply.
                rec2["activity_script"] = act          # fallback only
                changed_side = True
                try:
                    proj = self.project.load() if self.project else {}
                    for akey, arec in (proj or {}).get("areas", {}).items():
                        if not (arec and arec.get("committed")):
                            continue
                        acts = (self.project.activities_status(self.client, proj, akey)
                                or {}).get("activities") or []
                        hit = next((a for a in acts if a.get("entity_id") == act), None)
                        if hit:
                            if hit.get("source_eid"):
                                rec2["activity_source"] = hit["source_eid"]
                            rec2["activity_area"] = akey
                            break
                except Exception:
                    pass
                music_note = ((music_note + "; ") if music_note else "") + \
                    ("activity attached: the scene now also runs %s" % act) + \
                    ("" if rec2.get("activity_source") else
                     " (couldn't pin it to a device — it will break if the activity is renamed)")
            else:
                music_note = ((music_note + "; ") if music_note else "") + \
                    "activity skipped — pass the script entity_id from rooms_overview"
        if changed_side:
            if rec2:
                sm[seid] = rec2
            else:
                sm.pop(seid, None)
            _scene_music_save(sm)
        return music_note, changed_side, rec2

    def t_scene_companions(self, args):
        """Attach ProOS's half of a moment to a scene the platform made.

        THE POINT OF THIS TOOL: the platform's own scene tool is better at
        scenes than ours — it has optimistic locking and surgical edits.
        Use it. Then call this to add what it has never heard of: the music
        that should start, and the room activity that should come up.
        """
        seid = (args.get("scene_entity_id") or "").strip()
        if not seid.startswith("scene."):
            return {"error": "scene_entity_id is required, e.g. scene.work"}
        # A READING, NOT A RULE: the scene has to actually be on this box.
        try:
            live = self.client._req("GET", "/api/states/%s" % seid)
        except Exception:                                    # noqa: BLE001
            live = None
        if not (live and live.get("entity_id") == seid):
            return {"error": "there is no %s on this box — make the scene "
                             "first, then attach its companions" % seid}
        # ── A REGRESSION I CAUSED, FOUND BY DAVE THE SAME NIGHT (reg 247) ──
        # 1.0.494 stood scene_create down for the platform's own scene tool.
        # scene_create's answer used to end with: "then ASK the user if
        # they'd like it on their dashboard (scene_dashboard)". That
        # sentence went with it, and nothing replaced it — so the platform
        # made the scene, nobody attached the `dashboard_scene` label, and
        # Dave's new Work scene could not reach the homeowner's Scenes page
        # at all. Read off his box: scene.work, labels: [].
        #
        # THE PUBLISH IS A PROOS CONCEPT — the platform has never heard of
        # that label — so it belongs here with the other ProOS-only halves.
        # REPORTED, NOT DECIDED: this says whether it is published and names
        # the tool that publishes it. It does not publish anything on its
        # own; putting a client's scene on their screen is their call.
        _pub = None
        try:
            reg = {x.get("entity_id"): x for x in (self.client.entity_registry() or [])}
            _pub = "dashboard_scene" in ((reg.get(seid) or {}).get("labels") or [])
        except Exception:                                    # noqa: BLE001
            pass
        note, changed, rec = self._apply_companions(seid, args)
        if not changed:
            _o = {"ok": True, "scene_entity_id": seid, "changed": False,
                  "note": "nothing was attached or removed — pass music, "
                          "activity_script, remove_music or remove_activity"}
            if _pub is False:
                _o["on_scenes_page"] = False
                _o["next"] = ("this scene is NOT on the homeowner's Scenes "
                              "page — ask if they want it there, then "
                              "scene_dashboard")
            return _o
        self._audit("scene_companions", entity=seid)
        out = {"ok": True, "scene_entity_id": seid, "changed": True,
               # GROUND TRUTH, same contract scene_create has: what the
               # scene holds NOW, never what the caller asked for.
               "scene_now": {"activity": rec.get("activity_script") or None,
                             "music": rec.get("query") or None}}
        if note:
            out["music"] = note
        if _pub is not None:
            out["on_scenes_page"] = _pub
            if not _pub:
                out["next"] = ("this scene is NOT on the homeowner's Scenes "
                               "page — ask if they want it there, then "
                               "scene_dashboard")
        # A CLAIM NEEDS PROOF: attaching music never plays a note, and if
        # nothing on this box can play it that is said HERE, not discovered
        # by someone standing in a silent room.
        if rec.get("query"):
            out["music_proven"] = False
            if not self.ma:
                out["music_note"] = (
                    "SAVED, BUT NOTHING WILL PLAY IT: no music engine is "
                    "linked. Build the moment as a script instead — read the "
                    "speaker, write the calls, RUN it, read the speaker back "
                    "to prove it, then attach that script here as the "
                    "activity companion.")
        return out

    def t_scene_photo(self, args):
        eid = (args.get("scene_entity_id") or "").strip()
        q = (args.get("photo_query") or "").strip()
        cfg_id = self._scene_cfg_id(eid)
        if not str(cfg_id or "").startswith("proos_assist_"):
            return {"error": "can only re-photo scenes ProOS Assist created"}
        if not q:
            return {"error": "photo_query required"}
        try:
            from . import scenephotos
            nm = (self.client._req("GET", "/api/states/%s" % eid) or {}).get("attributes", {}).get("friendly_name") or q
            photo, source = resolve_scene_photo(nm, q, cfg_id)
            res = scenephotos.set_photo(eid, photo=photo)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        self._audit("scene_photo", entity=eid)
        out = {"ok": True, "scene_entity_id": eid, "photo_source": source,
               "photo": (res.get("record") or {}).get("photo")}
        if source == "curated_no_key":
            out["photo_note"] = ("used a curated photo — a custom AI image needs an OpenAI image "
                                 "key in Pro › Tech Tools › Assist AI.")
        return out

    def t_scene_apply(self, args):
        eid = (args.get("scene_entity_id") or "").strip()
        out = apply_scene(self.client, self.ws_call, self.project, self.ma,
                          eid, self.user)
        if out.get("ok"):
            self._audit("scene_apply", entity=eid, music=bool(out.get("music")))
            out.setdefault("next", "verify the target entities now match the scene")
        return out

    def t_scene_dashboard(self, args):
        eid = (args.get("scene_entity_id") or "").strip()
        if not self._owns_scene(eid):
            return {"error": "can only pin scenes ProOS Assist created"}
        show = args.get("show", True)
        lbl = "dashboard_scene"   # the dashboard's own scenes-page label
        if not self.ws_call:
            return {"error": "label update unavailable"}
        try:
            # ensure the label exists (idempotent — create only if missing)
            have = {r.get("label_id") for r in (self.ws_call("config/label_registry/list") or [])}
            if lbl not in have:
                try:
                    self.ws_call("config/label_registry/create", name=lbl)
                except Exception:  # noqa: BLE001 - may race/exist; harmless
                    pass
            reg = {x.get("entity_id"): x for x in (self.client.entity_registry() or [])}
            cur = set((reg.get(eid) or {}).get("labels") or [])
            if show:
                cur.add(lbl)
            else:
                cur.discard(lbl)
            self.ws_call("config/entity_registry/update", entity_id=eid, labels=sorted(cur))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        self._audit("scene_dashboard", entity=eid, show=bool(show))
        return {"ok": True, "scene_entity_id": eid, "on_dashboard": bool(show),
                "note": "photo is set from the scene's matched image"}

    def t_scene_delete(self, args):
        eid = (args.get("scene_entity_id") or "").strip()
        cfg_id = self._scene_cfg_id(eid)
        if not str(cfg_id or "").startswith("proos_assist_"):
            return {"error": "can only delete scenes ProOS Assist created"}
        try:
            # DELETE keys off the scene's CONFIG id, not its (name-derived) entity_id.
            self.client._req("DELETE", "/api/config/scene/config/%s" % cfg_id)
            self.client._req("POST", "/api/services/scene/reload", {})
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        try:
            from . import scenephotos
            scenephotos.remove(eid)
        except Exception:  # noqa: BLE001
            pass
        sm = _scene_music_load()
        if sm.pop(eid, None) is not None:
            _scene_music_save(sm)
        self._audit("scene_delete", entity=eid)
        return {"ok": True, "deleted": eid}

    def t_automation_create(self, args):
        if not _is_pro(self.user):
            return {"error": "creating automations needs installer/tech access — I can't do that for a homeowner account"}
        alias = (args.get("alias") or "").strip()
        trig = args.get("trigger") or []
        act = args.get("action") or []
        if not alias or not trig or not act:
            return {"error": "alias, trigger and action required"}
        aid = self._slugify(alias, "proos_assist")
        cfg = {"id": aid, "alias": alias, "trigger": trig,
               "condition": args.get("condition") or [], "action": act,
               "mode": (args.get("mode") or "single")}
        try:
            self.client._req("POST", "/api/config/automation/config/%s" % aid, cfg)
        except Exception as e:  # noqa: BLE001
            return {"error": "couldn't save automation: %s" % e}
        self._audit("automation_create", alias=alias, id=aid)
        return {"ok": True, "automation_entity_id": "automation.%s" % aid, "alias": alias,
                "next": "test it now with automation_trigger, then verify the result"}

    def t_automation_trigger(self, args):
        if not _is_pro(self.user):
            return {"error": "installer/tech access required"}
        eid = (args.get("automation_entity_id") or "").strip()
        if not eid.startswith("automation."):
            return {"error": "automation_entity_id required"}
        self.client._req("POST", "/api/services/automation/trigger", {"entity_id": eid})
        self._audit("automation_trigger", entity=eid)
        return {"ok": True, "triggered": eid, "next": "verify the actions took effect"}

    def t_automation_delete(self, args):
        if not _is_pro(self.user):
            return {"error": "installer/tech access required"}
        eid = (args.get("automation_entity_id") or "").strip()
        oid = eid.split(".", 1)[1] if "." in eid else eid
        if not oid.startswith("proos_assist_"):
            return {"error": "can only delete automations ProOS Assist created"}
        try:
            self.client._req("DELETE", "/api/config/automation/config/%s" % oid)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        self._audit("automation_delete", entity=eid)
        return {"ok": True, "deleted": eid}

    # -- awareness ----------------------------------------------------------
    # These read the SAME live verdicts the dashboards render. No fallback to
    # guessing: if the awareness layer isn't running, the tool says so, because
    # "I can't see the home right now" is a truthful answer and "everything
    # looks fine" without evidence is not.
    def t_device_liveness(self, args):
        """Raw per-device evidence (W4): integration state + witness answer +
        verdict. The tool that lets Assist EARN an all-clear instead of
        inferring one from silence."""
        fn = self.awareness.get("watchers")
        if not fn:
            return {"error": "the awareness layer isn't running — liveness is not visible right now"}
        rep = fn() or {}
        items = rep.get("items") or []
        aid = (args.get("area_id") or "").strip()
        _n = lambda x: re.sub(r"[^a-z0-9]+", "_", str(x or "").lower()).strip("_")
        if aid:
            items = [i for i in items if _n(i.get("area")) == _n(aid)]
        devs = [{"name": i.get("name"), "area": i.get("area"),
                 "state": i.get("state"),
                 "witness": ("present" if i.get("reachable") is True
                             else "gone" if i.get("reachable") is False
                             else "none-bound" if not i.get("has_signal")
                             else "unknown"),
                 "verdict": i.get("verdict")} for i in items]
        # Installer exclusions stay VISIBLE (never silently absent): the
        # Room Devices toggle removes a device from the counts, not from view.
        for x in (rep.get("excluded_items") or []):
            if aid and _n(x.get("area")) != _n(aid):
                continue
            devs.append({"name": x.get("name"), "area": x.get("area"),
                         "state": None, "witness": "excluded-by-installer",
                         "verdict": "excluded"})
        return {"devices": devs, "count": len(devs),
                "note": "witness=gone while state=off means dead/unplugged/cut off, "
                        "not switched off — a switched-off device stays on the network. "
                        "excluded-by-installer devices are not watched or counted."}

    def t_home_status(self, args):
        fn = self.awareness.get("watchers")
        if not fn:
            return {"error": "the awareness layer isn't running — device health is not visible right now"}
        rep = fn() or {}
        items = rep.get("items") or []
        faults = [i for i in items if i.get("status") == "fault"]
        amber = [i for i in items if i.get("status") == "amber"]
        # W4 — "normal" is EARNED: say what was independently confirmed, what
        # has no witness, and what the witness contradicts. Never bare normal.
        conf = sum(1 for i in items if i.get("reachable") is True)
        gone = sum(1 for i in items if i.get("reachable") is False)
        nowit = sum(1 for i in items if not i.get("has_signal"))
        # A-3 (audit, 15 Aug). These four numbers did not add up to `watched`.
        # A device whose signal is BOUND but has never answered (reachable is
        # None) fell into none of them and simply disappeared from the
        # statement — so a home could report "16 watched · 9 confirmed · 0 not
        # answering · 0 with no witness" and Assist would never account for the
        # missing seven. A count that cannot be reconciled is a count nobody
        # can check, which is the whole of this finding.
        silent = sum(1 for i in items
                     if i.get("has_signal") and i.get("reachable") is None)
        out = {"overall": rep.get("status"), "summary": rep.get("summary"),
               "witness_coverage": {
                   "watched": len(items),
                   "confirmed_on_network": conf,
                   "not_answering": gone,
                   "bound_but_silent": silent,
                   "no_witness_bound": nowit,
                   "excluded_by_installer": int(rep.get("excluded") or 0),
                   "statement": ("%d watched · %d independently confirmed on the "
                                 "network · %d not answering · %d bound but never "
                                 "answered · %d with no witness"
                                 % (len(items), conf, gone, silent, nowit))},
               "watched": len(items),
               "faults": [{"name": i.get("name"), "area": i.get("area"),
                           "kind": i.get("kind"), "verdict": i.get("verdict"),
                           "guidance": i.get("guidance"),
                           "recovery": i.get("recovery")} for i in faults],
               "attention": [{"name": i.get("name"), "area": i.get("area"),
                              "verdict": i.get("verdict")} for i in amber]}
        mon = self.awareness.get("monitor")
        if mon:
            try:
                rooms = mon() or {}
                bad = {k: v for k, v in rooms.items()
                       if (v or {}).get("status") not in (None, "ok", "idle")}
                if bad:
                    out["rooms_attention"] = bad
            except Exception:
                pass
        return out

    def t_room_health(self, args):
        fn = self.awareness.get("room_health")
        if not fn:
            return {"error": "the awareness layer isn't running — room health is not visible right now"}
        aid = (args.get("area_id") or "").strip()
        if not aid:
            return {"error": "area_id required"}
        try:
            out = fn(aid) or {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        # Attach this room's device fault verdicts so one call tells the story.
        # The watcher labels items with the room NAME while callers hold the
        # area_id — compare slugified so "Family Room" matches "family_room".
        def _n(s):
            return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
        try:
            wrep = (self.awareness.get("watchers") or (lambda: {}))() or {}
            mine = [i for i in (wrep.get("items") or [])
                    if _n(i.get("area")) == _n(aid)
                    and i.get("status") in ("fault", "amber")]
            if mine:
                out["device_faults"] = [{"name": i.get("name"), "verdict": i.get("verdict"),
                                         "guidance": i.get("guidance")} for i in mine]
        except Exception:
            pass
        return out

    def t_recovery_history(self, args):
        fn = self.awareness.get("audit")
        if not fn:
            return {"error": "no awareness history available"}
        try:
            events = fn() or []
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        n = max(1, min(int(args.get("limit") or 30), 100))
        return {"events": events[:n]}

    def t_device_recover(self, args):
        # A homeowner CAN run recovery — that's how "it's not working" turns
        # into "fixed" without a truck roll — but only with explicit in-chat
        # consent, recorded in the audit. Pro tiers act on their own authority.
        if not _is_pro(self.user) and not args.get("confirmed"):
            return {"error": "needs the user's ok first — explain what you found in plain "
                             "words, ask if they'd like you to try fixing it, and call "
                             "again with confirmed=true only after a clear yes"}
        fn = self.awareness.get("recover")
        if not fn:
            return {"error": "recovery isn't available on this system"}
        eid = (args.get("entity_id") or "").strip()
        if not eid or "." not in eid:
            return {"error": "entity_id required"}
        try:
            out = fn(eid) or {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        self._audit("device_recover", entity=eid, result=out.get("ok"),
                    consent=("user-confirmed" if not _is_pro(self.user) else "pro"))
        if not out.get("ok"):
            out["next"] = "tell the user plainly it didn't come back, and offer flag_for_pro"
        return out

    # -- capability tools ---------------------------------------------------
    # Only offered when the home has the class (see _TOOL_GATES). The rails
    # here are CODE, not prompt: disarm doesn't exist, unlock is tier-gated.
    def _domain_states(self, domain):
        out = []
        try:
            for st in (self.client._req("GET", "/api/states") or []):
                eid = st.get("entity_id") or ""
                if eid.startswith(domain + "."):
                    a = st.get("attributes") or {}
                    out.append({"entity_id": eid,
                                "name": a.get("friendly_name") or eid,
                                "state": st.get("state")})
        except Exception:
            pass
        return out

    def t_security_status(self, args):
        panels = self._domain_states("alarm_control_panel")
        # Zone detail rides on binary_sensors that belong to the panel's
        # platform (door/window/motion) — report only the open ones.
        zones = []
        try:
            for st in (self.client._req("GET", "/api/states") or []):
                eid = st.get("entity_id") or ""
                a = st.get("attributes") or {}
                if (eid.startswith("binary_sensor.")
                        and a.get("device_class") in ("door", "window", "motion", "opening")
                        and st.get("state") == "on"):
                    zones.append({"name": a.get("friendly_name") or eid,
                                  "kind": a.get("device_class")})
        except Exception:
            pass
        return {"panels": panels, "open_zones": zones}

    def t_security_arm(self, args):
        eid = (args.get("entity_id") or "").strip()
        mode = (args.get("mode") or "").strip().lower()
        if not eid.startswith("alarm_control_panel."):
            return {"error": "entity_id must be an alarm panel"}
        if mode not in ("home", "away"):
            return {"error": "mode must be home or away — this tool NEVER disarms"}
        svc = "alarm_arm_home" if mode == "home" else "alarm_arm_away"
        self.client._req("POST", "/api/services/alarm_control_panel/%s" % svc,
                         {"entity_id": eid})
        self._audit("security_arm", entity=eid, mode=mode)
        return {"ok": True, "armed": mode, "entity_id": eid,
                "note": "confirm with verify — panels take a moment to arm"}

    def t_locks_status(self, args):
        return {"locks": self._domain_states("lock")}

    def t_lock_control(self, args):
        eid = (args.get("entity_id") or "").strip()
        action = (args.get("action") or "").strip().lower()
        if not eid.startswith("lock."):
            return {"error": "entity_id must be a lock"}
        if action == "unlock":
            if not _is_pro(self.user):
                # Remote unlock by voice/chat is the classic smart-home hole —
                # a homeowner-tier caller can never do it through here.
                return {"error": "unlocking isn't available here — use the lock's "
                                 "own app or keypad"}
        elif action != "lock":
            return {"error": "action must be lock or unlock"}
        self.client._req("POST", "/api/services/lock/%s" % action, {"entity_id": eid})
        self._audit("lock_control", entity=eid, action=action)
        return {"ok": True, "action": action, "entity_id": eid}

    def t_cameras_status(self, args):
        cams = self._domain_states("camera")
        motion = []
        try:
            for st in (self.client._req("GET", "/api/states") or []):
                eid = st.get("entity_id") or ""
                a = st.get("attributes") or {}
                if (eid.startswith("binary_sensor.")
                        and a.get("device_class") in ("motion", "occupancy", "sound")
                        and st.get("state") == "on"):
                    motion.append({"name": a.get("friendly_name") or eid,
                                   "kind": a.get("device_class")})
        except Exception:
            pass
        return {"cameras": cams, "active_detections": motion}

    def t_weather(self, args):
        """The home's own weather provider: current conditions + forecast."""
        panels = []
        try:
            for st in (self.client._req("GET", "/api/states") or []):
                eid = st.get("entity_id") or ""
                if eid.startswith("weather."):
                    a = st.get("attributes") or {}
                    panels.append((eid, st.get("state"), a))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        if not panels:
            return {"error": "no weather provider is set up in this home"}
        eid, cond, a = panels[0]
        out = {"condition": cond,
               "temperature": a.get("temperature"),
               "apparent_temperature": a.get("apparent_temperature"),
               "humidity": a.get("humidity"),
               "wind_speed": a.get("wind_speed"),
               "units": {"temperature": a.get("temperature_unit"),
                         "wind": a.get("wind_speed_unit")}}
        # Modern HA serves the forecast via a response service.
        try:
            r = self.client._req(
                "POST", "/api/services/weather/get_forecasts?return_response",
                {"entity_id": eid, "type": "daily"}) or {}
            fc = (((r.get("service_response") or r) or {}).get(eid) or {}).get("forecast") or []
            out["forecast"] = [{k: f.get(k) for k in
                               ("datetime", "condition", "temperature", "templow",
                                "precipitation", "precipitation_probability")}
                              for f in fc[:5]]
        except Exception:
            fc = a.get("forecast") or []       # older HA: rides on attributes
            if fc:
                out["forecast"] = fc[:5]
        return out

    def t_flag_for_pro(self, args):
        summary = (args.get("summary") or "").strip()
        if not summary:
            return {"error": "summary required"}
        fn = self.awareness.get("flag")
        if not fn:
            return {"error": "flagging isn't available on this system"}
        rec = fn({"summary": summary,
                  "detail": (args.get("detail") or "").strip(),
                  "entity_id": (args.get("entity_id") or "").strip(),
                  "by": self.user.get("name") or "unknown",
                  "tier": _tier(self.user)})
        self._audit("flag_for_pro", summary=summary)
        return {"ok": True, "flag_id": (rec or {}).get("id"),
                "note": "logged for the installer — tell the user it's been passed on"}

    # -- memory (phase 2) ---------------------------------------------------
    # ── the knowledge store ─────────────────────────────────────────────────
    # Reads are shaped by TIER only in the sense the store already
    # decides: the product's knowledge is everyone's, the installer's
    # site notes are the professional's record. No other rule here.
    def t_knowledge_search(self, args):
        from . import knowledge as _kn
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "say what you want to know about"}
        hits = _kn.search(q, pro=_is_pro(self.user))
        if not hits:
            return {"hits": [], "note": "nothing written down about that yet — "
                                        "that is not evidence either way"}
        return {"hits": hits,
                "note": "read one whole with knowledge_read(path) before you "
                        "rely on it"}

    def t_knowledge_read(self, args):
        from . import knowledge as _kn
        d = _kn.read((args.get("path") or "").strip(), pro=_is_pro(self.user))
        if not d:
            return {"error": "no document with that path"}
        return {"path": d["path"], "title": d.get("title"),
                "layer": d.get("layer"), "body": d.get("body")}

    def t_knowledge_write(self, args):
        from . import knowledge as _kn
        try:
            rec = _kn.write(args.get("title") or "", args.get("body") or "",
                            author=self.user.get("name") or "")
        except ValueError as e:
            return {"error": str(e)}
        self._audit("knowledge_write", note=rec["path"])
        return {"ok": True, "path": rec["path"], "title": rec["title"],
                "note": "written to this house's record"}

    def t_memory_get(self, args):
        uid = self.user.get("id") or "anon"
        rec = _mem_load().get(uid) or {}
        return {"facts": rec.get("facts") or [],
                "learned": rec.get("learned") or [],
                "declines": rec.get("declines") or []}

    def t_memory_set(self, args):
        """Pin something to remember about this person. Default is a TOLD fact
        (they stated it). learned=true pins a LEARNED preference you inferred from
        how they use the home (soft, H3). decline=true records a suggestion they
        waved off, so you never re-offer it — a no sticks (H5). forget removes a
        matching item from ALL streams. Per person."""
        fact = (args.get("fact") or "").strip()
        if not fact:
            return {"error": "fact required"}
        uid = self.user.get("id") or "anon"
        store = _mem_load()
        rec = store.setdefault(uid, {"facts": [], "learned": [], "declines": []})
        facts = rec.setdefault("facts", [])
        learned = rec.setdefault("learned", [])
        declines = rec.setdefault("declines", [])
        if args.get("forget"):
            low = fact.lower()
            rec["facts"] = [f for f in facts if low not in f.lower()]
            rec["learned"] = [l for l in learned
                              if low not in (l.get("text", "").lower())]
            rec["declines"] = [d for d in declines
                               if low not in (d.get("text", "").lower())]
        elif args.get("decline"):
            # a suggestion they declined — remember so you don't nag (a no sticks)
            if all(fact != d.get("text") for d in declines):
                declines.append({"text": fact, "ts": round(time.time(), 1)})
                rec["declines"] = declines[-_MEM_MAX:]
        elif args.get("learned"):
            # a learned preference — soft, timestamped, distinct from told facts
            if all(fact != l.get("text") for l in learned):
                learned.append({"text": fact, "ts": round(time.time(), 1)})
                rec["learned"] = learned[-_MEM_MAX:]
        else:
            if fact not in facts:
                facts.append(fact)
                rec["facts"] = facts[-_MEM_MAX:]
        _mem_save(store)
        self._audit("memory_set", forget=bool(args.get("forget")),
                    learned=bool(args.get("learned")),
                    decline=bool(args.get("decline")))
        return {"ok": True, "facts": rec["facts"],
                "learned": rec["learned"], "declines": rec["declines"]}


# ── system prompt ────────────────────────────────────────────────────────────

def _room_contents(client, project_mod, aid):
    """EVERYTHING in the room, from BOTH halves of the commissioning.

    Register 108 — the Bedroom bug, hours after the Office fix. Assist said
    "the Bedroom doesn't have a Netflix-capable device" in a room whose record
    holds a TV, an Apple TV and a Shield. The inventory I shipped that morning
    read only `roomdevices.available()`, which EXCLUDES media_player by design
    ("AV power/routing is the committed activity's job") — and I declared that
    partial list COMPLETE. The model then reasoned correctly from a false fact.

    The room genuinely has two halves, owned by two stores:
      - AV, with roles, in the project record (display/sources/audio/…)
      - everything else in roomdevices (lights, blinds, climate, fans, locks)
    A "complete" inventory must merge both. Returns None when NEITHER half can
    be read — unknown is not zero, and nothing false is claimed."""
    out, seen, have = [], set(), False
    try:
        areas = (project_mod.load() or {}).get("areas") or {}
        rec = next((r for r in areas.values()
                    if (r or {}).get("area_id") == aid), None)
        if rec is not None:
            have = True
            av = []
            if rec.get("display"):
                av.append((rec["display"], "display"))
            for e in (rec.get("sources") or []):
                av.append((e, "source"))
            for e in (rec.get("tvaudio") or []):
                av.append((e, "tv audio"))
            for e in (rec.get("speakers") or []):
                av.append((e, "speaker"))
            for e in (rec.get("audio") or []):
                av.append((e, "audio"))
            if rec.get("avswitch"):
                av.append((rec["avswitch"], "switcher"))
            ids = [e for e, _ in av if e not in seen]
            names = {}
            try:
                snap = client.snapshot(ids) or {}
                for e in ids:
                    names[e] = ((snap.get(e) or {}).get("attributes") or {})                         .get("friendly_name")
            except Exception:                                    # noqa: BLE001
                pass
            for e, role in av:
                if e in seen:
                    continue
                seen.add(e)
                # THE SAME WITHHELD READING (register 246). This is the room
                # inventory the model is handed for the room the person is
                # standing in — and it gave a NAME and a ROLE and nothing
                # else. A Sonos called "Office" sitting beside a HomePod
                # called "HomePod" is then anybody's guess, and Assist
                # guessed wrong to Dave's face on his own homeowner app.
                _d = {"domain": e.split(".", 1)[0] if "." in e else "media_player",
                      "name": names.get(e)
                      or e.split(".")[-1].replace("_", " ").title(),
                      "role": role}
                _ig = ((rec.get("meta") or {}).get(e) or {}).get("integration")
                if _ig:
                    _d["integration"] = _ig
                out.append(_d)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import roomdevices
        avail = roomdevices.available(client, aid)
        if avail is not None:
            have = True
            for d in avail:
                e = d.get("entity_id")
                if e and e in seen:
                    continue
                if e:
                    seen.add(e)
                out.append({"domain": d.get("domain"),
                            "name": d.get("name") or e})
    except Exception:                                            # noqa: BLE001
        pass
    return out if have else None


def _where_prompt(where: dict) -> str:
    """Tell the model WHERE the person is standing, AND HOW SURE WE ARE.

    Without this every bare request is a guessing game: "turn the lights off"
    has no answer, so the assistant either interrogates the user or picks a
    room at random. A person speaking in their kitchen means the kitchen, and
    an assistant that has to ask is the thing that makes it feel like software
    rather than a house that understands you.

    BUT CONFIDENCE IS PART OF THE FACT. This prompt was written for a
    MICROPHONE, where the room is certain by definition — a satellite in the
    bedroom is in the bedroom. An APP is not: a phone can be anywhere. Sending
    a guessed room in the permissive form would turn a guess into a confident
    action in the wrong room, which is the one failure a house assistant may
    never have.

    So callers must say how sure they are, and only "certain" earns the right
    to act unasked:

      certain   the room whose page is open, or a panel pinned at install
      probable  presence, a stale sticky choice, anything inferred
      (absent)  say nothing; Assist asks, and the answer is the consent

    THE DEFAULT IS THE CAUTIOUS ONE. A caller that forgets to say gets
    "probable", because the mistake a hurried caller makes must be the safe
    one. (Dave, 12 Aug: "if it can't confirm then it asks — which then confirms
    and provides consent.")"""
    if not where or not where.get("area_id"):
        return ""
    name = where.get("area_name") or where.get("area_id")
    aid = where.get("area_id")
    # WHAT IS IN THE ROOM, said out loud, so the model confirms against the
    # truth instead of assuming. An empty list is a FACT ("no controllable
    # devices"); an absent key means the lookup failed and nothing is claimed.
    inv = ""
    if "contents" in where:
        items = where.get("contents") or []
        if items:
            by = {}
            for d in items:
                nm = d.get("name") or "?"
                if d.get("role"):
                    nm += " (%s)" % d["role"]
                by.setdefault(d.get("domain") or "other", []).append(nm)
            parts = ["%s: %s" % (dom, ", ".join(ns[:6])
                                 + (" +%d more" % (len(ns) - 6) if len(ns) > 6 else ""))
                     for dom, ns in sorted(by.items())]
            # A HINT TO VERIFY, NEVER GOSPEL (register 108). The first
            # version said "the complete list — a device type not listed is
            # NOT in this room, say so rather than acting" — and that line
            # instructed the model to skip its own verification. It obeyed,
            # and told Dave the Bedroom had no Netflix device while the record
            # held an Apple TV and a Shield. Injected context must inform the
            # model's reasoning, not forbid it: confirm-don't-assume applies
            # to Assist's own inputs too.
            inv = ("IN THIS ROOM (what the commissioning knows): "
                   + "; ".join(parts) + ". If they ask for something NOT in "
                   "this list, do not assume either way — VERIFY with "
                   "rooms_overview or area_entities first, then say what you "
                   "actually found.\n")
        else:
            inv = ("THIS ROOM HAS NO CONTROLLABLE DEVICES assigned yet — "
                   "nothing here can be switched, so say that.\n")
    if where.get("confidence") == "certain":
        return (
            "\nWHERE THEY ARE: %s (area_id '%s'). Anything said without naming a room means "
            "THIS room — 'the lights', 'in here', 'turn it off', 'play something'. Act on %s "
            "without asking which room. Only ask when they name no room AND the request "
            "genuinely cannot apply here. If they name a different room, use that one.\n"
            % (name, aid, name)) + inv
    return (
        "\nWHERE THEY MIGHT BE: %s (area_id '%s') — this is a GUESS, not something "
        "they told us. Treat a bare request as PROBABLY about %s, but CONFIRM before "
        "acting: name the room in your question, e.g. 'in the %s?'. Their answer is "
        "both the confirmation and the go-ahead, so one question is enough — do not "
        "ask twice. Reading and diagnosing need no confirmation; only acting does. "
        "If they name a different room, use that one.\n"
        % (name, aid, name, name)) + inv


def _system_context(user: dict, where: dict | None = None) -> str:
    """The DYNAMIC half of the prompt (A5): who, role, memory, where. Kept small
    and SEPARATE from the doctrine so the big doctrine can be prompt-cached across
    every turn — this little block is the only part that changes per user/turn."""
    who = (user or {}).get("name") or "the user"
    tier = _tier(user)
    rec = _mem_load().get((user or {}).get("id") or "anon") or {}
    facts = rec.get("facts") or []
    learned = [l.get("text") for l in (rec.get("learned") or []) if l.get("text")]
    declines = [d.get("text") for d in (rec.get("declines") or []) if d.get("text")]
    ctx = "\n\nYou are speaking with %s (role: %s)." % (who, tier)
    if facts:
        ctx += " What you remember about %s (they told you): %s." % (who, "; ".join(facts))
    if learned:
        # LEARNED preferences are soft — picked up from how they use the home, not
        # stated. Present them as such so the model treats them as a hint to
        # personalise or to ask a smarter question, never as fact (H3 doctrine).
        ctx += (" What you've learned about %s from how they use the home (soft — a "
                "hint to personalise or confirm, never a certainty): %s."
                % (who, "; ".join(learned)))
    if declines:
        # things they've waved off — a no sticks; never re-offer these (H5).
        ctx += (" Suggestions %s has DECLINED — do NOT re-offer these: %s."
                % (who, "; ".join(declines)))
    ctx += _where_prompt(where or {})
    return ctx


def _music_doctrine(engine: bool) -> str:
    """What the model is told about music depends on WHAT IT IS HOLDING.

    18 Aug 2026. Asked for "a work scene that just plays Triple M 80s at
    low volume in the office", Assist called the optional engine's
    search tool, got "not connected", and told Dave to go and link an
    add-on — while the station was already on his Sonos with a content
    id, a volume and a title. The old paragraph named engine tools as
    THE way to do music. When the engine isn't there, that instruction
    is a dead end; the hand method is the answer and always was.
    """
    hand = ("Read the speaker, write the script from those exact readings, "
            "RUN it, and read the speaker back to prove it. ")
    if not engine:
        return ("MUSIC: no music engine is linked here, and that does NOT mean "
                "music cannot be done — the room's own speaker can. " + hand
                + "Never tell anyone music is impossible because an optional "
                  "add-on isn't linked, or send them off to link one for "
                  "something the room's speaker already does.\n")
    return ("MUSIC: an engine IS linked — music_search, music_play and "
            "music_playlist_create are its lane, for library search and "
            "cross-brand grouping, in a room with a committed speaker. For a "
            "specific station or source on a room's OWN speaker, the hand "
            "method is still the truth. " + hand + "\n")


def _system_doctrine(user: dict, home_name: str, engine: bool = False) -> str:
    """The STATIC half of the prompt (A5): identical for every turn at a
    given tier + home, so it prompt-caches.

    THE CUT (19 Aug 2026, register 227). This was 3,216 words — 36
    sentences of NEVER / ALWAYS / MUST — and Dave measured what that
    cost: a blank session holding only the platform's tools did in five
    minutes what Assist could not in two months. His words: "everything
    you seem to do is building tools to do what you're already doing
    with straight MCP… that should be the only difference."

    ONE TEST decides what may live here: **is it true on EVERY turn,
    whatever the subject?** Tier law, never claiming what you did not
    read, the method, consent, and how the product speaks — those are
    always true. Everything else is knowledge that matters WHEN THE
    SUBJECT ARISES: how a remote entity differs from power, what a
    stale incident report is worth, how a room's habit earns its keep.
    That is not deleted — it moved to the knowledge store (register
    226), where it is READ when it is relevant instead of recited
    before every question. `knowledge_store_bench` pins that each fact
    is still there, so this cut cannot lose one.
    """
    return (
        "You are Pro Assist, the assistant for '%s'. Your home turf is this house — you "
        "can see it, control it and watch over it — and you are a genuine assistant "
        "besides: answer anything, at natural length. A control command gets one short "
        "confirmation of what you did, not how.\n"
        "WHO YOU'RE TALKING TO decides what you may do, and the tools you hold already "
        "match it — a capability someone lacks simply is not there, so never announce "
        "'access denied' or discuss a power they do not have. Everyone may look at "
        "anything, control the committed home — lights, volume, activities, scenes, "
        "music — and lock a door. Installer, tech and Developer may unlock a door, "
        "change automations, and commission rooms and endpoints. Tech and Developer may "
        "change settings, integrations and the network.\n"
        "LOOK BEFORE YOU SPEAK — NOTHING IS CLAIMED THAT WAS NOT READ. This is the whole "
        "product. Never assume a device's state; you have tools to know it. Before you "
        "say what is playing or how loud a room is, and before any volume or mute "
        "command, call room_status for that room and answer from what it returns — it "
        "names the endpoint actually playing. room_volume and room_media act on the ROOM, "
        "so they never move the wrong device. Never say 'already muted' or 'it's playing' "
        "unless you just read it.\n"
        "SILENCE MUST BE EARNED. 'Everything is normal' is a CLAIM: say the coverage — how "
        "many devices were watched, how many were confirmed independently, how many are "
        "not answering. No fault raised is not the same as no fault existing. And a "
        "FAILURE must be as earned as a success: a raw device state is ONE witness; when "
        "it disagrees with the room's committed verdict the verdict wins, and the "
        "person's own eyes outrank both — ask what they SEE before announcing a failure "
        "they can refute by looking up. A room's verdict carries verified_by: the reading "
        "that earned the word confirmed. Say a room is confirmed only WITH that reading, "
        "in those words; where none came back, describe what you read instead.\n"
        "BUILD IT THE WAY YOU WOULD BY HAND, AND PROVE IT. When something has no tool that "
        "fits, do not bend one that doesn't and do not describe what you would need: READ "
        "the devices, WRITE it from those exact readings, RUN it, then READ THE DEVICES "
        "BACK. If it did not land — it loaded but is still paused, the volume did not "
        "move — FIX IT AND RUN IT AGAIN. Only then say it works. A "
        "saved thing you have not run is NOT tested.\n"
        "YOU HOLD THE PLATFORM'S OWN TOOLS (names beginning ha_) beside the ProOS ones, "
        "and THEY ARE NOT A LAST RESORT — reach for them the moment a ProOS tool cannot "
        "answer. ProOS's tools speak the COMMISSIONED home and are the right way to "
        "control AV — rooms_overview, home_status, room_health, room_read, "
        "recovery_history, usage_history, memory_get. The platform's tools see EVERYTHING "
        "the house has, commissioned or not. A dead end is not an answer. NO "
        "SUCH ENTITY IS NOT A BROKEN DEVICE: a state that reads missing means the id is "
        "wrong, not the device — search for the real one and read THAT.\n"
        "WHAT THIS PRODUCT AND THIS HOUSE HAVE LEARNED IS WRITTEN DOWN — signals, diagnosis, "
        "awareness, moments, habits, consent, and the installer's own notes about THIS "
        "house. knowledge_search it BEFORE you conclude anything you are unsure of, and "
        "the moment something behaves in a way you did not expect: all of it was written "
        "down because it was learned the hard way. When you learn something here worth "
        "keeping, ask, then knowledge_write it.\n"
        "CONFIRM, DON'T ASSUME — AND THE CONFIRM IS THE CONSENT. Ambiguity is not a reason "
        "to guess: ask ONE sharp question about the single thing in doubt, and that same "
        "answer is your permission. Diagnose freely for anyone; change nothing until they "
        "say yes. Destructive actions are confirmed first. Offer at most ONE suggestion "
        "where it genuinely fits, never act without the yes, and a decline sticks.\n"
        + _music_doctrine(engine) +
        "SPEAK THE HOME'S WORDS — every tier, installers included. Name rooms, devices and "
        "activities the way the home does: never an entity id, area slug or integration "
        "term in anything you SAY. Ids are identity — they stay in tool calls, where they "
        "are required, and never in speech. And nobody below Developer hears the name of "
        "the platform underneath this product, or of the supplier behind you."
        + ("" if _is_pro(user) else "\nYou are talking with a HOMEOWNER: plain, everyday "
           "language for everything about the home. You may control the home, manage music, "
           "run checks and — with their ok — recoveries; you cannot commission devices or "
           "rooms. When something is beyond a remote fix, say you'll pass it to their "
           "installer and flag_for_pro.")
    ) % (home_name or "this home",)

def _system_prompt(user: dict, home_name: str, where: dict | None = None,
                   engine: bool = False) -> str:
    """The full system string (used by OpenAI and by benches): the static
    doctrine first, then the small dynamic context. Static-first means OpenAI's
    automatic prefix caching catches the doctrine too, and the Claude adapter can
    cache-mark exactly the doctrine block (see _claude_system)."""
    return (_system_doctrine(user, home_name, engine)
            + _system_context(user, where))


# ── provider adapters ────────────────────────────────────────────────────────

def _http_json(url, payload, headers):
    # Rate limits are handled HERE, invisibly (Dave, 4 Aug: a raw HTTP 429
    # JSON blob landed in the homeowner's chat). 429s carry a "try again in
    # Xs" hint — wait it out (capped) and retry before ever failing.
    import time as _t
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **headers},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:400]
            except Exception:
                pass
            last = RuntimeError("provider HTTP %s: %s" % (e.code, body))
            if e.code == 429 and attempt < 2:
                m = re.search(r"try again in ([0-9.]+)s", body)
                wait = min(float(m.group(1)) if m else 5.0, 20.0) + 0.5
                print("  [assist] provider 429 — waiting %.1fs and retrying"
                      % wait, flush=True)
                _t.sleep(wait)
                continue
            raise last from e
    raise last


def _last_user_text(history):
    for m in reversed(history or []):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _route_model(main_model, fast_model, text):
    """Which model handles THIS turn (A5). Default — no fast_model — is the main
    model for everything, so nothing changes out of the box. If the installer
    configured a fast_model, a SHORT single-clause command ('lights off', 'mute
    the office') routes to it for speed + cost; any question, compound request or
    rich language stays on the strong model, so the reasoning Assist is known for
    is never dumbed down."""
    if not fast_model:
        return main_model
    t = " ".join((text or "").strip().lower().split())
    if (len(t) <= 40 and "?" not in t
            and not re.search(r"\b(and|then|why|how|what|explain|because|should|could)\b|,", t)):
        return fast_model
    return main_model


def _claude_tools(runner):
    """Tool schemas for Claude with the LAST one cache-marked, so the whole tool
    block — large and fully static — is prompt-cached across turns (A5)."""
    ts = [{"name": t["name"], "description": t["description"],
           "input_schema": t["input_schema"]} for t in _active_tools(runner)]
    if ts:
        ts[-1] = {**ts[-1], "cache_control": {"type": "ephemeral"}}
    return ts


def _claude_system(doctrine, context):
    """System as two blocks: the static doctrine (cache-marked) then the small
    dynamic context (not cached). The breakpoint on the doctrine also covers the
    tools before it, so the entire static prefix is a single cached read (A5)."""
    return [{"type": "text", "text": doctrine, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context}]


# ── STAGE 6 BUILD 1 (16 Aug 2026): A CUT TOOL RESULT SAYS IT WAS CUT ────────
# Both provider paths did `json.dumps(out)[:8000]` — a raw, silent slice.
# A device absent from a truncated room list read exactly like a device
# absent from the HOME: silence presented as evidence inside the
# assistant's own eyes, and the slice could even land mid-token, handing
# the model unparseable JSON. The cap stays (a full-house dump can flood
# the context — the cap was never the lie; the silence was): an oversized
# result now arrives as a parseable envelope that DECLARES it is partial,
# states both sizes, and says plainly that absence from it is not absence
# from the home (assist_truncation_marker_bench.py).
_TOOL_RESULT_CAP = 8000


def _tool_payload(out) -> str:
    s = json.dumps(out)
    if len(s) <= _TOOL_RESULT_CAP:
        return s
    kept = s[:_TOOL_RESULT_CAP - 700]     # room for the envelope itself
    return json.dumps({
        "proos_truncated": True,
        "note": ("TRUNCATED TOOL RESULT: only the first %d of %d characters "
                 "are here. Anything not shown is MISSING FROM THIS RESULT, "
                 "not absent from the home — re-query with a narrower filter "
                 "(one room, one entity) before claiming something is not "
                 "there." % (len(kept), len(s))),
        "partial": kept,
    })



def _ran_out(runner, partial_text: str = "") -> str:
    """What to say when the steps run out — from the RUN RECORD, never a
    bare apology.

    THE OLD LINE WAS "I hit my action limit for one request." True, and
    useless: it REPLACED whatever the model was going to say, so a turn
    that had already changed things in the house reported nothing about
    them. A person was left not knowing what had been done to their
    home. (19 Aug 2026 — the audit called this a silent stop; reading it,
    it was not silent, it was WORSE than silent: it spoke, and said
    nothing that mattered.)

    Now it says what was actually done, what was only read, and that it
    did not finish — and it never claims the job worked.
    """
    acts = [a.get("tool") for a in (getattr(runner, "actions", None) or [])
            if a.get("tool")]
    reads = len([t for t in (getattr(runner, "trace", None) or [])
                 if t.get("kind") == "read"])
    parts = ["I ran out of steps before I finished this — I have not "
             "completed it, and I am not claiming it works."]
    if acts:
        seen, uniq = set(), []
        for a in acts:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        parts.append("What I actually changed: %d action%s (%s)."
                     % (len(acts), "" if len(acts) == 1 else "s",
                        ", ".join(uniq[:6])))
    else:
        parts.append("Nothing in the home was changed.")
    if reads:
        parts.append("I read %d thing%s along the way."
                     % (reads, "" if reads == 1 else "s"))
    if (partial_text or "").strip():
        parts.append("Where I had got to: " + partial_text.strip())
    parts.append("Say carry on and I will pick up from here.")
    return " ".join(parts)


def _chat_claude(cfg, doctrine, context, history, runner):
    model = _route_model(cfg.get("model") or DEFAULT_MODELS["claude"],
                         (cfg.get("fast_model") or "").strip(),
                         _last_user_text(history))
    tools = _claude_tools(runner)
    system = _claude_system(doctrine, context)
    messages = list(history)
    for _ in range(_MAX_TOOL_ROUNDS):
        resp = _http_json("https://api.anthropic.com/v1/messages",
                          {"model": model, "max_tokens": 2048, "system": system,
                           "messages": messages, "tools": tools},
                          {"x-api-key": cfg.get("api_key") or "",
                           "anthropic-version": "2023-06-01"})
        content = resp.get("content") or []
        messages.append({"role": "assistant", "content": content})
        if resp.get("stop_reason") != "tool_use":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            return text.strip() or "(no reply)", messages
        results = []
        for b in content:
            if b.get("type") == "tool_use":
                out = runner.run(b.get("name"), b.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": b.get("id"),
                                "content": _tool_payload(out)})
        messages.append({"role": "user", "content": results})
        _partial = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text")
    return _ran_out(runner, _partial), messages


def _chat_openai(cfg, system, history, runner):
    model = _route_model(cfg.get("model") or DEFAULT_MODELS["openai"],
                         (cfg.get("fast_model") or "").strip(),
                         _last_user_text(history))
    tools = [{"type": "function",
              "function": {"name": t["name"], "description": t["description"],
                           "parameters": t["input_schema"]}} for t in _active_tools(runner)]
    messages = [{"role": "system", "content": system}] + list(history)
    for _ in range(_MAX_TOOL_ROUNDS):
        resp = _http_json("https://api.openai.com/v1/chat/completions",
                          {"model": model, "messages": messages, "tools": tools,
                           "tool_choice": "auto"},
                          {"Authorization": "Bearer %s" % (cfg.get("api_key") or "")})
        msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip() or "(no reply)", messages[1:]
        for c in calls:
            fn = (c.get("function") or {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            out = runner.run(fn.get("name"), args)
            messages.append({"role": "tool", "tool_call_id": c.get("id"),
                             "content": _tool_payload(out)})
        _partial = msg.get("content") or ""
    return _ran_out(runner, _partial), messages[1:]


# ── entry point ──────────────────────────────────────────────────────────────

# ── the fast path is retired (Assist Redesign A3, 6 Aug) ──────────────────────
# There used to be a wall of regexes here that answered "lights off", "mute the
# office" etc. BEFORE the model, for speed. Every new phrasing needed another
# regex, forever — the "write a scenario for every case" trap Dave asked us to
# stop feeding. It's gone. One brain now: every request reasons over the tools,
# and the tools (room_status to LOOK, room_volume/room_media/area_control to
# ACT) make "confirm, don't assume" real. _room_vol_targets and _area_from_text
# stayed — they're the endpoint resolver those tools are built on.


def _room_vol_targets(runner, area_id):
    """The media_player entity Assist should drive for a VOLUME command in this
    room — ENDPOINT-DRIVEN (Endpoint Model Spec v2, 6 Aug).

    The room's verdict already resolves the ACTIVE audio device in `audio_entity`:
    musicstat.decide_music picks the speaker that is PLAYING (or its group
    coordinator), or the room's primary speaker when idle, and the watch verdict
    names the TV-audio device the same way. That is exactly the volume target — it
    can't drift and it follows whatever the homeowner just started (the Office
    Sonos/HomePod case: 'turn it up' moved the HomePod while it played, then the
    Sonos once that was the one playing). Use it directly.

    Falls back to the committed video/audio-volume endpoints only when the verdict
    names no audio device. ([], None) means the room has NO volume endpoint at all
    — Assist says so rather than spraying every player in the area (the old
    device_control default, which moved the wrong device). Returns (entities, ctx)."""
    verdict_eid = "sensor.proos_activity_%s" % area_id
    snap = {}
    try:
        snap = runner.client.snapshot([verdict_eid]) or {}
    except Exception:
        pass
    v = snap.get(verdict_eid) or {}
    vatt = v.get("attributes") or {}
    watching = (str(v.get("state") or "").startswith("watch_")
                or str(vatt.get("activity_key") or "").startswith("watch_"))
    ae = vatt.get("audio_entity")
    if isinstance(ae, str) and ae:
        return [ae], ("video" if watching else "audio")
    # Fallback: the committed endpoints from the record.
    try:
        rec = (runner.project.load().get("areas") or {}).get(area_id) or {}
    except Exception:
        rec = {}
    vv = [e for e in (rec.get("video_volume") or []) if isinstance(e, str) and e]
    av = [e for e in (rec.get("audio_volume") or []) if isinstance(e, str) and e]
    if watching and vv:
        return vv, "video"
    if av:
        return av, "audio"
    if vv:
        return vv, "video"
    return [], None


def _area_from_text(runner, text):
    """A room NAMED in the text ('mute the office') -> (area_id, area_name). The
    longest registry-name match wins ("Bec's Office" over "Office"). (None, None)
    when no room is named — the caller keeps the current-room scope."""
    try:
        areas = runner.client.area_registry() or []
    except Exception:
        areas = []
    tl = " " + " ".join(str(text or "").lower().split()) + " "
    best = None
    for a in areas:
        nm = str(a.get("name") or "").strip()
        aid = a.get("area_id")
        if nm and aid and (" " + nm.lower() + " ") in tl:
            if best is None or len(nm) > len(best[1]):
                best = (aid, nm)
    return best if best else (None, None)


def chat(client, ws_call, project_mod, user: dict, text: str,
         session: str = "default", home_name: str = "", ma=None,
         where: dict | None = None, awareness=None, mcp=None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "empty message"}
    runner = ToolRunner(client, ws_call, project_mod, user, ma=ma,
                        awareness=awareness, mcp=mcp)

    # THE ROOM'S CONTENTS TRAVEL WITH THE ROOM (Dave, 12 Aug, register 106).
    # "We should not have to build every scenario — that's the reason for using
    # Claude or ChatGPT: it is supposed to KNOW, by confirming not assuming."
    # The Office bug was not a missing rule, it was a missing FACT: the model
    # was told where the person was but not what was there, so "turn the lights
    # off" looked perfectly sensible in a room with no lights. Given the
    # contents, the model handles lights, blinds, heating — anything — by
    # reasoning, with no scenario code. One mechanism: the same
    # roomdevices.available() list that area_entities and area_control read.
    if where and where.get("area_id"):
        try:
            contents = _room_contents(client, project_mod, where["area_id"])
            if contents is not None:
                where = dict(where)
                where["contents"] = contents
        except Exception:                                        # noqa: BLE001
            pass                       # unknown stays unknown — never invented

    # No fast-path any more (A3, 6 Aug): one brain handles everything by reasoning
    # over the tools. Every command is a model round-trip — the cost Dave accepted
    # to stop patching a scenario per phrase. A5 (prompt caching + model routing)
    # is what keeps that fast and cheap.
    cfg = load_config()
    if not (cfg.get("provider") and cfg.get("api_key")):
        return {"error": "Pro Assist AI is not configured — set provider + API key in Pro › Tech Tools"}
    key = ((user or {}).get("id") or "anon", session or "default")
    with _LOCK:
        history = _safe_trim(list(_SESSIONS.get(key) or []), _MAX_TURNS)
    history.append({"role": "user", "content": text})
    # What the model is TOLD must match what it is HANDED: the engine's
    # tools are only offered when the engine is linked, so the doctrine
    # only names them then (18 Aug 2026).
    _eng = bool(getattr(runner, "ma", None))
    system = _system_prompt(user, home_name, where, _eng)  # OpenAI + benches
    try:
        if cfg["provider"] == "claude":
            # Claude gets the doctrine + context SPLIT so the doctrine (and the
            # tool block before it) prompt-caches across turns (A5).
            reply, full = _chat_claude(cfg,
                                       _system_doctrine(user, home_name, _eng),
                                       _system_context(user, where), history, runner)
        else:
            reply, full = _chat_openai(cfg, system, history, runner)
    except Exception as e:  # noqa: BLE001
        # The homeowner NEVER sees a raw provider error (Dave, 4 Aug) —
        # the detail goes to the log; the chat gets plain words.
        print("  [assist] provider error: %s" % e, flush=True)
        if "429" in str(e) or "rate_limit" in str(e):
            return {"error": "I'm answering a lot of requests right now — "
                             "give me a few seconds and ask again."}
        return {"error": "I couldn't reach my assistant service just then — "
                         "please try again in a moment."}
    # Persist a TRIMMED window. Tool blocks stay inside the stored turns so the
    # model keeps short-term context of what it just did.
    with _LOCK:
        _SESSIONS[key] = _safe_trim(full, _MAX_TURNS)
    return {"reply": reply, "actions": runner.actions,
            "trace": runner.trace,     # every tool call — checking included
            "provider": cfg["provider"]}


# ── THE PROOF RUN (register 111) ───────────────────────────────────────────
# Dave, 12 Aug, on the tool audit's scoreboard: "I couldn't even tell you
# where you got this from — that's how much this needs to be confirmed."
# He believes what he can test. So the read tools prove themselves ON THE BOX:
# every one runs live and shows its answer beside something the installer can
# check with their own eyes. Read-only is CODE here, not a promise — the
# guard refuses every write and a recorded attempt fails the whole run.

class _ReadOnlyClient:
    """Wraps the real client for a proof run. GET passes through; everything
    else is refused AND recorded. HA's one read-via-POST idiom (response
    services, '?return_response') is refused too — the forecast is the only
    thing lost — but is recorded separately so a pure read doesn't count as
    an attempted write."""

    def __init__(self, real):
        self._real = real
        self.write_attempts = []
        self.refused_reads = []

    def _req(self, method, path, payload=None):
        if (method or "GET").upper() != "GET":
            if "return_response" in (path or ""):
                self.refused_reads.append("%s %s" % (method, path))
            else:
                self.write_attempts.append("%s %s" % (method, path))
            raise RuntimeError("proof run is read-only: refused %s %s"
                               % (method, path))
        return self._real._req(method, path, payload)

    def call_service(self, *a, **k):
        self.write_attempts.append("call_service %s.%s" % (a[0] if a else "?",
                                                           a[1] if len(a) > 1 else "?"))
        raise RuntimeError("proof run is read-only: refused a service call")

    def __getattr__(self, name):
        return getattr(self._real, name)


def _proof_summary(tool, out):
    """The tool's answer in the installer's words — what they can eyeball."""
    if not isinstance(out, dict):
        return str(out)[:200]
    if out.get("error"):
        return "says: %s" % out["error"]
    try:
        if tool == "rooms_overview":
            rs = out.get("rooms") or []
            return "%d room(s): %s" % (len(rs), ", ".join(
                (r.get("name") or "?") + ("" if r.get("committed") else " (uncommitted)")
                for r in rs[:8]) or "none")
        if tool == "home_status":
            wc = (out.get("witness_coverage") or {}).get("statement") or ""
            return "%s — %s" % (out.get("overall") or "?", wc)
        if tool == "health_incidents":
            inc = out.get("incidents") or []
            return ("%d open incident(s)" % len(inc)) + (
                ": " + "; ".join(i.get("title") or "?" for i in inc[:3]) if inc else "")
        if tool == "device_liveness":
            ds = out.get("devices") or []
            conf = sum(1 for d in ds if d.get("witness") == "present")
            gone = sum(1 for d in ds if d.get("witness") == "gone")
            return ("%d device(s) watched — %d independently confirmed on the "
                    "network, %d not answering" % (len(ds), conf, gone))
        if tool == "recovery_history":
            ev = out.get("events") or []
            return "%d recent event(s)%s" % (len(ev),
                " — latest: %s %s" % (ev[0].get("entity"), ev[0].get("event")) if ev else "")
        if tool == "security_status":
            ps = out.get("panels") or []
            zs = out.get("open_zones") or []
            return "%d panel(s) (%s); open right now: %s" % (
                len(ps), ", ".join(sorted({p.get("state") or "?" for p in ps})) or "none",
                ", ".join(z.get("name") or "?" for z in zs[:6]) or "nothing")
        if tool == "locks_status":
            ls = out.get("locks") or []
            if not ls:
                return "no locks in this home"
            return "%d lock(s): %s" % (len(ls), ", ".join(
                "%s %s" % (d.get("attributes", {}).get("friendly_name")
                           or d.get("entity_id"), d.get("state")) for d in ls[:6]))
        if tool == "cameras_status":
            cs = out.get("cameras") or []
            mo = out.get("active_detections") or []
            return "%d camera(s) (%s); live detections: %s" % (
                len(cs), ", ".join(sorted({c.get("state") or "?" for c in cs})) or "none",
                ", ".join(m.get("name") or "?" for m in mo[:6]) or "none")
        if tool == "weather":
            return "%s, %s°" % (out.get("condition") or "?", out.get("temperature"))
        if tool == "scenes_list":
            sc = out.get("scenes") or []
            return "%d Assist-made scene(s)%s" % (len(sc), ": " + ", ".join(
                s.get("name") or "?" for s in sc[:6]) if sc else "")
        if tool == "memory_get":
            return "%d told fact(s), %d learned, %d declined" % (
                len(out.get("facts") or []), len(out.get("learned") or []),
                len(out.get("declines") or []))
        if tool == "room_status":
            eps = out.get("endpoints") or []
            return "activity: %s; %d volume endpoint(s)" % (
                out.get("activity") or "off", len(eps))
        if tool == "room_health":
            return "status: %s%s" % (out.get("status") or out.get("state") or "?",
                                     "; %d device fault(s)" % len(out["device_faults"])
                                     if out.get("device_faults") else "")
        if tool == "usage_history":
            return "journal summarised (%s)" % (", ".join(sorted(out.keys())[:5]))
        if tool == "room_read":
            return "live evidence gathered (%s)" % (", ".join(sorted(out.keys())[:5]))
        if tool == "area_entities":
            es = out.get("entities") or []
            return "%d real device(s) listed" % len(es)
        if tool == "get_states":
            sts = out.get("states") or {}
            return "; ".join("%s = %s" % (e.split(".")[-1], (s or {}).get("state"))
                             for e, s in list(sts.items())[:4]) or "no states"
        if tool == "verify":
            return ("agrees — all checks pass" if out.get("all_pass")
                    else "checks did not pass")
        if tool == "device_powerlog":
            ev = out.get("events") or out.get("log") or []
            return "%d power event(s) in the window" % len(ev)
    except Exception:                                            # noqa: BLE001
        pass
    return "answered (%s)" % (", ".join(sorted(out.keys())[:8]))


def proof_run(client, project_mod, user, ma=None, awareness=None) -> dict:
    """Every READ tool, run live, each answer beside what the installer can
    verify by looking. Installer-gated; read-only BY CONSTRUCTION."""
    if not _is_pro(user):
        return {"error": "installer access required — the proof run is a Pro instrument"}
    guard = _ReadOnlyClient(client)
    runner = ToolRunner(guard, None, project_mod, user, ma=ma,
                        awareness=awareness)
    results = []

    def run(tool, args=None, check=""):
        t0 = time.time()
        try:
            out = getattr(runner, "t_" + tool)(dict(args or {}))
        except Exception as e:                                   # noqa: BLE001
            results.append({"tool": tool, "ok": False,
                            "ms": int((time.time() - t0) * 1000),
                            "summary": "crashed: %s" % e, "check": check})
            return None
        results.append({"tool": tool, "ok": True,
                        "ms": int((time.time() - t0) * 1000),
                        "summary": _proof_summary(tool, out), "check": check})
        return out

    run("rooms_overview", check="the rooms you commissioned, by name")
    run("home_status", check="the Health page header")
    run("health_incidents", check="the incidents open on Health")
    run("device_liveness", check="the device counts on Health")
    run("recovery_history", {"limit": 10}, check="recoveries you know happened")
    run("security_status", check="your panels, and any door or window standing open")
    run("locks_status", check="your locks (or that you have none)")
    run("cameras_status", check="your cameras, and whatever is moving right now")
    run("weather", check="the sky outside")
    run("scenes_list", check="the scenes Assist has made")
    run("memory_get", check="what it remembers about you")

    rooms = []
    try:
        for key, rec in ((project_mod.load() or {}).get("areas") or {}).items():
            if rec and rec.get("committed"):
                rooms.append(rec.get("area_id") or key)
    except Exception:                                            # noqa: BLE001
        pass
    for i, aid in enumerate(rooms[:3]):
        run("room_status", {"area_id": aid},
            check="what the %s is doing right now" % aid)
        run("room_health", {"area_id": aid}, check="its verdicts on Health")
        if i == 0:
            run("usage_history", {"area_id": aid}, check="its learned habits")
            run("room_read", {"area_id": aid}, check="its live evidence bundle")
            run("area_entities", {"area_id": aid}, check="its real device list")
            verdict_eid = "sensor.proos_activity_%s" % aid
            gs = run("get_states", {"entity_ids": [verdict_eid]},
                     check="the room's verdict sensor")
            st = None
            try:
                st = (((gs or {}).get("states") or {}).get(verdict_eid) or {}).get("state")
            except Exception:                                    # noqa: BLE001
                pass
            if st is not None:
                v = run("verify",
                        {"checks": [{"entity_id": verdict_eid, "expect_state": st}]},
                        check="verify and get_states agree — two tools, one truth")
                if v is not None and not v.get("all_pass"):
                    results[-1]["ok"] = False
                    results[-1]["summary"] = ("verify DISAGREED with get_states "
                                              "on the same entity — defect")
    if not rooms:
        # No committed rooms yet: the room tools still answer honestly.
        for tool in ("room_status", "room_health", "usage_history",
                     "room_read", "area_entities"):
            run(tool, {"area_id": "none"},
                check="no committed rooms — an honest refusal is the pass")
        run("get_states", {"entity_ids": ["sensor.proos_home_summary"]},
            check="the home summary sensor")
        run("verify", {"checks": []}, check="an empty check set")

    ok = all(r["ok"] for r in results) and not guard.write_attempts
    return {"ok": ok, "ran": len(results),
            "passed": sum(1 for r in results if r["ok"]),
            "write_attempts": list(guard.write_attempts),
            "skipped_reads": list(guard.refused_reads),
            "read_only": "enforced in code — every write refused and recorded",
            "results": results,
            "note": ("each row's answer should match what you can see with "
                     "your own eyes; a row that doesn't is a defect — report "
                     "the row, not a feeling")}


def test_provider() -> dict:
    """One tiny round-trip to prove the key + model work. Tech-gated route."""
    cfg = load_config()
    if not (cfg.get("provider") and cfg.get("api_key")):
        return {"ok": False, "error": "not configured"}
    try:
        if cfg["provider"] == "claude":
            r = _http_json("https://api.anthropic.com/v1/messages",
                           {"model": cfg.get("model") or DEFAULT_MODELS["claude"],
                            "max_tokens": 8, "messages": [{"role": "user", "content": "ping"}]},
                           {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"})
            ok = bool(r.get("content"))
        else:
            r = _http_json("https://api.openai.com/v1/chat/completions",
                           {"model": cfg.get("model") or DEFAULT_MODELS["openai"],
                            "max_tokens": 8, "messages": [{"role": "user", "content": "ping"}]},
                           {"Authorization": "Bearer %s" % cfg["api_key"]})
            ok = bool(r.get("choices"))
        return {"ok": ok}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
