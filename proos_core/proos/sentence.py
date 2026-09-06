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
        title = _ok(_attr(snap, eid, "media_title"))
        artist = _ok(_attr(snap, eid, "media_artist"))
        # A real channel is content; the APP NAME is only sometimes.
        # Dave, 14 Aug (register 137): "HomePods seem to say AirMusic — this is
        # not required, needs to just be like Sonos: just the media info." A
        # speaker fed by AirPlay reports app_name 'AirMusic' — the name of the
        # phone app doing the SENDING. That is plumbing, and a Sonos never says
        # it. By ROLE, not by brand: on a video SOURCE the app IS the service
        # worth naming ("Stranger Things on Netflix"); on a speaker it is just
        # the sender, so it speaks only when it is all we have.
        station = _ok(_attr(snap, eid, "media_channel"))
        if not station:
            app = _ok(_attr(snap, eid, "app_name"))
            if app and (role == "source" or not title):
                station = app
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


# ── THE HOME LINE: FACTS, THEN WORDS (5 Sep 2026, register 318) ─────────────
# Dave: "Are you just writing patching or rules again for each scenario?"
# The first cut of the one-sentence home line (316) recovered each room's
# KIND by parsing Core's own clause sentences back — three patterns that
# worked only because Core wrote them. Now a room publishes one FACT —
#   {"kind": "watch"|"music"|"lights"|"on", "what": …, "where": …}
# — and both the room's clause and the whole-home sentence are RENDERED
# from facts. Nothing is parsed; one composer; every kind is one template.
KINDS = ("watch", "music", "lights", "on")


def home_fact(stv, label, provisional, devs, snap, room_name,
              media_app=None, content=None):
    """The HOME line's FACT for this room, or None when the room has
    nothing to say (off and dark, paused, provisional, unknown).
      watch  — what: the source/app being watched ("Apple TV", "YouTube")
      music  — (what unused)
      on     — what: the lit device names (list), or [] for "the room is on"
    'where' is the room's name as the home says it (with its article)."""
    if not stv or stv in ("unknown", "unavailable") or provisional:
        return None
    where = _the(room_name)
    if stv == "off":
        # assigned device facts still speak on the home line
        if playing_info(devs, snap):
            return {"kind": "music", "what": None, "where": where}
        names = _lit_names(devs, snap, room_name)
        return {"kind": "on", "what": list(names), "where": where} if names else None
    if stv in ("playing", "paused", "idle"):
        # paused/idle is silent on the home line too — the media card holds it,
        # the words don't (Dave, 6 Aug). Only active playback names the room.
        return {"kind": "music", "what": None, "where": where} if stv == "playing" else None
    if stv.startswith("watch_"):
        _s = _state_sentence(stv, label, devs, snap, room_name, home=True,
                             media_app=media_app, content=content)
        m = re.match(r"^Watching\s+(.+)$", _s or "", flags=re.I)
        if m:
            return {"kind": "watch", "what": m.group(1).strip(), "where": where}
        # not an activity name (a manually-lit room) — the home line names
        # the room's devices, never "The TV is on in the Family Room"
        _n = _lit_names(devs, snap, room_name)
        return {"kind": "on", "what": list(_n), "where": where}
    names = _lit_names(devs, snap, room_name)
    return {"kind": "on", "what": list(names), "where": where}


def lighting_fact(on, room_name):
    """A LIGHTING room's fact (281): lit lights, or nothing — an unlit
    room is not news. The same fact an AV/music room's lit lights make
    (317), so the sentence groups them all."""
    return {"kind": "lights", "what": None, "where": _the(room_name)} if on else None


def _names_phrase(where, names):
    """'The Family Room TV is on' · 'The Family Room TV and Shield are on'
    · 'The Study is on' (no names)."""
    base = _cap(where)
    names = [n for n in (names or []) if n]
    if not names:
        return "%s is on" % base
    if len(names) == 1:
        return "%s %s is on" % (base, names[0])
    return "%s %s and %s are on" % (base, ", ".join(names[:-1]), names[-1])


def render_fact(f):
    """ONE room's fact as a finished clause — the room row's own words:
    'Watching Apple TV in the Bedroom' · 'Music in the Office' · 'The
    lights are on in Jarrod's Room' · 'The Family Room TV is on'."""
    if not isinstance(f, dict):
        return None
    k, what, where = f.get("kind"), f.get("what"), f.get("where") or ""
    if k == "watch":
        return "Watching %s in %s" % (what, where)
    if k == "music":
        return "Music in %s" % where
    if k == "lights":
        return "The lights are on in %s" % where
    if k == "on":
        return _names_phrase(where, what if isinstance(what, list) else [])
    return None


def lighting_clause(on, room_name):
    """The HOME line's clause for a LIGHTING room (25 Aug 2026, register
    281 — Dave: "it says room is on in Jarrod's Room, this is misleading
    as it's just lights"). Rendered from lighting_fact."""
    return render_fact(lighting_fact(on, room_name))


def home_clause(stv, label, provisional, devs, snap, room_name,
                media_app=None, content=None):
    """The HOME line's clause for this room — a FINISHED natural phrase
    with the room's name in it: 'Watching Apple TV in the Bedroom' ·
    'Music in the Office' · 'The Family Room TV is on'. Content-free (the
    home page says WHERE; the area page carries the what). Rendered from
    home_fact — the same fact the whole-home sentence is built from."""
    return render_fact(home_fact(stv, label, provisional, devs, snap, room_name,
                                 media_app=media_app, content=content))


def _join_and(items, oxford=False):
    """'A', 'A and B', 'A, B and C' — the top-level parts of the home
    sentence take the serial comma ('…, and …') so two grouped lists can
    never run into each other; a list of rooms inside a part does not."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return "%s and %s" % (items[0], items[1])
    return "%s%s and %s" % (", ".join(items[:-1]), "," if oxford else "", items[-1])


def _by_name(places):  # (kept for callers; the composer sorts inline)
    """Rooms in name order, ignoring a leading 'the'."""
    return sorted(places, key=lambda x: x[4:].lower() if x.lower().startswith("the ") else x.lower())


def home_segments(facts):
    """THE HOME LINE, IN TAPPABLE PIECES (5 Sep 2026, register 321). Dave:
    "before it would link to the particular control page of the section
    you tapped on — now it just goes straight to the media page … mobile
    needs to be addressed." One composer, two outputs from it: the
    sentence (home_sentence, the join of these) and its segments —
    [{"t": text, "a": area_id or None, "k": kind or None}, …] — so a panel
    can make each room's words open THAT room, keep a room's words on one
    line, and let the joins wrap. Nothing is parsed on the glass.

    AV ONLY (Dave, 5 Sep: "change back to the old rule that it only shows
    AV summaries of the rooms — the summaries are just too long"): lights
    facts are not spoken here; the nav's light icon carries them (9 Aug),
    and a lighting room's own row still says "Lights on" (281). This
    reverses 317, on Dave's word, the same day.

    Order: watching, then music, then anything else that is on; like
    grouped with like, rooms in name order; serial comma between the
    top-level parts; a full stop; nothing happening -> []."""
    watch, music, other = [], [], []
    seen = set()
    for f in (facts or []):
        if isinstance(f, str):
            c = f.strip()
            if c and c not in seen:
                seen.add(c)
                other.append((("the " + c[4:]) if c.lower().startswith("the ") else c, None, None))
            continue
        if not isinstance(f, dict):
            continue
        k, where = f.get("kind"), (f.get("where") or "").strip()
        if not where or k == "lights":
            continue
        key = (k, str(f.get("what")), where)
        if key in seen:
            continue
        seen.add(key)
        aid = f.get("area_id") or None
        if k == "watch" and f.get("what"):
            watch.append((str(f["what"]).strip(), where, aid))
        elif k == "music":
            music.append((where, aid))
        elif k == "on":
            c = _names_phrase(where, f.get("what") if isinstance(f.get("what"), list) else [])
            other.append((("the " + c[4:]) if c.lower().startswith("the ") else c, aid, "on"))

    def _seq(items, oxford=False):
        """items: [(text, area, kind)] -> segments joined with ', ' / ' and '."""
        out = []
        n = len(items)
        for i, (t, a, k) in enumerate(items):
            if i > 0:
                if n == 2:
                    out.append({"t": " and ", "a": None, "k": None})
                elif i < n - 1:
                    out.append({"t": ", ", "a": None, "k": None})
                else:
                    out.append({"t": (", and " if oxford else " and "), "a": None, "k": None})
            out.append({"t": t, "a": a, "k": k})
        return out

    parts = []   # each part: list of segments
    if watch:
        parts.append([{"t": "watching ", "a": None, "k": None}]
                     + _seq([("%s in %s" % (w, p), a, "watch") for w, p, a in watch]))
    if music:
        rooms = sorted(dict.fromkeys(music), key=lambda x: x[0][4:].lower() if x[0].lower().startswith("the ") else x[0].lower())
        parts.append([{"t": "music in ", "a": None, "k": None}]
                     + _seq([(p, a, "music") for p, a in rooms]))
    for t, a, k in other:
        parts.append([{"t": t, "a": a, "k": k}])
    if not parts:
        return []
    segs = []
    n = len(parts)
    for i, part in enumerate(parts):
        if i > 0:
            if n == 2:
                segs.append({"t": " and ", "a": None, "k": None})
            elif i < n - 1:
                segs.append({"t": ", ", "a": None, "k": None})
            else:
                segs.append({"t": ", and ", "a": None, "k": None})
        segs.extend(part)
    segs[0] = dict(segs[0], t=_cap(segs[0]["t"]))
    segs.append({"t": ".", "a": None, "k": None})
    return segs


def home_sentence(facts):
    """THE HOME LINE IS ONE SENTENCE (register 316; facts-built since 318;
    the join of home_segments since 321). Dave, on "Watching YouTube in
    the Family Room · The lights are on in Jarrod's Room · Music in the
    Office": "when a bit is happening in the home it looks ridiculous …
    an actual summary all the time." Pure; None when nothing is happening
    (the surfaces keep the quiet-home environment line)."""
    segs = home_segments(facts)
    return "".join(s["t"] for s in segs) if segs else None


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
    "21.3° inside · 11.2° and partly cloudy outside". Lights are not
    words here (327); no devices -> ''."""
    eids = list(area_eids or [])
    parts = []
    # LIGHTS LEFT THE WORDS (Dave, 9 Aug; at the source since 327): the
    # environment line is climate and weather. Both pages stripped the
    # lights count on the glass; Core no longer writes it, so no surface
    # (the room hub printed env_line raw) can show it.
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
