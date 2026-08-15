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

SERVICE = "unifi_network"
_PROTECT_SERVICE = "unifi_protect"   # one local UniFi user serves both — reuse it

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
        meta = self._creds.meta(SERVICE) or {}
        host = meta.get("host"); user = meta.get("username"); pw = meta.get("password")
        verify = bool(meta.get("verify_ssl"))
        self._inherited = False
        if not (host and user and pw):
            # One local UniFi user serves both. When the Network integration has
            # no credential of its own, borrow the UniFi Protect login.
            pm = self._creds.meta(_PROTECT_SERVICE) or {}
            if pm.get("host") and pm.get("username") and pm.get("password"):
                host = host or pm.get("host")
                user = pm.get("username"); pw = pm.get("password")
                verify = bool(pm.get("verify_ssl"))
                self._inherited = True
        return (host, verify, user, pw)

    def configured(self) -> bool:
        host, _, u, pw = self._conf()
        return bool(host and u and pw)

    def set_config(self, host, username=None, password=None, verify_ssl=False) -> dict:
        meta = self._creds.meta(SERVICE) or {}
        host = (host or meta.get("host") or "").strip().rstrip("/").replace("https://", "").replace("http://", "")
        if not host:
            raise NetError(400, "host required")
        extra = {"verify_ssl": bool(verify_ssl),
                 "username": username if username is not None else meta.get("username"),
                 "password": password if password is not None else meta.get("password")}
        self._creds.put(SERVICE, "", name="UniFi Network", host=host, extra=extra)
        self._session = None
        return self.status()

    def _login(self):
        host, verify, user, pw = self._conf()
        if not (host and user and pw):
            raise NetError(409, "needs host + a LOCAL username/password")
        url = f"https://{host}/api/auth/login"
        data = json.dumps({"username": user, "password": pw, "rememberMe": True}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        ctx = None if verify else _ssl_unverified
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                token = None
                for c in (r.headers.get_all("Set-Cookie") or []):
                    m = re.search(r"(?:^|\s)TOKEN=([^;]+)", c)
                    if m:
                        token = m.group(1)
                csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
                if not token:
                    raise NetError(401, "login returned no token (use a LOCAL user, not SSO)")
                self._session = {"token": token, "csrf": csrf}
                return self._session
        except urllib.error.HTTPError as e:
            raise NetError(e.code, f"login failed: {e.code}")
        except urllib.error.URLError as e:
            raise NetError(502, f"cannot reach controller: {e.reason}")

    def _ensure(self):
        if self._session:
            return self._session
        with self._lock:
            return self._session or self._login()

    def _get(self, path):
        host, verify, _, _ = self._conf()
        s = self._ensure()
        req = urllib.request.Request(f"https://{host}{path}", method="GET",
            headers={"Accept": "application/json", "Cookie": f"TOKEN={s['token']}"})
        if s.get("csrf"):
            req.add_header("X-CSRF-Token", s["csrf"])
        ctx = None if verify else _ssl_unverified
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._session = None
            raise NetError(e.code, f"GET {path} failed: {e.code}")
        except urllib.error.URLError as e:
            raise NetError(502, f"cannot reach controller: {e.reason}")

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
        each with its port table. `/stat/device` is the other half of the API
        ProOS has been ignoring while it guessed.

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
        out = {"configured": self.configured(), "host": host, "has_login": bool(u and pw),
               "inherited": bool(getattr(self, "_inherited", False))}
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
    the question `OFFNET_WHEN_OFF` was invented to guess at."""
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
