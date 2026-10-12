"""PLATFORM AND OPERATING SYSTEM UPDATES, CONTROLLED BY ProOS — build 2 of "only serve ProOS" (register 813; Dave 13 Oct
07:48 "go"; the read is 807).

The box's system manager (the Supervisor) does every update; Core holds its admin role. READ IN ITS 2026.09.3 SOURCE:
  · POST /core/update {version, backup} — the platform to an EXACT version, with the Supervisor's own backup first.
  · POST /os/update {version} — the operating system to an exact version; the box keeps the previous one in its other
    boot slot.
  · The platform's update REQUIRES the Supervisor to be on its latest version (JobCondition.SUPERVISOR_UPDATED), and
    turning the Supervisor's auto-update off marks the box "unsupported" whenever it falls behind
    (EvaluateSupervisorVersion). So ProOS does NOT hold the Supervisor: it keeps itself current, as the platform needs.
    It is reported here, never controlled. (Correction to what was told to Dave at 07:46 — said in the register.)
  · Nothing updates the platform or the operating system by itself on this box: they change only when asked. Those two
    are what ProOS controls.

THE APPROVED VERSIONS ship INSIDE Core (approved_versions.json, beside this file): every Core update is the road a new
approval reaches a home by — the only road there is today ("currently we can only push out core updates"). A box is
never moved to a version that is not approved, never moved backwards by this, and never updated without a person
pressing Update in Pro. After an update Core checks the result itself and records it.
"""
from __future__ import annotations

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APPROVED_PATH = os.path.join(HERE, "approved_versions.json")
RECORD_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "platform_update.json")
FINISH_WITHIN_S = 45 * 60      # plumbing: an update that has not reached its version in this long is recorded as not completed

KINDS = {
    "platform": {"info": "/core/info", "update": "/core/update", "word": "the platform"},
    "os": {"info": "/os/info", "update": "/os/update", "word": "the operating system"},
}


def approved():
    """{kind: version} as shipped in this Core. {} when unreadable — then nothing is offered."""
    try:
        with open(APPROVED_PATH, encoding="utf-8") as fh:
            d = json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}
    return {k: str(d[k]) for k in KINDS if d.get(k)}


def _parts(v):
    out = []
    for p in str(v or "").replace("-", ".").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(-1)
    return out


def compare(a, b):
    """-1 / 0 / 1 for version a against b; None when either is missing."""
    if not a or not b:
        return None
    pa, pb = _parts(a), _parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _record():
    try:
        with open(RECORD_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _write(d):
    os.makedirs(os.path.dirname(RECORD_PATH), exist_ok=True)
    tmp = RECORD_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, RECORD_PATH)


def status(info_fn):
    """What the box runs, what is approved, and what can be done — every number read from the Supervisor."""
    ok = approved()
    rows = []
    for kind, k in KINDS.items():
        d = info_fn(k["info"]) or {}
        have, latest, want = d.get("version"), d.get("version_latest"), ok.get(kind)
        c = compare(have, want)
        if not have:
            state, words = "unknown", "The box didn't say which version it runs."
        elif not want:
            state, words = "not_approved", "No approved version ships with this Core — nothing is offered."
        elif c == 0:
            state, words = "approved", "On the approved version."
        elif c < 0:
            state, words = "update_available", "Version %s is approved for this box." % want
        else:
            state, words = "newer_than_approved", ("This box runs %s, newer than the approved %s. It was updated outside "
                                                   "ProOS; it is not moved back." % (have, want))
        rows.append({"kind": kind, "installed": have, "latest": latest, "approved": want, "state": state, "words": words})
    sv = info_fn("/supervisor/info") or {}
    rows.append({"kind": "supervisor", "installed": sv.get("version"), "latest": sv.get("version_latest"),
                 "approved": None, "state": "self_updating" if sv.get("auto_update") else "held",
                 "words": ("Keeps itself up to date, as the platform requires." if sv.get("auto_update") else
                           "Its own updates are switched off — the platform will refuse its updates while it is behind.")})
    return {"rows": rows, "last": _record().get("last"), "pending": _record().get("pending")}


def apply(kind, info_fn, req_fn, now=None):
    """Move one part of the box to its approved version. Returns {ok, words}. Refuses, saying why, anything else."""
    now = now or time.time()
    if kind not in KINDS:
        return {"ok": False, "words": "ProOS controls the platform and the operating system only."}
    rec = _record()
    if rec.get("pending"):
        return {"ok": False, "words": "An update is already running (%s to %s)." % (
            KINDS.get(rec["pending"].get("kind"), {}).get("word", "?"), rec["pending"].get("to"))}
    want = approved().get(kind)
    have = (info_fn(KINDS[kind]["info"]) or {}).get("version")
    c = compare(have, want)
    if not want:
        return {"ok": False, "words": "No approved version ships with this Core."}
    if c is None:
        return {"ok": False, "words": "The box didn't say which version it runs, so nothing was started."}
    if c == 0:
        return {"ok": False, "words": "Already on the approved version %s." % want}
    if c > 0:
        return {"ok": False, "words": "This box runs %s, newer than the approved %s — ProOS never moves a box back." % (have, want)}
    body = {"version": want, "backup": True, "background": True} if kind == "platform" else {"version": want}
    pending = {"kind": kind, "from": have, "to": want, "started": now}
    rec["pending"] = pending
    _write(rec)
    try:
        req_fn("POST", KINDS[kind]["update"], body)
    except Exception as e:                                       # noqa: BLE001
        if _is_timeout(e):
            # The system manager is still working on it (an OS update can outlast the request) — the minute check
            # records what actually happened.
            return {"ok": True, "words": "Started — the system manager is still working on it."}
        rec["pending"] = None
        rec["last"] = dict(pending, ended=now, ok=False, words="The system manager refused it: %s" % str(e)[:200])
        _write(rec)
        return {"ok": False, "words": rec["last"]["words"]}
    words = ("Updating the platform to %s. A backup is taken first; the platform restarts — about 5 to 15 minutes." % want
             if kind == "platform" else
             "Updating the operating system to %s. The box restarts; the previous version stays on it." % want)
    return {"ok": True, "words": words}


def _is_timeout(e):
    import socket
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    return isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout))


def check(info_fn, checks, log, now=None):
    """Once a minute: an update that reached its version is checked and recorded; one that didn't, after
    FINISH_WITHIN_S, is recorded as not completed. checks() -> [(name, ok, words)] — the box's own readings — or None
    while the platform is still starting (then the next minute looks again)."""
    now = now or time.time()
    rec = _record()
    p = rec.get("pending")
    if not p:
        return None
    have = (info_fn(KINDS.get(p.get("kind"), {}).get("info", "")) or {}).get("version")
    got = checks() if compare(have, p.get("to")) == 0 else None
    if got is not None:
        results = [{"check": n, "ok": bool(ok), "words": w} for n, ok, w in got]
        good = all(r["ok"] for r in results)
        rec["last"] = dict(p, ended=now, ok=good, checks=results,
                           words=("%s is on %s and every check passed." if good else
                                  "%s is on %s, but a check failed — see below.") % (
                                      KINDS[p["kind"]]["word"].capitalize(), p["to"]))
    elif now - float(p.get("started") or now) > FINISH_WITHIN_S:
        if compare(have, p.get("to")) == 0:
            words = "%s reached %s, but the platform did not answer within %d minutes." % (
                KINDS[p["kind"]]["word"].capitalize(), p["to"], FINISH_WITHIN_S // 60)
        else:
            words = "%s did not reach %s within %d minutes (it reports %s)." % (
                KINDS[p["kind"]]["word"].capitalize(), p["to"], FINISH_WITHIN_S // 60, have or "nothing")
        rec["last"] = dict(p, ended=now, ok=False, words=words)
    else:
        return None
    rec["pending"] = None
    _write(rec)
    log(rec["last"])
    return rec["last"]
