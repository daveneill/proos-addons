"""
ProOS Core — usage patterns (Pro-Assistant habit layer, H2, 7 Aug 2026).

Turns the room JOURNAL Core already keeps (verdict + external_control events)
into per-room HABITS: which activity a room is used for, at what time of day, on
weekdays vs weekends, and how often it's started externally (native remote).

PURE and READ-ONLY. It reads recorded history and computes patterns; it NEVER
reads or writes the live verdict, and nothing here can change what the engine
claims. A habit is a SOFT witness for Assist to reason with and to personalise —
never proof of the room's state right now. (Doctrine:
ProOS_Assist_Pro_Assistant_Design_2026-08-07.md — the engine proves; Assist
reasons; a habit is only ever a hint.)
"""
from __future__ import annotations
from datetime import datetime


def _is_active(to) -> bool:
    """A verdict state that means the room is being USED for something — not
    off/idle/starting/unavailable, which are not habits."""
    t = str(to or "")
    return t.startswith("watch_") or t in ("playing", "on", "listen", "music")


# hour-of-day → plain-English part of day (what a person would say)
_BUCKETS = (("early morning", 5, 9), ("morning", 9, 12), ("afternoon", 12, 17),
            ("evening", 17, 22))


def _part_of_day(hour: int) -> str:
    for name, lo, hi in _BUCKETS:
        if lo <= hour < hi:
            return name
    return "night"                      # 22:00–05:00


def patterns(events, min_days: int = 3) -> list:
    """Per-activity usage patterns for ONE room's journal events. Returns a list
    of dicts, most-used first:
      {activity, source, count, days, part_of_day, day_type, external_share,
       last_seen, habit}
    `habit` is True when the activity recurs across at least `min_days` distinct
    days — one-offs are reported but never called habits."""
    groups: dict = {}
    for ev in (events or []):
        if not isinstance(ev, dict) or ev.get("type") != "verdict":
            continue
        d = ev.get("data") or {}
        to = d.get("to")
        if not _is_active(to):
            continue
        try:
            dt = datetime.fromtimestamp(float(ev.get("ts")))
        except Exception:               # noqa: BLE001 — a bad row is just skipped
            continue
        g = groups.setdefault(to, {"count": 0, "days": set(), "hours": {},
                                   "sources": {}, "weekday": 0, "weekend": 0,
                                   "external": 0, "last": 0.0})
        g["count"] += 1
        g["days"].add(dt.date().toordinal())
        pod = _part_of_day(dt.hour)
        g["hours"][pod] = g["hours"].get(pod, 0) + 1
        src = d.get("source")
        if src:
            g["sources"][src] = g["sources"].get(src, 0) + 1
        if dt.weekday() >= 5:
            g["weekend"] += 1
        else:
            g["weekday"] += 1
        if d.get("external"):
            g["external"] += 1
        g["last"] = max(g["last"], float(ev.get("ts") or 0))

    out = []
    for to, g in groups.items():
        tot = g["weekday"] + g["weekend"]
        if tot and g["weekday"] / tot >= 0.7:
            day_type = "weekdays"
        elif tot and g["weekend"] / tot >= 0.7:
            day_type = "weekends"
        else:
            day_type = "any day"
        out.append({
            "activity": to,
            "source": (max(g["sources"], key=g["sources"].get)
                       if g["sources"] else None),
            "count": g["count"],
            "days": len(g["days"]),
            "part_of_day": (max(g["hours"], key=g["hours"].get)
                            if g["hours"] else None),
            "day_type": day_type,
            "external_share": (round(g["external"] / g["count"], 2)
                               if g["count"] else 0.0),
            "last_seen": g["last"],
            "habit": len(g["days"]) >= min_days,
        })
    out.sort(key=lambda p: p["count"], reverse=True)
    return out


def summary(events, min_days: int = 3) -> dict:
    """A room's usage at a glance: every pattern, the subset that are habits, and
    how many days of history it was drawn from — with the standing caveat that
    habits are soft evidence, never the live state."""
    pats = patterns(events, min_days)
    days = set()
    for ev in (events or []):
        if isinstance(ev, dict) and ev.get("type") == "verdict" and ev.get("ts"):
            try:
                days.add(datetime.fromtimestamp(float(ev["ts"])).date().toordinal())
            except Exception:           # noqa: BLE001
                pass
    return {
        "patterns": pats,
        "habits": [p for p in pats if p["habit"]],
        "observed_days": len(days),
        "note": "habits are soft evidence for reasoning and personalisation — "
                "never proof of what the room is doing right now (use room_status "
                "for that), and never a reason to act without a yes",
    }
