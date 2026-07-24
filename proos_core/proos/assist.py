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
            "has_key": bool(cfg.get("api_key"))}


def clear_data() -> None:
    """Factory-reset hook: forget provider config and every session."""
    try:
        if os.path.exists(_CFG_PATH):
            os.remove(_CFG_PATH)
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
]

_ATTR_KEYS = ("friendly_name", "brightness", "volume_level", "source", "media_title",
              "app_name", "current_temperature", "temperature", "hvac_mode",
              "current_position", "device_class")


def _slim_state(snap_val) -> dict:
    st = (snap_val or {})
    a = st.get("attributes") or {}
    return {"state": st.get("state"),
            "attributes": {k: a.get(k) for k in _ATTR_KEYS if a.get(k) is not None}}


class ToolRunner:
    """Executes tool calls for one chat turn as one authenticated caller."""

    def __init__(self, client, ws_call, project_mod, user: dict):
        self.client = client
        self.ws_call = ws_call
        self.project = project_mod
        self.user = user or {}
        self.actions = []          # audit of every side-effect this turn

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
        return {"rooms": rooms,
                "note": "entity ids and area ids are the identity — names are display only"}

    def t_room_activity(self, args):
        eid = (args.get("script_entity_id") or "").strip()
        if not re.match(r"^script\.proos_[a-z0-9_]+$", eid):
            return {"error": "not a ProOS activity script: %s" % eid}
        self.client.call_service("script", "turn_on", eid)
        self.actions.append({"tool": "room_activity", "target": eid})
        return {"ok": True, "fired": eid,
                "next": "verify the outcome with the verify tool before reporting success"}

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


# ── system prompt ────────────────────────────────────────────────────────────

def _system_prompt(user: dict, home_name: str) -> str:
    who = (user or {}).get("name") or "the user"
    return (
        "You are Pro Assist, the ProOS home assistant for '%s'. You are talking with %s. "
        "Be natural, brief and conversational — a capable house manager, not a robot.\n"
        "Rules that are enforced and must shape your behaviour:\n"
        "1. Ground yourself with rooms_overview before acting on rooms or media; entity/area ids "
        "are identity, names are display-only.\n"
        "2. AV power and source switching go through room_activity ONLY (the room's committed "
        "choreography). Lights, covers, climate, volume and transport may use device_control / "
        "area_control.\n"
        "3. After any action, verify with the verify tool and tell the user what ACTUALLY "
        "happened. If something failed, say so plainly and offer the fix.\n"
        "4. If a request is ambiguous (two rooms, two TVs), ask one short question instead of "
        "guessing.\n"
        "5. You cannot create or edit scenes and automations yet — say that capability is coming "
        "if asked."
    ) % (home_name or "this home", who)


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

def chat(client, ws_call, project_mod, user: dict, text: str,
         session: str = "default", home_name: str = "") -> dict:
    cfg = load_config()
    if not (cfg.get("provider") and cfg.get("api_key")):
        return {"error": "Pro Assist AI is not configured — set provider + API key in Pro › Tech Tools"}
    text = (text or "").strip()
    if not text:
        return {"error": "empty message"}
    key = ((user or {}).get("id") or "anon", session or "default")
    with _LOCK:
        history = list(_SESSIONS.get(key) or [])
    history.append({"role": "user", "content": text})
    runner = ToolRunner(client, ws_call, project_mod, user)
    system = _system_prompt(user, home_name)
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
