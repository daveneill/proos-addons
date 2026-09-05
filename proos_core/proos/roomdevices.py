"""
ProOS Core -- per-room NON-AV device commissioning (Pro Assist era).

The AV project (project.py) commits displays/sources/speakers for activities.
This is the OTHER half a room needs so the assistant can compose scenes and
control the space like Josh AI: the lights, shades, climate, fans, switches and
locks that physically live in the room.

Model (matches the installer's chosen behaviour: auto-list + confirm, with
optional per-device roles):
  * Core AUTO-DISCOVERS every controllable non-AV entity assigned to the area
    (entity area override, else its device's area) from the live registry.
  * By default every discovered device is AVAILABLE to the assistant.
  * The installer can EXCLUDE a device (assistant won't see or touch it) and/or
    give it a ROLE label (e.g. 'main') so the assistant picks the right one.
  * A device has ONE NAME (Dave, 5 Sep 2026, register 328: "I just changed the
    names of the lights in the room page and it does not change the actual
    name — this needs to be the same and editable from both places"). Until
    then the room page kept a private "assistant name" in this overlay, so the
    room page, the device page and the platform each showed a different name
    for the same light. The overlay no longer holds names. rename() writes the
    entity's registry name, and — when the device has exactly one primary
    entity (a bulb, a plug, a blind) — the device's user name too, so every
    surface reads the same word. rename_device() is the same rule from the
    device side. A name left in an older overlay is moved into the registry
    the first time the room is read, then dropped.

The overlay (exclude / power_exclude / awareness_exclude / role) is keyed by
IMMUTABLE ids (area_id, entity_id) per the Identity Architecture Standard.
Store: /data/room_devices.json. Cleared by factory reset.
"""
from __future__ import annotations
import json
import os

from .membership import area_of

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "room_devices.json")

# Controllable, non-AV domains a room scene / assistant would touch. media_player
# is deliberately excluded — AV power/routing is the committed-activity's job.
CONTROLLABLE_DOMAINS = ("light", "switch", "cover", "climate", "fan",
                        "lock", "humidifier", "vacuum")


# ── capabilities ──────────────────────────────────────────────────────────────

def light_caps(attrs: dict) -> dict:
    """What a light can do, from supported_color_modes. A light whose ONLY mode
    is onoff can't dim — so the assistant must not promise brightness."""
    modes = [str(m).lower() for m in (attrs.get("supported_color_modes") or [])]
    dimmable = any(m not in ("onoff", "unknown") for m in modes) if modes else False
    color = any(m in ("hs", "rgb", "rgbw", "rgbww", "xy") for m in modes)
    return {"dimmable": dimmable, "color": color, "color_temp": "color_temp" in modes}


def _caps_for(domain: str, attrs: dict) -> dict:
    if domain == "light":
        return light_caps(attrs)
    if domain == "cover":
        return {"position": attrs.get("current_position") is not None}
    if domain == "climate":
        return {"hvac_modes": attrs.get("hvac_modes") or []}
    return {}


# ── store ─────────────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _STORE)


def area_overlay(area_id: str) -> dict:
    """Per-entity overrides for an area: {entity_id: {excluded, role, name}}."""
    return (load().get(area_id) or {}) if area_id else {}


def set_devices(area_id: str, updates: list, client=None) -> dict:
    """Apply a list of {entity_id, excluded?, role?, name?} to an area. A field
    left out is unchanged; setting role to '' clears it. A `name` is NOT an
    overlay field any more (register 328): it goes to the registry through
    rename(), when a client is given, so the name is the device's real name.
    Returns {ok, area_id, overlay, renamed:[…]}; a rename that the platform
    refused is reported in `errors` rather than silently kept."""
    area_id = (area_id or "").strip()
    if not area_id:
        return {"error": "area_id required"}
    d = load()
    area = dict(d.get(area_id) or {})
    renamed, errors = [], []
    for u in (updates or []):
        eid = (u.get("entity_id") or "").strip()
        if "." not in eid:
            continue
        rec = dict(area.get(eid) or {})
        rec.pop("name", None)          # never stored here again
        if "name" in u:
            if client is None:
                errors.append({"entity_id": eid, "error": "rename needs the platform"})
            else:
                r = rename(client, eid, u.get("name"))
                (errors if r.get("error") else renamed).append(r)
        if "excluded" in u:
            rec["excluded"] = bool(u.get("excluded"))
        if "power_exclude" in u:
            # Second axis, deliberately separate from `excluded` (spec, Dave
            # 1 Aug): the device stays fully assist-ACCESSIBLE, but bulk
            # room-off/room-on never touches it — equipment plugs that must
            # stay powered unless it's a deliberate recovery.
            rec["power_exclude"] = bool(u.get("power_exclude"))
        if "awareness_exclude" in u:
            # Third axis (Dave, 9 Aug): the device stays in the room, stays
            # controllable, but the AWARENESS layer neither watches nor counts
            # it — for gear the class rules can't decide: a lamp that lives on
            # a wall switch, seasonal equipment, a guest-room plug. Honesty
            # rule: an excluded device LEAVES the watched/two-signal counts and
            # is reported as "excluded by installer" — never silently absent.
            rec["awareness_exclude"] = bool(u.get("awareness_exclude"))
        if "role" in u:
            r = (u.get("role") or "").strip()
            rec["role"] = r if r else None
        rec = {k: v for k, v in rec.items() if v not in (None, "")}
        if rec:
            area[eid] = rec
        else:
            area.pop(eid, None)
    if area:
        d[area_id] = area
    else:
        d.pop(area_id, None)
    _write(d)
    out = {"ok": True, "area_id": area_id, "overlay": area, "renamed": renamed}
    if errors:
        out["errors"] = errors
    return out


# ── one name (register 328) ──────────────────────────────────────────────────

def _primary_entities(ents: list, device_id) -> list:
    """The entities that ARE the device to a person: not config/diagnostic,
    not disabled. A bulb has one (its light); an AVR has several."""
    if not device_id:
        return []
    return [e for e in (ents or [])
            if e.get("device_id") == device_id
            and e.get("entity_category") not in ("config", "diagnostic")
            and not e.get("disabled_by")]


def rename(client, entity_id: str, name) -> dict:
    """Give an entity its name — the SAME name everywhere. Writes the entity's
    registry name; when its device has exactly one primary entity, writes the
    device's user name too, so the room page, the device page under Systems ›
    Integrations, and the platform all read one word. Empty name clears both
    (the device falls back to the name it was paired with). Brand-agnostic:
    the rule is about how many things a device is, not who made it."""
    eid = (entity_id or "").strip()
    name = (name or "").strip() or None
    if "." not in eid:
        return {"error": "entity_id required"}
    try:
        ents = client.entity_registry() or []
    except Exception as e:  # noqa: BLE001
        return {"error": "registry unavailable: %s" % e}
    ent = next((e for e in ents if e.get("entity_id") == eid), None)
    if not ent:
        return {"error": "unknown entity %s" % eid}
    try:
        client.set_entity_name(eid, name)
    except Exception as e:  # noqa: BLE001
        return {"error": "rename refused: %s" % e, "entity_id": eid}
    out = {"ok": True, "entity_id": eid, "name": name, "device_id": ent.get("device_id"),
           "device_renamed": False}
    prim = _primary_entities(ents, ent.get("device_id"))
    if len(prim) == 1 and prim[0].get("entity_id") == eid:
        try:
            client.set_device_name(ent.get("device_id"), name)
            out["device_renamed"] = True
        except Exception as e:  # noqa: BLE001
            out["device_error"] = str(e)
    return out


def rename_device(client, device_id: str, name) -> dict:
    """The same rule from the device side: the device's user name changes, and
    if the device is one primary entity, that entity's name follows — so a
    bulb renamed under Systems › Integrations reads the same on its room page."""
    did = (device_id or "").strip()
    name = (name or "").strip() or None
    if not did:
        return {"error": "device_id required"}
    try:
        client.set_device_name(did, name)
    except Exception as e:  # noqa: BLE001
        return {"error": "rename refused: %s" % e, "device_id": did}
    out = {"ok": True, "device_id": did, "name": name, "entity_renamed": None}
    try:
        ents = client.entity_registry() or []
    except Exception:
        ents = []
    prim = _primary_entities(ents, did)
    if len(prim) == 1:
        eid = prim[0].get("entity_id")
        try:
            client.set_entity_name(eid, name)
            out["entity_renamed"] = eid
        except Exception as e:  # noqa: BLE001
            out["entity_error"] = str(e)
    return out


def _migrate_names(client, area_id: str, overlay: dict) -> dict:
    """An older overlay may still carry names the installer typed on the room
    page. They were real intent — move each into the registry once (through
    rename, so the device follows), then drop it from the overlay. Returns
    the overlay without names."""
    if not any("name" in (v or {}) for v in overlay.values()):
        return overlay
    d = load()
    area = dict(d.get(area_id) or {})
    for eid, rec in list(area.items()):
        if "name" not in (rec or {}):
            continue
        n = rec.get("name")
        rec = dict(rec)
        if n and client is not None:
            r = rename(client, eid, n)
            if r.get("error"):
                continue           # keep it for next time; the platform was away
        rec.pop("name", None)
        if rec:
            area[eid] = rec
        else:
            area.pop(eid, None)
    if area:
        d[area_id] = area
    else:
        d.pop(area_id, None)
    _write(d)
    return area


def awareness_excluded() -> set:
    """Entity ids the installer excluded from awareness, across all areas.
    Fail-open: unreadable store == nothing excluded (never silently widen)."""
    out = set()
    try:
        for area in (load() or {}).values():
            for eid, rec in (area or {}).items():
                if isinstance(rec, dict) and rec.get("awareness_exclude"):
                    out.add(eid)
    except Exception:
        return set()
    return out


def clear() -> None:
    try:
        if os.path.exists(_STORE):
            os.remove(_STORE)
    except Exception:
        pass


# ── discovery (live registry) ─────────────────────────────────────────────────

def _entities_in_area(client, area_id: str) -> list:
    """Controllable non-AV entity-registry records assigned to the area, each
    joined to its device (name, integration). config/diagnostic entities (PoE
    ports, device-setting switches, etc.) are NOT room controls — dropped so the
    list stays meaningful. Returns dicts with the identifying detail the areas
    page needs to tell same-named devices apart."""
    try:
        devs = {d.get("id"): d for d in (client.device_registry() or [])}
        dev_areas = {i: (d or {}).get("area_id") for i, d in devs.items()}
        ents = client.entity_registry() or []
    except Exception:
        return []
    out = []
    for e in ents:
        eid = e.get("entity_id") or ""
        if "." not in eid:
            continue
        if eid.split(".", 1)[0] not in CONTROLLABLE_DOMAINS:
            continue
        if e.get("disabled_by") or e.get("hidden_by"):
            continue
        # config/diagnostic = not a room control (device settings, PoE ports…).
        if e.get("entity_category") in ("config", "diagnostic"):
            continue
        dev = devs.get(e.get("device_id")) or {}
        ea = area_of(e, dev_areas)
        if ea != area_id:
            continue
        dev_name = dev.get("name_by_user") or dev.get("name") or ""
        out.append({
            "entity_id": eid,
            "platform": e.get("platform") or "",
            "device_id": e.get("device_id"),
            "device_name": dev_name,
            "reg_name": e.get("name") or e.get("original_name") or "",
        })
    out.sort(key=lambda r: r["entity_id"])
    return out


def discover(client, area_id: str) -> dict:
    """Full picture for the areas page / assistant: every controllable non-AV
    device in the room, with identifying detail, live state, capabilities, and
    the installer's overlay (excluded / role / name)."""
    area_id = (area_id or "").strip()
    if not area_id:
        return {"area_id": area_id, "devices": []}
    overlay = _migrate_names(client, area_id, area_overlay(area_id))
    recs = _entities_in_area(client, area_id)
    eids = [r["entity_id"] for r in recs]
    try:
        snap = client.snapshot(eids) or {} if eids else {}
    except Exception:
        snap = {}
    devices = []
    for r in recs:
        eid = r["entity_id"]
        sv = snap.get(eid)
        st = sv if isinstance(sv, dict) else (getattr(sv, "__dict__", {}) or {})
        attrs = (st or {}).get("attributes") or {}
        ov = overlay.get(eid) or {}
        domain = eid.split(".", 1)[0]
        fn = attrs.get("friendly_name") or ""
        # Display name: the registry's own — entity name → device + entity →
        # friendly_name → the object_id. ONE name (register 328): no overlay
        # name sits in front of it any more, so this is what the device page
        # and the platform show too. Rows still differ when several share one
        # friendly_name.
        obj = eid.split(".", 1)[1]
        auto = r["reg_name"] or fn or obj.replace("_", " ")
        if r["device_name"] and r["device_name"].lower() not in (auto or "").lower():
            auto = "%s — %s" % (r["device_name"], auto) if r["reg_name"] else r["device_name"]
        devices.append({
            "entity_id": eid,
            "domain": domain,
            "integration": r["platform"],
            "device_name": r["device_name"],
            "ha_name": fn or eid,
            "name": auto or eid,
            "role": ov.get("role"),
            "excluded": bool(ov.get("excluded")),
            "power_exclude": bool(ov.get("power_exclude")),
            "awareness_exclude": bool(ov.get("awareness_exclude")),
            "state": (st or {}).get("state"),
            "offline": (st or {}).get("state") == "unavailable",
            "caps": _caps_for(domain, attrs),
        })
    return {"area_id": area_id, "devices": devices}


def available(client, area_id: str) -> list:
    """What the ASSISTANT may use in a room: discovered devices minus excluded,
    each with role/name/caps. Access follows the installer's list."""
    return [d for d in discover(client, area_id).get("devices", []) if not d["excluded"]]


# ── bulk room power (assist room_off / room_on) ──────────────────────────────
_POWER_DOMAINS = ("light", "switch", "fan")


def power_targets(devices) -> dict:
    """Bulk-power targets for the assistant's room_off/room_on, from a
    discover() device list: {"targets": {domain: [entity_ids]}, "skipped": n}.

    Pure; benched (tests/room_power_bench.py). Skips `excluded` (hidden from
    the assistant entirely) AND `power_exclude` (spec, Dave 1 Aug: the device
    stays fully assist-accessible one-by-one, but bulk room off/on never
    touches it — equipment plugs that must stay powered unless it's a
    deliberate recovery). `skipped` counts power-protected devices so the
    assistant can SAY what it deliberately left alone."""
    targets, skipped = {}, 0
    for d in (devices or []):
        if not isinstance(d, dict):
            continue
        dom = d.get("domain")
        eid = d.get("entity_id")
        if dom not in _POWER_DOMAINS or not eid:
            continue
        if d.get("excluded"):
            continue
        if d.get("power_exclude"):
            skipped += 1
            continue
        targets.setdefault(dom, []).append(eid)
    return {"targets": targets, "skipped": skipped}
