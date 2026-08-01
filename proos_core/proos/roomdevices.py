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
    give it a ROLE label + friendly name (e.g. role 'main' name 'Office Main')
    so the assistant speaks about it naturally and picks the right one.

Nothing here changes HA — it's an overlay keyed by IMMUTABLE ids (area_id,
entity_id) per the Identity Architecture Standard. Store: /data/room_devices.json
Cleared by factory reset.
"""
from __future__ import annotations
import json
import os

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


def set_devices(area_id: str, updates: list) -> dict:
    """Apply a list of {entity_id, excluded?, role?, name?} to an area. A field
    left out is unchanged; setting role/name to '' clears it."""
    area_id = (area_id or "").strip()
    if not area_id:
        return {"error": "area_id required"}
    d = load()
    area = dict(d.get(area_id) or {})
    for u in (updates or []):
        eid = (u.get("entity_id") or "").strip()
        if "." not in eid:
            continue
        rec = dict(area.get(eid) or {})
        if "excluded" in u:
            rec["excluded"] = bool(u.get("excluded"))
        if "power_exclude" in u:
            # Second axis, deliberately separate from `excluded` (spec, Dave
            # 1 Aug): the device stays fully assist-ACCESSIBLE, but bulk
            # room-off/room-on never touches it — equipment plugs that must
            # stay powered unless it's a deliberate recovery.
            rec["power_exclude"] = bool(u.get("power_exclude"))
        if "role" in u:
            r = (u.get("role") or "").strip()
            rec["role"] = r if r else None
        if "name" in u:
            n = (u.get("name") or "").strip()
            rec["name"] = n if n else None
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
    return {"ok": True, "area_id": area_id, "overlay": area}


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
        dev_area = dev.get("area_id")
        ea = e.get("area_id") or dev_area
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
    recs = _entities_in_area(client, area_id)
    eids = [r["entity_id"] for r in recs]
    overlay = area_overlay(area_id)
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
        # Best display name: installer override → registry entity name → device
        # + entity → friendly_name → the object_id. Guarantees rows differ even
        # when several share one friendly_name.
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
            "name": ov.get("name") or auto or eid,
            "role": ov.get("role"),
            "excluded": bool(ov.get("excluded")),
            "power_exclude": bool(ov.get("power_exclude")),
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
