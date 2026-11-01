"""PROOS ASSIST, SPOKEN IN REAL TIME — the prototype (register 1148; 1 Nov 2026).

Dave, 15:27: "I was using the new chatGPT realtime conversation can we replace our current AI assist with [it]";
15:32: "sort those and then build the prototype so we can see as no matter what way we go it will need the ProOS assist
to be rock solid to communicate with".

THE PLATFORM FIRST (CLAUDE.md). Read 1 Nov: the platform's Assist pipeline is speech-to-text → agent → text-to-speech;
audio-to-audio engines are its own work in progress ("Pluggable Voice Pipeline Engines", drafts in Sep 2026), not in the
box's version. So the voice mics stay on the platform's pipeline, and this prototype is for a screen (the phone, a
panel) whose browser can talk to the provider directly.

THE SHAPE — ONE BRAIN, A NEW MOUTH:
  - the browser talks AUDIO straight to the provider's realtime model over WebRTC, with a SHORT-LIVED key Core asks the
    provider for. The home's own key never leaves the box.
  - the model is given the SAME instructions as Assist (the doctrine, the person, the house rules, the room they are in
    with each device's id and reading — 1136/1137) and the SAME tools, filtered for the same person (_active_tools).
  - every tool the model calls comes back to Core, which runs it through the SAME ToolRunner: the same consent, the same
    house rules, the same "part of the TV" and "hidden from Assist" refusals, the same read-back. Nothing is rebuilt;
    nothing reaches the platform except through Core.

Limits, stated: the deferred tool groups (more_tools) are not offered — a realtime tool call carries no runner from one
call to the next; one session per press.

REGISTER 1153 — FOR EVERYONE IN THE HOME (Dave, 1 Nov 18:41: "its a bit useless if it does not work for homeowner as that
what is meant for"; rulings: every mic becomes Live; everyone, switchable). Talk Live is on for every signed-in person
unless the installer turns it off for the home (assist.json realtime_enabled) or for one person (realtime_off_for).
AND THE TOOL ROUTE NOW CHECKS THE PERSON. The model only ever SEES this person's tools, but the tool route takes a name
from a page — before this build any name ToolRunner knew would have run, whoever asked. run_tool now runs only a tool in
this person's own list (the same _active_tools a chat turn offers); anything else is refused in words.
"""
from __future__ import annotations

import json
import time

CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
DEFAULT_MODEL = "gpt-realtime-2"     # the provider's current realtime model (May 2026); assist.json "realtime_model"
DEFAULT_VOICE = "marin"              # assist.json "realtime_voice"

VOICE_PREFACE = (
    "YOU ARE SPEAKING OUT LOUD, IN REAL TIME. Keep every answer short and natural — one or two sentences unless asked "
    "for more. Never read out ids, entity names in brackets, or lists of settings. When you call a tool, you may say a "
    "few words first ('One moment'); say the result only when the tool has answered, and say only what it confirmed. "
    "If you did not hear something clearly, ask — never guess a room, a device or a number.\n\n")


def instructions(assist_mod, user: dict, home_name: str, where: dict | None) -> str:
    """Assist's own instructions for this person and this room, with the voice preface."""
    return VOICE_PREFACE + assist_mod._system_prompt(user, home_name, where or {}, False)


def tools(assist_mod, runner) -> list:
    """Assist's own tools for this person, in the provider's realtime shape; the deferred-group door left out."""
    out = []
    for t in assist_mod._active_tools(runner):
        if t.get("name") == assist_mod._MORE_TOOLS.get("name"):
            continue
        out.append({"type": "function", "name": t["name"], "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}}})
    return out


def new_session(assist_mod, cfg: dict, runner, user: dict, home_name: str, where: dict | None, post=None,
                voice: str = "ryan") -> dict:
    """Ask the provider for a short-lived key for ONE realtime session configured as ProOS Assist.

    REGISTER 1150 — voice "ryan" (the default, Dave 1 Nov 17:16 "okay go"): the model answers in TEXT and the page speaks
    each sentence in the HOME'S OWN voice through Core (/assist/speak — the ProOS voice pipeline's en_GB-ryan, the same
    voice as every room), so the product keeps one voice; the provider's spoken output is not used (and not paid for).
    voice "provider": the provider speaks with its own voice, the fastest road."""
    if (cfg or {}).get("provider") != "openai" or not cfg.get("api_key"):
        return {"error": "the realtime voice needs Assist on OpenAI with its key set (Pro › Tech Tools)"}
    model = cfg.get("realtime_model") or DEFAULT_MODEL
    tl = tools(assist_mod, runner)
    sess = {"type": "realtime", "model": model,
            "instructions": instructions(assist_mod, user, home_name, where),
            "tools": tl, "tool_choice": "auto",
            "audio": {"input": {"transcription": {"model": "gpt-4o-mini-transcribe"}}}}
    if voice == "provider":
        sess["audio"]["output"] = {"voice": cfg.get("realtime_voice") or DEFAULT_VOICE}
    else:
        sess["output_modalities"] = ["text"]      # register 1150: the home's own voice speaks the words
    body = {"session": sess}
    post = post or assist_mod._http_json
    t0 = time.time()
    r = post(CLIENT_SECRETS, body, {"Authorization": "Bearer %s" % cfg["api_key"]})
    value = (r or {}).get("value") or ((r or {}).get("client_secret") or {}).get("value")
    if not value:
        return {"error": "the provider gave no session key", "detail": str(r)[:200]}
    return {"ok": True, "value": value, "expires_at": r.get("expires_at"), "model": model,
            "tools": len(tl), "ms": int((time.time() - t0) * 1000), "voice": "provider" if voice == "provider" else "ryan"}


def available_for(cfg: dict, user: dict) -> tuple:
    """(True, "") when this person may use Talk Live now; else (False, the reason in words)."""
    cfg = cfg or {}
    if not (user or {}).get("id"):
        return False, "sign in first"
    if cfg.get("provider") != "openai" or not cfg.get("api_key"):
        return False, "Talk Live needs Assist on OpenAI with its key set (Pro › Tech Tools)"
    if cfg.get("realtime_enabled", True) is False:
        return False, "Talk Live is turned off for this home"
    if str(user.get("id")) in {str(x) for x in (cfg.get("realtime_off_for") or [])}:
        return False, "Talk Live is turned off for you"
    return True, ""


def allowed_names(assist_mod, runner) -> set:
    """The tools this person may call — exactly the list the model is given."""
    return {t["name"] for t in tools(assist_mod, runner)}


def run_tool(runner, name: str, arguments, allowed=None) -> dict:
    """One tool the realtime model called, run through Assist's own ToolRunner — every rule applies.
    `allowed`: this person's own tool names (register 1153) — any other name is refused before anything runs."""
    if allowed is not None and name not in allowed:
        return {"error": "that tool is not available to you", "_ms": 0}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except Exception:                                        # noqa: BLE001
            return {"error": "the tool's arguments were not readable"}
    if not isinstance(arguments, dict):
        arguments = {}
    t0 = time.time()
    try:
        out = runner.run(name, arguments)
    except Exception as e:                                       # noqa: BLE001
        out = {"error": str(e)[:200]}
    if not isinstance(out, dict):
        out = {"result": out}
    out.setdefault("_ms", int((time.time() - t0) * 1000))
    return out
