"""GOOGLE AND ALEXA THROUGH THE MATTER HUB — Core's door to it (register 1330, 13 Nov 2026, plan item 24).

Dave, 13 Nov 13:06 "1. yes 2. yes 3. yes" (install the community Matter Hub; the Office lights for the test), then
13:2x "Core talks to it". PLATFORM FIRST: the platform reaches Google and Alexa only through Nabu Casa's cloud, which
shows "Home Assistant Cloud" in the customer's app; the Matter Hub (RiDDiX fork, an app on the platform) publishes the
platform's own devices as a local Matter bridge. ProOS builds no bridge — it asks the hub for one, and reads it back.

HOW CORE REACHES IT. The hub's settings answer only the Supervisor (it starts with --http-ip-whitelist=172.30.32.2 and
a random ingress port), through an ingress session — and only the platform may ask the Supervisor for one, for a user
it names (read on the box 13 Nov: Core asking the Supervisor got 401 (1330b); Core asking through the platform's
websocket got "unauthorized", Core's platform user not being an admin (1330c)). So the DOOR asks (door build 11,
1330d): Core calls the door's /api/proos_core_only/matterhub/<bridge path> on the platform from inside the box; the door,
inside the platform, makes the session for its panel user, calls the hub, and hands back the hub's answer. The session
never leaves the platform; the door answers Core's own address only; ProOS's front door never passes that path.

WHAT IS READ, NOT ASSUMED: the bridge's status, how many devices it carries, the devices that FAILED to publish,
whether it is commissioned, and which controllers hold it (its fabrics — Google, Amazon, Apple are named by the vendor
id the controller reported; an id ProOS does not know is shown as its number, never guessed).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

SLUG_SUFFIX = "_hamh"                  # the hub's stable app; its prefix is the Supervisor's hash of the source
BRIDGE_PREFIX = "ProOS Bridge"
# Matter vendor ids as the Connectivity Standards Alliance assigns them — the controller states its own.
CONTROLLERS = {0x6006: "Google Home", 0x1217: "Amazon Alexa", 0x1349: "Apple Home", 0x110A: "SmartThings"}


def find_slug(sv) -> str | None:
    """The installed Matter Hub app, asked of the Supervisor — None when it is not installed."""
    for a in (sv("GET", "/addons") or {}).get("addons", []):
        if str(a.get("slug") or "").endswith(SLUG_SUFFIX) and a.get("installed", True):
            return a["slug"]
    return None


DOOR_PATH = "/api/proos_core_only/matterhub/"


class Hub:
    """sv: Core's Supervisor call (returns the 'data'); base: the platform's address from inside the box;
    opener: urllib's, swappable on the bench."""

    def __init__(self, sv, base, opener=None):
        self.sv, self.base, self.open = sv, base, (opener or urllib.request.urlopen)

    def info(self) -> dict:
        slug = find_slug(self.sv)
        if not slug:
            return {"installed": False}
        i = self.sv("GET", "/addons/%s/info" % slug) or {}
        return {"installed": True, "slug": slug, "version": i.get("version"), "running": i.get("state") == "started",
                "entry": i.get("ingress_entry") or i.get("ingress_url")}

    def call(self, method: str, path: str, body=None, timeout: float = 20.0):
        inf = self.info()
        if not inf.get("installed"):
            raise RuntimeError("the Matter Hub is not installed on this box")
        if not inf.get("running"):
            raise RuntimeError("the Matter Hub is installed but not running")
        if not inf.get("entry"):
            raise RuntimeError("the Supervisor gave no address for the Matter Hub")
        token = inf["entry"].strip("/").split("/")[-1]           # /api/hassio_ingress/<token>/ → <token>
        url = (self.base().rstrip("/") + DOOR_PATH + path.lstrip("/") + "?" +
               urllib.parse.urlencode({"slug": inf["slug"], "token": token}))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.open(req, timeout=timeout) as r:
            raw = r.read().decode() or "null"
        return json.loads(raw)


def bridge_view(b: dict) -> dict:
    """The hub's bridge → what Pro shows. Every field is the hub's own reading."""
    com = b.get("commissioning") or {}
    fabrics = [CONTROLLERS.get(f.get("rootVendorId"), "Vendor %s" % f.get("rootVendorId"))
               for f in (com.get("fabrics") or [])]
    return {"id": b.get("id"), "name": b.get("name"), "status": b.get("status"), "reason": b.get("statusReason"),
            "devices": b.get("deviceCount"), "port": b.get("port"),
            "failed": [{"entity_id": f.get("entityId"), "reason": f.get("reason")} for f in (b.get("failedEntities") or [])],
            "paired": bool(com.get("isCommissioned")), "controllers": fabrics,
            "code": com.get("manualPairingCode"), "proos": str(b.get("name") or "").startswith(BRIDGE_PREFIX)}


def test_bridge_request(area_id: str, domains=("light",), name: str = BRIDGE_PREFIX) -> dict:
    """The test bridge (1330): one area's devices of the named kinds — both must match ("all")."""
    area_id = str(area_id or "").strip()
    if not area_id:
        raise ValueError("a room is needed for the test bridge")
    inc = [{"type": "area", "value": area_id}]
    doms = [d for d in domains if d]
    if len(doms) != 1:
        raise ValueError("the test bridge carries one kind of device")   # hub filters: 'all' would need every domain
    inc.append({"type": "domain", "value": doms[0]})
    return {"name": name, "filter": {"include": inc, "exclude": [], "includeMode": "all"}}


def status(hub: Hub) -> dict:
    inf = hub.info()
    out = {"installed": inf.get("installed", False), "running": inf.get("running", False), "version": inf.get("version"),
           "bridges": []}
    if inf.get("installed") and inf.get("running"):
        out["bridges"] = [bridge_view(b) for b in (hub.call("GET", "bridges") or [])]
    return out


def make_test_bridge(hub: Hub, area_id: str) -> dict:
    for b in hub.call("GET", "bridges") or []:
        if str(b.get("name") or "").startswith(BRIDGE_PREFIX):
            raise ValueError("a ProOS Bridge is already on this box — remove it first")
    made = hub.call("POST", "bridges", test_bridge_request(area_id))
    return bridge_view(hub.call("GET", "bridges/%s" % made["id"]) or made)


def remove_bridge(hub: Hub, bridge_id: str) -> bool:
    for b in hub.call("GET", "bridges") or []:
        if b.get("id") == bridge_id:
            if not str(b.get("name") or "").startswith(BRIDGE_PREFIX):
                raise ValueError("only ProOS's own bridge is removed here")
            hub.call("DELETE", "bridges/%s" % bridge_id)
            return True
    raise ValueError("no such bridge")
