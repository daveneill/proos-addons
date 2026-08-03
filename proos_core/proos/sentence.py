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


def _ok(x):
    if not x:
        return None
    t = str(x).strip()
    return t if t and not _PKG.match(t) else None


def _attr(snap, eid, key):
    return ((snap.get(eid) or {}).get("attributes") or {}).get(key)


def _state(snap, eid):
    return (snap.get(eid) or {}).get("state") or ""


def playing_info(devs, snap):
    """station/app — title — artist from any committed playing device,
    sources first (video), then the rest (music lives on speakers)."""
    def read(eid):
        if _state(snap, eid) not in ("playing", "paused"):
            return None
        bits = [_ok(_attr(snap, eid, "media_channel"))
                or _ok(_attr(snap, eid, "app_name")),
                _ok(_attr(snap, eid, "media_title")),
                _ok(_attr(snap, eid, "media_artist"))]
        bits = [b for b in bits if b]
        return " — ".join(bits) if bits else None

    ordered = ([e for e, d in (devs or {}).items()
                if (d or {}).get("role") == "source"]
               + [e for e, d in (devs or {}).items()
                  if (d or {}).get("role") != "source"])
    for e in ordered:
        info = read(e)
        if info:
            return info
    return None


def _lit_names(devs, snap, room_name):
    names = []
    rn = str(room_name or "")
    for e, d in (devs or {}).items():
        if str((d or {}).get("state")) not in LIT:
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
    if stv in ("playing", "paused", "idle"):
        return "Music" if home else (label or "Playing")
    if stv.startswith("watch_"):
        t = str(label or stv).replace("_", " ")
        return re.sub(r"^Watch\s+", "Watching ", t, flags=re.I)
    names = _lit_names(devs, snap, room_name)
    if not names:
        return "On"
    if len(names) == 1:
        return names[0] + " is on"
    return ", ".join(names[:-1]) + " and " + names[-1] + " are on"


def area_sentence(stv, label, provisional, devs, snap, room_name):
    if not stv or stv in ("unknown", "unavailable"):
        return None
    if provisional:
        return "Not commissioned yet"
    if stv == "off":
        return "Off"
    info = playing_info(devs, snap)
    if info:
        return info
    return _state_sentence(stv, label, devs, snap, room_name, home=False)


def home_word(stv, label, provisional, devs, snap, room_name):
    if not stv or stv in ("unknown", "unavailable", "off") or provisional:
        return None
    return _state_sentence(stv, label, devs, snap, room_name, home=True)
