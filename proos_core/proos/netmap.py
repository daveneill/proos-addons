"""
Automatic device reachability harvesting.

The two-signal awareness verdict needs an independent liveness signal per device.
Rather than hand-configure one, Core derives each device's IP from Home
Assistant's own on-disk stores (granted by the homeassistant_config map) and
builds a reachability map keyed by entity_id -> {"ip": ...}. That IP is then
TCP-probed straight from Core (reachability.tcp_reachable), so NO HA ping sensor
need exist. Adding a device to HA gives it a second signal with zero ProOS
configuration -- which is the whole point.

Where the IP comes from, in priority order:
  1. The device's config-entry `data` (e.g. apple_tv `address`, androidtv_remote
     / unifiprotect `host`) -- read from core.config_entries.
  2. The device registry's `configuration_url` (e.g. Sonos, discovered by SSDP
     with an empty entry `data`, still exposes http://<ip>:1400/... here).
  3. A general IPv4 scan of the entry data, so integrations not named below still
     resolve whenever an IP is actually present.

Pure stdlib. If the stores can't be read (map not granted, file mid-write), it
returns {} -- callers treat an absent signal as simply single-signal, never an
error. Boot is never blocked by this.
"""
from __future__ import annotations
import json
import os
import re

_STORAGE = "/homeassistant/.storage"

# Config-entry `data` keys that tend to hold the device host/IP.
_HOST_KEYS = ("host", "address", "ip", "ip_address", "hostname")

# A port each integration is likely to answer on. This is a *reliability* hint
# only: tcp_reachable() counts a refused connection as reachable too, so an exact
# port isn't required -- hitting an open one just makes a live device
# unmistakable and avoids a stealth-firewalled port reading as a timeout.
_PROBE_PORT = {
    "apple_tv": 7000, "androidtv_remote": 6466, "firetv": 5555,
    "sonos": 1400, "heos": 1255, "cast": 8009, "roku": 8060,
    "webostv": 3000, "samsungtv": 8001, "samsungtv_smart": 8001,
    "bravia_tv": 80, "philips_js": 1925, "unifiprotect": 443, "kodi": 8080,
}

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")


def _valid_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        octs = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octs):
        return False
    # drop unroutable / meaningless probe targets
    if octs[0] in (0, 127) or s == "0.0.0.0":
        return False
    return True


def _ip_in(value) -> str | None:
    """First real IPv4 inside a string (plain, host:port, or a URL)."""
    if not isinstance(value, str) or not value:
        return None
    m = _IPV4.search(value)
    if m and _valid_ipv4(m.group(1)):
        return m.group(1)
    return None


def _host_from_data(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    for k in _HOST_KEYS:
        ip = _ip_in(str(data.get(k, "")))
        if ip:
            return ip
    for v in data.values():                      # last resort: any IPv4 anywhere
        ip = _ip_in(v if isinstance(v, str) else "")
        if ip:
            return ip
    return None


def _read(base: str, name: str) -> dict:
    try:
        with open(os.path.join(base, name)) as f:
            return json.load(f).get("data", {}) or {}
    except Exception:
        return {}


def _norm_mac(v) -> str | None:
    """12 lowercase hex chars, separators stripped -- so aa:BB-cc.. all match."""
    if not isinstance(v, str):
        return None
    h = "".join(c for c in v.lower() if c in "0123456789abcdef")
    return h if len(h) == 12 else None


def _augment_from_unifi(out: dict, entities: list, devices: list,
                        entry_domain: dict, client) -> int:
    """Overlay precise per-device IPs from the UniFi Network integration.

    UniFi Network is the DHCP/controller authority, so it knows every client's
    real IP keyed by MAC -- including each camera individually (not hidden behind
    the Protect NVR). We read its device_tracker entities' live `ip`/`mac`
    attributes, then match those MACs to each HA device's registry `connections`.
    A hit gives that device's entities their true per-device IP, overriding the
    coarser config-entry value. No-op if UniFi isn't present or tracks nothing.
    """
    trackers = [e["entity_id"] for e in entities
                if isinstance(e.get("entity_id"), str)
                and e["entity_id"].startswith("device_tracker.")
                and entry_domain.get(e.get("config_entry_id")) == "unifi"
                and not e.get("disabled_by")]
    if not trackers or client is None:
        return 0
    try:
        snap = client.snapshot(trackers)
    except Exception:
        return 0

    mac_ip: dict[str, str] = {}
    for rec in snap.values():
        a = (rec or {}).get("attributes", {}) or {}
        ip = _ip_in(str(a.get("ip") or a.get("ip_address") or ""))
        mac = _norm_mac(a.get("mac") or a.get("mac_address"))
        if ip and mac:
            mac_ip[mac] = ip
    if not mac_ip:
        return 0

    dev_macs: dict[str, set] = {}
    for d in devices:
        did = d.get("id")
        if not did:
            continue
        macs = set()
        for pair in (d.get("connections") or []):
            if (isinstance(pair, (list, tuple)) and len(pair) > 1
                    and str(pair[0]).lower() in ("mac", "mac_address")):
                m = _norm_mac(pair[1])
                if m:
                    macs.add(m)
        if macs:
            dev_macs[did] = macs

    hits = 0
    for ent in entities:
        eid = ent.get("entity_id")
        did = ent.get("device_id")
        if not eid or ent.get("disabled_by") or not did:
            continue
        for m in dev_macs.get(did, ()):
            if m in mac_ip:
                dom = entry_domain.get(ent.get("config_entry_id"))
                spec = {"ip": mac_ip[m], "via": "unifi"}
                if dom:
                    spec["domain"] = dom
                    if dom in _PROBE_PORT:
                        spec["port"] = _PROBE_PORT[dom]
                out[eid] = spec        # UniFi per-device IP wins over the entry value
                hits += 1
                break
    return hits


def harvest(storage_dir: str = _STORAGE, client=None) -> dict:
    """Return {entity_id: {"ip", "port"?, "domain"?, "via"}} for every entity
    whose device IP could be derived. Safe/empty on any read failure.

    When a `client` is supplied, UniFi Network's live per-device IPs are overlaid
    on top (via MAC match), which is both more precise and per-camera-accurate."""
    entries = _read(storage_dir, "core.config_entries").get("entries", []) or []
    devices = _read(storage_dir, "core.device_registry").get("devices", []) or []
    entities = _read(storage_dir, "core.entity_registry").get("entities", []) or []

    entry_ip: dict[str, str] = {}
    entry_domain: dict[str, str] = {}
    for e in entries:
        eid = e.get("entry_id")
        if not eid:
            continue
        entry_domain[eid] = e.get("domain")
        ip = _host_from_data(e.get("data") or {})
        if ip:
            entry_ip[eid] = ip

    dev_ip: dict[str, str] = {}
    dev_entry: dict[str, str] = {}
    for d in devices:
        did = d.get("id")
        if not did:
            continue
        ces = d.get("config_entries") or []
        if ces:
            dev_entry[did] = ces[0]
        ip = _ip_in(d.get("configuration_url") or "")
        if not ip:
            for pair in (d.get("connections") or []):
                ip = _ip_in(pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else "")
                if ip:
                    break
        if ip:
            dev_ip[did] = ip

    out: dict[str, dict] = {}
    for ent in entities:
        eid = ent.get("entity_id")
        if not eid or ent.get("disabled_by"):
            continue
        ce = ent.get("config_entry_id")
        did = ent.get("device_id")
        ip = via = None
        dom = entry_domain.get(ce)
        if ce and ce in entry_ip:
            ip, via = entry_ip[ce], "config_entry"
        elif did and did in dev_ip:
            ip, via = dev_ip[did], "device_url"
            dom = dom or entry_domain.get(dev_entry.get(did))
        if not ip:
            continue
        spec = {"ip": ip, "via": via}
        if dom:
            spec["domain"] = dom
            if dom in _PROBE_PORT:
                spec["port"] = _PROBE_PORT[dom]
        out[eid] = spec

    # Overlay UniFi Network's precise per-device IPs (per-camera accurate).
    try:
        _augment_from_unifi(out, entities, devices, entry_domain, client)
    except Exception:
        pass
    return out
