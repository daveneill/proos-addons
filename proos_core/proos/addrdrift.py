"""AN INTEGRATION TALKING TO AN ADDRESS THE DEVICE NO LONGER HAS — register 1201 (3 Nov 2026).

Dave 21:21: the Bedroom TV stopped responding because its address changed on the network; "Why was this not able to see
that the IP address that it was getting on the Unifi Network was different to the one that was in the config setup???"
21:27: "yes but this needs to support all brands of TVs and in theory any integration that relys on an IP address for
control and support that if a home does not have network controller integration then this is obviously not available".

Two READINGS compared — nothing guessed (Rule Zero):
  * the address an integration was SET UP with — the platform's own record of that integration (its config entry's data
    and options: host / ip_address / ip / address / url …), an IPv4 literal only;
  * (an integration with many devices on MACs — a network controller, a hub — is compared only through a MAC it stored
    itself: which of its devices the address belongs to cannot be known)
  * the address the NETWORK sees right now for the same device — the network controller's client list, as the platform
    publishes it (a router-sourced device_tracker carrying `mac` and `ip`, state `home`), matched by the device's MAC
    from the platform's device registry (or a MAC the integration itself stored).
They differ → the integration is talking to an address the device no longer has.

Brand- and integration-agnostic: any integration whose record carries an address, any device with a MAC. Says NOTHING
when it cannot know: no network controller in the home (no such client list at all — `available` False), a device the
network does not see right now (a TV in standby is off the network by design — Dave, 3 Nov 09:05), a device with no
MAC on record, or an integration set up by name rather than by address (a name cannot be compared without asking DNS).
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

# Where integrations keep the address they talk to (plumbing: the platform's own field names across integrations).
ADDRESS_KEYS = ("host", "ip_address", "ip", "address", "host_ip", "url", "base_url", "ws_url")
MAC_KEYS = ("mac", "mac_address", "macaddress")
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$")


def _ipv4(v) -> str | None:
    """The IPv4 literal in a stored value (a bare address, "host:port" or a URL), else None."""
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if "://" in s:
        s = urlparse(s).hostname or ""
    elif s.count(":") == 1:
        s = s.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


def norm_mac(v) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if not _MAC_RE.match(s):
        return None
    hexs = re.sub(r"[^0-9a-f]", "", s)
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))


def configured_address(entry: dict) -> str | None:
    """The IPv4 address an integration was set up to talk to, from its own record (options win over data)."""
    for src in ((entry or {}).get("options") or {}, (entry or {}).get("data") or {}):
        for k in ADDRESS_KEYS:
            ip = _ipv4(src.get(k)) if isinstance(src, dict) else None
            if ip:
                return ip
    return None


def network_clients(states: list) -> tuple[bool, dict]:
    """(available, {mac: {"ip", "entity", "name"}}) from the network controller's client list as the platform publishes
    it. `available` is False when the home has no network controller integration at all — then nothing is said."""
    available, seen = False, {}
    for s in states or []:
        eid = s.get("entity_id") or ""
        if not eid.startswith("device_tracker."):
            continue
        a = s.get("attributes") or {}
        mac = norm_mac(a.get("mac"))
        if not mac or a.get("source_type") != "router":
            continue
        available = True
        ip = _ipv4(a.get("ip"))
        if s.get("state") == "home" and ip:
            seen[mac] = {"ip": ip, "entity": eid, "name": a.get("friendly_name") or eid}
    return available, seen


def drifts(entries: list, devices: list, states: list) -> dict:
    """{"available": bool, "drifts": [...]} — every integration talking to an address its device no longer has.

    Each drift: entry_id, domain, title, device (name), mac, configured, seen, tracker."""
    available, seen = network_clients(states)
    out = []
    if not available:
        return {"available": False, "drifts": out}
    by_entry: dict = {}
    for d in devices or []:
        macs = [norm_mac(c[1]) for c in (d.get("connections") or []) if isinstance(c, (list, tuple)) and len(c) == 2
                and str(c[0]).lower() == "mac"]
        for eid in d.get("config_entries") or []:
            by_entry.setdefault(eid, []).append((d, [m for m in macs if m]))
    for e in entries or []:
        if (e.get("disabled_by") or e.get("source") == "ignore"):
            continue
        cfg = configured_address(e)
        if not cfg:
            continue
        stored = [norm_mac((e.get(k) or {}).get(m)) for k in ("data", "options") for m in MAC_KEYS
                  if isinstance(e.get(k), dict)]
        # A hub (a network controller, a bridge with many devices of its own) has many devices with MACs: which one the
        # address belongs to cannot be known, so only an integration with ONE device on a MAC is compared — unless the
        # integration stored the MAC itself.
        mine = [x for x in by_entry.get(e.get("entry_id"), []) if x[1]]
        one = len({m for x in mine for m in x[1]}) == 1          # one device's MAC, however many records share it
        cands = [(None, [m for m in stored if m])] + (mine[:1] if one else [])
        for dev, macs in cands:
            hit = next((m for m in macs if m in seen), None)
            if not hit:
                continue
            now_ip = seen[hit]["ip"]
            if now_ip != cfg:
                name = ((dev or {}).get("name_by_user") or (dev or {}).get("name") or e.get("title") or e.get("domain"))
                out.append({"entry_id": e.get("entry_id"), "domain": e.get("domain"), "title": e.get("title") or "",
                            "device": name, "mac": hit, "configured": cfg, "seen": now_ip,
                            "tracker": seen[hit]["entity"]})
            break
    return {"available": True, "drifts": out}


def incident(d: dict) -> dict:
    """Health's card for one drift — what is wrong, what it reads, and the fix in the trade's words."""
    nm = d.get("device") or d.get("title") or d.get("domain")
    return {"kind": "address_drift", "room": "Network", "slug": "network", "severity": "warning",
            "title": "%s — Its Integration Has the Wrong Address" % nm,
            "cause": ("%s's integration (%s) is set up to talk to %s, but the network sees this device (MAC %s) at %s "
                      "right now — so commands to it go nowhere. Reserve its address in the network controller, or "
                      "update the integration to %s (Settings › the integration › Reconfigure)."
                      % (nm, d.get("title") or d.get("domain"), d.get("configured"), d.get("mac"), d.get("seen"),
                         d.get("seen"))),
            "subject": "entry:%s" % d.get("entry_id"), "actions": []}
