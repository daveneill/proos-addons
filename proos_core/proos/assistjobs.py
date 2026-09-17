"""A TURN IS A JOB — the work outlives the browser that asked for it.

15 Sep 2026, register 466. Dave, watching Assist build a nightly schedule for
the Family Room Apple TV: three replies in a row saying "Timed out — complex
requests can take a while and may still have completed."

TWO CLOCKS THAT WERE NEVER SET TO EACH OTHER. The glass gave a turn NINETY
SECONDS and then aborted the request. Core is built for up to 24 rounds with
the model, each with its own 75-second allowance, plus the time the actual
devices take in between — and that turn was turning the Family Room on,
waiting for the TV to confirm power, reading the Apple TV's app list, writing
the schedule and putting the room back off. Register 228 raised the round
limit to 24 on purpose, after measuring that one real room is about nine
steps; nobody moved the ninety seconds with it. The engine was allowed a
nine-step job on a stopwatch set for one round.

"MAY STILL HAVE COMPLETED" WAS NOT A HEDGE. An abort stops the browser
listening. Core never hears it: nothing is cancelled, the job runs to the end.
The add-on log has that turn finishing — the app launch and the automation
write both landed. So every "keep going" started a FRESH turn beside one still
working, and the box ended the evening with TWO 6pm automations, from a job
Dave had been told had failed. The last reply, "nothing's running from my
side", was honest and wrong in effect: a turn was not a thing Assist could
look at, so it could not see its own work in flight.

THE RULING (Dave, 15 Sep): a turn must not be a request that can be hung up
on. It is a JOB — it is started, it is watched, and it is one.

WHAT THIS MODULE IS. A registry of turns, in memory, one thread each:

  start()        begins a turn, or — if that person already has one running in
                 that session — RETURNS THE ONE ALREADY RUNNING rather than a
                 second. This is the rule that ends duplicate work. "Keep
                 going" joins; it never races.
  view()         what the glass polls: state, how long, the live step list as
                 the runner fills it, and the whole result once there is one.
  latest()       the running (or last finished) job for a session, so a
                 reloaded page finds its own turn again instead of losing it.

WHAT IT IS NOT. It does not cancel, because Core cannot un-turn-on a television
— a job that has begun acting is finished honestly and reported, never
abandoned half-done. It is not a queue: one turn at a time per person per
session is the whole concurrency model, and it is deliberate. It writes
nothing to disk; a restart loses the record of a turn, which is correct — a
restart lost the turn too.
"""
from __future__ import annotations

import threading
import time
import uuid

# One turn at a time, per person per session. Everything below is guarded by
# this; the work itself runs outside it.
#
# RE-ENTRANT ON PURPOSE. start() decides under the lock whether this question
# joins a turn already running, and the answer it hands back is view() — which
# takes the lock itself. A plain Lock deadlocked there on the very first join,
# which is the one path this whole module exists for.
_LOCK = threading.RLock()
_JOBS: dict = {}          # job_id -> record
_BY_KEY: dict = {}        # (user_id, session) -> job_id, the latest

# A finished job is kept long enough for a phone that went to sleep mid-turn
# to come back and collect its answer, and no longer.
_KEEP_S = 30 * 60
_KEEP_N = 40


def _key(user_id, session):
    return ((user_id or "anon"), (session or "default"))


def _prune(now=None):
    """Called with _LOCK held."""
    now = now or time.time()
    dead = [j for j, r in _JOBS.items()
            if r["state"] != "running" and (now - (r.get("finished") or now)) > _KEEP_S]
    if len(_JOBS) - len(dead) > _KEEP_N:
        done = sorted((r for r in _JOBS.values() if r["state"] != "running"),
                      key=lambda r: r.get("finished") or 0)
        dead += [r["id"] for r in done[:len(_JOBS) - len(dead) - _KEEP_N]]
    for j in set(dead):
        r = _JOBS.pop(j, None)
        if r and _BY_KEY.get(_key(r.get("user"), r.get("session"))) == j:
            _BY_KEY.pop(_key(r.get("user"), r.get("session")), None)


def _steps(rec):
    """The live step list. It is the runner's own trace — the SAME record the
    finished turn reports and the Master Log renders, read while it is still
    being written, never a second parallel account of what happened."""
    runner = rec.get("runner")
    tr = list(getattr(runner, "trace", None) or []) if runner is not None else []
    return tr or list(rec.get("trace") or [])


def view(job_id: str, user_id=None) -> dict | None:
    """What the glass polls. `user_id`, when given, is a door: a turn belongs
    to the person who asked it, and nobody else may read it — not because a
    turn is a secret, but because a job id is not an identity."""
    with _LOCK:
        rec = _JOBS.get(job_id)
        if not rec:
            return None
        if user_id is not None and rec.get("user") != user_id:
            return None
        out = {"job": rec["id"], "state": rec["state"], "session": rec.get("session"),
               "text": rec.get("text"), "started": rec.get("started"),
               "finished": rec.get("finished"),
               "elapsed": round((rec.get("finished") or time.time()) - rec["started"], 1),
               "steps": _steps(rec)}
        if rec["state"] == "done":
            out["result"] = rec.get("result") or {}
        elif rec["state"] == "error":
            out["error"] = rec.get("error") or "the turn stopped unexpectedly"
        return out


def latest(user_id, session) -> dict | None:
    """The turn this session is on — running, or the last one finished. A page
    that was reloaded mid-turn asks this and finds its own work again."""
    with _LOCK:
        jid = _BY_KEY.get(_key(user_id, session))
    return view(jid, user_id) if jid else None


def running_for(user_id, session):
    """The job id of a turn still working for this person in this session, or
    None. The one question that stops a second turn being started."""
    with _LOCK:
        jid = _BY_KEY.get(_key(user_id, session))
        rec = _JOBS.get(jid) if jid else None
        return jid if (rec and rec["state"] == "running") else None


def start(user_id, session, text, work) -> dict:
    """Begin a turn — or hand back the one already running.

    `work` is called with one argument, a callback that hands this module the
    live ToolRunner, and returns the finished result dict. Everything about
    WHAT a turn does stays in assist.py; this module only owns when it starts,
    whether it is alone, and how it is watched.
    """
    with _LOCK:
        _prune()
        jid = _BY_KEY.get(_key(user_id, session))
        rec = _JOBS.get(jid) if jid else None
        if rec and rec["state"] == "running":
            # THE RULE THAT ENDS THE DUPLICATE WORK. Somebody impatient with a
            # turn that is taking its time gets THAT turn, told plainly that it
            # is already going — not a second one racing it to the same room.
            out = view(jid)
            out["joined"] = True
            return out
        jid = uuid.uuid4().hex[:16]
        rec = {"id": jid, "user": user_id, "session": session, "text": text,
               "state": "running", "started": time.time(), "finished": None,
               "runner": None, "trace": [], "result": None, "error": None}
        _JOBS[jid] = rec
        _BY_KEY[_key(user_id, session)] = jid

    def _hand(runner):
        with _LOCK:
            rec["runner"] = runner

    def _run():
        try:
            res = work(_hand)
            with _LOCK:
                rec["result"] = res if isinstance(res, dict) else {"reply": str(res)}
                rec["state"] = "done"
        except Exception as e:                                   # noqa: BLE001
            # A turn that fell over says so and stays readable. The detail goes
            # to the log; the person gets plain words, the same rule chat()
            # already keeps for a provider error.
            print("  [assist] job %s failed: %s" % (jid, e), flush=True)
            with _LOCK:
                rec["error"] = ("Something went wrong part-way through that — "
                                "check the home before asking again.")
                rec["state"] = "error"
        finally:
            with _LOCK:
                # The trace is kept off the runner once the turn is over, so a
                # finished job holds its own record and nothing keeps a whole
                # ToolRunner alive for half an hour.
                rec["trace"] = list(getattr(rec.get("runner"), "trace", None) or [])
                rec["runner"] = None
                rec["finished"] = time.time()

    t = threading.Thread(target=_run, name="assist-job-" + jid, daemon=True)
    t.start()
    out = view(jid)
    out["joined"] = False
    return out
