"""
ProOS Core -- UniFi Network controller client (PoE port auto-map).

Home Assistant exposes UniFi PoE port switches but NOT which port a client is on (the tracker
lacks it; diagnostics redact it). The only source is the controller itself. This client logs
into the UniFi OS console (its own credential entry, separate from Protect) and reads the active
client list -- each client carries sw_mac + sw_port -- then maps that to the HA PoE port switch
entity so the recovery editor can auto-suggest the port.
"""
from __future__ import annotations
import json
import re
import ssl
import threading
import time
import urllib.request
import urllib.error

from . import reach as _reach
from . import platform_entries as _pe

# register 437: one memory of "the controller is not answering" for every
# caller, so a dead address costs the box ONE short timeout a minute, never
# 15 s per call behind a lock
_REACH = _reach.Backoff("UniFi Network controller")

SERVICE = "unifi_network"            # the name Core's stores used for this system (register 439: nothing is stored under it now)
_PROTECT_SERVICE = "unifi_protect"   # one local UniFi user serves both — reuse it
# REGISTER 439: the controller's address and login are the UniFi Network
# integration's own, configured in the integration and read from ITS entry
# every call. When the Network integration is not set up, the UniFi Protect
# integration's entry serves (one local user serves both, as before). Core
# keeps no copy of any of it.
_DOMAIN = "unifi"
_PROTECT_DOMAIN = "unifiprotect"

_ssl_unverified = ssl.create_default_context()
_ssl_unverified.check_hostname = False
_ssl_unverified.verify_mode = ssl.CERT_NONE


class NetError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class UniFiNetClient:
    def __init__(self, creds):
        self._creds = creds
        self._session = None
        self._lock = threading.Lock()

    def _conf(self):
        """(host, verify, None, None): the controller's ADDRESS from the platform's
        own UniFi Network entry (the Protect entry when there is none). Register 695:
        ProOS never reads the integration's login, so the last two are always None."""
        d = _pe.data(_DOMAIN)
        self._inherited = False
        self._source = _DOMAIN if d.get("host") else ""
        if not d.get("host"):
            pm = _pe.data(_PROTECT_DOMAIN)
            if pm.get("host"):
                d = pm
                self._inherited = True
                self._source = _PROTECT_DOMAIN
        host = str(d.get("host") or "").strip().rstrip("/").replace("https://", "").replace("http://", "")
        return (host, bool(d.get("verify_ssl")), None, None)

    def configured(self) -> bool:
        """Never: ProOS does not sign in to the controller (691) and holds no login (695)."""
        return False

    def _login(self):
        # REGISTER 691 (Dave, 6 Oct 2026: "Just do what the platform does"): ProOS does
        # not sign in to the UniFi controller. Its own logins — retried on every call
        # with no pause — could exhaust the console's login limit and lock everyone
        # out. Only the platform's integration signs in. (695: the old sign-in code is gone.)
        raise NetError(409, "ProOS does not sign in to the controller; the platform's integration does")

    def _ensure(self):
        if self._session:
            return self._session
        with self._lock:
            return self._session or self._login()

    def _get(self, path):
        host, verify, _, _ = self._conf()
        try:
            _REACH.check()
        except _reach.Unreachable as e:
            raise NetError(502, str(e))
        s = self._ensure()
        req = urllib.request.Request(f"https://{host}{path}", method="GET",
            headers={"Accept": "application/json", "Cookie": f"TOKEN={s['token']}"})
        if s.get("csrf"):
            req.add_header("X-CSRF-Token", s["csrf"])
        ctx = None if verify else _ssl_unverified
        try:
            with urllib.request.urlopen(req, timeout=_reach.TIMEOUT, context=ctx) as r:
                _REACH.ok()
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._session = None
            raise NetError(e.code, f"GET {path} failed: {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _REACH.failed(f"cannot reach controller at {host}: {getattr(e, 'reason', e)}")
            raise NetError(502, f"cannot reach controller: {getattr(e, 'reason', e)}")

    def clients(self) -> list:
        """Active clients (each with mac, ip, sw_mac, sw_port). UniFi OS proxy path first,
        then the legacy controller path."""
        for path in ("/proxy/network/api/s/default/stat/sta", "/api/s/default/stat/sta"):
            try:
                d = self._get(path)
                if isinstance(d, dict) and isinstance(d.get("data"), list):
                    return d["data"]
            except NetError:
                continue
        return []

    def devices(self) -> list:
        """The controller's own GEAR list — switches, access points, gateways —
        each with its port table. `/stat/device` is the other half of the
        API ProOS has been ignoring.

        Dave, 16 Aug 2026: *"This entire product is based on building a toolbox
        of PHYSICAL tools… NOTHING is virtual — so if built this way, nothing
        can physically give incorrect information."*

        This is that tool. Per port it carries `up` (is there link), `enable`,
        `speed`, `poe_enable` / `poe_power` (are watts actually flowing), and
        per device the `uplink` that says what feeds it. That is electrical
        state, present tense, read from the thing the cables plug into — not
        an inference about what a device class probably does.

        Same shape and the same failure behaviour as `clients()`: an empty list
        means ProOS could not ask, and every caller must treat that as "I do
        not know", never as "nothing is connected"."""
        for path in ("/proxy/network/api/s/default/stat/device",
                     "/api/s/default/stat/device"):
            try:
                d = self._get(path)
                if isinstance(d, dict) and isinstance(d.get("data"), list):
                    return d["data"]
            except NetError:
                continue
        return []

    def status(self) -> dict:
        host, verify, u, pw = self._conf()
        out = {"configured": self.configured(), "host": host, "has_login": False,
               "inherited": bool(getattr(self, "_inherited", False)),
               # register 439: where the address and login come from — the
               # platform's own entry — so Pro can say so and offer its door
               "source": getattr(self, "_source", "") or "",
               "entry_id": (_pe.entry(getattr(self, "_source", "") or _DOMAIN) or {}).get("entry_id")}
        if self.configured():
            try:
                out["clients"] = len(self.clients())
                out["reachable"] = True
            except NetError as e:
                out["reachable"] = False
                out["error"] = e.message
        return out


def _norm_ip(s):
    return (s or "").strip()


def _mac(v):
    return str(v or "").strip().lower().replace("-", ":")


# ── PHYSICAL READINGS ───────────────────────────────────────────────────────
# Pure functions over what the controller reported. They answer questions ProOS
# used to guess at, and every one of them returns None when the answer is not
# in the data — because "I cannot see it" and "it is off" are different facts,
# and conflating them is the defect that runs through this whole codebase.
#
# Nothing here decides what a reading MEANS. A port being down is a fact; what
# to tell an installer about it belongs to the layer that has the room, the
# device and the customer in front of it.

def gear_list(devices):
    """{mac: {"name", "model", "type", "online"}} — the controller's OWN list
    of switches, access points and gateways.

    This replaces fingerprinting a device_tracker for the absence of an `oui`
    plus the presence of an `update.*` entity. That test was a guess about what
    gear looks like; this is the controller stating what its gear IS."""
    out = {}
    for d in (devices or []):
        m = _mac(d.get("mac"))
        if not m:
            continue
        out[m] = {"name": (d.get("name") or d.get("model") or m),
                  "model": d.get("model"),
                  "type": d.get("type"),          # usw / uap / ugw
                  # UniFi: state 1 = connected. Anything else is not "online",
                  # and a MISSING state is unknown, not offline.
                  "online": (None if d.get("state") is None
                             else d.get("state") == 1)}
    return out


def port_table(devices, gear_mac):
    """{port_idx: port_record} for one piece of gear, or {} if not present."""
    gm = _mac(gear_mac)
    for d in (devices or []):
        if _mac(d.get("mac")) != gm:
            continue
        out = {}
        for p in (d.get("port_table") or []):
            idx = p.get("port_idx")
            if idx is not None:
                out[int(idx)] = p
        return out
    return {}


def _watts(p):
    try:
        w = float(p.get("poe_power"))
    except (TypeError, ValueError):
        return None
    return w


def port_reading(devices, gear_mac, port_idx):
    """The PHYSICAL state of one switch port, or None if it cannot be read.

    Keys, each straight from the controller and each possibly None:
      gear · gear_mac · port · gear_online
      link      is there link on this port right now
      enabled   is the port administratively enabled
      speed     negotiated speed in Mbps
      poe       are watts actually flowing (None when the port has no PoE)
      watts     how many

    This is the reading that tells a sleeping television from a dead switch —
    the question `OFFNET_WHEN_OFF` stands in for."""
    gm = _mac(gear_mac)
    gl = gear_list(devices).get(gm)
    if not gl or port_idx is None:
        return None
    p = port_table(devices, gm).get(int(port_idx))
    if p is None:
        return None
    w = _watts(p)
    return {
        "gear": gl["name"], "gear_mac": gm, "port": int(port_idx),
        "gear_online": gl["online"],
        "link": (None if p.get("up") is None else bool(p.get("up"))),
        "enabled": (None if p.get("enable") is None else bool(p.get("enable"))),
        "speed": p.get("speed"),
        # poe_enable says the port is CONFIGURED for PoE; watts say power is
        # actually flowing. A port configured for PoE delivering 0.0 W is a
        # different fact from a port with no PoE at all, and both are useful.
        "poe": (None if not p.get("poe_enable") else (w or 0.0) > 0.0),
        "watts": w,
    }


def client_port(devices, clients, mac=None, ip=None):
    """Where a device is physically plugged in: the port_reading for the switch
    port carrying this client, or None.

    None means ProOS could not establish it — no controller, an unknown client,
    a wireless client, or gear that is not reporting. It never means "nowhere"."""
    want_mac, want_ip = _mac(mac), _norm_ip(ip)
    for c in (clients or []):
        if want_mac and _mac(c.get("mac")) != want_mac:
            continue
        if not want_mac and want_ip and _norm_ip(c.get("ip")) != want_ip:
            continue
        if not want_mac and not want_ip:
            continue
        # A wireless client has no switch port. Saying so is a fact; inventing
        # one would be the same mistake as the old co-drop list.
        if c.get("is_wired") is False:
            return None
        return port_reading(devices, c.get("sw_mac"), c.get("sw_port"))
    return None


def uplink_of(devices, gear_mac):
    """The mac of the gear that feeds this gear, or None. This is how a dead
    switch is known to have taken an access point — and everything on it —
    with it, without inferring anything from timing."""
    gm = _mac(gear_mac)
    for d in (devices or []):
        if _mac(d.get("mac")) != gm:
            continue
        up = d.get("uplink") or {}
        return _mac(up.get("uplink_mac")) or None
    return None


def fed_by(devices, gear_mac):
    """Every piece of gear downstream of this one, following uplinks. Returns
    macs, including gear that is offline — a switch that took an AP down is
    exactly the case this exists for. Cycle-safe."""
    gm = _mac(gear_mac)
    known = set(gear_list(devices))
    out, changed = set(), True
    while changed:
        changed = False
        for m in known:
            if m in out or m == gm:
                continue
            up = uplink_of(devices, m)
            if up == gm or up in out:
                out.add(m)
                changed = True
    return sorted(out)


def suggest_poe_switch(net_client, ha_client, entity, ip=None, mac=None):
    """Return the HA PoE port switch entity for `entity`, or None. Join the device to a UniFi
    client by IP (reliable) or MAC, read sw_mac + sw_port, then find the HA switch DEVICE whose
    connection MAC == sw_mac and return its switch.*_port_<sw_port> entity."""
    from . import netmap
    # 1) device IP (config-entry / device-url derived), if not supplied
    if not ip:
        try:
            ip = (netmap.harvest(client=ha_client).get(entity) or {}).get("ip")
        except Exception:
            ip = None
    # 2) find the UniFi client -> sw_mac + sw_port
    try:
        clients = net_client.clients()
    except Exception:
        return None
    cl = None
    for c in clients:
        if ip and _norm_ip(c.get("ip")) == _norm_ip(ip):
            cl = c
            break
        if mac and (c.get("mac") or "").lower() == mac.lower():
            cl = c
            break
    if not cl:
        return None
    sw_mac = (cl.get("sw_mac") or "").lower()
    sw_port = cl.get("sw_port")
    if not sw_mac or sw_port is None:
        return None
    # 3) map sw_mac -> HA switch device -> its port_<sw_port> switch entity
    try:
        _entries, devices, entities = netmap.load_registries(client=ha_client)
    except Exception:
        return None
    dev_id = None
    for d in devices:
        for pair in (d.get("connections") or []):
            if isinstance(pair, (list, tuple)) and len(pair) > 1 and (pair[1] or "").lower() == sw_mac:
                dev_id = d.get("id")
                break
        if dev_id:
            break
    if not dev_id:
        return None
    want = re.compile(r"_port_%d$" % int(sw_port))
    for e in entities:
        eid = e.get("entity_id") or ""
        if (e.get("device_id") == dev_id and eid.startswith("switch.")
                and e.get("platform") == "unifi" and want.search(eid)):
            return eid
    return None


def vlan_isolation(clients, ip=None, mac=None):
    """Is the client at `ip`/`mac` on a DIFFERENT network/VLAN than the home's
    main LAN? A pyatv/Companion session that keeps flapping is very often an
    Apple TV segregated onto an IoT/guest VLAN: mDNS/Bonjour (which pyatv uses
    to keep the link alive) does not cross VLANs unless the network reflects
    multicast. This reads the certified UniFi Network client list ProOS already
    fetches and reports the isolation as EVIDENCE for the incident — never a gate.

    Purely additive and OPTIONAL: returns None whenever it cannot be sure — no
    clients, target not found, target has no network, or a single network in
    play. Nothing in the system depends on UniFi; when it is not configured the
    caller simply gets None and the incident keeps its generic advice.

    The 'main LAN' is the most common network among WIRED clients — the ProOS
    box and the infrastructure are wired there, so this needs no box-IP guess.
    """
    if not clients:
        return None
    ipn = _norm_ip(ip)
    macn = (mac or "").strip().lower()
    target = None
    for c in clients:
        cip = _norm_ip(c.get("ip") or c.get("last_ip") or c.get("fixed_ip"))
        if ipn and cip == ipn:
            target = c
            break
        if macn and (c.get("mac") or "").strip().lower() == macn:
            target = c
            break
    if not target:
        return None
    t_net = target.get("network_id")
    if not t_net:
        return None
    wired = [c.get("network_id") for c in clients
             if c.get("is_wired") and c.get("network_id")]
    pool = wired or [c.get("network_id") for c in clients if c.get("network_id")]
    if not pool:
        return None
    counts = {}
    for n in pool:
        counts[n] = counts.get(n, 0) + 1
    main_net = max(counts, key=counts.get)
    if t_net == main_net:
        return None                       # same LAN as the home — not the cause
    main = next((c for c in clients if c.get("network_id") == main_net), {})
    return {
        "network": target.get("network") or t_net,
        "vlan": target.get("gw_vlan"),
        "main_network": main.get("network") or main_net,
        "main_vlan": main.get("gw_vlan"),
    }


# ── REGISTER 1219 — THE PORT MAP (stage 2 of the UniFi suite) ────────────────────────────────────────────────────
# Dave, 5 Nov 2026: the camera test — turn off the port a camera hangs off and Assist must find it, report the port is
# off and, with permission, turn it back on. The mockup he agreed draws every switch's face: what is on each port,
# link, speed, PoE watts, and the ports that must never be switched off. The tables come from the controller through
# ProOS's own local user (uniconsole, 1214/1215) — the same /stat/device and /stat/sta these helpers were written for
# before 691 left them unfed. Switching itself stays the PLATFORM's (its Port N / PoE switches and Power-Cycle
# buttons, matched here by their unique ids "port-", "poe-", "power_cycle-" + switch MAC + "_" + port).
#
# LOCKED PORTS — the guardrail Dave agreed in the mockup ("a 🔒 port feeds the uplink, an access point, the console or
# this box and can't be switched off"). Each lock is read from the tables, never assumed: the switch's own uplink port
# (port is_uplink, or the device's uplink names it), a port other UniFi gear hangs off (that gear's uplink names this
# switch and port), a port carrying the ProOS box (a wired client with the box's address). A port whose carriage
# cannot be read gets no lock and no "free" either — `known` is False and the guard keeps refusing it.

def _port_enabled(p):
    if p.get("enable") is False or str(p.get("forward") or "").lower() == "disabled":
        return False
    if p.get("enable") is True:
        return True
    return None


def port_map(devices, clients, entities=None, box_ips=()):
    """Every switch the controller reports, each with its ports as the controller describes them.

    Returns [{mac, name, model, type, online, ports: [...]}] (switches only — gear with a port table and not an access
    point). Each port: idx, name, link, enabled, speed, media, poe {capable, on, mode, watts}, uplink, carries [{kind,
    name, mac, ip}], lock (why it must never be switched off, or None), known (could ProOS read what it carries),
    controls {port, poe, power_cycle: {entity_id, enabled} or None}. Absent facts are None — never guessed."""
    gear = gear_list(devices)
    box = {str(i).strip() for i in (box_ips or ()) if i}
    by_uid = {}
    for e in (entities or []):
        if e.get("platform") == "unifi" and e.get("unique_id"):
            by_uid[str(e["unique_id"]).lower()] = e

    def control(kind, mac, idx):
        e = by_uid.get("%s-%s_%s" % (kind, mac, idx))
        return {"entity_id": e.get("entity_id"), "enabled": not e.get("disabled_by")} if e else None

    downstream = {}                                       # (switch mac, port) -> [gear names]
    for d in (devices or []):
        up = d.get("uplink") or {}
        um, rp = _mac(up.get("uplink_mac")), up.get("uplink_remote_port")
        if um and rp is not None:
            downstream.setdefault((um, int(rp)), []).append(d.get("name") or d.get("model") or _mac(d.get("mac")))
    on_port = {}                                          # (switch mac, port) -> wired clients
    clients_read = clients is not None
    for c in (clients or []):
        if c.get("is_wired") is False:
            continue
        sm, sp = _mac(c.get("sw_mac")), c.get("sw_port")
        if sm and sp is not None:
            on_port.setdefault((sm, int(sp)), []).append(c)

    out = []
    for d in (devices or []):
        m = _mac(d.get("mac"))
        table = d.get("port_table") or []
        if not m or not table or str(d.get("type") or "") == "uap":
            continue
        own_up = (d.get("uplink") or {}).get("port_idx")
        ports = []
        for p in table:
            idx = p.get("port_idx")
            if idx is None:
                continue
            idx = int(idx)
            gear_here = downstream.get((m, idx), [])
            cl_here = on_port.get((m, idx), [])
            carries = [{"kind": "gear", "name": n} for n in gear_here] + [
                {"kind": "client", "name": c.get("name") or c.get("hostname") or c.get("oui") or _mac(c.get("mac")),
                 "mac": _mac(c.get("mac")), "ip": c.get("ip")} for c in cl_here]
            uplink = bool(p.get("is_uplink")) or (own_up is not None and int(own_up) == idx)
            # ONE MECHANISM (register 1221): the locks are register 450's port guard (portpath.port_guard — feeds other
            # gear, carries more than one device, carries the box), plus the switch's own uplink, which that guard
            # cannot see from the far side. 1219 had its own copy of the first and third and missed the second.
            if uplink:
                lock = "this switch's own uplink: switching it off cuts the switch off"
            else:
                from . import portpath as _pp
                lock = _pp.port_guard(devices, clients if clients_read else [], m, idx, box_ips=list(box))
            w = _watts(p)
            capable = bool(p.get("port_poe")) or p.get("poe_caps") not in (None, 0)
            ports.append({
                "idx": idx, "name": p.get("name") or ("Port %d" % idx),
                "link": None if p.get("up") is None else bool(p.get("up")),
                "enabled": _port_enabled(p),
                "speed": p.get("speed"), "media": p.get("media"),
                "poe": {"capable": capable,
                        "on": (None if not capable else bool(p.get("poe_enable")) and (w or 0.0) > 0.0),
                        "mode": p.get("poe_mode"), "watts": w} if capable else None,
                "uplink": uplink, "carries": carries, "lock": lock,
                "known": clients_read,
                "controls": {"port": control("port", m, idx), "poe": control("poe", m, idx),
                             "power_cycle": control("power_cycle", m, idx)},
            })
        ports.sort(key=lambda x: x["idx"])
        g = gear.get(m) or {}
        out.append({"mac": m, "name": g.get("name") or m, "model": d.get("model"), "type": d.get("type"),
                    "online": g.get("online"), "ports": ports})
    out.sort(key=lambda s: (-len(s["ports"]), str(s["name"]).lower()))
    return out


# ── REGISTER 1222 — WHERE A DEVICE IS (Dave's camera test) ───────────────────────────────────────────────────────
# A device on a port that has been switched off DROPS OUT of the controller's live client table — exactly when Assist
# is asked "why has the camera gone offline". So ProOS keeps what it has SEEN: each time the map is read with the
# client table, every wired device's switch and port is written down with the time it was seen there. That is an
# observation with its date on it — never a guess — and it is said as "last seen on … at …". The controller's own
# client history (fields it keeps for offline clients) is read too when it offers them.

def _mem_path():
    import os
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "unifi_port_seen.json")


def remember(switches, path=None, now=None):
    """Write down where every wired device in the map was seen. Never raises."""
    import json as _j
    import os
    import time as _t
    path = path or _mem_path()
    try:
        seen = _j.load(open(path)) if os.path.exists(path) else {}
    except Exception:                                            # noqa: BLE001
        seen = {}
    ts = now or _t.time()
    for s in (switches or []):
        for p in s.get("ports") or []:
            for c in p.get("carries") or []:
                m = _mac(c.get("mac"))
                if c.get("kind") == "client" and m:
                    seen[m] = {"switch": s.get("mac"), "switch_name": s.get("name"), "port": p.get("idx"),
                               "ip": c.get("ip"), "name": c.get("name"), "seen": ts}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            _j.dump(seen, f)
        os.replace(tmp, path)
    except Exception:                                            # noqa: BLE001
        pass
    return len(seen)


def recall(mac=None, ip=None, path=None):
    """Where ProOS last saw this device (by MAC, else address), or None."""
    import json as _j
    import os
    path = path or _mem_path()
    try:
        seen = _j.load(open(path)) if os.path.exists(path) else {}
    except Exception:                                            # noqa: BLE001
        return None
    m = _mac(mac)
    if m and m in seen:
        return dict(seen[m], mac=m)
    if ip:
        for k, v in seen.items():
            if str(v.get("ip") or "") == str(ip):
                return dict(v, mac=k)
    return None


def locate(switches, mac=None, ip=None, history=None, path=None):
    """(switch, port, how) for a device: live in the map; else the controller's own client history; else where ProOS
    last saw it. how = {"source": "live" | "controller" | "seen", "seen": time or None}. (None, None, None) when
    nothing is known — never a guess."""
    m, ipn = _mac(mac), str(ip or "").strip()
    by_mac = {s.get("mac"): s for s in (switches or [])}

    def port_of(sm, idx):
        s = by_mac.get(_mac(sm))
        if not s or idx is None:
            return None, None
        p = next((x for x in s.get("ports") or [] if x.get("idx") == int(idx)), None)
        return (s, p) if p else (None, None)

    for s in (switches or []):
        for p in s.get("ports") or []:
            for c in p.get("carries") or []:
                if (m and _mac(c.get("mac")) == m) or (not m and ipn and str(c.get("ip") or "") == ipn):
                    return s, p, {"source": "live", "seen": None}
    for c in (history or []):
        if (m and _mac(c.get("mac")) == m) or (not m and ipn and str(c.get("last_ip") or c.get("ip") or "") == ipn):
            sm = c.get("last_uplink_mac") or c.get("sw_mac")
            idx = c.get("last_uplink_remote_port") or c.get("sw_port")
            s, p = port_of(sm, idx)
            if p:
                return s, p, {"source": "controller", "seen": c.get("last_seen")}
    r = recall(m, ipn, path)
    if r:
        s, p = port_of(r.get("switch"), r.get("port"))
        if p:
            return s, p, {"source": "seen", "seen": r.get("seen")}
    return None, None, None


def clear(path=None):
    """Factory reset (register 1222): where ProOS saw this house's devices is per-site — it goes with the house."""
    import os
    try:
        os.remove(path or _mem_path())
        return True
    except OSError:
        return False


# ── REGISTER 1225 — NETWORK → DEVICES, CLIENTS, WI-FI (the agreed mockup's other Network sections; Dave 6 Nov "sure go") ──
# Read through ProOS's own console sign-in from the controller's own tables. Each view is an explicit list of plain
# fields: the Wi-Fi records carry the networks' passwords, and NOTHING outside the named fields ever leaves Core.
_KIND = {"udm": "Gateway", "ugw": "Gateway", "uxg": "Gateway", "usw": "Switch", "uap": "Access Point"}
_SEC = {"wpapsk": "WPA Personal", "wpaeap": "WPA Enterprise", "open": "Open", "wep": "WEP"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def devices_view(devices):
    names = {_mac(d.get("mac")): d.get("name") or d.get("model") for d in (devices or [])}
    out = []
    for d in (devices or []):
        up = d.get("uplink") or {}
        out.append({
            "mac": _mac(d.get("mac")), "name": d.get("name") or d.get("model") or _mac(d.get("mac")),
            "model": d.get("model"), "kind": _KIND.get(str(d.get("type") or ""), str(d.get("type") or "").upper() or None),
            "online": None if d.get("state") is None else d.get("state") == 1,
            "ip": d.get("ip"), "version": d.get("version") or d.get("displayable_version"),
            "upgradable": bool(d.get("upgradable")), "upgrade_to": d.get("upgrade_to_firmware") if d.get("upgradable") else None,
            "clients": d.get("num_sta"), "uptime_s": d.get("uptime"),
            "uplink": names.get(_mac(up.get("uplink_mac"))) or (up.get("uplink_device_name") or None),
            "uplink_port": up.get("uplink_remote_port"),
            "temperature": next((_f(t.get("value")) for t in (d.get("temperatures") or []) if isinstance(t, dict)), None),
            "ports": len(d.get("port_table") or []) if str(d.get("type") or "") != "uap" else None,
        })
    order = {"Gateway": 0, "Switch": 1, "Access Point": 2}
    out.sort(key=lambda x: (order.get(x["kind"], 3), str(x["name"]).lower()))
    return out


def clients_view(clients, devices):
    names = {_mac(d.get("mac")): d.get("name") or d.get("model") for d in (devices or [])}
    out = []
    for c in (clients or []):
        wired = bool(c.get("is_wired"))
        out.append({
            "mac": _mac(c.get("mac")), "name": c.get("name") or c.get("hostname") or c.get("oui") or _mac(c.get("mac")),
            "ip": c.get("ip"), "wired": wired, "network": c.get("network"),
            "switch": names.get(_mac(c.get("sw_mac"))) if wired else None, "switch_mac": _mac(c.get("sw_mac")) if wired else None,
            "port": c.get("sw_port") if wired else None,
            "wifi": c.get("essid") if not wired else None, "ap": names.get(_mac(c.get("ap_mac"))) if not wired else None,
            "signal": c.get("signal") if not wired else None, "band": c.get("radio") if not wired else None,
            "uptime_s": c.get("uptime"), "maker": c.get("oui") or None,
        })
    out.sort(key=lambda x: str(x["name"]).lower())
    return out


def wifi_view(wlans, clients):
    count = {}
    for c in (clients or []):
        if c.get("essid") and not c.get("is_wired"):
            count[c["essid"]] = count.get(c["essid"], 0) + 1
    out = []
    for w in (wlans or []):
        bands = w.get("wlan_bands") or ([w["wlan_band"]] if w.get("wlan_band") else [])
        out.append({                                   # NAMED FIELDS ONLY — the record also holds the password
            "id": w.get("_id"), "name": w.get("name"), "enabled": bool(w.get("enabled")),
            "security": _SEC.get(str(w.get("security") or ""), str(w.get("security") or "") or None),
            "wpa_mode": w.get("wpa_mode"), "bands": [{"2g": "2.4 GHz", "5g": "5 GHz", "6g": "6 GHz"}.get(b, b) for b in bands],
            "guest": bool(w.get("is_guest")), "hidden": bool(w.get("hide_ssid")), "clients": count.get(w.get("name"), 0),
        })
    return out
