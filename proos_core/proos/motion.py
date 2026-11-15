"""MOTION THAT TELLS THE TRUTH — what the dashboard's moving pictures follow (register 1365, list 10.32; 15 Nov 2026).

Dave 21:36 chose eight of ten playable previews (docs/mockups/ProOS_Motion_Showcase_2026-11-15.html), 21:38 "Build them
all then I can test each one". Every movement on the glass follows a READING the page already receives from the platform
(a device's state); this module only says what each reading SHOULD become, so the page can tick a row when it does.
Nothing here decides state and nothing here is timed.

PLATFORM FIRST:
  · an activity is a platform script; what it should leave each device in is activitytest.expectations() — the same
    readings the room proof judges by (register 1340), not a second list
  · a scene is the platform's scene; what it sets each device to is the platform's own scene config
    (/api/config/scene/config/<id>, read with Core's token because a resident's sign-in may not read it)

Only ProOS adds: grouping those readings by DEVICE with the device's own name, so a person sees "Family Room TV ✓",
not three checks against one entity.
"""
from __future__ import annotations

# what a scene's stored attributes are allowed to be compared on — the state alone (a light set to 40% is "on"; the
# platform can round a level, so a level is never the test of whether the scene arrived)
SCENE_STATES = ("on", "off", "open", "closed", "locked", "unlocked", "playing", "paused", "idle",
                "heat", "cool", "heat_cool", "auto", "dry", "fan_only")


def _name(names, eid):
    try:
        n = (names or {}).get(eid)
    except Exception:                         # noqa: BLE001
        n = None
    return n or str(eid or "").split(".", 1)[-1].replace("_", " ").strip().title() or "A device"


def activity_rows(expectations, names=None):
    """expectations: activitytest.expectations() output. Returns one row per DEVICE, in the order the activity first
    touches it: {entity_id, name, checks:[{what, state_in|not_in|attr+equals}]}. The page ticks a row when every one
    of its checks reads true."""
    rows, by = [], {}
    for c in expectations or []:
        eid = c.get("entity_id")
        if not eid or c.get("verdict"):
            continue
        if eid not in by:
            by[eid] = {"entity_id": eid, "name": _name(names, eid), "checks": []}
            rows.append(by[eid])
        chk = {"what": c.get("what") or ""}
        for k in ("state_in", "not_in", "attr", "equals"):
            if k in c:
                chk[k] = c[k]
        by[eid]["checks"].append(chk)
    return rows


def scene_rows(cfg, names=None):
    """cfg: the platform's scene config ({name, entities: {entity_id: {state, ...}}}). Returns
    {name, entities:[{entity_id, name, state}]} — only entities whose stored state is a plain state the page can read
    back; anything else is left out and counted, never guessed."""
    cfg = cfg or {}
    ents, skipped = [], 0
    for eid, want in (cfg.get("entities") or {}).items():
        s = want.get("state") if isinstance(want, dict) else want
        s = str(s or "").strip().lower()
        if s in SCENE_STATES:
            ents.append({"entity_id": eid, "name": _name(names, eid), "state": s})
        else:
            skipped += 1
    return {"name": cfg.get("name") or "", "entities": ents, "not_read": skipped}
