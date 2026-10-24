"""THE WEATHER AS AN INSTANT SENTENCE — "what's the weather tomorrow" — register 978 (24 Oct 2026).

Dave, 24 Oct: make Assist quicker. Measured on his box (register 977): "What's the weather like tomorrow?" took 12.1 s on
the AI road — the house itself answered in 0.008 s; the rest was the AI reading its instructions and choosing the tool,
then a web search nobody needed.

THE PLATFORM FIRST. The platform's own sentence library answers the weather NOW ("what's the weather" → its own weather
intent) and nothing else: no tomorrow, no weekday, no "will it rain". Its own mechanism for a sentence the home is given is
a conversation trigger on an automation (register 733, 853) — matched on the box in ~150 ms, no AI. And its own way to read
a forecast is the weather.get_forecasts action with a response. So Core writes ONE automation, "ProOS · Voice · Weather":
the sentences for today, tomorrow, each weekday and rain; its actions ask the home's own weather provider for the daily
forecast and say the day's answer from it. Nothing is guessed: the words come from the provider's own reading for that
date, and a day the provider has no forecast for says so. ProOS adds only the sentences and the words; the platform does
the matching, the forecast and the speaking.

THE PROVIDER is the first weather entity the home has — the same one Assist's own weather tool reads (assist.t_weather),
so the instant answer and the AI's answer never disagree. No provider, no automation.
"""
from __future__ import annotations

from proos import project as _project

VOICE_WEATHER_ID = "proos_voice_weather"

_WX = "(weather|forecast)"
_LIKE = "[going to be] [like]"
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def day_sentences(day: str) -> list:
    """Every way a person asks about one day: 'today', 'tomorrow', or a weekday ('on saturday')."""
    d = day if day in ("today", "tomorrow") else "[on] " + day
    out = [
        "(what's|what is|whats|how's|how is) the %s %s %s" % (_WX, _LIKE, d),
        "(what's|what is|whats|how's|how is) the %s %s for %s" % (_WX, _LIKE, d),
        "what (will|does) the %s (be|look) like %s" % (_WX, d),
        "%s %s" % (_WX, d),
        "what's it (going to be|gonna be) like %s" % d,
        "how (hot|warm|cold) (will it be|is it [going to be]) %s" % d,
        "%s %s" % (day, _WX),
    ]
    if day in ("today", "tomorrow"):
        out.append("%s's %s" % (day, _WX))
    return out


def rain_sentences(day: str) -> list:
    """Every way a person asks whether it will rain on one day."""
    d = day if day in ("today", "tomorrow") else "[on] " + day
    return [
        "(will|is) it [going to] rain %s" % d,
        "is rain (expected|forecast) %s" % d,
        "(do|will) i need an umbrella %s" % d,
        "(any|is there any) rain %s" % d,
    ]


# What the provider's own condition words mean in a sentence (the platform's fixed list of weather conditions).
CONDITION_WORDS = {
    "clear-night": "clear", "cloudy": "cloudy", "exceptional": "unusual weather", "fog": "foggy", "hail": "hail",
    "lightning": "storms", "lightning-rainy": "storms and rain", "partlycloudy": "partly cloudy", "pouring": "heavy rain",
    "rainy": "rain", "snowy": "snow", "snowy-rainy": "sleet", "sunny": "sunny", "windy": "windy",
    "windy-variant": "windy and cloudy",
}

# The day the trigger asked about, as days from today; a weekday as its number (Monday 0), resolved when spoken.
_TODAY_OFFSET = {"today": 0, "tomorrow": 1}

# Jinja: find the provider's forecast for the asked date and say it. `fc` is the day list, `ask` the trigger's id.
SAY = r"""{%- set ask = said[trigger.id] -%}
{%- set fc = ((wx.get(provider) or {}).get('forecast') if wx is defined and wx is mapping else []) or [] -%}
{%- if ask.weekday is not none -%}
  {%- set off = (ask.weekday - now().weekday()) % 7 -%}
{%- else -%}
  {%- set off = ask.offset -%}
{%- endif -%}
{%- set want = (now() + timedelta(days=off)).date() -%}
{%- set ns = namespace(f=none) -%}
{%- for f in fc -%}
  {%- if ns.f is none and f.get('datetime') and (as_local(as_datetime(f.get('datetime')))).date() == want -%}{%- set ns.f = f -%}{%- endif -%}
{%- endfor -%}
{%- set name = ask.name -%}
{%- if ns.f is none -%}
There's no forecast for {{ ask.bare }} yet.
{%- else -%}
  {%- set f = ns.f -%}
  {%- set cond = words.get(f.get('condition'), (f.get('condition') or '') | replace('-', ' ')) -%}
  {%- set hi = f.get('temperature') -%}{%- set lo = f.get('templow') -%}
  {%- set pp = f.get('precipitation_probability') -%}{%- set mm = f.get('precipitation') -%}
  {%- if ask.rain -%}
    {%- if pp is not none -%}
      {%- if pp | float(0) >= 50 -%}Rain is likely {{ name }}, {{ pp | round(0) | int }}% chance{%- elif pp | float(0) >= 20 -%}There's a {{ pp | round(0) | int }}% chance of rain {{ name }}{%- else -%}Rain isn't expected {{ name }}, {{ pp | round(0) | int }}% chance{%- endif -%}
      {%- if mm is not none and mm | float(0) > 0 -%}, about {{ mm | round(0) | int if mm | float(0) >= 1 else mm }} millimetres{%- endif -%}.
    {%- elif mm is not none -%}
      {%- if mm | float(0) > 0 -%}Rain is forecast {{ name }}, about {{ mm | round(0) | int if mm | float(0) >= 1 else mm }} millimetres.{%- else -%}No rain is forecast {{ name }}.{%- endif -%}
    {%- else -%}
      {{ ask.title }}: {{ cond }}. The forecast doesn't say how likely rain is.
    {%- endif -%}
  {%- else -%}
{{ ask.title }}: {{ cond }}
    {%- if hi is not none -%}, a high of {{ hi | round(0) | int }}{%- endif -%}
    {%- if lo is not none -%}{{ ' and' if hi is not none else ', a' }} low of {{ lo | round(0) | int }}{%- endif -%}
    {%- if pp is not none and pp | float(0) >= 20 -%}, {{ pp | round(0) | int }}% chance of rain{%- elif pp is none and mm is not none and mm | float(0) >= 1 -%}, about {{ mm | round(0) | int }} millimetres of rain{%- endif -%}.
  {%- endif -%}
{%- endif -%}"""


def voice_weather_config(provider: str | None):
    """The automation for the home's weather provider, or None when the home has none."""
    if not provider:
        return None
    triggers, said, seen = [], {}, set()
    days = ["today", "tomorrow"] + _DAYS
    for day in days:
        for rain in (False, True):
            cmds = [s for s in (rain_sentences(day) if rain else day_sentences(day)) if s not in seen]
            if not cmds:
                continue
            seen.update(cmds)
            tid = "%s|%s" % ("rain" if rain else "wx", day)
            triggers.append({"trigger": "conversation", "command": cmds, "id": tid})
            said[tid] = {"offset": _TODAY_OFFSET.get(day, 0),
                         "weekday": _DAYS.index(day) if day in _DAYS else None,
                         "name": day if day in _TODAY_OFFSET else "on " + day.capitalize(),
                         "title": day.capitalize() if day in _TODAY_OFFSET else "On " + day.capitalize(),
                         "bare": day if day in _TODAY_OFFSET else day.capitalize(),
                         "rain": rain}
    cfg = {"id": VOICE_WEATHER_ID, "alias": "ProOS · Voice · Weather",
           "description": "Written by ProOS Core: the day's forecast from the home's own weather provider, said at once. "
                          "Edit it and Core leaves it alone.",
           "mode": "parallel", "max": 10,
           "triggers": triggers, "conditions": [],
           "actions": [{"action": "weather.get_forecasts", "target": {"entity_id": provider},
                        "data": {"type": "daily"}, "response_variable": "wx", "continue_on_error": True},
                       {"set_conversation_response": SAY}],
           "variables": {"said": said, "provider": provider, "words": CONDITION_WORDS}}
    cfg["variables"]["proos_hash"] = _project._voice_hash(cfg)
    return cfg


def provider_of(states: list) -> str | None:
    """The home's weather provider: the first weather entity, as Assist's own weather tool picks it."""
    for st in states or []:
        eid = (st or {}).get("entity_id") or ""
        if eid.startswith("weather."):
            return eid
    return None


def voice_weather(client) -> dict:
    """Write the weather sentences into the platform (differences only; an installer-edited automation is left alone)."""
    try:
        states = client._req("GET", "/api/states") or []
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
    prov = provider_of(states)
    out = _project.voice_write(client, VOICE_WEATHER_ID, voice_weather_config(prov))
    if out.get("ok"):
        out["provider"] = prov
    return out
