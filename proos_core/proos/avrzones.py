"""
ProOS Core -- AVR zone detection (brand-agnostic).

An AVR with a second amplified zone (Zone 2/3) can drive ANOTHER room's audio.
HA integrations model a zone as a SEPARATE media_player entity on the SAME HA
device as the main zone (denonavr, yamaha, onkyo, heos, arcam ... all group this
way). So the detection keys off HA's own device grouping, never off a brand name.

A device may also expose a streaming sub-player (HEOS / AirPlay) on the same
device — that is NOT an amplified zone. We can't tell them apart by name, so the
signal is the SOURCE LIST: a real zone shares the AVR's inputs (it switches the
same sources); a streaming sub-player has its own (or none). ProOS offers the
likely zones; the installer confirms (design 5 Aug: detect + guide).
"""
from __future__ import annotations


def _source_list(client, eid: str) -> list:
    try:
        st = client._req("GET", "/api/states/%s" % eid) or {}
        sl = (st.get("attributes") or {}).get("source_list")
        return [s for s in (sl or []) if isinstance(s, str)]
    except Exception:
        return []


def _name(client, eid: str) -> str:
    try:
        st = client._req("GET", "/api/states/%s" % eid) or {}
        return (st.get("attributes") or {}).get("friendly_name") or eid
    except Exception:
        return eid


def _device_of(reg, eid: str):
    for e in (reg or []):
        if e.get("entity_id") == eid:
            return e.get("device_id")
    return None


def zones_of(client, avswitch_eid: str) -> dict:
    """The AVR's sibling media_player zones — same HA device as the committed
    AV-switch, excluding the main zone. Each: {entity, name, has_source_list,
    likely_zone}. `likely_zone` is True when it shares an input with the main zone
    (a real amplified zone) rather than being a streaming sub-player."""
    if not avswitch_eid:
        return {"avr": avswitch_eid, "device": None, "zones": []}
    try:
        reg = client.entity_registry() or []
    except Exception:
        reg = []
    dev = _device_of(reg, avswitch_eid)
    if not dev:
        return {"avr": avswitch_eid, "device": None, "zones": []}
    sibs = [e.get("entity_id") for e in reg
            if e.get("device_id") == dev
            and str(e.get("entity_id") or "").startswith("media_player.")
            and e.get("entity_id") != avswitch_eid]
    main_sl = set(_source_list(client, avswitch_eid))
    zones = []
    for eid in sorted(set(sibs)):
        sl = _source_list(client, eid)
        shares = bool(set(sl) & main_sl) if main_sl else bool(sl)
        zones.append({"entity": eid, "name": _name(client, eid),
                      "has_source_list": bool(sl), "likely_zone": bool(shares)})
    return {"avr": avswitch_eid, "device": dev, "zones": zones}


def has_bindable_zone(client, avswitch_eid: str) -> bool:
    """True if the AVR exposes at least one likely amplified zone to bind into
    another room. When False, the installer is guided to enable Zone 2 in the AVR
    integration's options (ProOS can't do that itself — design 5 Aug: detect + guide)."""
    return any(z.get("likely_zone") for z in zones_of(client, avswitch_eid).get("zones", []))
