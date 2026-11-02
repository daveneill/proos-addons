"""THE PERSON WHO ASKED IS TOLD HOW IT TURNED OUT — register 1179 (2 Nov 2026).

Dave 21:03: "the bedroom tv is not responding and ProOS doesn't even know" — read: Ryan answered at ~20:57:27, before
anything was checked; the room's own record found the TV never came on at 20:58:19 (Health: "Bedroom — Bedroom TV never
came on", 20:58:55) — on the installer's side, never in the conversation that asked. Dave 22:29, on the audit
("find → tell the person → hand to Assist → report → installer"): "Just go".

This module only WATCHES and REMEMBERS. When an Assist turn (typed, a room mic, Talk Live — any road) starts something
in a room, the rooms its words or targets name are watched; the room's OWN verdict decides the outcome — the same record
Health and the dashboards read (ctlbridge's "verdict" and "activity_missed" journal events), never a guess:
  - a verified verdict in the direction asked (on, or off for an Off)  → "ok"
  - the room's activity_missed                                       → "missed"
  - nothing within WATCH_S                                            → "unconfirmed"
The conversation collects the outcome (collect) and says it; one the conversation has not collected within COLLECT_S
(the person has moved on) is handed back for the asker's phone (due) — a failure only; a success needs no alert.
"""
import json
import re
import threading
import time

# Plumbing (not observation rules): how long a request is watched — a TV's start plus the room's miss check (~90 s,
# register 926) and margin; and how long the conversation has to pick an outcome up before the asker's phone is used.
WATCH_S = 150
COLLECT_S = 20
# What starts something whose outcome only the room's verdict can tell (a light or switch is read back in the turn
# itself — register 1147 — so it is not watched here).
ACT_TOOLS = ("room_activity", "app_launch", "watch_play", "platform_command", "room_off", "room_on")

_lock = threading.Lock()
_pending: list = []
_seq = [0]


def _norm(s) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split()) + " "


def watch(user: dict, acts: list, words: str, rooms: list, now=None):
    """Start watching the rooms an Assist turn's acts name. acts: [{"tool", "target", "args"?}]; rooms: [(id, name)].
    Returns the watch record, or None when nothing room-level was started or no room is named (never a guess)."""
    now = time.time() if now is None else now
    acts = [a for a in (acts or []) if (a or {}).get("tool") in ACT_TOOLS]
    if not acts or not (user or {}).get("id"):
        return None
    hay = _norm(" ".join([json.dumps(a, default=str) for a in acts] + [words or ""]))
    named = []
    for rid, rname in rooms or []:
        if not rid:
            continue
        if _norm(rid) in hay or (rname and _norm(rname) in hay):
            named.append((rid, rname or rid))
    if not named:
        return None
    expect_off = any(a.get("tool") == "room_off" for a in acts) or " off " in hay
    with _lock:
        _seq[0] += 1
        rec = {"id": _seq[0], "user": user.get("id"), "who": user.get("name") or "", "rooms": named,
               "t0": now, "expect": "off" if expect_off else "on", "result": None, "said": "", "at": None,
               "told": False}
        # one watch per person per room: a newer request replaces an older one still waiting
        _pending[:] = [p for p in _pending if not (p["user"] == rec["user"] and p["result"] is None
                                                   and {r for r, _ in p["rooms"]} & {r for r, _ in named})]
        _pending.append(rec)
        del _pending[:-50]
    return rec


def _words(kind: str, room: str, label: str, expect: str) -> str:
    label = (label or "").strip()
    if kind == "ok":
        return ("The %s is off." % room) if expect == "off" else (
            "%s is on in the %s." % (label, room) if label and label.lower() not in ("on", "watch tv") else
            "The %s is on." % room)
    if kind == "missed":
        return "%s didn't start in the %s — the TV never came on." % (label or "That", room)
    return "I couldn't confirm the %s — it hasn't reported back." % room


def check(read_fn, now=None) -> list:
    """Decide what can be decided. read_fn(room_id, since) → that room's journal events, newest first.
    Returns the records decided on this call."""
    now = time.time() if now is None else now
    done = []
    with _lock:
        waiting = [p for p in _pending if p["result"] is None]
    for p in waiting:
        kind = label = room = None
        for rid, rname in p["rooms"]:
            try:
                evs = list(reversed(read_fn(rid, p["t0"] - 1) or []))     # oldest first
            except Exception:                                    # noqa: BLE001
                evs = []
            miss = next((e for e in evs if e.get("type") == "activity_missed"), None)
            if miss:                                             # the room's own "never came on" outranks any verdict
                kind, label, room = "missed", (miss.get("data") or {}).get("label"), rname
                break
            for e in evs:
                d = e.get("data") or {}
                if (e.get("type") == "verdict" and d.get("verified") is True and not d.get("external")
                        and d.get("from") != d.get("to") and (str(d.get("to")) == "off") == (p["expect"] == "off")):
                    kind, label, room = "ok", d.get("label"), rname
                    break
            if kind:
                break
        if not kind and now - p["t0"] >= WATCH_S:
            kind, room = "unconfirmed", p["rooms"][0][1]
        if kind:
            with _lock:
                p["result"], p["at"] = kind, now
                p["said"] = _words(kind, room, label, p["expect"])
            done.append(p)
    return done


def collect(user_id, now=None) -> list:
    """The outcomes this person's conversation has not said yet — marked told."""
    out = []
    with _lock:
        for p in _pending:
            if p["user"] == user_id and p["result"] and not p["told"]:
                p["told"] = True
                out.append({"result": p["result"], "say": p["said"], "rooms": [r for r, _ in p["rooms"]]})
    return out


def due(now=None) -> list:
    """Failures nobody's conversation picked up within COLLECT_S — for the asker's phone; marked told."""
    now = time.time() if now is None else now
    out = []
    with _lock:
        for p in _pending:
            if p["result"] in ("missed", "unconfirmed") and not p["told"] and now - (p["at"] or now) >= COLLECT_S:
                p["told"] = True
                out.append(dict(p))
            elif p["result"] == "ok" and not p["told"] and now - (p["at"] or now) >= COLLECT_S:
                p["told"] = True                                 # a success nobody collected needs no alert
    return out


def clear():
    with _lock:
        _pending.clear()
