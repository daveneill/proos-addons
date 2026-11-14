"""ASSIST BUILDS AND TESTS A ROOM'S ACTIVITIES — register 1338 (14 Nov 2026).

Dave, 13 Nov: "keep the activities but … have Assist build the activities based on the setup and test them as I think the
generator is just using a generic one activity fits all template"; 14 Nov 07:02 "Yes" to the plan (generator stays the
starting point; Jarvis corrects a room's activities under the same names; tests each from the room's resting state with
the readings; the generator never overwrites one Jarvis has built or tested) — "just no testing in the bedroom right now".

What was read first (register 1337): the generator's sources and sound ARE per room, but its one Display On is the same in
every room and never touches Art Mode, while the Family Room's TV Off rests the Frame IN Art Mode (which the platform
reads as "on") — so the next Watch skips the TV and the room sits on artwork.

PLATFORM FIRST. The activity stays a plain platform script under its own name, written through the platform's own
script config (so Pro, the dashboard, voice and the platform's editor all keep working with it). The platform has no idea
what a ProOS activity is meant to END in, and no "test this from rest and prove it"; that is all this module adds:
  rewrite()      replaces the steps of an EXISTING ProOS activity, keeping its name and ProOS's markers, and marks it
                 built by Assist (the old test mark goes — new steps are untested steps)
  expectations() what the activity should leave the room in, read from the activity's OWN steps (each input it selects,
                 each device it turns on or off) and its kind (a Watch ends with the screen live and its source awake)
  run()          rests the room with its own TV Off, runs the activity, reads every expectation back from the platform
                 and, only when every one is seen, stamps the test mark: when, and the steps' hash at that moment
  protected()    the generator's question — built or tested by Assist → never written over, never pruned

THE TEST MARK IS A READING. It carries the content hash of the steps it was earned on; is_tested() is True only while the
steps are still those steps. Edit the activity afterwards and it reads untested again — nothing is claimed for steps
that were never run.
"""
from __future__ import annotations

import datetime
import json
import time

from . import generator as _gen

TESTED = "proos_tested"
BUILT = "proos_built_by"
TESTABLE_KINDS = ("watch_source", "watch_tv", "tv_off")
ASLEEP = ("off", "standby", "unavailable", "unknown")
REST_WAIT_S = 45.0       # plumbing: how long TV Off may take to rest the room (its own speaker wait is 15 s)
RUN_WAIT_S = 75.0        # plumbing: how long the activity may run (Display On waits 20 s; a wake retries)
READ_WAIT_S = 20.0       # plumbing: how long a slow device may take to report what it did
STEP_S = 1.0


def _vars(cfg):
    return dict((cfg or {}).get("variables") or {})


def protected(cfg) -> bool:
    """The generator's question (register 1338): an activity Assist built or tested is never replaced or removed by it."""
    return _gen._assist_owned(cfg)


def is_tested(cfg) -> bool:
    """True only while the activity's steps are the steps the test was passed on."""
    mark = _vars(cfg).get(TESTED)
    return isinstance(mark, dict) and bool(mark.get("hash")) and mark["hash"] == _gen._content_hash(cfg)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_activity(client, oid: str) -> dict:
    oid = str(oid or "").replace("script.", "", 1).strip()
    if not oid.startswith(_gen.PROOS_PREFIX + "_"):
        raise ValueError("not a ProOS activity: %s" % oid)
    cfg = _gen._as_cfg(client.get_script(oid))
    if not cfg:
        raise ValueError("no such activity on this box: %s" % oid)
    if not _vars(cfg).get("proos_kind"):
        raise ValueError("%s is not one of ProOS's activities (it carries no ProOS marker)" % oid)
    return cfg


def rewrite(client, oid: str, sequence, why: str = "") -> dict:
    """New steps for an existing ProOS activity — same script, same name, same markers."""
    oid = str(oid or "").replace("script.", "", 1).strip()
    cfg = read_activity(client, oid)
    if not isinstance(sequence, list) or not sequence or not all(isinstance(s, dict) for s in sequence):
        raise ValueError("sequence must be the activity's list of steps, as the platform's script editor writes them")
    if any(str(s.get("action") or "") == "script." + oid for s in sequence):
        raise ValueError("an activity cannot run itself")
    new = dict(cfg)
    new["sequence"] = sequence
    v = _vars(cfg)
    v.pop(TESTED, None)                       # new steps are untested steps
    v[BUILT] = "assist"
    v["proos_built_at"] = _now()
    if why:
        v["proos_built_why"] = str(why)[:300]
    new["variables"] = v                      # proos_hash is kept as it was: to the generator this is an edited script
    client.upsert_script(oid, new)
    back = read_activity(client, oid)
    return {"ok": back.get("sequence") == sequence, "script_entity_id": "script." + oid,
            "alias": back.get("alias"), "steps": len(back.get("sequence") or []), "tested": False}


def _target(step):
    t = step.get("target") or {}
    e = t.get("entity_id") or step.get("entity_id")
    if isinstance(e, list):
        return e[0] if len(e) == 1 else None
    return e


def expectations(client, cfg, display=None) -> list:
    """What the activity should leave the room in — from its own top-level steps and its kind. Each is a reading to
    take, never a guess: {entity_id, what, state_in | attr+equals}."""
    v = _vars(cfg)
    kind = v.get("proos_kind")
    out, seen = [], set()

    def add(c):
        k = (c["entity_id"], c.get("attr"), tuple(c.get("state_in") or ()), c.get("equals"))
        if k not in seen:
            seen.add(k)
            out.append(c)

    art = _gen._art_mode_switch(client, display) if display else None
    if kind in ("watch_source", "watch_tv") and display:
        if art:
            add({"entity_id": art, "what": "the TV is out of Art Mode", "state_in": ["off"]})
        add({"entity_id": display, "what": "the TV is on", "state_in": ["on"]})
    if kind == "watch_source" and v.get("proos_source"):
        add({"entity_id": v["proos_source"], "what": "the source is awake", "not_in": list(ASLEEP)})
    for s in cfg.get("sequence") or []:
        act = str(s.get("action") or s.get("service") or "")
        e = _target(s)
        if e and act == "media_player.select_source" and (s.get("data") or {}).get("source"):
            add({"entity_id": e, "what": ("input %s" if e == display else "the sound on %s") % s["data"]["source"],
                 "attr": "source", "equals": s["data"]["source"]})      # 1343: the TV's input vs the sound's route
        elif e and act == "media_player.turn_on" and e != display:
            add({"entity_id": e, "what": "it is on", "state_in": ["on"]})
        elif e and act == "media_player.turn_off":
            add({"entity_id": e, "what": "it is off", "state_in": list(ASLEEP)})
        elif e and act in ("switch.turn_on", "switch.turn_off"):
            add({"entity_id": e, "what": "switched %s" % act.split("_")[-1], "state_in": [act.split("_")[-1]]})
    return out


def _read(client, eid):
    try:
        st = client._req("GET", "/api/states/%s" % eid) or {}
    except Exception:                         # noqa: BLE001 — a device that can't be read is read as unavailable
        st = {}
    a = st.get("attributes") or {}
    return {"state": st.get("state", "unavailable"), "source": a.get("source"), "verified": a.get("verified")}


def verdict_check(sensor, kind, source=None) -> dict:
    """REGISTER 1340: what ProOS ITSELF must show for the room once the activity has run — the room's activity sensor
    (the old proof's judge, register 1084: a Watch passes only when the room's devices CONFIRM it, verified)."""
    return {"entity_id": sensor, "what": "ProOS shows the room %s" % ("off" if kind == "tv_off" else "watching it"),
            "verdict": kind, "source": source}


def check(client, checks) -> list:
    res = []
    for c in checks:
        r = _read(client, c["entity_id"])
        if c.get("verdict"):
            st = str(r["state"] or "")
            got = st if c["verdict"] == "tv_off" else "%s%s" % (st, "" if r.get("verified") is True else " (not confirmed)")
            if c["verdict"] == "tv_off":
                ok = st == "off"
            elif c["verdict"] == "watch_tv":
                ok = st == "watch_tv" and r.get("verified") is True
            else:
                ok = (st.startswith("watch_") and st != "watch_tv" and r.get("source") == c.get("source")
                      and r.get("verified") is True)
        elif c.get("attr"):
            got = r.get(c["attr"])
            ok = got == c.get("equals")
        elif c.get("not_in"):
            got = r["state"]
            ok = got not in c["not_in"]
        else:
            got = r["state"]
            ok = got in (c.get("state_in") or [])
        res.append({"entity_id": c["entity_id"], "what": c["what"], "reading": got, "pass": ok})
    return res


def _script_state(client, oid):
    return _read(client, "script." + oid)["state"]


def _run_and_wait(client, oid, limit, sleep, clock):
    client.call_service("script", "turn_on", "script." + oid)
    t0 = clock()
    sleep(STEP_S)
    while clock() - t0 < limit:
        if _script_state(client, oid) != "on":
            return round(clock() - t0, 1)
        sleep(STEP_S)
    return None


def rest_oid(cfg, oid):
    """The room's own TV Off — the same room's script, by the generator's one naming."""
    i = oid.find("_watch_")
    return (oid[:i] + "_tv_off") if i > 0 else None


def run(client, oid: str, display: str, sleep=None, clock=None, rest=True, verdict=None, mark=True) -> dict:
    """Rest the room, run the activity, read every expectation back. Marks it tested only when all are seen.
    REGISTER 1340: rest=False runs it straight from whatever the room is on (Prove the Room's source-to-source switch);
    verdict = the room's activity sensor, read as one more expectation; mark=False never ADDS a test mark (a switch
    only takes one off when it fails)."""
    sleep, clock = sleep or time.sleep, clock or time.time
    oid = str(oid or "").replace("script.", "", 1).strip()
    cfg = read_activity(client, oid)
    v = _vars(cfg)
    kind = v.get("proos_kind")
    if kind not in TESTABLE_KINDS:
        raise ValueError("%s is a building block (%s) — test the Watch activities that use it" % (oid, kind))
    out = {"script_entity_id": "script." + oid, "alias": cfg.get("alias"), "kind": kind}
    if kind != "tv_off" and rest:
        roid = rest_oid(cfg, oid)
        rcfg = _gen._as_cfg(client.get_script(roid)) if roid else None
        if not rcfg or _vars(rcfg).get("proos_kind") != "tv_off" or \
                _vars(rcfg).get("proos_area_id") != v.get("proos_area_id"):
            raise ValueError("this room has no TV Off to rest it with — can't test from rest")
        took = _run_and_wait(client, roid, REST_WAIT_S, sleep, clock)
        art = _gen._art_mode_switch(client, display) if display else None
        rest = [{"entity_id": display, "what": "the TV is resting", "state_in": list(ASLEEP)}]
        rested, t0 = [], clock()
        while True:
            rested = check(client, rest)
            if art and not rested[0]["pass"]:
                a = check(client, [{"entity_id": art, "what": "the TV is resting in Art Mode", "state_in": ["on"]}])
                rested = a if a[0]["pass"] else rested + a
            if all(r["pass"] for r in rested) or clock() - t0 >= READ_WAIT_S:
                break
            sleep(STEP_S)
        out["rest"] = {"by": "script." + roid, "seconds": took, "readings": rested}
        if took is None or not all(r["pass"] for r in rested):
            out.update(passed=False, stage="rest",
                       say="The room didn't reach its resting state, so the activity was not run.")
            return out
    exp = expectations(client, cfg, display)
    if verdict:
        exp.append(verdict_check(verdict, kind, v.get("proos_source")))
    ovr = overrides(cfg)
    took = _run_and_wait(client, oid, RUN_WAIT_S, sleep, clock)
    res, t0 = _vouched(check(client, exp), ovr), clock()
    while not all(r["pass"] for r in res) and clock() - t0 < READ_WAIT_S:
        sleep(STEP_S)
        res = _vouched(check(client, exp), ovr)
    passed = took is not None and bool(res) and all(r["pass"] for r in res)
    out.update(seconds=took, readings=res, passed=passed, stage="run")
    if took is None:
        out["say"] = "The activity was still running after %d seconds." % RUN_WAIT_S
    new = dict(cfg)
    nv = _vars(cfg)
    if passed and not mark:
        return out
    if passed:
        nv[TESTED] = {"at": _now(), "hash": _gen._content_hash(cfg), "by": "assist", "seconds": took,
                      "readings": ["%s: %s" % (r["what"], r["reading"]) for r in res]}
        if ovr:
            nv[TESTED]["overrides"] = ovr     # 1343: what the installer vouched for stays vouched for these steps
    elif TESTED in nv:
        nv.pop(TESTED)                        # a mark the activity no longer earns is taken off
    else:
        return out
    new["variables"] = nv
    client.upsert_script(oid, new)
    out["marked_tested"] = passed
    return out


def summary(client, area_slug: str) -> list:
    """Every ProOS activity of a room with who built it and whether its present steps are tested."""
    pre = "script.%s_%s_" % (_gen.PROOS_PREFIX, area_slug)
    tmpl = ("{{ states.script | map(attribute='entity_id') | select('match', %s) | list | to_json }}"
            % json.dumps("^" + pre.replace(".", "\\.")))
    rows = []
    for eid in json.loads(client.render_template(tmpl) or "[]"):
        cfg = _gen._as_cfg(client.get_script(eid.split(".", 1)[1])) or {}
        v = _vars(cfg)
        mark = v.get(TESTED) if isinstance(v.get(TESTED), dict) else {}
        rows.append({"script_entity_id": eid, "alias": cfg.get("alias"), "kind": v.get("proos_kind"),
                     "built_by": v.get(BUILT) or "generator", "tested": is_tested(cfg),
                     "tested_at": mark.get("at") if is_tested(cfg) else None,
                     "changed_since_test": bool(mark) and not is_tested(cfg)})
    return rows


def prove(client, plan, display, verdict=None, launch=None, progress=None, sleep=None, clock=None) -> dict:
    """PROVE THE ROOM IS ITS COMMISSIONING (register 1340, Dave 14 Nov 08:52: "I think prove the room is when it should
    have this commisioning process"). plan: [{script, alias, kind, sc?}] — the room's Watch activities, its TV Off and
    its app shortcuts. In order:
      1 · each Watch from REST (the room's TV Off first), every reading back, ProOS's own verdict too → marked tested
      2 · each Watch SWITCHED TO straight from another (W_last → W1 → W2 … → W_last) — a failure takes its mark off
      3 · each app shortcut, by 1084's rule (passes when the device shows it; taken but unseen = SENT)
      4 · TV Off, read back → marked tested
    A failed step never stops the run, and the room always ends with TV Off."""
    sleep, clock = sleep or time.sleep, clock or time.time
    watches = [p for p in plan if p.get("kind") in ("watch_source", "watch_tv")]
    offs = [p for p in plan if p.get("kind") == "tv_off"]
    scs = [p for p in plan if p.get("kind") == "shortcut"]
    steps = []

    def note(step):
        if progress:
            try:
                progress(step)
            except Exception:                 # noqa: BLE001
                pass

    def one(p, phase, label, **kw):
        note({"alias": label})
        t0 = clock()
        try:
            r = run(client, p["script"], display, sleep=sleep, clock=clock, verdict=verdict, **kw)
        except Exception as e:                # noqa: BLE001
            r = {"passed": False, "error": str(e)}
        bad = [x for x in (r.get("readings") or []) if not x["pass"]]
        rest_bad = [x for x in ((r.get("rest") or {}).get("readings") or []) if not x["pass"]]
        steps.append({"script": p["script"], "alias": label, "kind": p.get("kind"), "phase": phase,
                      "ok": bool(r.get("passed")), "seconds": r.get("seconds"),
                      "readings": r.get("readings") or [], "failed": bad, "rest_failed": rest_bad,
                      "tested": bool(r.get("passed")) and kw.get("mark", True),
                      "final": {"error": r.get("error") or r.get("say")}})

    for p in watches:
        one(p, "from_rest", "%s · from rest" % p.get("alias"))
    if len(watches) > 1:
        prev = watches[-1]
        for p in watches:                     # each Watch arrives from the one before it (the first from the last)
            if p is prev:
                continue
            one(p, "switch", "%s · switched from %s" % (p.get("alias"), prev.get("alias")), rest=False, mark=False)
            prev = p
    # REGISTER 1342 (Dave 14 Nov 09:56: "a media player will not report what app its on so this can only send the
    # command after confirming the unit is actually on … if its sent the command and it didnt change then all that can
    # be done is send command again"). Rulings 871/873 restated: the device confirmed on and the command taken IS the
    # pass — a device that cannot report its app is never marked down for it. Only a device that DOES report, and
    # shows something else, has the command sent once more; still not, it failed. A device that can't be read at all
    # was not confirmed on — that is not a pass.
    def _go(p):
        try:
            return (launch(p) if launch else {"error": "no launcher"}) or {}
        except Exception as e:                # noqa: BLE001
            return {"error": str(e)}

    for p in scs:
        note(p)
        t0 = clock()
        res, again = _go(p), False
        if not res.get("ok") and res.get("taken") and res.get("opened") is False:
            res, again = _go(p), True         # it reports, and showed something else — send it once more
        unc = res.get("unconfirmed") or []
        ok = bool(res.get("ok")) and not unc
        cant_tell = ok and res.get("opened") is not True
        steps.append({"script": p.get("script"), "alias": p.get("alias"), "kind": "shortcut", "phase": "shortcut",
                      "ok": ok, "sent": cant_tell, "sent_again": again,
                      "seconds": round(clock() - t0, 1),
                      "final": {"shows": res.get("shows") if res.get("opened") is True else None,
                                "error": res.get("error") or (("couldn't confirm %s is on" % " and ".join(unc))
                                                              if unc else None)}})
    for p in offs:
        one(p, "off", p.get("alias") or "TV Off")
    return {"steps": steps, "passed": sum(1 for s in steps if s["ok"]),
            "sent": 0, "total": len(steps),                          # 1342: a shortcut sent to a confirmed-on device passed
            "tested": sum(1 for s in steps if s.get("tested"))}


# ── REGISTER 1343 (Dave 14 Nov 11:46: "watch shield seems to keep failing but looks like it's working this is where
# there is no way to say it is working and to get a pass there always needs to be a way to get a pass or request change
# during this process"). THE INSTALLER'S EYES ARE A READING TOO. "It's Working" passes the activity as the installer
# saw it, and records WHICH readings disagreed — named, never hidden — so the next proof does not fail it again on the
# same reading while the steps are the same steps. A reading the installer did not vouch for still fails. Edit the
# steps and the vouching goes with the old test mark.

def overrides(cfg) -> list:
    mark = _vars(cfg).get(TESTED)
    if is_tested(cfg) and isinstance(mark, dict):
        return [o for o in (mark.get("overrides") or []) if isinstance(o, dict) and o.get("entity_id")]
    return []


def _vouched(res, ovr):
    keys = {(o["entity_id"], o.get("what")) for o in ovr}
    for r in res:
        if not r["pass"] and (r["entity_id"], r["what"]) in keys:
            r["pass"], r["vouched"] = True, True
    return res


def witness(client, oid: str, disagreed, who: str = "installer") -> dict:
    """The installer saw it working: mark it tested as theirs, keeping the readings that disagreed as its overrides."""
    oid = str(oid or "").replace("script.", "", 1).strip()
    cfg = read_activity(client, oid)
    ovr = [{"entity_id": d.get("entity_id"), "what": d.get("what"), "reading": d.get("reading")}
           for d in (disagreed or []) if isinstance(d, dict) and d.get("entity_id") and d.get("what")]
    keep = overrides(cfg)
    for o in keep:
        if (o["entity_id"], o.get("what")) not in {(x["entity_id"], x["what"]) for x in ovr}:
            ovr.append(o)
    new = dict(cfg)
    nv = _vars(cfg)
    nv[TESTED] = {"at": _now(), "hash": _gen._content_hash(cfg), "by": who, "witnessed": True, "overrides": ovr,
                  "readings": ["%s: %s (the installer saw it working)" % (o["what"], o.get("reading")) for o in ovr]}
    new["variables"] = nv
    client.upsert_script(oid, new)
    return {"ok": True, "script_entity_id": "script." + oid, "overrides": ovr}
