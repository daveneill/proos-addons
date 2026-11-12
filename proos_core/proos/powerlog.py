"""
ProOS Core — device power timeline with attribution (spec, 2 Aug 2026).

"An installer or Assist just needs to be able to see at a glance all the
times this TV or media player turned on or off... if it's happening at times
when clients did not prompt it, then it's an issue with the media player,
not the system." — the product is AWARENESS: show what happened and who did
it, and the pattern speaks for itself. Nothing here judges; nothing here
advises. It attributes.

WHO DID IT IS THE PLATFORM'S RECORD, NOT A TIME WINDOW (register 1324, 12 Nov 2026 — plan item B8, "Who did it"
from the platform's own Activity). Until now an ON was "proos" when one of the room's scripts ran within 120 s before
it, else "external": a guess from timing. The platform records the CAUSE of every state change — its logbook links the
change to the action that made it (a person through the app or Pro, an app on the box, an automation, HomeKit …) or to
nothing at all (the device changed by itself: its own remote, a timer, its app, CEC). Read on Dave's box: the Bedroom
TV's 04:01 UTC ON = script.turn_on by "Supervisor"; its 04:17 OFF = no cause; the 04:50 ON = script.turn_on by "Dave".
So: the timeline (when, on/off/no reading) comes from history as before; each change's cause comes from the logbook
entry of THAT change (its time, to the millisecond — the same record, not a window). No logbook entry → "no record",
never "the device did it". The "Supervisor" account is the one every app on the box acts through (ProOS Core, Music
Assistant …) — said as "an app on the box", never guessed to be ProOS.

Pure functions here; the server route and the Assist tool fetch and feed them. Benched offline
(tests/powerlog_bench.py).
"""
from __future__ import annotations


_OFFISH = ("off", "standby")
# NO READING IS NOT OFF (register 659; plan §4 bottom). The first version folded
# `unavailable` into "off" under the words "the timeline must never invent certainty"
# — but "OFF" IS a certainty: it told the installer the TV was switched off at a
# moment the platform only said it had lost the TV. A dropout is its own row.
_NOREADING = ("unavailable", "unknown", "", "none")


def _cls(state) -> str:
    st = str(state if state is not None else "").strip().lower()
    if st in _NOREADING:
        return "no_reading"
    return "off" if st in _OFFISH else "on"


def _onish(state) -> bool:
    return _cls(state) == "on"


def transitions(states) -> list:
    """[(ts, state)] sorted asc -> [{'ts', 'to'}] power transitions only.
    playing->paused is not a power event. `to` is "on", "off", or "no_reading"
    when the platform lost the device (unavailable/unknown) — said as that, never
    as off."""
    out, prev = [], None
    for ts, st in (states or []):
        cur = _cls(st)
        if prev is None:
            prev = cur
            continue
        if cur != prev:
            out.append({"ts": float(ts), "to": cur})
            prev = cur
    return out


def cause_of(row, users=None) -> dict:
    """One logbook row (the platform's record of a state change) → {by, who, how}.
    by: person | app | automation | script | integration | device | unknown."""
    if not row:
        return {"by": "unknown", "who": None, "how": None}
    users = users or {}
    ev = row.get("context_event_type") or ""
    dom, svc = row.get("context_domain") or "", row.get("context_service") or ""
    how = "%s.%s" % (dom, svc) if dom and svc else None
    uid = row.get("context_user_id")
    named = row.get("context_entity_id_name") or row.get("context_name") or row.get("context_entity_id")
    if ev == "automation_triggered":
        return {"by": "automation", "who": named, "how": how}
    if ev == "script_started":
        return {"by": "script", "who": named, "how": how}
    if ev == "call_service" or (not ev and uid):
        u = users.get(uid) if uid else None
        if u is not None:
            if u.get("system"):
                return {"by": "app", "who": "an app on the box (%s)" % (u.get("name") or "system"), "how": how}
            return {"by": "person", "who": u.get("name") or "a person", "how": how}
        if uid:
            return {"by": "person", "who": "an account no longer on the box", "how": how}
        if named:
            return {"by": "automation", "who": named, "how": how}
        return {"by": "app", "who": "the platform", "how": how}
    if ev:
        return {"by": "integration", "who": dom or named or ev, "how": how}
    return {"by": "device", "who": None, "how": None}


def attribute_from_logbook(trans, rows, users=None) -> list:
    """Each power change gets the cause the platform recorded for THAT change (matched on its time to the ms)."""
    from datetime import datetime
    by_ms = {}
    for r in (rows or []):
        w = r.get("when")
        try:
            ts = float(w) if isinstance(w, (int, float)) else datetime.fromisoformat(str(w).replace("Z", "+00:00")).timestamp()
        except Exception:                                        # noqa: BLE001
            continue
        by_ms[round(ts * 1000)] = r
    out = []
    for t in (trans or []):
        if t["to"] == "no_reading":
            out.append({"ts": t["ts"], "to": t["to"], "by": None, "who": None, "how": None})
            continue
        c = cause_of(by_ms.get(round(t["ts"] * 1000)), users)
        out.append(dict(t, **c))
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out


# 1324: script_runs / attribute / power_log — the 120-second "a room script ran just before" guess — are gone; the cause
# of each change is the platform's own record (cause_of, attribute_from_logbook).


def fetch_log(client, project_load, entity: str, hours: float = 48.0, ws_read=None) -> dict:
    """Recorder-backed timeline for one device, each change with the cause the platform recorded for it (1324).
    Shared by GET /devices/powerlog and the Assist tool. The platform's logbook unreadable → every cause "unknown"
    (said as no record), never "external"."""
    from datetime import datetime, timezone, timedelta
    from urllib.parse import quote

    slug = None
    try:
        proj = project_load() or {}
        for k, rec in (proj.get("areas") or {}).items():
            r = rec or {}
            members = {r.get("display"), r.get("tvaudio")}
            for b in ("sources", "audio", "speakers"):
                for it in (r.get(b) or []):
                    members.add(it.get("entity") if isinstance(it, dict)
                                else it)
            if entity in members:
                slug = r.get("area_id") or k
                break
    except Exception:                                            # noqa: BLE001
        slug = None

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=float(hours))
    ids = entity
    path = ("/api/history/period/%s?filter_entity_id=%s&end_time=%s"
            % (quote(start.isoformat()), quote(ids, safe=",._"),
               quote(end.isoformat())))
    dev_states = []
    try:
        for series in (client._req("GET", path) or []):
            if not series:
                continue
            if (series[0] or {}).get("entity_id") != entity:
                continue
            for row in series:
                lc = row.get("last_changed") or row.get("last_updated")
                try:
                    ts = datetime.fromisoformat(
                        str(lc).replace("Z", "+00:00")).timestamp()
                except Exception:                                # noqa: BLE001
                    continue
                dev_states.append((ts, row.get("state")))
    except Exception:                                            # noqa: BLE001
        pass
    dev_states.sort()
    rows, cause_read = [], True
    try:
        rows = client._req("GET", "/api/logbook/%s?entity=%s&end_time=%s"
                           % (quote(start.isoformat()), quote(entity, safe="._"), quote(end.isoformat()))) or []
    except Exception:                                            # noqa: BLE001
        rows, cause_read = [], False
    users = {}
    if ws_read is not None:      # a READ-only line to the platform (register 1324): the account list, nothing else
        try:
            for u in (ws_read("config/auth/list") or []):
                users[u.get("id")] = {"name": u.get("name"), "system": bool(u.get("system_generated"))}
        except Exception:                                        # noqa: BLE001
            users = {}
    log = attribute_from_logbook(transitions(dev_states), rows, users)
    for e in log:
        e["iso"] = datetime.fromtimestamp(
            e["ts"], timezone.utc).isoformat()
    return {"entity": entity, "room": slug, "hours": float(hours), "causes_read": cause_read,
            "log": log}
