"""
ProOS Core — device power timeline with attribution (spec, 2 Aug 2026).

"An installer or Assist just needs to be able to see at a glance all the
times this TV or media player turned on or off... if it's happening at times
when clients did not prompt it, then it's an issue with the media player,
not the system." — the product is AWARENESS: show what happened and who did
it, and the pattern speaks for itself. Nothing here judges; nothing here
advises. It attributes.

Attribution is concrete, brand-free and historical: an ON (or OFF) is
"proos" when one of the room's own activity scripts ran within the window
before it (the recorder keeps script runs like any state); otherwise
"external" — a native remote, an app, or a device waking the display through
CEC. All equally legitimate; the timeline only says which.

Pure functions here; the server route fetches recorder history and feeds
them. Benched offline (tests/powerlog_bench.py).
"""
from __future__ import annotations

from .live_ha import all_states as _all_states  # register 173: one seam

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


def script_runs(script_states) -> list:
    """[(ts, state)] for the room's proos scripts -> sorted run-start times
    (a script entity reads 'on' while running)."""
    runs = []
    for ts, st in (script_states or []):
        if str(st or "").strip().lower() == "on":
            runs.append(float(ts))
    runs.sort()
    return runs


def attribute(trans, runs, window: float = 120.0) -> list:
    """Attach attribution to each transition: 'proos' when a room script ran
    within `window` seconds BEFORE (or 5s after — command then state), else
    'external'. Never judged, only stated."""
    out = []
    for t in (trans or []):
        ts = t["ts"]
        if t["to"] == "no_reading":
            # nobody "did" a dropout — it is attributed to no one
            out.append({"ts": ts, "to": t["to"], "by": None})
            continue
        by = "external"
        for r in (runs or []):
            if -5.0 <= ts - r <= window:
                by = "proos"
                break
        out.append({"ts": ts, "to": t["to"], "by": by})
    return out


def power_log(dev_states, script_states, window: float = 120.0) -> list:
    """The timeline, newest first — the one call the route and Assist use."""
    log = attribute(transitions(dev_states), script_runs(script_states),
                    window)
    log.sort(key=lambda e: e["ts"], reverse=True)
    return log


def fetch_log(client, project_load, entity: str, hours: float = 48.0) -> dict:
    """Recorder-backed timeline for one device: resolve its room, pull the
    device's history plus the room's proos script runs in ONE recorder call,
    attribute, return. Shared by GET /devices/powerlog and the Assist tool.
    Fail-open: unknown room = no scripts = everything honest 'external'."""
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

    script_ids = []
    if slug:
        try:
            pref = "script.proos_%s_" % slug
            script_ids = [x.get("entity_id")
                          for x in (_all_states(client) or [])
                          if str(x.get("entity_id", "")).startswith(pref)]
        except Exception:                                        # noqa: BLE001
            script_ids = []

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=float(hours))
    ids = ",".join([entity] + script_ids)
    path = ("/api/history/period/%s?filter_entity_id=%s&end_time=%s"
            % (quote(start.isoformat()), quote(ids, safe=",._"),
               quote(end.isoformat())))
    dev_states, script_states = [], []
    try:
        for series in (client._req("GET", path) or []):
            if not series:
                continue
            eid0 = (series[0] or {}).get("entity_id")
            for row in series:
                lc = row.get("last_changed") or row.get("last_updated")
                try:
                    ts = datetime.fromisoformat(
                        str(lc).replace("Z", "+00:00")).timestamp()
                except Exception:                                # noqa: BLE001
                    continue
                (dev_states if eid0 == entity
                 else script_states).append((ts, row.get("state")))
    except Exception:                                            # noqa: BLE001
        pass
    dev_states.sort()
    script_states.sort()
    log = power_log(dev_states, script_states)
    for e in log:
        e["iso"] = datetime.fromtimestamp(
            e["ts"], timezone.utc).isoformat()
    return {"entity": entity, "room": slug, "hours": float(hours),
            "log": log}
