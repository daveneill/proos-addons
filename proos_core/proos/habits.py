"""
ProOS Core — habits, computed in the background (12 Sep 2026, register 430;
stage H1 of the memory plan).

Dave: "I want it to learn habits from all the logs etc and suggest
automations and scenes."

Until this build, usage.patterns() derived a room's habits from its journal
only when room_read or usage_history was called INSIDE a chat. Nothing ran
between conversations, so the product could describe a habit when asked and
never noticed one on its own. This module runs the SAME pure function —
usage.patterns, unchanged — over every room's journal once a night, and
writes the answer to /data/habits.json where Assist (and, in H2, the
suggestion card) reads it already reasoned.

What does NOT change: a habit is soft evidence, never proof of the room's
state now (usage.py doctrine); the engine never reads this file; the
three-distinct-days bar before anything is called a habit; and a habit
never ACTS — it may earn an offer, and only H2 makes offers. Dave's consent
ruling stands.

The file says how many rooms cleared the bar. On a box with a thin journal
that number is zero and the file says so — the honest reading, not a guess.
"""
from __future__ import annotations

import json
import os
import time

HABITS_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "habits.json")
FRESH_SECONDS = 26 * 3600          # a nightly file is fresh for a day and a bit
_SKIP_ROOMS = {"site", "service"}  # journal streams that are not rooms


def compute(journal, usage, now: float | None = None, min_days: int = 3) -> dict:
    """Every room's usage summary, from its journal. Pure apart from reading
    the journal; returns the dict that becomes the file."""
    now = now or time.time()
    rooms = {}
    for room in journal.rooms():
        if room in _SKIP_ROOMS:
            continue
        try:
            events = journal.read(room, limit=1000)
        except Exception:               # noqa: BLE001
            events = []
        s = usage.summary(events, min_days)
        if not s.get("patterns") and not s.get("observed_days"):
            continue                    # nothing was ever used here; say nothing
        rooms[room] = {"patterns": s["patterns"], "habits": s["habits"],
                       "observed_days": s["observed_days"]}
    cleared = sum(1 for r in rooms.values() if r["habits"])
    return {"computed_ts": round(now, 1), "min_days": min_days,
            "rooms": rooms, "rooms_seen": len(rooms), "rooms_with_habits": cleared,
            "note": "habits are soft evidence — never the live state, never a reason "
                    "to act without a yes"}


def save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HABITS_PATH), exist_ok=True)
        tmp = HABITS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, HABITS_PATH)
    except Exception:                   # noqa: BLE001
        pass


def load() -> dict:
    try:
        with open(HABITS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                   # noqa: BLE001
        return {}


def fresh(data: dict | None = None, now: float | None = None) -> bool:
    d = data if data is not None else load()
    try:
        return (now or time.time()) - float(d.get("computed_ts") or 0) < FRESH_SECONDS
    except Exception:                   # noqa: BLE001
        return False


def room(area_id: str, data: dict | None = None) -> dict | None:
    d = data if data is not None else load()
    return (d.get("rooms") or {}).get(area_id)


def refresh(journal, usage) -> dict:
    """Compute and write. The one call the nightly loop makes."""
    data = compute(journal, usage)
    save(data)
    return data


def clear() -> None:
    try:
        if os.path.exists(HABITS_PATH):
            os.remove(HABITS_PATH)
    except Exception:                   # noqa: BLE001
        pass
