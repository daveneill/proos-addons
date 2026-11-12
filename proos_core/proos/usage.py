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


# THE LEARNING ALGORITHM, L1 + L2 (register 1314, 12 Nov 2026 — ProOS_Assist_Learning_Algorithm_2026-09-17.md §2–3).
# Counting is not learning: three days was a habit whether it was last week or two months ago, at one hour or every
# hour. Each pattern now also carries the design's confidence — the PRODUCT of four scores, so one fatal weakness is
# never averaged away. Every number is the design's starting value, to be read off Dave's box before it is settled.
HALF_LIFE_DAYS = 14.0          # weight = 0.5 ^ (age / 14)
SUPPORT_FULL = 8.0             # weighted occurrences for full marks
SPREAD_FULL = 5.0              # distinct days for full marks
FRESH_FULL_DAYS = 7.0          # freshness 1.0 within a week…
FRESH_ZERO_DAYS = 42.0         # …tailing to 0 at six weeks


def _freshness(age_days: float) -> float:
    if age_days <= FRESH_FULL_DAYS:
        return 1.0
    if age_days >= FRESH_ZERO_DAYS:
        return 0.0
    return 1.0 - (age_days - FRESH_FULL_DAYS) / (FRESH_ZERO_DAYS - FRESH_FULL_DAYS)


def confidence_of(weighted: float, days: int, regularity: float, age_days: float) -> dict:
    """The design's confidence and its four parts — pure, so the bench and the box read the same arithmetic."""
    sup = min(1.0, weighted / SUPPORT_FULL)
    spr = min(1.0, days / SPREAD_FULL)
    reg = max(0.0, min(1.0, regularity))
    fre = _freshness(age_days)
    return {"support": round(sup, 3), "spread": round(spr, 3), "regularity": round(reg, 3),
            "freshness": round(fre, 3), "confidence": round(sup * spr * reg * fre, 3)}


# L3 (register 1315) — what each level of confidence permits (the design §4). "on" alone earns nothing.
BAND_MENTION = 0.40            # under this: not spoken of, not reasoned from
BAND_OFFER = 0.65              # may be offered once, as a scene
BAND_STRONG = 0.80             # + half its starts from outside ProOS: may be offered as an automation
_NOT_A_HABIT = {"on"}


def band(conf: float) -> str:
    c = float(conf or 0.0)
    return "strong" if c >= BAND_STRONG else "offer" if c >= BAND_OFFER else "mention" if c >= BAND_MENTION else "none"


def patterns(events, min_days: int = 3, now: float | None = None) -> list:
    """Per-activity usage patterns for ONE room's journal events. Returns a list
    of dicts, most-used first:
      {activity, label, source, count, days, part_of_day, day_type,
       external_share, last_seen, habit}
    `habit` is True when the activity recurs across at least `min_days` distinct
    days — one-offs are reported but never called habits."""
    import time as _t
    _now = float(now) if now is not None else _t.time()
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
                                   "external": 0, "last": 0.0, "labels": {},
                                   "weight": 0.0, "whours": {}})
        g["count"] += 1
        _w = 0.5 ** (max(0.0, (_now - float(ev.get("ts"))) / 86400.0) / HALF_LIFE_DAYS)   # L1
        g["weight"] += _w
        # the verdict's own label ("Apple TV") rides along so an offer built
        # from this pattern speaks the home's words, never the key (reg 431)
        lab = d.get("label")
        if lab:
            g["labels"][lab] = g["labels"].get(lab, 0) + 1
        g["days"].add(dt.date().toordinal())
        pod = _part_of_day(dt.hour)
        g["hours"][pod] = g["hours"].get(pod, 0) + 1
        g["whours"][pod] = g["whours"].get(pod, 0.0) + _w            # L2: the busiest part's SHARE, weighted
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
            "label": (max(g["labels"], key=g["labels"].get)
                      if g["labels"] else None),
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
            "weighted": round(g["weight"], 3),
            "band": None,                                   # set below, from the confidence (L3)
            **confidence_of(g["weight"], len(g["days"]),
                            (max(g["whours"].values()) / g["weight"]) if g["weight"] else 0.0,
                            max(0.0, (_now - g["last"]) / 86400.0) if g["last"] else 999.0),
        })
    for p in out:
        p["band"] = "none" if p["activity"] in _NOT_A_HABIT else band(p["confidence"])
        p["habit"] = p["band"] != "none"            # L3: a habit is a confidence of 0.40 or more, not three days
    out.sort(key=lambda p: (p["confidence"], p["count"]), reverse=True)
    return out


def expectation(events, at_ts, min_days: int = 3):
    """The habit that matches a GIVEN moment (H4): of the room's habits, the one
    whose time-of-day AND day-type (weekday/weekend) fit `at_ts`, strongest first.
    Returns that pattern dict or None. PURE — this is 'what the room is usually
    doing right now', a hint for Assist to stack and CONFIRM, never a claim about
    what IS happening (that's room_status) and never a licence to act."""
    try:
        dt = datetime.fromtimestamp(float(at_ts))
    except Exception:                   # noqa: BLE001
        return None
    now_pod = _part_of_day(dt.hour)
    now_weekend = dt.weekday() >= 5
    best = None
    for p in patterns(events, min_days, now=at_ts):     # 1315: confidence as of the moment asked about
        if not p["habit"] or p["part_of_day"] != now_pod:
            continue
        dt_ok = (p["day_type"] == "any day"
                 or (p["day_type"] == "weekdays" and not now_weekend)
                 or (p["day_type"] == "weekends" and now_weekend))
        if not dt_ok:
            continue
        if best is None or p["count"] > best["count"]:
            best = p
    return best


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
