"""The pages' own record of when they stopped answering — register 1116.

Dave, 31 Oct 13:48: in the ProOS app on his Mac "it will show the hand with the finger as a pointer then click fine but
then clicks are unresponsive for maybe 10 seconds and it does not show the hand pointer then works then stops again";
13:48 "dashboard and Pro mainly pro". Measured the same day in Chrome on his box: Pro answered three clicks half a second
apart, no long task, nothing covering the page — so the question is whether the PAGE freezes inside the Mac app's web
view, or the page never freezes and the app's own touch handling holds the clicks back. Only the page can say which.

A DIAGNOSTIC, not a feature. The pages note any moment their own clock stood still for more than half a second (a timer
that should fire every 100 ms and did not), with what was last clicked, and post the notes here. Core keeps the last 500
lines on disk and prints one line per note to its log. It decides nothing and changes nothing; it is read by hand.
Remove it (with its two page hooks and its bench) once the answer is in.
"""
import json
import os
import threading
import time

_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "ui_stalls.jsonl")
_KEEP = 500          # lines kept on disk — plumbing, not a rule
_LOCK = threading.Lock()


def _clean(v, n=120):
    return str(v if v is not None else "")[:n]


def record(body, user_agent="", path=None):
    """Store what a page sent. Returns how many notes were kept from it."""
    path = path or _PATH
    if not isinstance(body, dict):
        return 0
    stalls = body.get("stalls")
    if not isinstance(stalls, list):
        return 0
    now = time.time()
    rows = []
    for s in stalls[:50]:
        if not isinstance(s, dict):
            continue
        try:
            ms = int(s.get("ms") or 0)
        except Exception:                                        # noqa: BLE001
            continue
        if ms <= 0:
            continue
        rows.append({"t": round(now, 1), "app": _clean(body.get("app"), 20), "build": _clean(body.get("build"), 20),
                     "ua": _clean(user_agent, 160), "at": _clean(s.get("at"), 30), "ms": ms,
                     "page": _clean(s.get("page"), 60), "last": _clean(s.get("last"), 80),
                     "since_click": s.get("since_click") if isinstance(s.get("since_click"), (int, float)) else None,
                     "work": _clean(s.get("work"), 300)})   # 1118: Pro's own steps that ran during the freeze
    if not rows:
        return 0
    with _LOCK:
        try:
            old = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    old = fh.read().splitlines()
            keep = (old + [json.dumps(r) for r in rows])[-_KEEP:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(keep) + "\n")
            os.replace(tmp, path)
        except Exception as e:                                   # noqa: BLE001
            print("  ui-stall · could not keep notes: %s" % e, flush=True)
    for r in rows:
        print("  ui-stall · %s %s froze %d ms on %s, %s after clicking %s%s"
              % (r["app"], r["build"], r["ms"], r["page"] or "?",
                 ("%d ms" % r["since_click"]) if r["since_click"] is not None else "?", r["last"] or "nothing",
                 (" — running: " + r["work"]) if r["work"] else ""),
              flush=True)
    return len(rows)


def recent(n=200, path=None):
    """The newest notes, newest last."""
    path = path or _PATH
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()[-max(1, int(n)):]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:                                            # noqa: BLE001
        return []
