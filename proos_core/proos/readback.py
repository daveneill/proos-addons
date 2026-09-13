"""READ BACK AFTER ACTING — one mechanism (register 446, 13 Sep 2026, Build
E of the mirror programme, second half).

Dave's standard: "confirm, don't assume". The mirror audit counted at least
eight places where Core reported an act as done the moment the platform
accepted the service call — a power cycle, a PoE cycle, a volume change, a
transport command, an app launch, a shortcut, a channel — without reading
anything back. The platform holds the state that resulted; this module reads
it, with patience and a bound, and answers one of THREE things:

    confirmed  True   — the platform's state now shows what the act asked for
    confirmed  False  — it was read, and it does not
    confirmed  None   — it could not be read (no reader, the entity is
                        missing, the platform did not answer): UNVERIFIED —
                        never dressed up as either of the other two

The answer always carries what was actually read (`state`, the attributes
looked at, `missing`, how long it waited), so the words a person sees can
be the reading and not a feeling. Callers keep their own "ok" (the platform
accepted the call); `confirmed` is a separate fact and is never inferred
from "ok".
"""
from __future__ import annotations

import time

# The platform reports a mains plug or a light within a second or two; a
# media player or a remote may confirm only on its next poll (up to ~15 s
# for the slowest drivers Core has met). One bound per kind of question.
WAIT_FAST = 4.0
WAIT_SLOW = 15.0
STEP_STREAM = 0.25
STEP_REST = 2.0
SLOW_DOMAINS = ("media_player", "remote", "climate", "cover", "fan", "switch")


def _step(client) -> float:
    try:
        s = getattr(client, "stream", None)
        if s is not None and s.healthy():
            return STEP_STREAM
    except Exception:                                            # noqa: BLE001
        pass
    return STEP_REST


def _read(client, entity_id):
    try:
        row = (client.snapshot([entity_id]) or {}).get(entity_id)
    except Exception:                                            # noqa: BLE001
        return None
    return row


def confirm(client, entity_id, want, wait=None, attrs=()):
    """Read `entity_id` back from the platform until `want(row)` holds or the
    bound expires.

    want:  callable(row) -> True / False / None. None means "this row cannot
           answer the question" (e.g. the attribute is absent) and is reported
           as unverified, never as a failure.
    attrs: attribute names to carry in the answer, verbatim.
    """
    domain = (entity_id or "").split(".", 1)[0]
    if wait is None:
        wait = WAIT_SLOW if domain in SLOW_DOMAINS else WAIT_FAST
    step = _step(client)
    t0 = time.time()
    deadline = t0 + wait
    last = None
    verdict = None
    while True:
        row = _read(client, entity_id)
        last = row
        if row is None:
            verdict = None
        elif row.get("missing"):
            verdict = None
        else:
            try:
                verdict = want(row)
            except Exception:                                    # noqa: BLE001
                verdict = None
        # True: done. None: the question cannot be answered from this row —
        # polling will not make it answerable, so stop and say so. False:
        # read again until the bound.
        if verdict is not False or time.time() >= deadline:
            break
        time.sleep(step)
    out = {"entity_id": entity_id,
           "confirmed": (True if verdict is True else False if verdict is False else None),
           "state": (last or {}).get("state") if last else None,
           "missing": bool((last or {}).get("missing")) if last else None,
           "waited": round(time.time() - t0, 2)}
    if last and attrs:
        a = last.get("attributes") or {}
        out["attributes"] = {k: a.get(k) for k in attrs}
    if out["confirmed"] is None:
        out["note"] = ("unverified: " + ("the platform did not answer for this entity"
                                         if last is None else
                                         "the platform has no such entity" if last.get("missing") else
                                         "the platform's reading cannot answer this question"))
    return out


# ── the questions Core asks, as small readers ────────────────────────────────

def state_is(*states):
    states = tuple(s.lower() for s in states)

    def _w(row):
        st = str(row.get("state") or "").lower()
        if st in ("unavailable", "unknown", ""):
            return None
        return st in states
    return _w


def attr_is(name, value):
    def _w(row):
        a = (row.get("attributes") or {})
        if name not in a:
            return None
        return a.get(name) == value
    return _w


def attr_near(name, value, tol=0.02):
    def _w(row):
        a = (row.get("attributes") or {})
        v = a.get(name)
        if v is None:
            return None
        try:
            return abs(float(v) - float(value)) <= tol
        except (TypeError, ValueError):
            return None
    return _w


def attr_moved(name, before, direction):
    """direction +1 / -1: the numeric attribute moved that way from `before`."""
    def _w(row):
        a = (row.get("attributes") or {})
        v = a.get(name)
        if v is None or before is None:
            return None
        try:
            d = float(v) - float(before)
        except (TypeError, ValueError):
            return None
        if d == 0:
            return False
        return (d > 0) if direction > 0 else (d < 0)
    return _w


def attr_changed(name, before):
    def _w(row):
        a = (row.get("attributes") or {})
        if name not in a:
            return None
        return a.get(name) != before
    return _w


def attr_text_is(name, value):
    """Case-blind, whitespace-blind equality on a text attribute (a source name)."""
    def _w(row):
        a = (row.get("attributes") or {})
        if name not in a or a.get(name) is None:
            return None
        return " ".join(str(a.get(name)).split()).lower() == " ".join(str(value).split()).lower()
    return _w


def words(rb: dict, did: str) -> str:
    """One plain sentence for a person, from the reading."""
    c = rb.get("confirmed")
    if c is True:
        return "%s — confirmed (read back: %s)" % (did, rb.get("state"))
    if c is False:
        return "%s — the platform still reads %s; not confirmed" % (did, rb.get("state"))
    return "%s — %s" % (did, rb.get("note") or "unverified")
