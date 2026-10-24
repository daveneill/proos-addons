"""THE PANEL'S WAKE WORD STAYS WHAT A PERSON SET IT TO — one list 10.18, register 980 (25 Oct 2026).

Dave, 24 Oct (970uu): the G-Flip panel's own microphone off ("just use it as a room mic for now" — Office Voice answers
"Hey Jarvis" in the office). Set with the platform's own control, select.proos_panel_wake_word = no_wake_word. By the
evening it read "Hey Jarvis" again.

MEASURED ON THE BOX, 25 Oct 05:5x (register 980): the panel's microphone is the platform's own satellite add-on
(linux-voice-assistant 1.1.15). It keeps the chosen wake words in /share/assist_satellite/preferences.json. Chosen "off",
it saves `"active_wake_words": [null, null]` — and at its next start it reads that as nothing saved and falls back to its
start-up wake word: the add-on's `wake_word` option (Hey Jarvis), or, given an option that is not a wake word
(`no_wake_word` was tried), its own default, Okay Nabu. So the platform's satellite CANNOT keep "off" across a restart,
and its option cannot say "off" either. Every restart of the add-on (a box restart, an update) turned the panel back on.

THE PLATFORM FIRST — and why it is not enough here: the platform's own control (the select) is the way to choose, and
ProOS keeps using it. What it lacks is memory for one value. So ProOS adds only that: the last wake word a PERSON chose
for the panel (a change with a user behind it), and when the panel's satellite COMES BACK (its select returning from
unavailable) saying something else, ProOS puts the person's choice back through the same select. A change made while the
panel is up — by a person, an automation, anyone — is never touched. No choice ever made → nothing is ever written.
"""
from __future__ import annotations

import json
import os
import re

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
STORE = os.path.join(DATA_DIR, "panel_wake.json")
_SEL = re.compile(r"^select\.[a-z0-9_]+_wake_word(_\d+)?$")
_BACK_FROM = (None, "unavailable", "unknown")


def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, STORE)


def choices() -> dict:
    return dict(_load().get("chosen") or {})


def heard(data: dict, panel_selects, call) -> dict:
    """One state change from the platform's stream. panel_selects() → the panel's own wake-word select entity ids;
    call(domain, service, data) acts through the platform. Returns what it did (for the log and the bench)."""
    eid = (data or {}).get("entity_id") or ""
    if not _SEL.match(eid):
        return {}
    try:
        mine = set(panel_selects() or [])
    except Exception:                                            # noqa: BLE001
        mine = set()
    if eid not in mine:
        return {}
    new = data.get("new_state") or {}
    old = data.get("old_state") or {}
    val = new.get("state")
    if val in _BACK_FROM:
        return {}
    who = ((new.get("context") or {}).get("user_id"))
    if who:
        d = _load()
        d.setdefault("chosen", {})[eid] = val
        _save(d)
        return {"remembered": val, "entity": eid}
    if (old.get("state") if old else None) in _BACK_FROM:
        want = choices().get(eid)
        if want and want != val:
            call("select", "select_option", {"entity_id": eid, "option": want})
            return {"put_back": want, "was": val, "entity": eid}
    return {}
