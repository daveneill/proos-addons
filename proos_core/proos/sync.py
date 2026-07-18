"""
ProOS Core -- whole-home sync.

ONE operation that provisions an entire home: walk every room, and for each that
has a display, materialise its activity scripts. It reuses the exact per-room path
the rest of Core uses -- discover_av() to read the room, then generator.generate()
to write its editable scripts -- so there is no second generator and no chance of
the whole-home pass drifting from the per-room one. The scripts ARE the activities;
HA owns them; an installer can edit them afterward.

This is the installer/app entry point. Point Core at a home and call sync_all():
every TV room gets its standard CEC/AVR script set in a single pass. Rooms with no
display are skipped (empty stays empty). create-if-absent + self-heal by default,
so re-running the sync refreshes UNEDITED scripts to the current room (a removed
source's step disappears, a new source's step appears) while never clobbering an
installer's edits; overwrite=True is the explicit force-regenerate.

Same five-line shape a per-room setup uses, just iterated over the whole house --
which is precisely how a new install or a "re-scan this home" action should behave.
"""
from __future__ import annotations
import json

from .discovery import discover_av
from . import generator
from .controller import COMMISSIONING


def list_areas(client) -> list[str]:
    """Every area NAME HA knows, in one server-side render. Names (not ids) because
    discover_av()/COMMISSIONING are keyed by the area name, same as the add-on config."""
    raw = client.render_template("{{ areas() | map('area_name') | list | to_json }}")
    try:
        names = json.loads(raw)
    except Exception:
        return []
    return [a for a in names if a]


def sync_room(client, area: str, overwrite: bool = False) -> dict:
    """Provision one room: discover it, and if it has a display, generate its
    activity scripts. Returns a per-room result row (see sync_all)."""
    try:
        cluster = discover_av(client, area)
    except Exception as e:
        return {"area": area, "display": None, "skipped": f"discovery failed: {e}"}
    if cluster is None or getattr(cluster, "display", None) is None:
        return {"area": area, "display": None, "skipped": "no display"}
    res = generator.generate(client, cluster, COMMISSIONING.get(area), overwrite=overwrite)
    return {
        "area": area,
        "display": cluster.display.entity,
        "created": res["created"],
        "kept": res["kept"],
        "refreshed": res.get("refreshed", []),
        "object_ids": res["object_ids"],
    }


def sync_all(client, overwrite: bool = False) -> dict:
    """Walk every room with a display and generate its activity scripts.

    overwrite=False (default): create-if-absent + self-heal -- missing scripts are
                    written, UNEDITED scripts are refreshed to the current room,
                    installer-edited scripts survive untouched. Safe to re-run.
    overwrite=True : force-regenerate every generated script in every room.

    Returns:
      {
        "rooms":   [ {area, display, created[], kept[], refreshed[], object_ids[]}, ... ],
        "skipped": [ {area, display, skipped:<reason>}, ... ],
        "totals":  {rooms, created, kept, refreshed},
      }
    """
    rooms, skipped = [], []
    created_total = kept_total = refreshed_total = 0
    for area in list_areas(client):
        row = sync_room(client, area, overwrite=overwrite)
        if "skipped" in row:
            skipped.append(row)
            continue
        created_total += len(row["created"])
        kept_total += len(row["kept"])
        refreshed_total += len(row.get("refreshed", []))
        rooms.append(row)
    return {
        "rooms": rooms,
        "skipped": skipped,
        "totals": {"rooms": len(rooms),
                   "created": created_total,
                   "kept": kept_total,
                   "refreshed": refreshed_total},
    }
