"""ONE sentence engine, served by Core (3 Aug 2026).

By mid-morning the same sentence rules lived in two PWAs (dashboard +
Pro's mirror) — divergence guaranteed. Dave's mirror rule ("the installer
must read exactly what the homeowner reads") only holds if ONE thing
speaks. So Core computes the words once per sweep and publishes them on
the verdict sensor:

  sentence   — the AREA line. Content playing -> a natural phrase ("Wild
               Horses by Gino Vannelli on Triple M 80s"; watching folds in
               the source: "Ted Lasso on Apple TV"). Otherwise the
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


def _phrase(station, title, artist):
    """A natural content phrase from whichever parts are real (Dave, 7 Aug:
    richer than the old ' — ' chain):
      'Wild Horses by Gino Vannelli on Triple M 80s' · 'Stranger Things on
      Netflix' · 'Wild Horses by Gino Vannelli' · 'Wild Horses' · 'Triple M 80s'."""
    if title and artist and station:
        return "%s by %s on %s" % (title, artist, station)
    if title and artist:
        return "%s by %s" % (title, artist)
    if title and station:
        return "%s on %s" % (title, station)
    if title:
        return title
    if station:
        return station
    if artist:
        return artist
    return None


def _watch_source(label):
    """The source name out of an activity label, for the content combine —
    'Watch Apple TV' -> 'Apple TV', 'Watching TV' -> 'TV'. A label that is NOT
    an activity name ('On') returns None, so a manually-lit room never gets a
    bogus 'on On' tail."""
    import re as _re
    m = _re.match(r"^Watch(?:ing)?\s+(.+)$", str(label or "").strip(), _re.I)
    return m.group(1) if m else None


def playing_info(devs, snap, source_fallback=None):
    """A natural content phrase from any committed playing device, sources
    first (video), then the rest (music lives on speakers). Every playing
    device speaks, joined ' · '; identical info collapses.

    source_fallback (Dave, 7 Aug — reverses the 3 Aug 'never concatenate'):
    when a committed SOURCE reports a title but no station/app, name the
    activity's source, so 'Ted Lasso' becomes 'Ted Lasso on Apple TV'. Only the
    source, only when it would otherwise be a bare title — a real app/station
    (Netflix) always wins over the fallback.

    CLEARS WITH THE SAME RULES AS THE MEDIA PAGE: paused never claims; the
    TV-relay phantom (an audio device on its TV input reporting 'playing'
    forever) only speaks when a display in the room is lit; input echoes and
    machine junk never render."""
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
        station = _ok(_attr(snap, eid, "media_channel")) \
            or _ok(_attr(snap, eid, "app_name"))
        title = _ok(_attr(snap, eid, "media_title"))
        artist = _ok(_attr(snap, eid, "media_artist"))
        # INPUT ECHO is not content (Dave, 3 Aug: a Sonos relaying TV audio
        # titles itself "TV"/"TV Audio"; a Samsung says "HDMI 2"). A part equal
        # to / extending the device's OWN source name is the input echoing back,
        # on any brand. Real info through a relay (a station name) still speaks.
        src = str(_attr(snap, eid, "source") or "").strip().lower()
        if src:
            def _echo(b):
                return bool(b) and (b.lower() == src
                                    or b.lower().startswith(src + " "))
            if _echo(station):
                station = None
            if _echo(title):
                title = None
            if _echo(artist):
                artist = None
        # duplicates collapse (radio titles itself its station once)
        if title and station and title.lower() == station.lower():
            title = None
        if artist and station and artist.lower() == station.lower():
            artist = None
        if artist and title and artist.lower() == title.lower():
            artist = None
        # activity+content combine: a bare-title source names the activity's
        # source ('Ted Lasso on Apple TV'); a real app/station always wins.
        if role == "source" and title and not station and source_fallback:
            station = source_fallback
        return _phrase(station, title, artist)

    ordered = ([e for e, d in (devs or {}).items()
                if (d or {}).get("role") == "source"]
               + [e for e, d in (devs or {}).items()
                  if (d or {}).get("role") != "source"])
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


def _self_label(label, media_app, content):
    """The display-as-its-own-source label. A TV on its OWN input is either
    the TUNER (Live TV) or a BUILT-IN APP — and they are two different
    things (Dave, 2 Aug: "Live TV and Samsung TV are 2 different things —
    all TVs have a tuner input; the panel's platform is not a tuner, it's
    like an app"). A built-in app names itself; the tuner reads Live TV.
    Restored into the ONE SPEAKER engine 4 Aug — it was lost when the
    wording moved to Core (dashboard_self_content_bench)."""
    app = _ok(media_app)
    if app:
        return app
    if content == "live_tv":
        return "Live TV"
    return label


def _state_sentence(stv, label, devs, snap, room_name, home,
                    media_app=None, content=None):
    # PREMIUM WORDING (Dave, 3 Aug: "needs to read like Assist talking to
    # the homeowner, not 'On in Family Room'"). Full spec:
    # ProOS_Summary_Language_Spec.md.
    if stv in ("playing", "paused", "idle"):
        # Only actual PLAYBACK speaks. A paused/idle music room says NOTHING — the
        # media card holds it until the pause-clear timer expires, but the status
        # line goes quiet (Dave, 6 Aug: the Office read "Music is playing" while
        # both speakers were paused; "if it's paused it shouldn't say anything ...
        # until the clear timer expires").
        if stv != "playing":
            return None
        return "Music" if home else "Music is playing"
    if stv.startswith("watch_"):
        # A built-in app or the tuner names ITSELF ("Watching 7plus").
        self_lbl = _self_label(None, media_app, content)
        if self_lbl:
            return "Watching " + str(self_lbl).replace("_", " ")
        t = str(label or stv).replace("_", " ")
        if re.match(r"^Watching\s+", t, flags=re.I):
            return t
        if re.match(r"^Watch\s+", t, flags=re.I):
            return re.sub(r"^Watch\s+", "Watching ", t, flags=re.I)
        # The label is NOT an activity name — a manually-lit room carries
        # "On" (Dave, 4 Aug: the home line read "Watching On in the Family
        # Room"). Never verb a non-name: say what is actually on.
        names = _lit_names(devs, snap, room_name)
        return _names_sentence(names) or ("On" if home else "The room is on")
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


def home_clause(stv, label, provisional, devs, snap, room_name,
                media_app=None, content=None):
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
        # paused/idle is silent on the home line too — the media card holds it,
        # the words don't (Dave, 6 Aug). Only active playback names the room.
        return ("Music in %s" % place) if stv == "playing" else None
    if stv.startswith("watch_"):
        _s = _state_sentence(stv, label, devs, snap, room_name, home=True,
                             media_app=media_app, content=content)
        if _s and re.match(r"^Watching\s+", _s, flags=re.I):
            return _s + " in %s" % place
        # not an activity name (a manually-lit room) — the home line names
        # the room's devices, never "The TV is on in the Family Room"
        _n = _lit_names(devs, snap, room_name)
        return _names_clause(_n) if _n else "%s is on" % _cap(place)
    names = _lit_names(devs, snap, room_name)
    if names:
        return _names_clause(names)
    return "%s is on" % _cap(place)


def area_sentence(stv, label, provisional, devs, snap, room_name,
                  media_app=None, content=None):
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
    # The content phrase when a device reports what's playing — and when
    # WATCHING, the activity's source is folded in so a bare title reads
    # 'Ted Lasso on Apple TV' (Dave, 7 Aug: richer; reverses the 3 Aug
    # 'never concatenate'). A real app/station (Netflix) still wins. Otherwise
    # the commanded activity word ("Watching Apple TV" — known even when the
    # source reports nothing); otherwise the monitored device facts.
    src_label = None
    if stv.startswith("watch_"):
        src_label = _self_label(None, media_app, content) or _watch_source(label)
    info = playing_info(devs, snap, source_fallback=src_label)
    if info:
        return info
    return _state_sentence(stv, label, devs, snap, room_name, home=False,
                           media_app=media_app, content=content)


def home_word(stv, label, provisional, devs, snap, room_name,
              media_app=None, content=None):
    if not stv or stv in ("unknown", "unavailable") or provisional:
        return None
    if stv == "off":
        # assigned device facts speak on the home line too
        if playing_info(devs, snap):
            return "Music"
        return _names_sentence(_lit_names(devs, snap, room_name))
    return _state_sentence(stv, label, devs, snap, room_name, home=True,
                           media_app=media_app, content=content)


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
