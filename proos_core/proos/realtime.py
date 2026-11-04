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
    "for more. Never read out ids, entity names in brackets, or lists of settings. Before a tool that changes or reads "
    "something in the house, SAY NOTHING — it is done before your words could be heard; after it answers, say the "
    "result in a few words, and only what it confirmed. Only before web_search may you say a short 'One moment'. "
    "If you did not hear something clearly, ask — never guess a room, a device or a number. For anything outside this "
    "house — news, who holds an office, sport, weather, what's on — call web_search and answer from what it finds; "
    "never say you cannot check. "
    # REGISTER 1175 — Dave 21:00 (photo): a quiet, arm's-length phrase came back from the transcriber as Spanish ("Por
    # supuesto que lo pienso.") and the answer was in Spanish. The home's language is English.
    "Always answer in English, whatever language the words you are given are in. "
    # REGISTER 1179: an activity's outcome is only known when the room confirms it (a TV takes up to ~90 s); ProOS then
    # tells the person itself, in the conversation or on their phone.
    "When you start an activity, a TV or an app, say it is starting — never that it is on; ProOS tells the person "
    "when the room confirms it.\n\n")


def instructions(assist_mod, user: dict, home_name: str, where: dict | None) -> str:
    """Assist's own instructions for this person and this room, with the voice preface."""
    return VOICE_PREFACE + assist_mod._system_prompt(user, home_name, where or {}, False)


# REGISTER 1159 — THE WEB, IN TALK LIVE TOO. Dave 21:30, screenshot: asked "current president" and "Is Donald Trump the
# current president?" — Talk Live answered "I can't check current world leaders with the tools I have here". Typed Assist
# searches the web as standard (register 970mm: the provider's own web_search tool, switched on, nothing of ours built);
# the provider's realtime voice offers no such tool, and the doctrine forbids guessing, so it refused. Now the realtime
# model is given ONE function, web_search, and Core answers it with the SAME provider search typed Assist uses (the
# Responses API with its web_search tool and the home's country and time zone) — a short answer and its sources. What it
# finds is someone else's claim, never a reading of the house; the doctrine is unchanged.
WEB_TOOL = {"type": "function", "name": "web_search",
            "description": ("Look something up on the web — news, people in office, sport, weather, opening hours, what's "
                            "on, anything outside this house. Returns a short answer with its sources. Use it instead of "
                            "saying you cannot check."),
            "parameters": {"type": "object", "properties": {"query": {"type": "string",
                                                                       "description": "what to look up, in plain words"}},
                           "required": ["query"]}}


def web_lookup(assist_mod, cfg: dict, runner, query: str, post=None) -> dict:
    """The provider's own web search (the one typed Assist uses), asked for a short spoken answer."""
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return {"error": "nothing to look up"}
    cfg = cfg or {}
    if not assist_mod._web_on(cfg):
        return {"error": "web search is turned off for this home"}
    if cfg.get("provider") != "openai" or not cfg.get("api_key"):
        return {"error": "web search needs Assist on OpenAI with its key set"}
    body = {"model": cfg.get("fast_model") or cfg.get("model") or "gpt-4.1-mini",
            "tools": [assist_mod._web_tool_openai(runner)],
            "input": ("Today is %s. Look this up on the web and answer in one or two short sentences that can be "
                      "spoken aloud, with no links in the text: %s" % (time.strftime("%A %d %B %Y"), query)),
            "max_output_tokens": 400}
    post = post or assist_mod._http_json
    t0 = time.time()
    try:
        r = post(assist_mod._OPENAI_RESPONSES, body, {"Authorization": "Bearer %s" % cfg["api_key"]}) or {}
    except Exception as e:                                       # noqa: BLE001
        return {"error": "the web search failed: %s" % str(e)[:160]}
    try:                                                         # register 1162: the lookup's cost is counted too
        from . import aiusage as _aiu
        _u = r.get("usage") or {}
        _aiu.record_turn("assist", body["model"], [{
            "input": int(_u.get("input_tokens") or 0),
            "cached": int(((_u.get("input_tokens_details") or {}).get("cached_tokens")) or 0),
            "output": int(_u.get("output_tokens") or 0),
            "web": sum(1 for o in (r.get("output") or []) if isinstance(o, dict) and o.get("type") == "web_search_call")}])
    except Exception:                                            # noqa: BLE001
        pass
    text, sources = [], []
    for item in (r.get("output") or []):
        for c in (item.get("content") or []) if isinstance(item, dict) else []:
            if c.get("type") == "output_text":
                text.append(c.get("text") or "")
                for a in (c.get("annotations") or []):
                    if a.get("type") == "url_citation" and a.get("url") and len(sources) < 3:
                        sources.append({"title": a.get("title") or "", "url": a.get("url")})
    answer = (r.get("output_text") or " ".join(text)).strip()
    if not answer:
        return {"error": "the web search found nothing to say", "ms": int((time.time() - t0) * 1000)}
    return {"answer": answer[:900], "sources": sources, "ms": int((time.time() - t0) * 1000),
            "note": "found on the web — someone else's claim, not a reading of the house"}


def tools(assist_mod, runner) -> list:
    """Assist's own tools for this person, in the provider's realtime shape; the deferred-group door left out."""
    out = []
    for t in assist_mod._active_tools(runner):
        if t.get("name") == assist_mod._MORE_TOOLS.get("name"):
            continue
        out.append({"type": "function", "name": t["name"], "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}}})
    try:
        if assist_mod._web_on(assist_mod.load_config()) and not any(t["name"] == "web_search" for t in out):
            out.append(dict(WEB_TOOL))                           # register 1159
    except Exception:                                            # noqa: BLE001
        pass
    return out


def transcription(hints=None) -> dict:
    """REGISTER 1158 — THE CAPTION. Dave 20:48, screenshot: he said "turn the bedroom TV off", the TV went off and
    Assist confirmed it — but the chat showed his words as "Gym TV呀". The realtime model hears the audio itself (it
    acted rightly); the line in the chat comes from a SEPARATE transcriber that only captions, and the small one
    misheard with no language and no idea of the home's names. Now: the full transcriber, told it is English, and
    handed the home's own words — its room names and the devices in the room the person is in."""
    words = []
    for h in (hints or []):
        h = " ".join(str(h or "").split())
        if h and h not in words:
            words.append(h)
    # REGISTER 1163 — THE NAMES CAME OUT AS WORDS. Dave 2 Nov 06:47, screenshots: "lights on" captioned "Office.",
    # another command "Office Blinds". The list of the home's names handed to the transcriber (1158) made it write a
    # NAME for a short command. The list is gone; English and the full transcriber stay. (hints are accepted, unused.)
    return {"model": "gpt-4o-transcribe", "language": "en"}


# REGISTER 1174 — HOW SURE THE PROVIDER MUST BE THAT SOMEONE IS SPEAKING (its turn detection, 0–1). Dave 20:15, after
# the phone's echo canceller started working (1173): "was hearing a lot I had tv on in the room but it was pretty bad" —
# read: eight turns in 30 s that were the TV ("Era correcta", "Churchill", "The streets"…), each cutting Ryan off and
# sent as a question. His voice at the phone reached ~17,000 on the microphone, the TV 3,000–9,000. Ruled "let's try it":
# less sensitive to distant sound (was 0.5), and stricter still while Ryan is speaking, so stopping him takes a clear,
# close voice; the page switches between the two (session.update) as Ryan starts and finishes. Plumbing — both values
# are a first setting to tune by what Dave's trace shows.
# REGISTER 1193 — Dave 3 Nov 17:39: "ours gets everything I say wrong since we were playing with settings for noise".
# Read 17:36: his voice reached 2,200–4,400 on the microphone (1174 had switched the phone's own voice level off), and
# at 0.7 the start of a quieter sentence was caught late — words cut, misheard. Listening is back to the provider's
# standard 0.5; only while Ryan speaks does it stay at 0.9, so a TV still cannot cut him off.
VAD_NORMAL = 0.5
VAD_WHILE_SPEAKING = 0.9


def turn_detection(threshold: float) -> dict:
    """The provider's turn detection at the given certainty — the same shape everywhere it is sent."""
    return {"type": "server_vad", "threshold": threshold, "prefix_padding_ms": 300, "silence_duration_ms": 500,
            "create_response": False, "interrupt_response": True}


def new_session(assist_mod, cfg: dict, runner, user: dict, home_name: str, where: dict | None, post=None,
                voice: str = "ryan", hints=None) -> dict:
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
            # REGISTER 1154: the hearing is SAID, not left to the provider's defaults — 24 kHz 16-bit audio (what the app
            # sends), the provider's own turn detection (it decides when the person has finished, and lets them talk over
            # an answer), room (far-field) noise reduction — 1198, and the words transcribed for the chat.
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                # REGISTER 1163 → 1199. The provider does not answer by itself: the page waits for the
                                # caption, so a scrap or a non-English mishearing can be dropped unanswered (1174/1175).
                                # 1199 (the model answering the SOUND) was undone by 1203 (Dave 4 Nov): the caption is
                                # handed to the model as the person's words — what is shown is what is acted on.
                                "turn_detection": turn_detection(VAD_NORMAL),
                                # REGISTER 1198 — Dave 3 Nov 20:18: heard only "a couple of inches from the screen and
                                # talking loud", where ChatGPT hears him "anywhere in the room". 1154 chose the
                                # provider's headset kind ("for close-talking microphones such as headphones"), which
                                # suppresses a voice that is not at the microphone. The room kind ("for far-field
                                # microphones such as laptop or conference room microphones") is what a phone or iPad is.
                                "noise_reduction": {"type": "far_field"},
                                "transcription": transcription(hints)}}}
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
            "tools": len(tl), "ms": int((time.time() - t0) * 1000), "voice": "provider" if voice == "provider" else "ryan",
            # 1174: the two settings the page switches between as Ryan starts and stops speaking
            "vad": {"normal": turn_detection(VAD_NORMAL), "speaking": turn_detection(VAD_WHILE_SPEAKING)}}


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


def run_tool(runner, name: str, arguments, allowed=None, assist_mod=None, cfg=None) -> dict:
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
    if name == "web_search" and assist_mod is not None:          # register 1159: the provider's own search, via Core
        return web_lookup(assist_mod, cfg if cfg is not None else assist_mod.load_config(), runner,
                          arguments.get("query") or "")
    t0 = time.time()
    try:
        out = runner.run(name, arguments)
    except Exception as e:                                       # noqa: BLE001
        out = {"error": str(e)[:200]}
    if not isinstance(out, dict):
        out = {"result": out}
    out.setdefault("_ms", int((time.time() - t0) * 1000))
    return out
