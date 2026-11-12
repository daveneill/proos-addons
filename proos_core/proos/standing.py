"""STANDING INSTRUCTIONS — "if I ask for something twice, offer to do it from now on" (register 1316, 12 Nov 2026).

ProOS_Assist_Always_Analysing_Algorithm_2026-09-17.md, Loop 3c (A6). Dave, 17 Sep: "Is it possible that if I asked for
something it should be able to remember for next time?" — and the design (11 Nov): what it learns is WRITTEN AS A
PLATFORM AUTOMATION, so it is visible, editable and survives without ProOS.

How a request becomes a rule — three steps, the second is what makes it safe:
  1. FIRST TIME — Assist does what was asked; if the room is DOING something (its ProOS activity is not off), the
     request is filed as "asked once": the call, the room, what the room was doing, the words. No card, nothing said.
  2. SECOND TIME — the same call, the same room, the same activity, at least SECOND_ASK_GAP apart (two askings, not
     one sentence repeated in a conversation): Assist does it, and then says one line — "You've asked for that twice
     now — want me to do it automatically whenever <activity> starts in <room>?" Yes → a platform automation
     ("ProOS — as <person> asked: …"); No → never, and it is not asked again.
  3. STANDING — the platform runs it whenever that activity starts in that room. It is listed, and forgotten, like
     any other automation; GET /assist/standing lists them, POST /assist/standing/forget removes one.

What it never does: offer from ONE request; invent a trigger (a room doing nothing has nothing to trigger on); learn
anything but a device command of the entity's own domain (no reloads, no locks, no alarms — a door or an alarm is never
made automatic by a habit of asking); answer for another person (the record is per person).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "standing.json")
SECOND_ASK_GAP = 6 * 3600         # two askings at least six hours apart (plumbing: one conversation is not a pattern)
OFFER_TTL = 10 * 60               # a yes/no is an answer to the offer only within ten minutes of it
AUTO_PREFIX = "proos_standing_"
# device commands only — a lock, an alarm, a door, a garage, a valve is NEVER learned (it stays the person's act)
_LEARNABLE = {"light", "media_player", "switch", "fan", "cover", "climate", "remote", "humidifier", "select", "number"}
_NEVER = {"lock", "alarm_control_panel", "valve", "siren"}
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please|do it|go on|go ahead|sounds good|definitely)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|nah|don'?t|never|not now|no thanks)\b", re.I)


def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, STORE)
    except Exception:  # noqa: BLE001
        pass


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return [str(v)]


def normalise(args: dict):
    """A platform call (ha_call_service's arguments, either shape) → (domain, service, [entities], data) — or None when
    it is not a learnable device command."""
    a = args or {}
    dom, svc = str(a.get("domain") or ""), str(a.get("service") or "")
    data = dict(a.get("data") or {}) if isinstance(a.get("data"), dict) else {}
    if str(a.get("ws_command") or "") == "call_service":
        dom, svc = str(data.get("domain") or dom), str(data.get("service") or svc)
        tgt = data.get("target") if isinstance(data.get("target"), dict) else {}
        data = dict(data.get("service_data") or {}, **tgt)
    if not dom and "." in svc:
        dom, svc = svc.split(".", 1)
    ents = sorted(set(_as_list(a.get("entity_id")) + _as_list(data.pop("entity_id", None))))
    data.pop("area_id", None)
    data.pop("device_id", None)
    if not dom or not svc or not ents or dom in _NEVER or dom not in _LEARNABLE:
        return None
    if any(e.split(".", 1)[0] != dom for e in ents):
        return None                     # a service of the entities' own domain only
    clean = {}
    for k, v in sorted(data.items()):
        if isinstance(v, float):
            v = round(v / 0.05) * 0.05  # 0.31 and 0.30 are the same request
            v = round(v, 2)
        if isinstance(v, (str, int, float, bool)):
            clean[k] = v
    return dom, svc, ents, clean


def _sig(user_id, area, activity, call) -> str:
    raw = json.dumps([str(user_id or ""), area, activity, call[0], call[1], call[2], call[3]], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _words(call, names: dict) -> str:
    dom, svc, ents, data = call
    who = ", ".join(names.get(e) or e.split(".", 1)[1].replace("_", " ") for e in ents)
    if svc == "volume_set" and "volume_level" in data:
        return "set %s to volume %d%%" % (who, round(float(data["volume_level"]) * 100))
    if svc == "turn_on" and "brightness_pct" in data:
        return "set %s to %s%%" % (who, data["brightness_pct"])
    return "%s %s" % (svc.replace("_", " "), who)


def observe(user: dict, session: str, text: str, calls: list, area_of, activity_of, names: dict, now=None):
    """After a turn: file what was asked; on the SECOND asking, return the offer sentence (else None).
    calls: the platform calls the turn made that succeeded; area_of(entity) → area id; activity_of(area) →
    (state, label) of the room's ProOS activity."""
    now = float(now or time.time())
    uid = str((user or {}).get("id") or "")
    if not uid:
        return None
    d = _load()
    recs = d.setdefault("records", {})
    offer = None
    for args in calls or []:
        call = normalise(args)
        if call is None:
            continue
        area = area_of(call[2][0])
        if not area:
            continue
        act, label = activity_of(area) or (None, None)
        if not act or act in ("off", "unknown", "unavailable", "idle"):
            continue                    # nothing to trigger on: never invent one
        sig = _sig(uid, area, act, call)
        r = recs.get(sig)
        if r is None:
            recs[sig] = {"user": uid, "person": (user or {}).get("name") or "", "area": area, "activity": act,
                         "activity_label": label or act, "call": list(call), "count": 1, "first": now, "last": now,
                         "words": [str(text or "")[:200]], "status": "asked_once"}
            continue
        if r.get("status") != "asked_once":
            continue                    # offered, standing, or never: nothing more to say
        if now - float(r.get("last") or 0) < SECOND_ASK_GAP:
            r["last"] = now             # the same conversation — not a second asking
            continue
        r["count"] = int(r.get("count") or 1) + 1
        r["last"] = now
        r["words"] = (r.get("words") or []) + [str(text or "")[:200]]
        r["status"] = "offered"
        d.setdefault("pending", {})[str(session or "default") + "|" + uid] = {"sig": sig, "ts": now}
        offer = ("You've asked for that twice now — want me to do it automatically whenever %s starts in the %s?"
                 % (r["activity_label"], names.get("area:" + area) or area.replace("_", " ").title()))
    _save(d)
    return offer


def pending_for(user: dict, session: str, now=None):
    now = float(now or time.time())
    d = _load()
    p = (d.get("pending") or {}).get(str(session or "default") + "|" + str((user or {}).get("id") or ""))
    if not p or now - float(p.get("ts") or 0) > OFFER_TTL:
        return None
    return p


def is_answer(text: str):
    t = str(text or "")
    if _NO.search(t):
        return "no"
    if _YES.search(t):
        return "yes"
    return None


def automation(sig: str, r: dict, names: dict) -> dict:
    dom, svc, ents, data = r["call"]
    room = names.get("area:" + r["area"]) or r["area"].replace("_", " ").title()
    what = _words(r["call"], names)
    act = {"action": "%s.%s" % (dom, svc), "target": {"entity_id": list(ents)}}
    if data:
        act["data"] = dict(data)
    return {
        "id": AUTO_PREFIX + sig,
        "alias": "ProOS — as %s asked: %s when %s starts in the %s" % (r.get("person") or "you", what,
                                                                        r["activity_label"], room),
        "description": ("Learned by ProOS Assist (register 1316): asked twice — %s — and confirmed with a yes on %s. "
                        "Delete this automation to forget it." % ("; ".join('"%s"' % w for w in r.get("words") or []),
                                                                   time.strftime("%d %b %Y"))),
        "mode": "single",
        "triggers": [{"trigger": "state", "entity_id": "sensor.proos_activity_%s" % r["area"], "to": r["activity"]}],
        "conditions": [],
        "actions": [act],
    }


def answer(client, user: dict, session: str, text: str, names: dict, now=None):
    """A yes or no to the offer just made. Returns the reply, or None when this is not an answer to it."""
    p = pending_for(user, session, now)
    ans = is_answer(text) if p else None
    if not ans:
        return None
    d = _load()
    d.get("pending", {}).pop(str(session or "default") + "|" + str((user or {}).get("id") or ""), None)
    r = (d.get("records") or {}).get(p["sig"])
    if not r:
        _save(d)
        return None
    if ans == "no":
        r["status"] = "never"
        _save(d)
        return "Okay — I won't ask about that again."
    auto = automation(p["sig"], r, names)
    client._req("POST", "/api/config/automation/config/%s" % auto["id"], auto)
    r["status"] = "standing"
    r["automation"] = auto["id"]
    _save(d)
    return "Done — from now on, %s whenever %s starts in the %s. It's an automation called “%s”; delete it to forget." % (
        _words(r["call"], names), r["activity_label"],
        names.get("area:" + r["area"]) or r["area"].replace("_", " ").title(), auto["alias"])


def listing() -> list:
    out = []
    for sig, r in (_load().get("records") or {}).items():
        out.append({"id": sig, "person": r.get("person"), "area": r.get("area"), "activity": r.get("activity_label"),
                    "call": r.get("call"), "status": r.get("status"), "count": r.get("count"),
                    "words": r.get("words"), "automation": r.get("automation")})
    return sorted(out, key=lambda x: (x["status"] != "standing", x["area"] or ""))


def forget(client, sig: str) -> bool:
    d = _load()
    r = (d.get("records") or {}).pop(str(sig), None)
    if r is None:
        return False
    if r.get("automation"):
        try:
            client._req("DELETE", "/api/config/automation/config/%s" % r["automation"])
        except Exception:  # noqa: BLE001
            pass
    _save(d)
    return True


def clear(client=None) -> None:
    """A factory reset (register 1316): every record goes, and every automation it wrote — ProOS's own."""
    for r in (_load().get("records") or {}).values():
        if client is not None and r.get("automation"):
            try:
                client._req("DELETE", "/api/config/automation/config/%s" % r["automation"])
            except Exception:  # noqa: BLE001
                pass
    try:
        if os.path.exists(STORE):
            os.remove(STORE)
    except Exception:  # noqa: BLE001
        pass
