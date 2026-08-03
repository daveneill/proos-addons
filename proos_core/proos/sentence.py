"""ONE sentence engine, served by Core (3 Aug 2026).

By mid-morning the same sentence rules lived in two PWAs (dashboard +
Pro's mirror) — divergence guaranteed. Dave's mirror rule ("the installer
must read exactly what the homeowner reads") only holds if ONE thing
speaks. So Core computes the words once per sweep and publishes them on
the verdict sensor:

  sentence   — the AREA line. Content playing -> the info (station/app —
               title — artist, package strings filtered). Otherwise the
               state sentence ("TV is on", "Watching Apple TV").
               Off -> "Off" (surfaces decide whether off speaks).
  home_word  — the HOME line's room clause, content-free ("Music",
               "TV is on", "Watching Shield") — the home page says WHERE,
               never what track.

Pure functions over the room's devices{} map + a state snapshot.
Benched by tests/sentence_bench.py (mirrors the dashboard JS bench).
"""
from __future__ import annotations

import re

LIT = ("on", "playing", "paused")
_PKG = re.compile(r"^[a-z][\w-]*(\.[\w-]+){2,}$", re.I)
# Machine-shaped metadata (Dave, 3 Aug: radio ads push scheduler junk —
# "Asset Stop 00:00 2026-08-03T02:20:08.371Z" — into title AND artist).
# Brand-agnostic: timestamps, timecodes, uuid-ish ids, mostly-numeric
# strings never render. The honest ad-break summary is just the station.
_MACHINE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"        # ISO timestamp anywhere
    r"|^[0-9a-f]{8}-[0-9a-f]{4}-"                   # uuid-ish
    r"|^\d{1,2}:\d{2}(:\d{2})?$", re.I)          # bare timecode


def _ok(x):
    if not x:
        return None
    t = str(x).strip()
    if not t or _PKG.match(t) or _MACHINE.search(t):
        return None
    alnum = [c for c in t if c.isalnum()]
    if alnum and sum(c.isdigit() for c in alnum) / len(alnum) > 0.6:
        return None
    return t


def _attr(snap, eid, key):
    return ((snap.get(eid) or {}).get("attributes") or {}).get(key)


def _state(snap, eid):
    return (snap.get(eid) or {}).get("state") or ""


def playing_info(devs, snap):
    """station/app — title — artist from any committed playing device,
    sources first (video), then the rest (music lives on speakers).

    CLEARS WITH THE SAME RULES AS THE MEDIA PAGE (Dave, 3 Aug: summaries
    held stale tracks the media page had already cleared):
    * paused never claims a summary — a speaker paused for hours holds
      stale metadata (Bedroom: "Deep of You" long after the music ended)
    * the TV-relay phantom: an audio device on its TV input reports
      "playing" forever (HEOS/Sonos SPDIF relay) — it only speaks when a
      display in the room is actually lit (ported from the dashboard's
      #40 phantom rule)."""
    display_lit = any(
        (d or {}).get("role") == "display"
        and str((d or {}).get("state")) in ("on", "playing", "paused")
        for d in (devs or {}).values())

    def read(eid):
        if _state(snap, eid) != "playing":
            return None
        role = ((devs or {}).get(eid) or {}).get("role")
        if role != "source" and str(_attr(snap, eid, "source")) == "TV" \
                and not display_lit:
            return None
        bits = [_ok(_attr(snap, eid, "media_channel"))
                or _ok(_attr(snap, eid, "app_name")),
                _ok(_attr(snap, eid, "media_title")),
                _ok(_attr(snap, eid, "media_artist"))]
        # INPUT ECHO is not content (Dave, 3 Aug: Bedroom read just "TV" —
        # the Sonos relaying TV audio titles itself "TV"/"TV Audio", a
        # Samsung titles its feed "HDMI 2"). Metadata that equals or
        # extends the device's OWN source name is the input echoing back,
        # on any brand. Real info through a relay (a station name on an
        # AVR) differs from the input name, so it still speaks.
        src = str(_attr(snap, eid, "source") or "").strip().lower()
        if src:
            bits = [b for b in bits
                    if not (b and (b.lower() == src
                                   or b.lower().startswith(src + " ")))]
        # duplicates collapse ("Triple M 80s — Triple M 80s" reads once)
        seen, out = set(), []
        for b in bits:
            if not b or b.lower() in seen:
                continue
            seen.add(b.lower())
            out.append(b)
        return " — ".join(out) if out else None

    ordered = ([e for e, d in (devs or {}).items()
                if (d or {}).get("role") == "source"]
               + [e for e, d in (devs or {}).items()
                  if (d or {}).get("role") != "source"])
    # EVERY playing device speaks (Dave, 3 Aug: Office had the station
    # player AND a HomePod playing — only the first showed). Identical
    # info collapses (grouped speakers playing the same thing read once).
    infos = []
    for e in ordered:
        info = read(e)
        if info and info not in infos:
            infos.append(info)
    return " · ".join(infos) if infos else None


def _lit_names(devs, snap, room_name):
    # paused does not name a room on (stale players idle at paused for
    # days), and the TV-relay phantom never names it either — the same
    # clearing rules as the media page, everywhere.
    display_lit = any(
        (d or {}).get("role") == "display"
        and str((d or {}).get("state")) in ("on", "playing", "paused")
        for d in (devs or {}).values())
    names = []
    rn = str(room_name or "")
    for e, d in (devs or {}).items():
        if str((d or {}).get("state")) not in ("on", "playing"):
            continue
        if (d or {}).get("role") != "source" \
                and str(_attr(snap, e, "source")) == "TV" \
                and not display_lit:
            continue
        nm = _attr(snap, e, "friendly_name") \
            or e.split(".")[-1].replace("_", " ")
        nm = str(nm)
        if rn and nm.lower().startswith(rn.lower() + " "):
            nm = nm[len(rn) + 1:]
        if nm and nm.lower() != rn.lower() and nm not in names:
            names.append(nm)
    return names


def _state_sentence(stv, label, devs, snap, room_name, home):
    # PREMIUM WORDING (Dave, 3 Aug: "needs to read like Assist talking to
    # the homeowner, not 'On in Family Room'"). Full spec:
    # ProOS_Summary_Language_Spec.md.
    if stv in ("playing", "paused", "idle"):
        return "Music" if home else "Music is playing"
    if stv.startswith("watch_"):
        t = str(label or stv).replace("_", " ")
        return re.sub(r"^Watch\s+", "Watching ", t, flags=re.I)
    names = _lit_names(devs, snap, room_name)
    if not names:
        return "The room is on"
    return _names_sentence(names)


def _names_sentence(names):
    if not names:
        return None
    if len(names) == 1:
        return "The " + names[0] + " is on"
    return "The " + ", ".join(names[:-1]) + " and " + names[-1] + " are on"


def _the(room_name):
    """'the Bedroom', 'the Office' — but 'Bec's Office' / 'Ryans Room'
    (possessive room names, apostrophe or not) take no article."""
    rn = str(room_name or "").strip()
    if not rn or "'" in rn or "’" in rn:
        return rn
    parts = rn.split()
    if len(parts) > 1 and parts[0].lower().endswith("s"):
        return rn
    return "the " + rn


def _cap(s):
    return s[0].upper() + s[1:] if s else s


def home_clause(stv, label, provisional, devs, snap, room_name):
    """The HOME line's clause for this room — a FINISHED natural phrase
    with the room's name in it, ready to join: 'Watching Apple TV in the
    Bedroom' · 'Music in the Office' · 'The Family Room TV is on'.
    Content-free (the home page says WHERE; the area page carries the
    what). Served on the verdict so every panel repeats it verbatim."""
    if not stv or stv in ("unknown", "unavailable") or provisional:
        return None
    place = _the(room_name)

    def _names_clause(names):
        base = _cap(place)
        if len(names) == 1:
            return "%s %s is on" % (base, names[0])
        return "%s %s and %s are on" % (base, ", ".join(names[:-1]),
                                        names[-1])

    if stv == "off":
        # assigned device facts still speak on the home line
        if playing_info(devs, snap):
            return "Music in %s" % place
        names = _lit_names(devs, snap, room_name)
        return _names_clause(names) if names else None
    if stv in ("playing", "paused", "idle"):
        return "Music in %s" % place
    if stv.startswith("watch_"):
        t = str(label or stv).replace("_", " ")
        return re.sub(r"^Watch\s+", "Watching ", t, flags=re.I) \
            + " in %s" % place
    names = _lit_names(devs, snap, room_name)
    if names:
        return _names_clause(names)
    return "%s is on" % _cap(place)


def area_sentence(stv, label, provisional, devs, snap, room_name):
    if not stv or stv in ("unknown", "unavailable"):
        return None
    if provisional:
        return "Not commissioned yet"
    if stv == "off":
        # The room's COMMANDED state is off, but the summary reports the
        # devices ASSIGNED to the area (Dave, 3 Aug: monitoring is
        # membership — a playing speaker speaks even uncommitted).
        # Device facts only, never a guess.
        info = playing_info(devs, snap)
        if info:
            return info
        ns = _names_sentence(_lit_names(devs, snap, room_name))
        return ns or "Off"
    # ONE RULE, across the board (Dave, 3 Aug, final wording): the INFO
    # alone when a device reports content; otherwise the commanded
    # activity word ("Watching Apple TV" — commanded and power-confirmed,
    # so it's KNOWN even when the source reports nothing); otherwise the
    # monitored device facts. NEVER both concatenated.
    info = playing_info(devs, snap)
    if info:
        return info
    return _state_sentence(stv, label, devs, snap, room_name, home=False)


def home_word(stv, label, provisional, devs, snap, room_name):
    if not stv or stv in ("unknown", "unavailable") or provisional:
        return None
    if stv == "off":
        # assigned device facts speak on the home line too
        if playing_info(devs, snap):
            return "Music"
        return _names_sentence(_lit_names(devs, snap, room_name))
    return _state_sentence(stv, label, devs, snap, room_name, home=True)


_WEATHER_WORDS = {
    "partlycloudy": "partly cloudy", "clear-night": "clear night",
    "lightning-rainy": "lightning and rain", "rainy": "rain",
    "snowy-rainy": "snow and rain", "snowy": "snow", "pouring": "pouring rain",
}


def env_line(area_eids, snap):
    """The area's ENVIRONMENT, worded (Dave, 3 Aug: the summary is a live
    report of the devices ASSIGNED to the area — a thermostat assigned to
    the room speaks on that room's page; weather assigned to Home speaks
    on Home). Reads lights / climate / weather among the area's entities:
    "2 lights on · inside 21.3° · outside 11.2° partly cloudy".
    Zero lights on says nothing; no devices -> ''."""
    eids = list(area_eids or [])
    parts = []
    lights_on = sum(1 for e in eids
                    if e.startswith("light.") and _state(snap, e) == "on")
    if lights_on:
        parts.append("%d light%s on" % (lights_on,
                                        "" if lights_on == 1 else "s"))
    for e in eids:
        if e.startswith("climate."):
            t = _attr(snap, e, "current_temperature")
            if t is not None:
                parts.append("%s° inside" % t)
                break
    for e in eids:
        if e.startswith("weather."):
            stv = _state(snap, e)
            if stv and stv not in ("unknown", "unavailable"):
                t = _attr(snap, e, "temperature")
                w = _WEATHER_WORDS.get(stv,
                                       stv.replace("_", " ").replace("-", " "))
                parts.append(("%s° and %s outside" % (t, w))
                             if t is not None else ("%s outside" % w))
                break
    return " · ".join(parts)
