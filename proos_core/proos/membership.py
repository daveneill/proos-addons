"""
ONE resolver for "what room is an entity in".

Membership = the HA area assignment (Dave, 5 Aug 2026): a device assigned to a
room IS in it — that is the hinge of the Monitored/Committed model. There is ONE
order — the entity's own area override first, then its device's area — and it
lives HERE so it can never drift across call sites again. The 5 Aug audit found
six hand-written copies of this expression; they now all call `area_of`.

Identity keys off ids (area_id, device_id), never names.

IMPORTANT — this is the ENTITY-then-device resolver. The watcher deliberately
uses the DEVICE area ONLY (a stale entity-level override once mis-filed cameras
into one room), so it does NOT use this helper. That is a different question and
its divergence is intentional; see watcher.py.

This module imports nothing, so importing it can never create a cycle.
"""


def area_of(ent, dev_area):
    """The area_id an entity belongs to: its own entity-level area override if
    set, else its device's area. `ent` is an entity-registry row (a dict with
    ``area_id`` and ``device_id``); `dev_area` maps device_id -> area_id.
    Returns None when neither level carries an area."""
    return ent.get("area_id") or dev_area.get(ent.get("device_id"))
