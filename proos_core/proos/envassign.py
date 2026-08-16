"""Standard placement for environment entities (Dave, 3 Aug).

Field lesson: the weather integration ships with NO area, so under
"assigned is in the room" it spoke nowhere — and fixing it would have
meant another trip into HA. Standard instead: an unassigned weather
entity is ASSIGNED to the Home area once (a real registry write,
journaled, visible and movable in Pro). Option-gated (auto_home_weather,
default on). Pure selection lives here; benched.
"""
from __future__ import annotations


def unassigned_weather(entity_registry, device_areas):
    """weather.* entities with no area of their own AND none inherited
    from their device -> candidates for standard Home placement."""
    out = []
    for e in (entity_registry or []):
        eid = e.get("entity_id") or ""
        if not eid.startswith("weather."):
            continue
        if e.get("area_id"):
            continue
        if (device_areas or {}).get(e.get("device_id")):
            continue
        out.append(eid)
    return out
