"""THE PHYSICAL PATH, AS READINGS (register 450, 14 Sep 2026).

Dave, after the network change: devices sat offline; Fix and Assist could
not bring them back; he rebooted their switch ports in the controller's app
and they came back. Core knew the switch and port every one of those
devices hung off — the port reading on the awareness list — and the
platform's network integration exposes a control for every PoE port. Assist
was handed everything except that. This module is the join, as READINGS,
for Assist to reason over: which platform control drives a given port, and
why a port must not be switched off (it feeds other gear, carries more than
one device, or carries the box itself). No ladder lives here; nothing here
acts. Dave's ruling: Fix and auto-heal hand the fault to Assist, which
diagnoses and acts the way a Developer session with the platform's MCP does.
"""
from __future__ import annotations


def poe_control_for(client, gear_mac, port):
    """The platform's PoE port control (switch.*) for this switch port, or
    None. Matched by the integration's own unique id first
    (poe-<mac>_<port>), then by the gear's device and the port's own name —
    never by a name pattern across the house."""
    gm = str(gear_mac or "").lower().replace("-", ":")
    if not gm or port is None:
        return None
    try:
        ents = client.entity_registry() or []
    except Exception:  # noqa: BLE001
        return None
    want = "poe-%s_%s" % (gm, port)
    for e in ents:
        if (e.get("platform") == "unifi" and str(e.get("entity_id") or "").startswith("switch.")
                and str(e.get("unique_id") or "").lower() == want):
            return e
    try:
        devs = client.device_registry() or []
    except Exception:  # noqa: BLE001
        devs = []
    did = None
    for d in devs:
        for pair in (d.get("connections") or []):
            if isinstance(pair, (list, tuple)) and len(pair) > 1 and str(pair[1]).lower() == gm:
                did = d.get("id")
                break
        if did:
            break
    if not did:
        return None
    suffix = "_port_%s_poe" % port
    for e in ents:
        if e.get("platform") == "unifi" and e.get("device_id") == did:
            eid = str(e.get("entity_id") or "")
            nm = str(e.get("original_name") or e.get("name") or "").lower()
            if eid.startswith("switch.") and (eid.endswith(suffix) or nm == ("port %s poe" % port)):
                return e
    return None


def port_guard(devices, clients, gear_mac, port, box_ips=()):
    """Why this port must NOT be switched off, or None when it may be. Read
    from the controller's own tables: an uplink that feeds other gear, a port
    carrying more than one client, a port carrying the box itself."""
    gm = str(gear_mac or "").lower()
    try:
        p = int(port)
    except (TypeError, ValueError):
        return "no port number"
    for d in (devices or []):
        up = d.get("uplink") or {}
        if (str(up.get("uplink_mac") or "").lower() == gm
                and up.get("uplink_remote_port") is not None
                and int(up.get("uplink_remote_port")) == p):
            return "port %s feeds %s — other devices hang off it" % (p, d.get("name") or "other gear")
    on_port = [c for c in (clients or [])
               if str(c.get("sw_mac") or "").lower() == gm and c.get("sw_port") is not None
               and int(c.get("sw_port")) == p]
    if len(on_port) > 1:
        return "port %s carries %d devices" % (p, len(on_port))
    ips = {str(i) for i in (box_ips or []) if i}
    for c in on_port:
        if str(c.get("ip") or "") in ips:
            return "port %s carries this box" % p
    return None
