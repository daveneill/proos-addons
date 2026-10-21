"""ProOS Core — changing the box's own network, with a way back (register 969z, 21 Oct 2026).

Dave, 16:05: "There is no way to ever modify these settings if needed to change to static or change DNS addresses like
most boxes and the Gflip has 2 x LAN connections". The change itself is the platform's (the Supervisor's
/network/interface/<name>/update — the one its own screens use; proos/boxnet.update_body builds and checks it).

What only ProOS adds is the way back. A wrong static address can leave nobody able to reach the box. So every change
is held for KEEP_S: the installer opens Pro at the box's address (the new one if it moved) and presses Keep These
Settings; if nobody does, Core — which is on the box and needs no address to reach itself — puts the port back exactly
as the Supervisor stated it before the change, and says so in the journal. The held change is written to Core's store
so a restart in between still puts it back.
"""
from __future__ import annotations

import json
import os
import threading
import time

from proos import boxnet

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
PENDING_PATH = os.path.join(DATA_DIR, "net_pending.json")
KEEP_S = 120                       # plumbing: how long a change waits for Keep before it is put back (Dave's 2 minutes)

_lock = threading.Lock()
_timer = {"t": None}


def _load():
    try:
        with open(PENDING_PATH, encoding="utf-8") as fh:
            return json.load(fh) or None
    except Exception:  # noqa: BLE001
        return None


def _save(p):
    if p is None:
        try:
            os.remove(PENDING_PATH)
        except FileNotFoundError:
            pass
        return
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(p, fh)
    os.replace(tmp, PENDING_PATH)


def pending(now=None):
    """The change waiting for Keep, with the seconds left — or None."""
    p = _load()
    if not p:
        return None
    left = int(p["until"] - (now or time.time()))
    return {"interface": p["interface"], "label": p.get("label"), "seconds_left": max(0, left),
            "new_address": p.get("new_address")}


def apply(sup, name: str, form: dict, journal=None, now=None) -> dict:
    """Check, remember the port as it is, change it, and start the way back. Raises ValueError with a plain sentence."""
    if _load():
        raise ValueError("Another network change is waiting for Keep These Settings — keep it or put it back first.")
    ni = sup("GET", "/network/info") or {}
    raw = boxnet.raw_interface(ni, name)
    if raw is None:
        raise ValueError("This box has no port called %s." % name)
    body = boxnet.update_body(form)
    labels = boxnet._labels((boxnet._strip(ni).get("interfaces") or []))
    before = boxnet.restore_body(raw)
    p = {"interface": name, "label": labels.get(name) or name, "before": before, "after": body,
         "until": (now or time.time()) + KEEP_S,
         "new_address": (body.get("ipv4") or {}).get("address", [None])[0] if body.get("ipv4") else None}
    with _lock:
        _save(p)
    try:
        sup("POST", "/network/interface/%s/update" % name, body, 60)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _save(None)
        raise ValueError("The box refused the change: %s" % str(e)[:160])
    if journal:
        journal("network_change", {"interface": name, "label": p["label"], "after": body, "keep_within": KEEP_S})
    arm(sup, journal)
    return {"applied": True, "interface": name, "label": p["label"], "keep_within": KEEP_S,
            "new_address": p["new_address"]}


def keep(journal=None) -> dict:
    with _lock:
        p = _load()
        _save(None)
        t = _timer.get("t")
        if t:
            t.cancel()
            _timer["t"] = None
    if not p:
        return {"kept": False, "why": "nothing is waiting"}
    if journal:
        journal("network_kept", {"interface": p["interface"], "label": p.get("label")})
    return {"kept": True, "interface": p["interface"], "label": p.get("label")}


def put_back(sup, journal=None, why="asked") -> dict:
    with _lock:
        p = _load()
        t = _timer.get("t")
        if t:
            t.cancel()
            _timer["t"] = None
    if not p:
        return {"put_back": False, "why": "nothing is waiting"}
    try:
        sup("POST", "/network/interface/%s/update" % p["interface"], p["before"], 60)
    except Exception as e:  # noqa: BLE001
        if journal:
            journal("network_putback_failed", {"interface": p["interface"], "why": str(e)[:160]})
        arm(sup, journal, retry_in=15)           # try again — a box left unreachable is the one thing this exists for
        return {"put_back": False, "why": str(e)[:160]}
    with _lock:
        _save(None)
    if journal:
        journal("network_put_back", {"interface": p["interface"], "label": p.get("label"), "why": why,
                                     "restored": p["before"]})
    return {"put_back": True, "interface": p["interface"], "label": p.get("label")}


def arm(sup, journal=None, retry_in=None):
    """Start (or restart, e.g. after Core restarts) the clock on a waiting change."""
    p = _load()
    if not p:
        return
    left = retry_in if retry_in is not None else max(0.0, p["until"] - time.time())
    with _lock:
        t = _timer.get("t")
        if t:
            t.cancel()
        _timer["t"] = threading.Timer(left, lambda: put_back(sup, journal, why="not kept within %d s" % KEEP_S))
        _timer["t"].daemon = True
        _timer["t"].start()
