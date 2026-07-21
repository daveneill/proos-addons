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

    def _source(self):
        """Where the login comes from. Network's OWN entry wins; otherwise it reuses the UniFi
        PROTECT login (same UniFi OS console, one local user for both). Returns
        (host, verify_ssl, username, password, inherited)."""
        meta = self._creds.meta(SERVICE) or {}
        host, user, pw = meta.get("host"), meta.get("username"), meta.get("password")
        verify = bool(meta.get("verify_ssl"))
        if host and user and pw:
            return (host, verify, user, pw, False)
        pm = self._creds.meta("unifi_protect") or {}
        return (host or pm.get("host"), verify or bool(pm.get("verify_ssl")),
                user or pm.get("username"), pw or pm.get("password"), True)

    def _conf(self):
        h, v, u, pw, _ = self._source()
        return (h, v, u, pw)

    def configured(self) -> bool:
        h, _, u, pw, _ = self._source()
        return bool(h and u and pw)

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

    def disconnect(self) -> dict:
        """Forget the stored controller login (Connect/Disconnect parity with Protect)."""
        self._creds.put(SERVICE, "", name="UniFi Network", host="",
                        extra={"verify_ssl": False, "username": None, "password": None})
        self._session = None
        return self.status()

    def status(self) -> dict:
        host, verify, u, pw, inherited = self._source()
        out = {"configured": bool(host and u and pw), "host": host, "username": u,
               "has_login": bool(u and pw), "inherited": inherited}
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
