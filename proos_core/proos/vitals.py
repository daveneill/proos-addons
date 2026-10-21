"""Core's own vital signs — so ProOS can say when it is blind.

Register 148 (14 Aug 2026): "A repair that reports nothing is the fault class
this product exists to kill … any reader that can fail must fail LOUDLY, never
quietly return nothing." Plan item 2 (1 Oct 2026, G-44): the sweep judged the
whole house against an empty read when a fetch failed, and nothing anywhere
said when the sweep, the health scan or the watcher last finished — so a
stalled subsystem looked exactly like a quiet house.

Each long-running subsystem STAMPS when it completes a pass, or says BLIND
with the reason when it could not read. `/health` serves the stamps with
their ages; Health raises a card from them at read time (healthmon), so the
card appears even if the thread that should have raised it is the one that
stopped. Only ProOS knows its own threads — the platform has no view of them
(the 18 Sep law: build only what only ProOS knows).

PATIENCE (declared in docs/ProOS_Rule_Ledger.md): how long a pass may be
overdue before ProOS says so. Each is several times the subsystem's own
cadence, so a slow pass on a big house is never mistaken for a stopped one.

AGES ARE MEASURED ON THE MONOTONIC CLOCK (register 650, 2 Oct 2026). The staging
rig stepped the wall clock 24 hours and every stamp looked a day old: Health
raised "Health checks is not running" and it reached the phone. A box with no
clock battery steps its wall clock exactly like that when it finds the time
server after a boot. Wall times are kept only to SHOW when a pass happened.
"""
import threading
import time

# subsystem -> (words for the glass, seconds overdue before it is said)
SUBSYSTEMS = {
    "sweep": ("Room status", 120),          # nominal 2 s; a big house measured 30–60 s
    "health_scan": ("Health checks", 420),  # once a minute on the clock (register 649)
    "watcher": ("Device watch", 90),        # 5 s reconciliation
}

_lock = threading.Lock()
_born = time.time()
_born_mono = time.monotonic()
_v = {}          # name -> {"at": wall ts of last good pass (display), "mono": its monotonic ts (ages),
                 #          "ok": bool, "why": str|None, "since": wall ts blind began}


def stamp(name):
    """A pass completed with a real reading."""
    now, mono = time.time(), time.monotonic()
    with _lock:
        _v[name] = {"at": now, "mono": mono, "ok": True, "why": None, "since": None}


def blind(name, why):
    """A pass ran but could not read — say why; keep the last good time."""
    now = time.time()
    with _lock:
        cur = _v.get(name) or {"at": None}
        _v[name] = {"at": cur.get("at"), "mono": cur.get("mono"), "ok": False, "why": str(why)[:200],
                    "since": cur.get("since") or now}


def read(mono=None):
    """{name: {label, last_ok, age_s, ok, why, overdue}} for /health and Health.
    `mono` is a monotonic reading (for a driven bench); ages never use the wall clock."""
    mono = time.monotonic() if mono is None else mono
    out = {}
    with _lock:
        snap = {k: dict(v) for k, v in _v.items()}
    for name, (label, limit) in SUBSYSTEMS.items():
        v = snap.get(name) or {"at": None, "ok": True, "why": None, "since": None}
        ref = v.get("mono") or _born_mono
        age = mono - ref
        out[name] = {"label": label,
                     "last_ok": v.get("at"),
                     "age_s": round(age, 1),
                     "ok": bool(v.get("ok")),
                     "why": v.get("why"),
                     "blind_since": v.get("since"),
                     "overdue": age > limit,
                     "limit_s": limit}
    return out


def faults(mono=None):
    """The subsystems that are blind or overdue, in words — what Health shows."""
    out = []
    for name, r in read(mono).items():
        if not r["ok"] or r["overdue"]:
            mins = int(r["age_s"] // 60)
            when = ("%d minutes" % mins) if mins >= 2 else ("%d seconds" % int(r["age_s"]))
            if not r["ok"]:
                cause = ("%s could not read the house (%s); its last good reading was %s ago. "
                         "Nothing it reports until then is current." % (r["label"], r["why"] or "no reason given", when))
            else:
                cause = ("%s has not completed a pass for %s. Until it does, the house is "
                         "not being checked — silence here is not an all-clear." % (r["label"], when))
            out.append({"name": name, "label": r["label"], "cause": cause,
                        "since": r["blind_since"] or r["last_ok"] or _born})
    return out
