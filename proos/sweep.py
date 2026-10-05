"""The Full Assist Sweep — Dave's ruling R1, 2 Oct 2026 (register 663).

"A full Assist sweep of the home that can be done daily on a schedule and manually
triggered … it needs to be confirming NOT assuming items are okay." The contents are
documented, line by line, in docs/ProOS_Full_Assist_Sweep_2026-10-02.md — this module is
that document, and nothing else.

Every check ends in exactly one of three answers, each carrying the evidence and the time
it was true:
    confirmed  — ProOS read the thing itself and it is right
    problem    — ProOS read it and it is wrong
    unknown    — there is nothing to ask, or what it asks did not answer (said, with why;
                 never counted as okay)

The sweep READS ONLY. It never wakes, cycles or reloads anything.

Readers are injected (the server wires the real ones; benches and the rig wire fakes), so
every check is driven, not described. A reader that raises makes ITS check "unknown" with
the reason — it never makes the whole sweep fail, and never reads as okay.
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

DATA = os.environ.get("PROOS_DATA_DIR", "/data")
LAST_PATH = os.path.join(DATA, "sweep_last.json")
CFG_PATH = os.path.join(DATA, "sweep_config.json")
ACTED_PATH = os.path.join(DATA, "sweep_acted.json")
# Integrations whose pairing step Pro can run through the platform's own flow and report on
# (Pro's _samsungDoPair: the IP Control reconfigure flow). A card gets a Pair button ONLY for
# these — "as long as it's not a button that does nothing" (Dave, 2 Oct). PLUMBING: a fact
# about Pro's code, not about the house.
PAIRABLE = ("samsungtv_smart",)
# the integration's own instruction, as it writes it ("re-pair required — re-pair via the
# integration options"); read verbatim, never inferred (plan D1)
PAIR_WORDS = "re-pair"
DEFAULT_TIME = "07:00"     # PLUMBING: when ProOS looks; it claims nothing about the house
PERIOD_S = 86400           # the sweep's cadence — daily, Dave's ruling R1 — and so its time limit
_lock = threading.Lock()
_running = {"on": False}

CONFIRMED, PROBLEM, UNKNOWN = "confirmed", "problem", "unknown"
_IN_USE_BAD = ("setup_retry", "setup_error", "not_loaded", "migration_error", "failed_unload",
               "setup_in_progress")


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return None


def _when(t, now):
    if not t:
        return "an unknown time"
    lt = time.localtime(t)
    day = "today" if time.localtime(now)[:3] == lt[:3] else (
        "yesterday" if time.localtime(now - 86400)[:3] == lt[:3] else time.strftime("%a %-d %b", lt))
    return "%s %s" % (time.strftime("%-I:%M %p", lt).lower(), day)


def _item(check, subject, answer, words, at=None, card=False, ref=None, actions=None):
    return {"check": check, "subject": subject, "answer": answer, "words": words,
            "evidence_at": at, "card": bool(card and answer == PROBLEM), "ref": ref,
            "actions": list(actions or [])}


# ── the checks — one function each, in the document's order ──────────────────
def check_proos(r, now):
    out = []
    try:
        v = r.vitals()
        for name, x in v.items():
            ok = x.get("ok") and not x.get("overdue")
            out.append(_item("1a", x.get("label") or name, CONFIRMED if ok else PROBLEM,
                             ("%s completed a pass %ds ago" % (x.get("label"), int(x.get("age_s") or 0))) if ok
                             else ("%s is not running — already on Health" % x.get("label")),
                             _iso(x.get("last_ok")), ref="core_blind"))
    except Exception as e:                                       # noqa: BLE001
        out.append(_item("1a", "ProOS's own jobs", UNKNOWN, "could not read Core's vital signs: %s" % e))
    try:
        u = r.self_update()
        if u is None:
            out.append(_item("1b", "ProOS version", UNKNOWN, "the box's update entity for ProOS was not found"))
        else:
            inst, latest = u.get("installed_version"), u.get("latest_version")
            ok = inst and latest and inst == latest
            out.append(_item("1b", "ProOS version", CONFIRMED if ok else PROBLEM,
                             ("on the latest ProOS (%s)" % inst) if ok
                             else ("ProOS %s is available, the box runs %s — already on Health" % (latest, inst)),
                             u.get("last_changed"), ref="update_pending"))
    except Exception as e:                                       # noqa: BLE001
        out.append(_item("1b", "ProOS version", UNKNOWN, "could not read the update entity: %s" % e))
    # 1c · ALERTS TO PHONES — CONFIRMED PER PHONE, NOT COUNTED (plan B1, 3 Oct 2026; L-4).
    # On Dave's box this line read "3 phones can receive alerts — confirmed" while every
    # alert died in Core for want of an Apple push key. A phone is confirmed only by the
    # last real outcome of an alert to it; a phone ProOS cannot see the outcome for is
    # said to be unconfirmed, with why; a failed last alert is a problem with the reason.
    try:
        phones = r.phones()
        if not phones:
            out.append(_item("1c", "Alerts to phones", PROBLEM,
                             "no phone can receive alerts — no notify service for a phone exists on the box",
                             _iso(now), card=True))
        else:
            status = {}
            names = {}
            try:
                status = r.push_status() or {}
                names = r.phone_names() or {}
            except Exception:                                    # noqa: BLE001
                pass
            for svc in phones:
                name = names.get(svc) or svc.replace("mobile_app_", "").replace("_", " ")
                st = status.get(name)
                subj = "Alerts to %s" % name
                if st is None:
                    out.append(_item("1c", subj, UNKNOWN,
                                     "ProOS has no record of an alert reaching %s — either none has been sent, "
                                     "or it uses a push service ProOS can't see. Send a test from "
                                     "Pro › Toolbox › Notifications." % name))
                elif st.get("ok"):
                    out.append(_item("1c", subj, CONFIRMED,
                                     "the last alert to %s was %s" % (name, st.get("words") or "delivered"),
                                     _iso(st.get("at"))))
                else:
                    out.append(_item("1c", subj, PROBLEM,
                                     "the last alert to %s did not get through: %s" % (name, st.get("words")),
                                     _iso(st.get("at")), card=True))
    except Exception as e:                                       # noqa: BLE001
        out.append(_item("1c", "Alerts to phones", UNKNOWN, "could not read the box's services: %s" % e))
    return out


def _committed_members(r):
    """[(room name, area id, entity, role)] for every committed room's AV members."""
    out = []
    for key, rec in ((r.project() or {}).get("areas") or {}).items():
        if not (rec and rec.get("committed")):
            continue
        name, aid = rec.get("name") or key, rec.get("area_id") or key
        if rec.get("display"):
            out.append((name, aid, rec["display"], "display"))
        for e in rec.get("sources") or []:
            out.append((name, aid, e, "source"))
        for e in rec.get("audio") or []:
            out.append((name, aid, e, "audio"))
    return out


def check_integrations(r, now, members, ent_dev, dev_entries):
    try:
        entries = {e.get("entry_id"): e for e in (r.config_entries() or [])}
    except Exception as e:                                       # noqa: BLE001
        return [_item("2", "Integrations", UNKNOWN, "could not read the box's integrations: %s" % e)]
    used = {}
    for room, _aid, eid, _role in members:
        for ce in dev_entries.get(ent_dev.get(eid), []):
            used.setdefault(ce, set()).add(room)
    out = []
    for ce, rooms in sorted(used.items()):
        e = entries.get(ce)
        if not e or e.get("source") == "ignore":
            continue
        title = e.get("title") or e.get("domain") or ce
        st = e.get("state")
        rl = ", ".join(sorted(rooms))
        if st == "loaded":
            out.append(_item("2", title, CONFIRMED, "%s is loaded (used by %s)" % (title, rl), _iso(now)))
        else:
            why = e.get("reason")
            out.append(_item("2", title, PROBLEM,
                             "%s is not running on the box — the platform says '%s'%s. %s cannot be controlled "
                             "or watched through it until it is." % (title, st, (" (%s)" % why) if why else "", rl),
                             _iso(now), card=True))
    if not out:
        out.append(_item("2", "Integrations", UNKNOWN, "no integration could be matched to a committed device"))
    return out


def check_devices(r, now, members, ent_dev, since):
    try:
        items = (r.watchers() or {}).get("items") or []
    except Exception as e:                                       # noqa: BLE001
        return [_item("3", "Devices", UNKNOWN, "could not read the device watch: %s" % e)]
    by_ent = {i.get("entity"): i for i in items if i.get("entity")}
    by_dev = {}
    for i in items:
        d = ent_dev.get(i.get("entity"))
        if d:
            by_dev.setdefault(d, i)
    try:
        states = r.states() or {}
    except Exception:                                            # noqa: BLE001
        states = {}
    out, seen_dev = [], set()
    for room, _aid, eid, role in members:
        dev = ent_dev.get(eid)
        if dev and dev in seen_dev:
            continue
        seen_dev.add(dev or eid)
        w = by_ent.get(eid) or by_dev.get(dev)
        name = (w or {}).get("name") or ((states.get(eid) or {}).get("attributes") or {}).get("friendly_name") or eid
        subj = "%s (%s)" % (name, room)
        if w and w.get("status") == "fault":
            out.append(_item("3", subj, PROBLEM, "%s — already on Health: %s" % (name, w.get("guidance") or w.get("verdict")),
                             w.get("since"), ref="watcher"))
            continue
        ws = (w or {}).get("witness_sensor")
        if not ws:
            out.append(_item("3", subj, UNKNOWN, "%s has no network witness bound, so ProOS cannot confirm it is "
                                                 "there — bind one in Pro › Room" % name))
            continue
        row = states.get(ws) or {}
        wst, wat = row.get("state"), _ts(row.get("last_changed"))
        if wst in (None, "unavailable", "unknown"):
            out.append(_item("3", subj, UNKNOWN, "%s's network witness (%s) itself is not answering" % (name, ws)))
        elif wst in ("home", "on", "connected"):
            out.append(_item("3", subj, CONFIRMED, "%s is on the network (%s, since %s)" % (name, ws, _when(wat, now)),
                             row.get("last_changed")))
        elif wat and wat >= since:
            out.append(_item("3", subj, CONFIRMED, "%s was on the network within the last day (left at %s — %s)"
                             % (name, _when(wat, now), ws), row.get("last_changed")))
        else:
            out.append(_item("3", subj, PROBLEM, "%s — the network has not seen it since %s (%s): more than a "
                                                 "day, the sweep's own period." % (name, _when(wat, now), ws),
                             row.get("last_changed"), card=True))
    return out


def _acted():
    try:
        with open(ACTED_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                            # noqa: BLE001
        return {}


def mark_acted(entry_id, now=None):
    """A repair ran for this integration (Pro's Pair succeeded): its log words from before
    now are history, not the present."""
    a = _acted()
    a[str(entry_id)] = now or time.time()
    os.makedirs(DATA, exist_ok=True)
    with open(ACTED_PATH, "w", encoding="utf-8") as fh:
        json.dump(a, fh)


def check_log_words(r, now, members, ent_dev, dev_entries, reg_entries):
    """Check 4 — the integration's OWN words (R1, part 2). The platform's system log, read
    for the integrations the rooms use, quoted verbatim; nothing is interpreted."""
    try:
        entries = {e.get("entry_id"): e for e in (r.config_entries() or [])}
        logs = r.system_log() or []
    except Exception as e:                                       # noqa: BLE001
        return [_item("4", "The integrations' own words", UNKNOWN, "could not read the box's log: %s" % e)]
    hosts = {e.get("entry_id"): str(((e.get("data") or {}).get("host")) or "") for e in (reg_entries or [])}
    used = {}
    for room, _aid, eid, _role in members:
        for ce in dev_entries.get(ent_dev.get(eid), []):
            e = entries.get(ce)
            if e and e.get("source") != "ignore":
                used.setdefault(ce, set()).add(room)
    acted = _acted()
    by_domain = {}
    for ce in used:
        by_domain.setdefault(entries[ce].get("domain"), []).append(ce)
    out = []
    heard = {ce: [] for ce in used}
    for row in logs:
        name = str(row.get("name") or "")
        ts = row.get("timestamp") or 0
        if ts < now - PERIOD_S:
            continue
        for dom, ces in by_domain.items():
            if not (name == "custom_components." + dom or name.startswith("custom_components.%s." % dom)
                    or name == "homeassistant.components." + dom
                    or name.startswith("homeassistant.components.%s." % dom)):
                continue
            msgs = [str(m) for m in (row.get("message") or [])]
            target = [ce for ce in ces if hosts.get(ce) and any(hosts[ce] in m for m in msgs)]
            if not target and len(ces) == 1:
                target = ces
            for ce in (target or ces):
                if ts > float(acted.get(ce, 0)):
                    heard[ce].append(row)
    # LIVE CHECK FIRST (Dave's ruling, 5 Oct 2026). A line in the log is history; it
    # becomes an alarm only if a live check of that integration's devices, done NOW,
    # also fails. Read once: the platform's own entry state and each device's state.
    try:
        _states = r.states() or {}
        _states_ok = True
    except Exception:                                            # noqa: BLE001
        _states, _states_ok = {}, False
    _ents_of = {}
    for _room, _aid, _eid, _role in members:
        for _ce in dev_entries.get(ent_dev.get(_eid), []):
            _ents_of.setdefault(_ce, []).append(_eid)

    def _live(ce):
        """('failing', why) | ('ok', why) | ('unknown', why) — read now."""
        st = str(entries.get(ce, {}).get("state") or "")
        if st and st != "loaded":
            return "failing", "the platform reports the integration as '%s'" % st
        if not _states_ok:
            return "unknown", "could not read its devices now"
        ents = sorted(set(_ents_of.get(ce) or []))
        if not ents:
            return "unknown", "no device of it is in a room to check"
        bad = [x for x in ents if str((_states.get(x) or {}).get("state") or "unavailable")
               in ("unavailable", "unknown")]
        if bad:
            return "failing", "%s %s not reporting now" % (", ".join(bad[:3]), "is" if len(bad) == 1 else "are")
        return "ok", "%d device%s reporting now" % (len(ents), "" if len(ents) == 1 else "s")

    for ce in sorted(used):
        e = entries[ce]
        title = e.get("title") or e.get("domain")
        rows = heard[ce]
        if not rows:
            out.append(_item("4", title, CONFIRMED, "%s wrote no warnings or errors to the box's log in the last day"
                             % title, _iso(now)))
            continue
        for row in sorted(rows, key=lambda x: -(x.get("timestamp") or 0))[:3]:
            lvl = str(row.get("level") or "").upper()
            quote = " / ".join(dict.fromkeys(str(m) for m in (row.get("message") or [])))[:400]
            words = "%s told the box: '%s' — %d time%s, last %s" % (
                title, quote, int(row.get("count") or 1), "" if int(row.get("count") or 1) == 1 else "s",
                _when(row.get("timestamp"), now))
            err = lvl in ("ERROR", "CRITICAL")
            # PLAN D1 (3 Oct 2026; L-8). The Frame's integration logs "token rejected …
            # re-pair required — re-pair via the integration options" at WARNING, so the
            # sweep made no card and no Pair button for the one case the button exists for.
            # When a pairable integration's OWN words instruct a re-pair, that instruction
            # is the card — quoted verbatim, the button runs the platform's pairing flow.
            asks_pair = (e.get("domain") in PAIRABLE
                         and any(PAIR_WORDS in str(m).lower() for m in (row.get("message") or [])))
            live, why = _live(ce)
            if live == "ok":
                out.append(_item("4", title, CONFIRMED, "%s — OK now (%s)" % (words, why), _iso(now)))
                continue
            if live == "unknown":
                out.append(_item("4", title, UNKNOWN, "%s — %s" % (words, why), _iso(now)))
                continue
            card = err or asks_pair
            acts = ([{"kind": "pair", "entry_id": ce, "label": "Pair"}]
                    if (card and e.get("domain") in PAIRABLE) else [])
            out.append(_item("4", title, PROBLEM, "%s — and %s" % (words, why), _iso(row.get("timestamp")),
                             card=card, actions=acts))
    return out


def check_witnesses(r, now):
    try:
        incs = [i for i in (r.incidents() or []) if i.get("kind") in ("witness_broken", "missing_witness", "witness_blackout")]
    except Exception as e:                                       # noqa: BLE001
        return [_item("5", "Witness coverage", UNKNOWN, "could not read Health: %s" % e)]
    if not incs:
        return [_item("5", "Witness coverage", CONFIRMED, "every bound witness has testified (no binding fault on Health)",
                      _iso(now))]
    return [_item("5", i.get("subject") or "witness", PROBLEM, "already on Health: %s" % i.get("title"),
                  _iso(now), ref=i.get("kind")) for i in incs]


def check_gear(r, now):
    try:
        g = r.gear()
    except Exception as e:                                       # noqa: BLE001
        return [_item("6", "Network gear", UNKNOWN, "the network controller did not answer: %s" % e)]
    if g is None:
        return [_item("6", "Network gear", UNKNOWN, "no network controller is configured, so ProOS cannot read the "
                                                    "switches and access points")]
    out = []
    for mac, x in sorted(g.items(), key=lambda kv: str(kv[1].get("name"))):
        on = x.get("online")
        nm = x.get("name") or mac
        if on is True:
            out.append(_item("6", nm, CONFIRMED, "%s is online to the network controller" % nm, _iso(now)))
        elif on is False:
            out.append(_item("6", nm, PROBLEM, "%s is offline — already on Health" % nm, _iso(now), ref="infra_down"))
        else:
            out.append(_item("6", nm, UNKNOWN, "the controller does not say whether %s is online" % nm))
    return out or [_item("6", "Network gear", UNKNOWN, "the controller listed no gear")]


def check_activities(r, now):
    out = []
    try:
        incs = r.incidents() or []
    except Exception:                                            # noqa: BLE001
        incs = []
    for key, rec in ((r.project() or {}).get("areas") or {}).items():
        if not (rec and rec.get("committed") and rec.get("display")):
            continue
        room = rec.get("name") or key
        try:
            acts = r.activities(rec.get("area_id") or key) or []     # by area id (register 441)
        except Exception as e:                                   # noqa: BLE001
            out.append(_item("7", room, UNKNOWN, "could not read %s's activities: %s" % (room, e)))
            continue
        bad = [i for i in incs if i.get("slug") == (rec.get("area_id") or key)
               and i.get("kind") in ("committed_missing", "prepare", "record_fault")]
        if bad:
            out.append(_item("7", room, PROBLEM, "already on Health: %s" % bad[0].get("title"), _iso(now),
                             ref=bad[0].get("kind")))
        elif not acts:
            out.append(_item("7", room, PROBLEM, "%s is committed but has no activities on the box" % room,
                             _iso(now), card=True))
        else:
            out.append(_item("7", room, CONFIRMED, "%s has %d activit%s on the box: %s" % (
                room, len(acts), "y" if len(acts) == 1 else "ies", ", ".join(a.get("label") or a.get("entity_id")
                                                                          for a in acts)), _iso(now)))
    return out


def check_backups(r, now):
    try:
        c = r.backups()
    except Exception as e:                                       # noqa: BLE001
        return [_item("8", "Backups", UNKNOWN, "could not read the box's backup schedule: %s" % e)]
    if c.get("enabled") is False:
        return [_item("8", "Backups", PROBLEM, "the box's automatic backups are off", _iso(now), card=True)]
    if c.get("enabled") is None:
        return [_item("8", "Backups", UNKNOWN, "the box did not say whether automatic backups are on")]
    last = _ts(c.get("last_completed"))
    nxt = _ts(c.get("next"))
    if not last:
        return [_item("8", "Backups", PROBLEM, "automatic backups are on but none has completed", _iso(now), card=True)]
    # within one schedule period: the box's own next run is after now, and the last one is
    # no older than the gap between it and the next one the box has planned
    period = (nxt - last) if (nxt and nxt > last) else 86400
    if now - last <= period + 3600:
        return [_item("8", "Backups", CONFIRMED, "last automatic backup completed %s" % _when(last, now),
                      c.get("last_completed"))]
    return [_item("8", "Backups", PROBLEM, "the last automatic backup was %s — older than the box's own schedule"
                  % _when(last, now), c.get("last_completed"), card=True)]


def check_savant(r, now):
    try:
        s = r.savant()
    except Exception as e:                                       # noqa: BLE001
        return [_item("9", "Savant", UNKNOWN, "could not read the Savant feed: %s" % e)]
    if not s or not s.get("enabled"):
        return []                                    # not switched on: not part of this home
    if s.get("connected"):
        return [_item("9", "Savant", CONFIRMED, "a Savant host is connected to ProOS", _iso(now))]
    return [_item("9", "Savant", PROBLEM, "the Savant feed is on but no Savant host is connected", _iso(now), card=True)]


# ── the sweep ────────────────────────────────────────────────────────────────
def load_last():
    try:
        with open(LAST_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                            # noqa: BLE001
        return None


def run(r, now=None, trigger="manual"):
    """One full sweep. Returns the report (also saved and journalled)."""
    now = now or time.time()
    with _lock:
        if _running["on"]:
            return {"state": "running"}
        _running["on"] = True
    try:
        # the time limit is the sweep's own cadence (Dave, R1): one day. Not "since the last
        # sweep" — a TV that left just after one sweep would then wait two days to be said.
        since = now - PERIOD_S
        try:
            reg_entries, devs, ents = r.registries()
        except Exception:                                        # noqa: BLE001
            reg_entries, devs, ents = [], [], []
        ent_dev = {e.get("entity_id"): e.get("device_id") for e in ents if e.get("device_id")}
        dev_entries = {d.get("id"): list(d.get("config_entries") or []) for d in devs}
        members = _committed_members(r)
        items = []
        items += check_proos(r, now)
        items += check_integrations(r, now, members, ent_dev, dev_entries)
        items += check_devices(r, now, members, ent_dev, since)
        items += check_log_words(r, now, members, ent_dev, dev_entries, reg_entries)
        items += check_witnesses(r, now)
        items += check_gear(r, now)
        items += check_activities(r, now)
        items += check_backups(r, now)
        items += check_savant(r, now)
        counts = {k: sum(1 for i in items if i["answer"] == k) for k in (CONFIRMED, PROBLEM, UNKNOWN)}
        counts["checked"] = len(items)
        # THE SWEEP IS A LIST OF WHAT IT CHECKED (Dave's ruling, 5 Oct 2026), not a second
        # count of problems beside the alarm list: every problem it found is on Health —
        # as its own card, or as a reference to the card already there — and is said so.
        rep = {"state": "done", "at": now, "at_iso": _iso(now), "trigger": trigger, "since": since,
               "counts": counts, "items": items,
               "headline": "%d checked · %d confirmed · %d could not be confirmed · %d on Health" % (
                   len(items), counts[CONFIRMED], counts[UNKNOWN], counts[PROBLEM])}
        try:
            os.makedirs(DATA, exist_ok=True)
            tmp = LAST_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rep, fh)
            os.replace(tmp, LAST_PATH)
        except Exception as e:                                   # noqa: BLE001
            print("  [sweep] could not save the report: %s" % e, flush=True)
        return rep
    finally:
        _running["on"] = False


def cards(now=None):
    """The last sweep's NEW problems as Health incidents (read time). Problems another check
    already shows on Health are referenced in the report, never carded twice. They stand
    until a later sweep reads the thing right."""
    rep = load_last() or {}
    out = []
    for it in rep.get("items") or []:
        if not it.get("card"):
            continue
        out.append({"kind": "sweep", "room": "ProOS", "slug": "site",
                    "severity": "critical" if it["check"] in ("2", "1c", "4") else "warning",
                    "audience": "installer", "title": "Sweep — %s" % it["subject"],
                    "cause": it["words"] + " (found by the full sweep, %s)" % _when(rep.get("at"), now or time.time()),
                    "subject": "sweep:%s:%s" % (it["check"], it["subject"]),
                    "id": "sw-" + hashlib.sha1(("%s|%s" % (it["check"], it["subject"])).encode()).hexdigest()[:10],
                    "since": rep.get("at"), "last_seen": rep.get("at"), "actions": it.get("actions") or []})
    return out


# ── the daily schedule — the time lives in /data, and so does "done today" ───
def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as fh:
            c = json.load(fh)
    except Exception:                                            # noqa: BLE001
        c = {}
    return {"time": c.get("time") or DEFAULT_TIME, "enabled": c.get("enabled", True)}


def save_cfg(c):
    cur = load_cfg()
    if "time" in c:
        hh, mm = [int(x) for x in str(c["time"]).split(":")]
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("time must be HH:MM")
        cur["time"] = "%02d:%02d" % (hh, mm)
    if "enabled" in c:
        cur["enabled"] = bool(c["enabled"])
    os.makedirs(DATA, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cur, fh)
    return cur


def due(now=None):
    """True when today's scheduled sweep has not run. "Ran today" is read from the saved
    report, never from memory — a Core restart must not run a second sweep (the lesson of
    register 662's backups)."""
    now = now or time.time()
    c = load_cfg()
    if not c["enabled"]:
        return False
    hh, mm = [int(x) for x in c["time"].split(":")]
    lt = time.localtime(now)
    sched = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if now < sched:
        return False
    last = load_last() or {}
    return float(last.get("at") or 0) < sched
