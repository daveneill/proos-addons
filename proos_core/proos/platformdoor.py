"""THE BOX'S ADDRESS IS ProOS'S — build 3 of "only serve ProOS" (register 945; the read is 805, builds 1–2 are 808/813).

Dave, 13 Oct: "if an installer entered the IP address of the core … they will get the homeassistant login I need it all
changed to only serve ProOS"; 18 Oct, on the first flashed G-Flip: "thats a home assistant signin that was supposed to be
gone and only ours".

THE PLATFORM'S OWN MECHANISMS (read in Home Assistant 2026.10.2 and Supervisor of 16 Oct 2026):
  - its HTTP settings (server_host, server_port) are stored; a new setting is tried as "pending" and the platform reverts
    to the last good one by itself after 5 minutes unless it is promoted — the lock-out guard;
  - the Supervisor reads the platform's binds and port from the platform itself and checks it can reach them
    (frontend_check.bind_reachable: the bind must include the platform's own address inside the box);
  - add-ons reach the platform by the name homeassistant at the address and port the Supervisor reports.

On a ProOS image box the platform is bound inside the box only (127.0.0.1 and its address on the Supervisor's network,
port 8123 — the image's first boot sets that and promotes it once the platform answers), and ProOS Core holds the box's
own address (port 80) with its front door (808). Then one thing is left that only ProOS knows: the address the platform
gives OUT for itself — its internal URL, used for links it hands to the home's devices (a speaker fetching an
announcement) and the address the apps are told. Left alone it names the platform's own port, which the home's
network can no longer reach. So, while the platform is bound inside the box, its internal URL is kept at the box's own
address (port 80, ProOS's door, which passes /api/* through) — and moved when the box's address moves (the 13 Sep law:
a network change must never break ProOS). A box whose platform listens on the home's network is never touched.
"""
from __future__ import annotations

INSIDE = ("127.0.0.1", "::1", "172.30.32.1")


def bound_inside(http_config) -> bool:
    """True when the platform's confirmed HTTP setting binds only addresses inside the box."""
    stable = (http_config or {}).get("stable") or {}
    hosts = stable.get("server_host") or []
    if not hosts:
        return False                       # no bind list = every address: the platform is on the home's network
    return all(str(h) in INSIDE for h in hosts)


def box_address(network_info):
    """The box's own LAN address, from the Supervisor's /network/info."""
    from proos.speakermusic import box_address as _ba
    return _ba(network_info)


def want_url(network_info):
    ip = box_address(network_info)
    return ("http://%s" % ip) if ip else None


def ensure(sup, ws) -> dict:
    """One pass: converge, don't clobber. Returns what it found and did."""
    out = {"inside": False, "changed": False}
    try:
        cfg = ws("http/config")
    except Exception as e:  # noqa: BLE001 — an older platform has no stored HTTP settings: nothing to keep
        out["error"] = "http/config: %s" % str(e)[:160]
        return out
    out["inside"] = bound_inside(cfg)
    if not out["inside"]:
        return out
    want = want_url((sup("GET", "/network/info") or {}))
    if not want:
        out["error"] = "the box's address is not known yet"
        return out
    out["want"] = want
    have = None
    try:
        have = (ws("get_config") or {}).get("internal_url")
    except Exception:  # noqa: BLE001
        pass
    out["had"] = have
    if have != want:
        ws("config/core/update", internal_url=want)
        out["changed"] = True
    return out
