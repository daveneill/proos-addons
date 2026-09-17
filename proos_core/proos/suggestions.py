"""
ProOS Core — suggestions: scenes and automations OFFERED from habits
(12 Sep 2026, register 431; stage H2 of the memory plan).

Dave: "I want it to learn habits from all the logs etc and suggest
automations and scenes."

H1 (register 430) writes /data/habits.json every night. This module turns
the habits in that file into OFFERS — one sentence each, in the home's own
words — for the "Pro Assist noticed" card on the Dashboard and the same
list with its evidence in Pro. Two kinds, in order of trust:

  scene       a room is used for one activity on enough distinct days
              → "want a '<Room> <Activity>' scene?"
  automation  that same activity is usually STARTED from outside ProOS
              (the native remote, a source waking the display) →
              "want ProOS to set the room up when that happens?"

What this module never does: act. A habit earns an OFFER, never an act
(Dave's consent ruling, register 266). Yes on the card is the yes — the
app hands the `ask` sentence to Pro Assist, which builds the thing the way
it builds anything (read the room → write → read back → say so). Never is
recorded here AND in the person's declines (assist memory), so neither the
card nor the model re-offers it: a no sticks. Not now snoozes a week.

Pure apart from its own store: reads habits.json, never the journal, never
the live state, never a service. The verdict engine never reads this.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "suggestions.json")
SNOOZE_SECONDS = 7 * 86400
EXTERNAL_SHARE_FOR_AUTOMATION = 0.5   # half or more of its starts came from outside ProOS
_SKIP_ACTIVITIES = {"on"}             # "the room was on" is not a habit worth a scene
_MUSIC = {"playing", "music", "listen"}
_ANSWERS = ("yes", "later", "never")


# ── words ───────────────────────────────────────────────────────────────────
def _label(room: str, p: dict) -> str:
    """What the activity is called on the glass: the verdict's own label when
    the journal carried one, else derived from the key (never a slug)."""
    lab = (p.get("label") or "").strip()
    if lab:
        return lab
    act = str(p.get("activity") or "")
    if act in _MUSIC:
        return "Music"
    if act.startswith("watch_"):
        act = act[len("watch_"):]
        # keys are often "<room>_<room>_<source>" — strip the room prefixes
        r = str(room or "")
        while r and act.startswith(r + "_"):
            act = act[len(r) + 1:]
    words = [w for w in act.replace("-", "_").split("_") if w]
    return " ".join(w.upper() if w in ("tv", "hdmi", "atv") else w.capitalize()
                    for w in words) or "that"


def _when(p: dict) -> str:
    pod = p.get("part_of_day") or ""
    dt = p.get("day_type") or "any day"
    lead = {"weekdays": "most weekday", "weekends": "most weekend"}.get(dt, "most")
    return ("%s %ss" % (lead, pod)) if pod else ("%s days" % lead)


def _sid(room: str, label: str, kind: str) -> str:
    key = "%s|%s|%s" % (room, label.lower(), kind)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _room_name(room: str, names: dict | None) -> str:
    n = (names or {}).get(room)
    return n if n else str(room or "").replace("_", " ").title()


def _the(name: str, cap: bool = False) -> str:
    """The room with its article — "the Office", "the Family Room" — unless
    the name is already somebody's: "Ryan's Room", "Bec's Office" take no
    "the" (register 436: "The Ryan's Room is used for…" on Dave's box). A
    possessive is the tell, not a list of names."""
    n = str(name or "")
    if "'" in n or "\u2019" in n:
        return n
    return ("The " if cap else "the ") + n


# ── derive ──────────────────────────────────────────────────────────────────
def derive(habits: dict, names: dict | None = None) -> list:
    """Every offer the habits file supports, strongest first. Pure."""
    out = {}
    for room, rec in ((habits or {}).get("rooms") or {}).items():
        for p in (rec or {}).get("habits") or []:
            act = str(p.get("activity") or "")
            if not act or act in _SKIP_ACTIVITIES:
                continue
            label = _label(room, p)
            rn = _room_name(room, names)
            ev = {"days": int(p.get("days") or 0), "count": int(p.get("count") or 0),
                  "last_seen": p.get("last_seen"), "part_of_day": p.get("part_of_day"),
                  "day_type": p.get("day_type"),
                  "external_share": float(p.get("external_share") or 0.0)}
            base = {"room": room, "room_name": rn, "activity": act, "label": label,
                    "evidence": ev, "title": "Pro Assist noticed"}
            scene_name = "%s %s" % (rn, label)
            s = dict(base, id=_sid(room, label, "scene"), kind="scene",
                     text=("%s is used for %s %s. Want a ‘%s’ scene that "
                           "sets it up in one tap?" % (_the(rn, True), label, _when(p), scene_name)),
                     ask=("Yes — build a scene called ‘%s’ that sets %s up "
                          "for %s the way it is usually used. Read the room first, then "
                          "tell me what the scene does." % (scene_name, _the(rn), label)))
            _keep(out, s)
            if ev["external_share"] >= EXTERNAL_SHARE_FOR_AUTOMATION:
                a = dict(base, id=_sid(room, label, "automation"), kind="automation",
                         text=("%s in %s is usually started with its own remote. Want "
                               "ProOS to set the room up the way you like it when that "
                               "happens?" % (label, _the(rn))),
                         ask=("Yes — when %s starts in %s on its own (someone used "
                              "the remote), have ProOS set the room up the way it usually "
                              "is. Build that automation and tell me what it does."
                              % (label, _the(rn))))
                _keep(out, a)
    rows = list(out.values())
    rows.sort(key=lambda r: (-r["evidence"]["days"], -r["evidence"]["count"],
                             r["kind"] != "scene", r["room_name"]))
    return rows


def _keep(out: dict, s: dict) -> None:
    """Two habit keys for one thing (e.g. watch_apple_tv and the room-prefixed
    key) collapse to one offer — the stronger evidence wins."""
    cur = out.get(s["id"])
    if cur is None or (s["evidence"]["days"], s["evidence"]["count"]) > (
            cur["evidence"]["days"], cur["evidence"]["count"]):
        out[s["id"]] = s


# ── the store: what people answered ─────────────────────────────────────────
def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                   # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, STORE)
    except Exception:                   # noqa: BLE001
        pass


def answer(sid: str, ans: str, by: str = "", text: str = "") -> dict:
    """Record what a person said to an offer. Returns the record."""
    ans = str(ans or "").lower().strip()
    if ans not in _ANSWERS:
        return {"error": "answer must be one of %s" % ", ".join(_ANSWERS)}
    if not sid:
        return {"error": "id required"}
    d = _load()
    answers = d.setdefault("answers", {})
    answers[sid] = {"answer": ans, "ts": round(time.time(), 1), "by": by or "",
                    "text": (text or "")[:300]}
    _save(d)
    return {"ok": True, "id": sid, "answer": ans}


def forget_text(text: str) -> int:
    """A forget on the memory page strikes the matching Never here too, so
    the offer may come back — one forget, every store (register 428 rule)."""
    low = (text or "").strip().lower()
    if not low:
        return 0
    d = _load()
    answers = d.get("answers") or {}
    gone = [k for k, v in answers.items()
            if (v.get("text") or "").strip().lower() == low]
    for k in gone:
        answers.pop(k, None)
    if gone:
        _save(d)
    return len(gone)


def _hidden(rec: dict | None, now: float) -> bool:
    if not rec:
        return False
    a = rec.get("answer")
    if a in ("yes", "never"):
        return True
    if a == "later":
        try:
            return now - float(rec.get("ts") or 0) < SNOOZE_SECONDS
        except Exception:               # noqa: BLE001
            return False
    return False


def pending(habits: dict, names: dict | None = None, declines=(),
            now: float | None = None) -> list:
    """The offers still open: derived, minus answered (yes / never / snoozed),
    minus anything in the person's declines by its words."""
    now = now or time.time()
    answers = (_load().get("answers") or {})
    low = {str(t or "").strip().lower() for t in (declines or ())}
    out = []
    for s in derive(habits, names):
        if _hidden(answers.get(s["id"]), now):
            continue
        if s["text"].strip().lower() in low:
            continue
        out.append(s)
    return out


def answered(habits: dict, names: dict | None = None) -> list:
    """For Pro: every offer with what was said to it (the evidence beside)."""
    answers = (_load().get("answers") or {})
    rows = []
    for s in derive(habits, names):
        rec = answers.get(s["id"])
        if rec:
            rows.append(dict(s, answer=rec.get("answer"), answered_ts=rec.get("ts"),
                             by=rec.get("by") or ""))
    return rows


def clear() -> None:
    try:
        if os.path.exists(STORE):
            os.remove(STORE)
    except Exception:                   # noqa: BLE001
        pass
