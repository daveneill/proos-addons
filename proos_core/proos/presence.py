"""HOME AND AWAY, THE UNIFI WAY: EACH PERSON'S PRIMARY DEVICE — register 1358 (15 Nov 2026).

Dave 16:13 (UniFi's Geofencing screen: Geofencing On · Console Location · Geofence Radius · Primary Device), 16:14
"yes" to the plan, 16:15 "yes" to build. List item 10.30.

THE PLATFORM'S OWN MACHINERY, NOTHING REBUILT:
  · a person is the platform's person record; home or away is the state of the trackers it follows
  · the platform links EVERY device a person signs in on to their record (read on Dave's box 15 Nov: "ProOS Developer"
    followed the iPad AND the Pro app on his MacBook) — so a Mac or a wall panel keeps someone "home" for ever
  · the home's location and radius are the platform's home zone

WHAT ONLY PROOS KNOWS, KEPT HERE: which of a person's devices is their PRIMARY DEVICE — the one that decides their
home and away. Chosen in Pro's People or the app's Coming and Going; until then the first iPhone the person signed in
on (an iPhone travels with its owner; a Mac or an iPad is never chosen for them — only by them). ProOS keeps the
person's record following that one device: the app sign-ins' trackers in the record are exactly [the primary's];
anything else the record follows (a network presence tracker an installer added) is left alone.

A person with no app device that can be primary is left as the platform has them, and said so.
"""
from __future__ import annotations

import json
import os
import threading

STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "presence.json")
_lock = threading.Lock()


def _load():
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d):
    tmp = "%s.%d.%d.tmp" % (STORE, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def is_phone(model):
    """An iPhone, by the model the app reports at sign-in ("iPhone19,7"). A Mac running the iPad app reports an iPad
    model, so it is never taken for a phone."""
    return str(model or "").startswith("iPhone")


def people(persons, entries, trackers, states=None, primaries=None):
    """persons: the platform's person records [{id, name, user_id, device_trackers}].
    entries: the platform's config entries (storage shape). trackers: {config_entry_id: device_tracker entity id} for
    the app sign-ins. states: {entity_id: state, "person:<person id>": the person's own state}. primaries: {person_id: entry_id} (ProOS's choice).
    Returns each person with their app devices, the primary in force, and why."""
    states, primaries = states or {}, primaries or {}
    apps = [e for e in entries or [] if e.get("domain") == "mobile_app"]
    out = []
    for p in persons or []:
        uid = p.get("user_id")
        devs = []
        for e in sorted(apps, key=lambda x: str(x.get("created_at") or "")):
            d = e.get("data") or {}
            if not uid or d.get("user_id") != uid:
                continue
            t = trackers.get(e.get("entry_id"))
            devs.append({"entry_id": e.get("entry_id"), "name": d.get("device_name") or e.get("title") or "a device",
                         "model": d.get("model") or "", "phone": is_phone(d.get("model")), "tracker": t,
                         "state": states.get(t) if t else None})
        chosen = primaries.get(p.get("id"))
        prim, why = None, "none"
        if chosen and any(x["entry_id"] == chosen and x["tracker"] for x in devs):
            prim, why = chosen, "chosen"
        else:
            first = next((x for x in devs if x["phone"] and x["tracker"]), None)
            if first:
                prim, why = first["entry_id"], "first iPhone"
        out.append({"person_id": p.get("id"), "name": p.get("name"), "user_id": uid, "devices": devs,
                    "primary": prim, "why": why, "follows": list(p.get("device_trackers") or []),
                    "state": states.get("person:%s" % p.get("id"))})
    return out


def wanted_trackers(person, app_trackers):
    """The record's trackers as ProOS keeps them: everything not from an app sign-in, kept; of the app sign-ins'
    trackers, only the primary's. REGISTER 1360 (read on Dave's box 15 Nov: "ProOS Developer" followed the homeowner
    login's iPad): a person never follows another login's device, and with no primary follows no app device at all —
    a Mac or an iPad is never chosen for anyone; Pro says "choose one"."""
    keep = [t for t in person["follows"] if t not in app_trackers]
    prim = next((d["tracker"] for d in person["devices"] if d["entry_id"] == person.get("primary")), None)
    return keep + ([prim] if prim else [])


def missing_persons(users, persons, entries):
    """REGISTER 1360: every login that has signed in with a ProOS app needs its own person record, or it can never be
    home or away. Read on Dave's box 15 Nov: the homeowner login (his iPhone, iPad and Mac) had none — the platform
    makes a person only through its own onboarding; logins made by Core or by the ProOS sign-in get none.
    Returns [(user_id, name)] to create — active, not the platform's own system logins, with an app sign-in."""
    have = {p.get("user_id") for p in persons or [] if p.get("user_id")}
    with_app = {(e.get("data") or {}).get("user_id") for e in entries or [] if e.get("domain") == "mobile_app"}
    out = []
    for u in users or []:
        uid = u.get("id")
        if (not uid or uid in have or uid not in with_app or u.get("system_generated")
                or u.get("is_active") is False):
            continue
        out.append((uid, (u.get("name") or "").strip() or "Person"))
    return out


def keep(read, write, users=None, create=None):
    """Bring every person's record onto their primary device. read() → (persons, entries, trackers, states);
    write(person_record, device_trackers) does the platform's person/update. With users() and create(user_id, name)
    (the platform's person/create), a login with an app sign-in and no person gets one first (1360).
    Returns what was changed."""
    with _lock:
        persons, entries, trackers, states = read()
        if users and create:
            made = missing_persons(users(), persons, entries)
            for uid, name in made:
                create(uid, name)
            if made:
                persons, entries, trackers, states = read()
        app_trackers = set(trackers.values())
        changed = []
        by_id = {p.get("id"): p for p in persons or []}
        for p in people(persons, entries, trackers, states, _load().get("primary") or {}):
            want = wanted_trackers(p, app_trackers)
            if want is None or sorted(want) == sorted(p["follows"]):
                continue
            write(by_id[p["person_id"]], want)
            changed.append({"person": p["name"], "follows": want})
        return changed


def choose(person_id, entry_id):
    """Record a person's primary device (the caller has checked it is one of theirs)."""
    with _lock:
        d = _load()
        d.setdefault("primary", {})[str(person_id)] = str(entry_id)
        _save(d)
    return {"ok": True}
