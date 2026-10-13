"""PUTTING A DEVICE IN A ROOM, THE PLATFORM'S WAY — register 817 (Dave 13 Oct 08:43 "yes"; the read is 815).

READ IN THE PLATFORM'S 2026.10.1 SOURCE (helpers/entity_registry.py): an entity with NO NAME OF ITS OWN represents its device,
so its room belongs on the DEVICE. Such an entity carrying a room of its own gets a repair ("Entity <x> has an area but no name
of its own"), and any edit that sets a room or a name on it is REFUSED ("set the area on its device instead"). Clearing the
entity's own room is still allowed.

ProOS placed devices by writing the room on the entity — the "pin" the AV quarantine needed. That quarantine was retired on
4 Aug (project.py): the committed record is ProOS's truth and the platform's room is a courtesy written WITH the platform. So
the pin has no job left, and ProOS now writes the room where the platform wants it:
  · an entity that has no name of its own and belongs to a device → the DEVICE's room (and the entity's own cleared);
  · any other entity → its own room, as before;
  · and if the platform still refuses an entity's room in those words, the device road — the platform's own instruction.
"No name of its own" is the platform's test, read from its registry list: no user name, no original name (the list already
gives the original name with the device's name stripped, as the platform judges it), or a user name that is only the device's.
"""
from __future__ import annotations

REFUSAL = "set the area on its device instead"


def _device_names(client, device_id):
    try:
        for d in client.device_registry() or []:
            if d.get("id") == device_id:
                return [n for n in (d.get("name_by_user"), d.get("name")) if n]
    except Exception:                                            # noqa: BLE001
        pass
    return []


def has_own_name(entry, device_names=()):
    """The platform's test (async_get_unprefixed_name), on the registry list's own fields."""
    name = (entry.get("name") or "").strip()
    if name:
        if entry.get("next_name_part") == "device":
            for dn in device_names:
                if name.lower() == dn.strip().lower():
                    return False
                if name.lower().startswith(dn.strip().lower() + " "):
                    return bool(name[len(dn.strip()):].strip())
        return True
    return bool((entry.get("original_name") or "").strip())


def represents_device(client, entry):
    """True when this entity stands for its device in the platform's model — its room is the device's."""
    if not entry or not entry.get("device_id"):
        return False
    return not has_own_name(entry, _device_names(client, entry.get("device_id")))


def _entry(client, entity_id):
    for e in client.entity_registry() or []:
        if e.get("entity_id") == entity_id:
            return e
    return None


def _device_road(client, entry, area_id):
    client.set_device_area(entry.get("device_id"), area_id or None)
    if entry.get("area_id"):
        client.set_entity_area(entry.get("entity_id"), None)
    return {"ok": True, "road": "device", "device_id": entry.get("device_id"), "area_id": area_id or None}


def place(client, entity_id, area_id):
    """Put entity_id's device (or the entity itself) in area_id; None/"" takes it out of its room."""
    entry = _entry(client, entity_id)
    if entry and represents_device(client, entry):
        return _device_road(client, entry, area_id)
    try:
        client.set_entity_area(entity_id, area_id or None)
        return {"ok": True, "road": "entity", "area_id": area_id or None}
    except Exception as e:                                       # noqa: BLE001
        if REFUSAL in str(e) and entry and entry.get("device_id"):
            return _device_road(client, entry, area_id)
        raise


def tidy(client):
    """Every entity the platform flags (a room of its own, a device, no name of its own): its room moves to its device —
    the same room — and the entity's own is cleared. Nothing changes room. Returns what was done, for the Master Log."""
    done = []
    devs = {}
    try:
        for d in client.device_registry() or []:
            devs[d.get("id")] = d
    except Exception:                                            # noqa: BLE001
        return done
    for e in client.entity_registry() or []:
        dev = devs.get(e.get("device_id"))
        if not e.get("area_id") or not dev:
            continue
        names = [n for n in (dev.get("name_by_user"), dev.get("name")) if n]
        if has_own_name(e, names):
            continue
        row = {"entity": e.get("entity_id"), "room": e.get("area_id"), "device_had": dev.get("area_id")}
        try:
            if dev.get("area_id") != e.get("area_id"):
                client.set_device_area(dev.get("id"), e.get("area_id"))
            client.set_entity_area(e.get("entity_id"), None)
            row["ok"] = True
        except Exception as ex:                                  # noqa: BLE001
            row.update(ok=False, error=str(ex)[:160])
        done.append(row)
    return done
