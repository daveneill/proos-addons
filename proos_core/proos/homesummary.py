"""THE HOME LINE, WRITTEN (register 1545 — Dave, 28 Nov 2026 17:05: "I have alwyas not been happy with the word summary on the
home page as to what is going on in the rest of the home can this be better or handled by assist AI as to write an actual
summary"; 17:13: "yes go with 2 with 1 as the words underneath and lights not included").

Two lines, both from the SAME facts every room already serves (its home fact and its own sentence):

  rule_words — the words underneath. What is on and where, in the room's own words: "Letters never sent by Her Name is Sage
               in the Office. Stranger Things on Netflix in the Family Room." Instant, always true, no AI needed.
  written    — the AI's line over them. One short question to a small model, given ONLY those facts and the time of day.
               Asked in the background (the sweep never waits), asked again only when the facts change, and what it writes
               is CHECKED before it is shown: it must name every room that has something on, no room that hasn't, nothing
               of the platform's name, and fit the line. Fail any of those and the words underneath stand.

Lights are not in either line (Dave 17:13). Nothing on: neither line speaks — the weather line stands (1504).
"""
from __future__ import annotations

import re
import threading
import time

ASK = None            # callable(prompt) -> the AI's text; set by Core when an AI is configured
MIN_GAP_S = 15        # never two questions closer than this — a run of changes is one question, of the last facts
MAX_CHARS = 170       # the line is one line under the word Home
KEPT = 24             # readings remembered
_PLAIN_MUSIC = ("music is playing", "music", "off", "")
_NOT_SAID = re.compile(r"home\s*assistant|\bhass\b|music\s*assistant|\bentity\b|media_player|\bsensor\b", re.I)

_lock = threading.Lock()
_made = {}            # facts key -> the checked words, or "" when the AI's words were not fit to show
_busy = set()
_last = [0.0]


def _cap(s):
    return s[0].upper() + s[1:] if s else s


def _items(rooms):
    """[(kind, words, room name)] — watching, then music, then anything else that is on. No lights."""
    watch, music, other = [], [], []
    for r in rooms or []:
        f = (r or {}).get("fact")
        if not isinstance(f, dict):
            continue
        k, where = f.get("kind"), str(f.get("where") or "").strip()
        s = str((r or {}).get("sentence") or "").strip()
        if not where or k == "lights":
            continue
        room = str((r or {}).get("room") or where)
        if k == "music":
            music.append(("music", ("%s in %s" % (s, where)) if s.lower() not in _PLAIN_MUSIC else "Music in %s" % where, room))
        elif k == "watch":
            what = str(f.get("what") or "").strip()
            if s and s.lower() != what.lower() and not s.lower().startswith("watching"):
                watch.append(("watch", "%s in %s" % (s, where), room))
            elif what:
                watch.append(("watch", "Watching %s in %s" % (what, where), room))
        elif k == "on":
            names = [n for n in (f.get("what") if isinstance(f.get("what"), list) else []) if n]
            base = _cap(where)
            if not names:
                other.append(("on", "%s is on" % base, room))
            elif len(names) == 1:
                other.append(("on", "%s %s is on" % (base, names[0]), room))
            else:
                other.append(("on", "%s %s and %s are on" % (base, ", ".join(names[:-1]), names[-1]), room))
    return watch + music + other


def rule_words(rooms):
    """The words underneath: one sentence a room, or None when nothing is on."""
    it = _items(rooms)
    return " ".join(_cap(w) + "." for _, w, _ in it) if it else None


def prompt(items, part=None):
    return ("Write the one-line summary shown at the top of a home's control screen.\n"
            "These are the only facts — everything that is on in the home right now:\n%s\n%s"
            "Write ONE natural sentence (two short ones at most), under 140 characters, that says what is going on in the home. "
            "Name every room listed, by its name as written. Use only these facts: add no room, device, person, reason, "
            "mood or advice that is not written here, and do not say what is off. No greeting, no quotes, no emoji.\n"
            "Reply with the sentence alone."
            % ("\n".join("- %s" % w for _, w, _ in items), ("It is the %s.\n" % part) if part else ""))


def accept(text, items, all_rooms=()):
    """The AI's words, cleaned, if they are fit to show — else None."""
    t = " ".join(str(text or "").split()).strip().strip('"“”\'').strip()
    if not t or len(t) > MAX_CHARS or _NOT_SAID.search(t):
        return None
    on = {r.lower() for _, _, r in items}
    low = t.lower()
    said = set()
    for name in sorted({str(n) for n in list(all_rooms) + [r for _, _, r in items] if n}, key=len, reverse=True):
        n = name.lower()
        if re.search(r"(?<![\w'’])%s(?![\w])" % re.escape(n), low):
            said.add(n)
            low = re.sub(r"(?<![\w'’])%s(?![\w])" % re.escape(n), " ", low)   # "Bec's Office" is not also "Office"
    if said != on:
        return None
    return t


def busy():
    with _lock:
        return bool(_busy)


def forget():
    with _lock:
        _made.clear()
        _busy.clear()
        _last[0] = 0.0


def _write(key, items, part, all_rooms):
    words = ""
    try:
        t0 = time.time()
        text = ASK(prompt(items, part))
        words = accept(text, items, all_rooms) or ""
        if text is not None:                                     # None: no AI is set up — nothing to say about it
            print("  [home line] the AI wrote in %.1fs: %r — %s"
                  % (time.time() - t0, str(text or "")[:150], "shown" if words else "NOT shown (the plain words stand)"), flush=True)
    except Exception as e:                                       # noqa: BLE001
        print("  [home line] the AI didn't answer (%s) — the plain words stand" % str(e)[:120], flush=True)
    with _lock:
        _busy.discard(key)
        _made[key] = words
        while len(_made) > KEPT:
            _made.pop(next(iter(_made)))


def written(rooms, part=None, all_rooms=(), now=None):
    """The AI's checked line for THESE facts, or None (not written yet, not fit, no AI, nothing on). Never waits."""
    items = _items(rooms)
    if not items or ASK is None:
        return None
    key = "%s|%s" % (part or "", "\n".join(w for _, w, _ in items))
    now = time.time() if now is None else now
    with _lock:
        if key in _made:
            return _made[key] or None
        if key in _busy or _busy or (now - _last[0]) < MIN_GAP_S:
            return None
        _busy.add(key)
        _last[0] = now
    threading.Thread(target=_write, args=(key, items, part, list(all_rooms)), daemon=True, name="home-line").start()
    return None
