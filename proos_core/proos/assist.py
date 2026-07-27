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
import urllib.request
import urllib.error

_CFG_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "assist.json")
_LOCK = threading.Lock()
_SESSIONS: dict = {}          # (user_id, session) -> [ {role, content}, ... ]
_MAX_TURNS = 24               # rolling window (user+assistant messages kept)
_MAX_TOOL_ROUNDS = 8
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
    for k in ("provider", "model"):
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
                    "scripts own the device ordering that makes it reliable.",
     "input_schema": {"type": "object", "properties": {
         "script_entity_id": {"type": "string", "description": "script.proos_* entity id"}},
         "required": ["script_entity_id"]}},
    {"name": "device_control",
     "description": "Control ONE device by entity_id with a whitelisted action. Media player POWER "
                    "is rejected here by design — use room_activity. data carries service fields "
                    "(brightness_pct, volume_level 0-1, temperature, source, position...).",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"},
         "action": {"type": "string", "description": "one of: " + ", ".join(sorted(_DEVICE_ACTIONS))},
         "data": {"type": "object", "description": "optional service data"}},
         "required": ["entity_id", "action"]}},
    {"name": "area_control",
     "description": "Control a whole room at once, live-resolved from the registry: lights / "
                    "switches / fans on-off, covers open-close, pause media. Use for 'turn off the "
                    "office lights' style requests. Domains: " + ", ".join(sorted(_AREA_DOMAINS)),
     "input_schema": {"type": "object", "properties": {
         "area_id": {"type": "string"},
         "domain": {"type": "string"},
         "action": {"type": "string"},
         "data": {"type": "object"}},
         "required": ["area_id", "domain", "action"]}},
    {"name": "get_states",
     "description": "Read the live state + key attributes of up to 40 entities by id.",
     "input_schema": {"type": "object", "properties": {
         "entity_ids": {"type": "array", "items": {"type": "string"}}},
         "required": ["entity_ids"]}},
    {"name": "app_launch",
     "description": "Open a streaming app (Netflix, Disney+, YouTube…) in a room. A room can have "
                    "several devices that run apps — the smart TV, an Apple TV, a Shield. Call with "
                    "just area_id + app first: if only one device has it, it launches; if MORE than "
                    "one does, you get {needs_choice, options:[{entity_id,name}]} — ASK the user "
                    "which device, then call again with `device` set to their chosen entity_id. If "
                    "the app isn't anywhere you get the available list — say so, don't guess. Make "
                    "sure the display is ON first (run the room's watch activity). Verify after.",
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
                    "on a command echo.",
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
    {"name": "memory_get",
     "description": "Recall the pinned facts you've saved about this user (preferences, routines). "
                    "Call at the start of a conversation when personalisation would help.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "memory_set",
     "description": "Pin a durable fact about this user for future conversations (e.g. 'likes jazz "
                    "at dinner', 'prefers the kitchen HomePod'). Keep facts short and factual.",
     "input_schema": {"type": "object", "properties": {
         "fact": {"type": "string"},
         "forget": {"type": "boolean", "description": "true to remove a previously-pinned fact matching `fact`"}},
         "required": ["fact"]}},
    {"name": "scenes_list",
     "description": "List existing ProOS-created scenes (name + entity_id) so you can apply, "
                    "update or delete one. Only scenes this assistant made are shown.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "scene_create",
     "description": "Create a NEW scene, or UPDATE one you already made. A scene is a saved set of "
                    "device states you recall in one go (e.g. 'Movie Night' = lights 15%). states is "
                    "a list of {entity_id, state, attributes} — capture LIGHTS, climate and covers "
                    "for the mood. NEVER include media players (media_player.*): a scene can't start "
                    "playback (use music_play) and TV/AV power is a room activity. To make a new "
                    "scene, omit scene_entity_id — a fresh scene is created even if another room has "
                    "the same name. To change a scene you created, pass its scene_entity_id (do NOT "
                    "rely on the name). photo_query is a short vivid MOOD description (e.g. 'dim "
                    "cinema room, warm glow') matched to a dashboard photo. After creating, apply and "
                    "verify (the test loop). Reuse committed member ids from rooms_overview.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "scene_entity_id": {"type": "string", "description": "ONLY to update an existing ProOS scene; omit for a new one"},
         "states": {"type": "array", "items": {"type": "object", "properties": {
             "entity_id": {"type": "string"},
             "state": {"type": "string"},
             "attributes": {"type": "object", "description": "e.g. brightness_pct, color_temp"}},
             "required": ["entity_id", "state"]}},
         "photo_query": {"type": "string", "description": "vivid mood description to match a photo"}},
         "required": ["name", "states"]}},
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
                    "are lists of standard HA config dicts. Prefer firing a scene or a room activity "
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
    {"name": "device_recover",
     "description": "Run the configured self-heal for one faulted device — integration "
                    "reload, or the installer-assigned smart-plug/PoE power-cycle when "
                    "one is set. Same executor the automatic recovery uses. Installer/"
                    "tech/owner only; check room_health or home_status first and only "
                    "recover devices that actually show a fault. Follow with verify.",
     "input_schema": {"type": "object", "properties": {
         "entity_id": {"type": "string"}}, "required": ["entity_id"]}},
]

_ATTR_KEYS = ("friendly_name", "brightness", "volume_level", "source", "media_title",
              "app_name", "current_temperature", "temperature", "hvac_mode",
              "current_position", "device_class", "supported_color_modes",
              "supported_features", "hvac_modes")


def _light_caps(attrs: dict) -> dict:
    """What a light can actually do, from supported_color_modes. A light whose
    ONLY mode is onoff can't dim — so the assistant must not promise brightness."""
    modes = [str(m).lower() for m in (attrs.get("supported_color_modes") or [])]
    dimmable = any(m not in ("onoff", "unknown") for m in modes) if modes else False
    color = any(m in ("hs", "rgb", "rgbw", "rgbww", "xy") for m in modes)
    color_temp = "color_temp" in modes
    return {"dimmable": dimmable, "color": color, "color_temp": color_temp}

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


def _slim_state(snap_val) -> dict:
    st = (snap_val or {})
    a = st.get("attributes") or {}
    return {"state": st.get("state"),
            "attributes": {k: a.get(k) for k in _ATTR_KEYS if a.get(k) is not None}}


class ToolRunner:
    """Executes tool calls for one chat turn as one authenticated caller."""

    def __init__(self, client, ws_call, project_mod, user: dict, ma=None,
                 awareness=None):
        self.client = client
        self.ws_call = ws_call
        self.project = project_mod
        self.ma = ma               # MaCommissioner (music tools); None if unlinked
        # Bridge to the awareness layer: dict of callables supplied by server.py
        # (watchers report, monitor, per-room health, audit trail, recover).
        # The whole point of ProAssist is that it answers from these VERDICTS —
        # a "Pro in the box" who has actually looked, not one who guesses.
        self.awareness = awareness or {}
        self.user = user or {}
        self.actions = []          # audit of every side-effect this turn

    def _audit(self, tool, **info):
        rec = {"tool": tool, **info}
        self.actions.append(rec)
        # Same log stream the watcher uses, so every side-effect is traceable.
        print("  [assist] %s %s by %s(%s)" % (
            tool, {k: v for k, v in info.items() if k != 'result'},
            self.user.get("name") or "?", _tier(self.user)), flush=True)

    # -- helpers ------------------------------------------------------------
    def _members(self, proj) -> set:
        out = set()
        for rec in (proj or {}).get("areas", {}).values():
            if rec and rec.get("committed"):
                for e in ([rec.get("display")] + list(rec.get("sources") or [])
                          + list(rec.get("audio") or [])):
                    if e:
                        out.add(e)
        return out

    def run(self, name: str, args: dict):
        fn = getattr(self, "t_" + name, None)
        if not fn:
            return {"error": "unknown tool %s" % name}
        try:
            return fn(args or {})
        except Exception as e:  # noqa: BLE001 - the model must see failures, not stack traces
            return {"error": str(e)}

    # -- tools --------------------------------------------------------------
    def t_rooms_overview(self, args):
        proj = self.project.load()
        areas = {a.get("area_id"): a.get("name") for a in (self.client.area_registry() or [])}
        rooms, ents = [], []
        for key, rec in (proj or {}).get("areas", {}).items():
            if not rec:
                continue
            members = []
            for role, ids in (("display", [rec.get("display")]),
                              ("source", rec.get("sources") or []),
                              ("speaker", rec.get("audio") or [])):
                for e in ids:
                    if e and all(m["entity_id"] != e for m in members):
                        members.append({"entity_id": e, "role": role})
                        ents.append(e)
            aid = rec.get("area_id") or key
            rooms.append({"area_id": aid,
                          "name": rec.get("name") or areas.get(aid) or key,
                          "kind": rec.get("kind"),
                          "committed": bool(rec.get("committed")),
                          "members": members,
                          "activities": []})
        # live states for members (one snapshot), activities from stored scripts
        snap = self.client.snapshot(ents) if ents else None
        for r in rooms:
            for m in r["members"]:
                sv = snap.get(m["entity_id"]) if snap else None
                m.update(_slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {}))
            if r["committed"]:
                try:
                    st = self.project.activities_status(self.client, self.project.load(), r["area_id"])
                    r["activities"] = [{"script_entity_id": a.get("entity_id") or ("script." + a.get("object_id", "")),
                                        "name": a.get("alias") or a.get("kind"),
                                        "kind": a.get("kind")}
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
                        r["music_speaker"] = spk
                except Exception:
                    pass
        return {"rooms": rooms,
                "note": "entity ids and area ids are identity — names are display only. "
                        "music_play works in any room with a music_speaker."}

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
        payload = {"area_id": aid}
        payload.update({k: v for k, v in data.items() if k not in ("entity_id", "area_id")})
        self.client._req("POST", "/api/services/%s/%s" % (domain, action), payload)
        self.actions.append({"tool": "area_control", "target": aid,
                             "action": "%s.%s" % (domain, action)})
        return {"ok": True, "called": "%s.%s" % (domain, action), "area_id": aid}

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

    def t_verify(self, args):
        checks = args.get("checks") or []
        ids = [c.get("entity_id") for c in checks if c.get("entity_id")][:40]
        snap = self.client.snapshot(ids) if ids else {}
        results = []
        for c in checks:
            e = c.get("entity_id")
            sv = snap.get(e)
            cur = _slim_state(sv if isinstance(sv, dict) else getattr(sv, "__dict__", {}) or {})
            ok = True
            why = []
            if c.get("expect_state") is not None and cur.get("state") != c["expect_state"]:
                ok = False
                why.append("state is %s, expected %s" % (cur.get("state"), c["expect_state"]))
            for k, v in (c.get("expect_attr") or {}).items():
                if (cur.get("attributes") or {}).get(k) != v:
                    ok = False
                    why.append("%s is %s, expected %s" % (k, (cur.get("attributes") or {}).get(k), v))
            results.append({"entity_id": e, "pass": ok,
                            "actual": cur, "why": "; ".join(why) or "as expected"})
        return {"results": results, "all_pass": all(r["pass"] for r in results) if results else False}

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
            ea = e.get("area_id") or dev_area.get(e.get("device_id"))
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
                    "note": "these are the room's available devices (installer-committed). Use these "
                            "EXACT entity_ids in scene_create. Respect caps: only set brightness "
                            "where caps.dimmable is true; an on/off light can only be on/off. Never "
                            "invent ids."}
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
                "note": "use these EXACT entity_ids in scene_create. Respect caps: only set "
                        "brightness on a light where caps.dimmable is true; an on/off light can "
                        "only be on/off. Never invent ids."}

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
            ea = e.get("area_id") or dev_area.get(e.get("device_id"))
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
            return {"error": "music isn't linked (ProOS Music not set up)"}
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "query required"}
        limit = int(args.get("limit") or 6)
        res = self.ma.search(q, media_types=args.get("kinds") or None, limit=limit)
        return {"results": self._slim_search(res),
                "note": "pass a result's uri to music_play or music_playlist_create"}

    def t_music_play(self, args):
        if not self.ma:
            return {"error": "music isn't linked (ProOS Music not set up)"}
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
            return {"error": "music isn't linked (ProOS Music not set up)"}
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
                out.append({"entity_id": eid,
                            "name": (s.get("attributes") or {}).get("friendly_name") or eid})
        return {"scenes": out}

    def t_scene_create(self, args):
        name = (args.get("name") or "").strip()
        states = args.get("states") or []
        if not name or not states:
            return {"error": "name and states required"}
        entities = {}
        unknown = []
        dropped = []
        for st in states:
            e = (st.get("entity_id") or "").strip()
            if "." not in e:
                continue
            # Media players never belong in a scene: a scene can only RESTORE a
            # state, it can't start playback (that's music_play) and AV power is a
            # room activity. A captured 'playing'/'paused'/'idle' just makes the
            # scene inert. Reject so the model captures real ambiance instead.
            if e.startswith("media_player."):
                return {"error": ("%s is a media player — leave it out. A scene can't start "
                                  "playback: use music_play for audio, or a room activity for "
                                  "TV/AV power. Capture lights, climate and covers for the mood." % e)}
            # The entity must ACTUALLY exist — don't let a guessed id like
            # 'light.office_lamp' get saved into a scene that then does nothing.
            live = None
            try:
                live = self.client._req("GET", "/api/states/%s" % e)
            except Exception:
                live = None
            if not (live and live.get("entity_id") == e):
                unknown.append(e)
                continue
            ent = {"state": st.get("state")}
            attrs = dict(st.get("attributes") or {})
            # Don't promise capabilities the device lacks. An on/off light can't
            # dim or take colour — strip those so the scene sets what it CAN, and
            # verify won't later report a false failure on brightness.
            if e.startswith("light."):
                caps = _light_caps(live.get("attributes") or {})
                if not caps["dimmable"]:
                    for k in ("brightness", "brightness_pct"):
                        if k in attrs:
                            attrs.pop(k, None)
                            dropped.append((e, "brightness", "on/off light"))
                if not (caps["color"] or caps["color_temp"]):
                    for k in ("rgb_color", "hs_color", "xy_color", "color_temp", "color_temp_kelvin", "color_name"):
                        if k in attrs:
                            attrs.pop(k, None)
                            dropped.append((e, "colour", "no colour support"))
            for k, v in attrs.items():
                ent[k] = v
            entities[e] = ent
        if unknown:
            return {"error": ("these entity_ids don't exist: %s. Call area_entities(area_id) to get "
                              "the room's REAL light/cover/climate ids and use those exact ids — "
                              "never invent an entity_id." % ", ".join(unknown))}
        if not entities:
            return {"error": ("no valid entities. Call area_entities(area_id) to list the room's real "
                              "lights/climate/covers, then capture those (not media players).")}
        # UPDATE in place only when the caller names the scene to change; otherwise
        # a NEW scene gets a fresh, non-colliding id so it never overwrites another
        # room's same-named scene (the display name can stay short).
        upd_eid = (args.get("scene_entity_id") or "").strip()
        if upd_eid:
            cid = self._scene_cfg_id(upd_eid)
            if not str(cid or "").startswith("proos_assist_"):
                return {"error": "can only update scenes ProOS Assist created; omit scene_entity_id to make a new one"}
            sid = cid
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
        self._audit("scene_create", name=name, entity=seid, entities=len(entities))
        out = {"ok": True, "scene_entity_id": seid, "name": name,
               "photo_source": photo_source,
               "next": "apply it with scene_apply, verify the entities reached these states, "
                       "then ASK the user if they'd like it on their dashboard (scene_dashboard)"}
        if dropped:
            # e.g. "office main light: brightness (on/off light)". Tell the user so
            # the assistant never silently promises dimming a device can't do.
            out["adjusted"] = ["%s: %s (%s)" % (e, what, why) for (e, what, why) in dropped]
            out["adjusted_note"] = ("some requested settings weren't supported by the device and were "
                                    "left off — tell the user plainly (e.g. that light is on/off only, "
                                    "so it can't dim). Don't report these as failures in verify.")
        if photo_source == "curated_no_key":
            out["photo_note"] = ("used a curated photo — AI-generated scene images need an OpenAI "
                                 "image key (Pro › Tech Tools › Assist AI). Mention this if the user "
                                 "expected a custom picture.")
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
        if not eid.startswith("scene."):
            return {"error": "scene_entity_id required"}
        self.client._req("POST", "/api/services/scene/turn_on", {"entity_id": eid})
        self._audit("scene_apply", entity=eid)
        return {"ok": True, "applied": eid, "next": "verify the target entities now match the scene"}

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
    def t_home_status(self, args):
        fn = self.awareness.get("watchers")
        if not fn:
            return {"error": "the awareness layer isn't running — device health is not visible right now"}
        rep = fn() or {}
        items = rep.get("items") or []
        faults = [i for i in items if i.get("status") == "fault"]
        amber = [i for i in items if i.get("status") == "amber"]
        out = {"overall": rep.get("status"), "summary": rep.get("summary"),
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
        if not _is_pro(self.user):
            return {"error": "recovery needs installer or tech access — I can flag this "
                             "for the installer instead"}
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
        self._audit("device_recover", entity=eid, result=out.get("ok"))
        return out

    # -- memory (phase 2) ---------------------------------------------------
    def t_memory_get(self, args):
        uid = self.user.get("id") or "anon"
        facts = (_mem_load().get(uid) or {}).get("facts") or []
        return {"facts": facts}

    def t_memory_set(self, args):
        fact = (args.get("fact") or "").strip()
        if not fact:
            return {"error": "fact required"}
        uid = self.user.get("id") or "anon"
        store = _mem_load()
        rec = store.setdefault(uid, {"facts": []})
        facts = rec.setdefault("facts", [])
        if args.get("forget"):
            low = fact.lower()
            rec["facts"] = [f for f in facts if low not in f.lower()]
        else:
            if fact not in facts:
                facts.append(fact)
                rec["facts"] = facts[-_MEM_MAX:]
        _mem_save(store)
        self._audit("memory_set", forget=bool(args.get("forget")))
        return {"ok": True, "facts": rec["facts"]}


# ── system prompt ────────────────────────────────────────────────────────────

def _where_prompt(where: dict) -> str:
    """Tell the model WHERE the person is standing.

    Without this every bare request is a guessing game: "turn the lights off"
    has no answer, so the assistant either interrogates the user or picks a
    room at random. A person speaking in their kitchen means the kitchen, and
    an assistant that has to ask is the thing that makes it feel like software
    rather than a house that understands you."""
    if not where or not where.get("area_id"):
        return ""
    name = where.get("area_name") or where.get("area_id")
    return (
        "\nWHERE THEY ARE: %s (area_id '%s'). Anything said without naming a room means "
        "THIS room — 'the lights', 'in here', 'turn it off', 'play something'. Act on %s "
        "without asking which room. Only ask when they name no room AND the request "
        "genuinely cannot apply here. If they name a different room, use that one.\n"
        % (name, where.get("area_id"), name))


def _system_prompt(user: dict, home_name: str, where: dict | None = None) -> str:
    who = (user or {}).get("name") or "the user"
    tier = _tier(user)
    facts = (_mem_load().get((user or {}).get("id") or "anon") or {}).get("facts") or []
    mem = ("\nWhat you remember about %s: %s." % (who, "; ".join(facts))) if facts else ""
    mem += _where_prompt(where or {})
    return (
        "You are Pro Assist, the ProOS home assistant for '%s'. You are talking with %s "
        "(role: %s). Be natural, brief and conversational — a capable house manager, not a robot. "
        "Answer in one short spoken sentence: say what you did, not how. No preamble, no "
        "restating the request, no lists unless asked.%s\n"
        "Rules that are enforced and must shape your behaviour:\n"
        "1. Ground yourself with rooms_overview before acting on rooms or media; entity/area ids "
        "are identity, names are display-only.\n"
        "2. AV power and source switching go through room_activity ONLY. Lights, covers, climate, "
        "volume and transport use device_control / area_control. To open a streaming app (Netflix, "
        "Disney+, YouTube…) run the room's watch activity to power the display on, THEN app_launch; "
        "if app_launch returns needs_choice (several devices can run it) ASK which, then call again "
        "with device set — never pick for them.\n"
        "3. MUSIC: music_search to find something, music_play to play it in a room (by area_id), "
        "music_playlist_create to build a personalised playlist (search for tracks, then create "
        "with their uris). Music only works in rooms with a committed speaker.\n"
        "4. MEMORY: use memory_get when personalisation helps, and memory_set to remember a "
        "durable preference the user shares. Don't pester — save only meaningful, lasting facts.\n"
        "5. SCENES capture LIGHTS/climate/covers for a mood — NEVER media players (a scene can't "
        "start playback: use music_play; TV/AV power is a room activity). BEFORE creating a scene, "
        "call area_entities(area_id) to get the room's REAL entity ids, states AND capabilities — "
        "build the scene from those exact ids; NEVER invent an id like 'light.office_lamp' "
        "(scene_create rejects unknown ids). RESPECT capabilities: only set brightness on a light "
        "whose caps.dimmable is true, and colour only where caps.color/color_temp is true — an "
        "on/off light can ONLY be on/off, so don't promise dimming it. If scene_create returns "
        "'adjusted', tell the user plainly what the device couldn't do (e.g. 'Office Main is an "
        "on/off light so it turns on but can't dim') — that is NOT a failure, don't retry it. If a "
        "light is 'unavailable' it's offline — you may still include it, but say it was offline "
        "during the test. ALWAYS follow the "
        "create-test loop — create, scene_apply, verify the entities really reached those states, "
        "adjust if not, before telling the user it's ready. Give scenes SHORT names (2-3 words, "
        "e.g. 'Relaxed Evening') — the room is known. Making a scene for a room that already has a "
        "same-named one in ANOTHER room is fine: omit scene_entity_id and a separate scene is "
        "created (it will NOT overwrite the other). To MODIFY a scene you just made, call "
        "scene_create again WITH its scene_entity_id — never rely on the name to update. "
        "THEN ask if they'd like it on their "
        "dashboard and, if yes, call scene_dashboard. Give scene_create a vivid photo_query "
        "describing the mood so a fitting image is matched to the scene; scene_photo re-matches if "
        "they want a different picture. Keep AV power out of scenes (use activities). automation_create builds time/state "
        "automations (installer/tech only); test with automation_trigger then verify.\n"
        "6. After any control action, verify and tell the user what ACTUALLY happened; if it "
        "failed, say so plainly and offer the fix.\n"
        "7. Ambiguity → ask one short question instead of guessing.\n"
        "8. Destructive actions (deleting, overwriting) must be confirmed with the user first.\n"
        "9. AWARENESS — you can SEE the home. For 'is everything ok', 'any problems', or any "
        "general state question call home_status; when something 'isn't working' or 'won't play' "
        "call room_health for that room FIRST and answer from its verdicts and guidance — never "
        "speculate, never walk the user through generic troubleshooting when the diagnosis is one "
        "call away. recovery_history tells you what has happened (faults, recoveries) — check it "
        "before promising a flaky device is fine. A device in 'standby' is asleep, which is NORMAL "
        "resting state for TVs and streamers — never report standby as a problem.\n"
        "10. FIXING: when a fault has a configured recovery, device_recover runs the same "
        "self-heal the watcher uses (installer/tech/owner only), then verify and report what "
        "actually happened. For a homeowner, explain the issue in plain language, do NOT attempt "
        "repairs; offer to pass it to their installer instead.\n"
        "11. COMPOUND requests ('dim the lights and put on some jazz') are normal: do every part, "
        "in sensible order, and confirm in ONE sentence. Follow-ups lean on context — 'turn it up' "
        "means whatever you just acted on."
        + ("" if _is_pro(user) else "\nYou are talking with a HOMEOWNER: plain, everyday language "
           "only. Say 'I'll let your installer know' rather than technical detail; name devices "
           "the way the home does, never integrations, entity ids or platform terms. You may "
           "control their home and manage music/playlists, but not commission devices or rooms, "
           "and never attempt repairs.")
    ) % (home_name or "this home", who, tier, mem)


# ── provider adapters ────────────────────────────────────────────────────────

def _http_json(url, payload, headers):
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
        raise RuntimeError("provider HTTP %s: %s" % (e.code, body)) from e


def _chat_claude(cfg, system, history, runner):
    model = cfg.get("model") or DEFAULT_MODELS["claude"]
    tools = [{"name": t["name"], "description": t["description"],
              "input_schema": t["input_schema"]} for t in TOOLS]
    messages = list(history)
    for _ in range(_MAX_TOOL_ROUNDS):
        resp = _http_json("https://api.anthropic.com/v1/messages",
                          {"model": model, "max_tokens": 1024, "system": system,
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
                                "content": json.dumps(out)[:8000]})
        messages.append({"role": "user", "content": results})
    return "I hit my action limit for one request — tell me if you'd like me to continue.", messages


def _chat_openai(cfg, system, history, runner):
    model = cfg.get("model") or DEFAULT_MODELS["openai"]
    tools = [{"type": "function",
              "function": {"name": t["name"], "description": t["description"],
                           "parameters": t["input_schema"]}} for t in TOOLS]
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
                             "content": json.dumps(out)[:8000]})
    return "I hit my action limit for one request — tell me if you'd like me to continue.", messages[1:]


# ── entry point ──────────────────────────────────────────────────────────────

# ── fast path ────────────────────────────────────────────────────────────────
# The everyday commands are a closed set, and sending them to a cloud model to
# be re-derived every time is why the assistant feels slow. "Lights off" should
# be instant and should still work when the internet is down; only genuinely
# open-ended language needs the model. So: match confidently or fall through —
# a wrong guess is far worse than a round-trip, and anything ambiguous goes to
# the model untouched.
_FAST_OFF = re.compile(r"^(turn |switch |put )?(the )?lights?( in here| here)? (off|out)$|"
                       r"^lights? (off|out)$|^(kill|all) lights$|"
                       r"^(turn|switch|put) (off|out) (the )?lights?( in here| here)?$", re.I)
_FAST_ON = re.compile(r"^(turn |switch |put )?(the )?lights?( in here| here)? on$|^lights? on$|"
                      r"^(turn|switch|put) on (the )?lights?( in here| here)?$", re.I)
_FAST_DIM = re.compile(r"^(set |put |dim |turn )?(the )?lights?( in here| here)?"
                       r"( to| at| down to| up to)? (\d{1,3})\s*%?$", re.I)
_FAST_ROOM_OFF = re.compile(r"^(turn |switch )?(everything|it all|all of it) off"
                            r"( in here| here)?$|^(room|everything) off$", re.I)
_FAST_VOL = re.compile(r"^(turn (it |the volume )?)?(up|down)$|^volume (up|down)$|"
                       r"^(louder|quieter)$", re.I)
_FAST_MUTE = re.compile(r"^(mute|unmute|silence)( it| that)?$", re.I)
_FAST_PAUSE = re.compile(r"^(pause|stop|resume|play|continue)( it| that| music)?$", re.I)


def _fast_intent(runner, text: str, where: dict):
    """Handle a common command locally. Returns a spoken reply, or None.

    None means "not confident" — the request goes to the model exactly as if
    this function didn't exist."""
    area = (where or {}).get("area_id")
    if not area:
        return None                      # no room context -> nothing is unambiguous
    t = " ".join((text or "").strip().rstrip("."). split())
    room = (where or {}).get("area_name") or "here"

    def light(**svc):
        r = runner.run("area_control", {"area_id": area, "domain": "light", **svc})
        return None if (r or {}).get("error") else r

    if _FAST_OFF.match(t):
        return "Lights off." if light(service="turn_off") else None
    if _FAST_ON.match(t):
        return "Lights on." if light(service="turn_on") else None
    m = _FAST_DIM.match(t)
    if m:
        pct = max(1, min(100, int(m.group(5))))
        ok = light(service="turn_on", data={"brightness_pct": pct})
        return ("Lights to %d%%." % pct) if ok else None
    if _FAST_ROOM_OFF.match(t):
        r = runner.run("room_activity", {"area_id": area, "activity": "off"})
        return ("Everything off in the %s." % room) if not (r or {}).get("error") else None
    if _FAST_MUTE.match(t):
        want = not t.lower().startswith("un")
        r = runner.run("device_control", {"area_id": area, "domain": "media_player",
                                          "service": "volume_mute",
                                          "data": {"is_volume_muted": want}})
        return ("Muted." if want else "Unmuted.") if not (r or {}).get("error") else None
    m = _FAST_VOL.match(t)
    if m:
        up = bool(re.search(r"up|louder", t, re.I))
        r = runner.run("device_control", {"area_id": area, "domain": "media_player",
                                          "service": "volume_up" if up else "volume_down"})
        return ("Turned it %s." % ("up" if up else "down")) if not (r or {}).get("error") else None
    m = _FAST_PAUSE.match(t)
    if m:
        word = m.group(1).lower()
        svc = "media_pause" if word in ("pause", "stop") else "media_play"
        r = runner.run("device_control", {"area_id": area, "domain": "media_player",
                                          "service": svc})
        return ("Paused." if svc == "media_pause" else "Playing.") \
            if not (r or {}).get("error") else None
    return None


def chat(client, ws_call, project_mod, user: dict, text: str,
         session: str = "default", home_name: str = "", ma=None,
         where: dict | None = None, awareness=None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "empty message"}
    runner = ToolRunner(client, ws_call, project_mod, user, ma=ma,
                        awareness=awareness)

    # Everyday commands answer locally: instant, and they keep working with the
    # internet down. Deliberately BEFORE the config check — a home shouldn't
    # stop taking "lights off" because nobody set an API key.
    try:
        quick = _fast_intent(runner, text, where or {})
    except Exception:                                   # never let it break chat
        quick = None
    if quick:
        return {"reply": quick, "actions": runner.actions, "provider": "local"}

    cfg = load_config()
    if not (cfg.get("provider") and cfg.get("api_key")):
        return {"error": "Pro Assist AI is not configured — set provider + API key in Pro › Tech Tools"}
    key = ((user or {}).get("id") or "anon", session or "default")
    with _LOCK:
        history = list(_SESSIONS.get(key) or [])
    history.append({"role": "user", "content": text})
    system = _system_prompt(user, home_name, where)
    try:
        if cfg["provider"] == "claude":
            reply, full = _chat_claude(cfg, system, history, runner)
        else:
            reply, full = _chat_openai(cfg, system, history, runner)
    except Exception as e:  # noqa: BLE001
        return {"error": "assistant unavailable: %s" % e}
    # Persist a TRIMMED window. Tool blocks stay inside the stored turns so the
    # model keeps short-term context of what it just did.
    with _LOCK:
        _SESSIONS[key] = full[-_MAX_TURNS:]
    return {"reply": reply, "actions": runner.actions,
            "provider": cfg["provider"]}


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
