"""NETWORK → NETWORKS · INTERNET · GUESTS · FIREWALL & RULES · POWER · EVENTS — register 1237 (the mockup Dave agreed;
6 Nov: "yes all of it please", then "okay go").

The controller's own tables, read through ProOS's local console user (measured on Dave's console 6 Nov: networkconf 6
— LAN, VPN server, VPN client, WAN; v2 firewall-policies 66 across 6 zones; portforward 1; trafficrules 0; stat/guest 0;
guest_access setting; stat/health; stat/device PoE on 2 of 8; system-log via POST with the CSRF token).
NAMED FIELDS ONLY. These records carry VPN private keys, pre-shared keys, RADIUS and portal secrets and payment keys —
none of that leaves Core: every view copies only the fields it names.
"""
import re

_PURPOSE = {"corporate": "Network", "guest": "Guest Network", "wan": "Internet", "remote-user-vpn": "VPN Server",
            "vpn-client": "VPN Client", "site-vpn": "Site-to-Site VPN", "vlan-only": "VLAN Only"}


def networks_view(confs):
    out = []
    for n in (confs or []):
        if not isinstance(n, dict):
            continue
        p = str(n.get("purpose") or "")
        out.append({"name": n.get("name") or "Network", "kind": _PURPOSE.get(p, p.replace("-", " ").title() or "Network"),
                    "enabled": n.get("enabled", True) is not False, "vlan": n.get("vlan") if n.get("vlan_enabled") else None,
                    "subnet": n.get("ip_subnet") if p not in ("wan",) else None,
                    "dhcp": bool(n.get("dhcpd_enabled")) if p in ("corporate", "guest") else None,
                    "dhcp_range": ("%s – %s" % (n.get("dhcpd_start"), n.get("dhcpd_stop")))
                                  if n.get("dhcpd_enabled") and n.get("dhcpd_start") else None,
                    "wan_slot": n.get("wan_networkgroup") if p == "wan" else None,
                    "wan_type": n.get("wan_type") if p == "wan" else None})
    order = ["Internet", "Network", "Guest Network", "VLAN Only", "VPN Server", "VPN Client", "Site-to-Site VPN"]
    out.sort(key=lambda x: (order.index(x["kind"]) if x["kind"] in order else 99, str(x["name"]).lower()))
    return out


def internet_view(health, confs):
    """The internet line(s): the controller's own health (status, ISP, address, speed, latency) and each WAN by name."""
    sub = {h.get("subsystem"): h for h in (health or []) if isinstance(h, dict)}
    w, www = sub.get("wan") or {}, sub.get("www") or {}
    wans = [{"name": n.get("name") or "Internet", "slot": n.get("wan_networkgroup"), "type": n.get("wan_type"),
             "enabled": n.get("enabled", True) is not False}
            for n in (confs or []) if isinstance(n, dict) and n.get("purpose") == "wan"]
    return {"status": w.get("status"), "isp": w.get("isp_name") or w.get("isp_organization"), "address": w.get("wan_ip"),
            "gateway": w.get("gw_name"), "latency_ms": www.get("latency") if www.get("latency") is not None else w.get("latency"),
            "uptime_s": www.get("uptime") or w.get("uptime"), "down_mbps": www.get("xput_down"), "up_mbps": www.get("xput_up"),
            "speedtest": www.get("speedtest_status"), "internet": www.get("status"), "wans": wans}


def guests_view(guests, setting, wlans):
    """Guest access: the guest Wi-Fi networks, the portal's on/off and how a guest gets in, and who is on it now."""
    g = (setting or [{}])[0] if isinstance(setting, list) else (setting or {})
    gw = [w.get("name") for w in (wlans or []) if isinstance(w, dict) and w.get("is_guest")]
    return {"guest_wifi": gw, "portal": bool(g.get("portal_enabled")),
            "how": {"none": "No sign-in", "hotspot": "Hotspot (vouchers, payment or password)", "password": "Password",
                    "custom": "External portal", "facebook_wifi": "Facebook Wi-Fi"}.get(str(g.get("auth") or ""), None),
            "expires_min": g.get("expire") if isinstance(g.get("expire"), (int, float)) else None,
            "guests_now": [{"name": x.get("name") or x.get("hostname") or "Guest", "authorized": bool(x.get("authorized")),
                            "minutes_left": int((x.get("end") or 0) - (x.get("start") or 0)) // 60 if x.get("end") else None}
                           for x in (guests or []) if isinstance(x, dict) and not x.get("expired")][:100]}


def firewall_view(policies, zones, forwards, traffic):
    """Firewall rules (zone-based), port forwards and traffic rules — names, zones, what they do; never secrets."""
    zn = {str(z.get("_id")): z.get("name") or z.get("zone_key") for z in (zones or []) if isinstance(z, dict)}

    def side(x):
        x = x or {}
        return zn.get(str(x.get("zone_id")), x.get("zone_id") and "Zone") or "Any"

    rules = []
    for p in (policies or []):
        if not isinstance(p, dict):
            continue
        rules.append({"name": p.get("name") or "Rule", "enabled": p.get("enabled", True) is not False,
                      "action": str(p.get("action") or "").title() or None, "from": side(p.get("source")),
                      "to": side(p.get("destination")), "protocol": p.get("protocol") if p.get("protocol") not in (None, "all") else "Any",
                      "built_in": bool(p.get("predefined")), "index": p.get("index")})
    rules.sort(key=lambda r: (r["built_in"], r["index"] if isinstance(r["index"], (int, float)) else 1e9))
    fw = [{"name": f.get("name") or "Port Forward", "enabled": f.get("enabled", True) is not False,
           "outside_port": f.get("dst_port"), "to": f.get("fwd"), "to_port": f.get("fwd_port"),
           "protocol": f.get("proto"), "from": f.get("src") if f.get("src") not in (None, "any") else "Anywhere"}
          for f in (forwards or []) if isinstance(f, dict)]
    tr = [{"name": t.get("description") or t.get("name") or "Traffic Rule", "enabled": t.get("enabled", True) is not False,
           "action": str(t.get("action") or "").title() or None} for t in (traffic or []) if isinstance(t, dict)]
    return {"zones": sorted(set(v for v in zn.values() if v)), "rules": rules, "port_forwards": fw, "traffic_rules": tr,
            "custom": sum(1 for r in rules if not r["built_in"]), "built_in": sum(1 for r in rules if r["built_in"])}


def power_view(devices):
    """PoE power per switch: its budget, what is drawn now, the busiest ports; and any power strip's outlets."""
    out = []
    for d in (devices or []):
        if not isinstance(d, dict) or not d.get("port_table"):
            continue
        ports = [p for p in d.get("port_table") or [] if isinstance(p, dict)]
        draws = []
        for p in ports:
            try:
                w = float(p.get("poe_power") or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                draws.append({"port": p.get("port_idx"), "name": p.get("name"), "watts": round(w, 1)})
        budget = d.get("total_max_power")
        if not draws and budget is None:
            continue
        draws.sort(key=lambda x: -x["watts"])
        out.append({"name": d.get("name") or d.get("model") or "Switch", "model": d.get("model"),
                    "budget_w": budget, "used_w": round(sum(x["watts"] for x in draws), 1), "ports_powered": len(draws),
                    "top": draws[:8],
                    "outlets": [{"name": o.get("name"), "on": bool(o.get("relay_state"))} for o in (d.get("outlet_table") or []) if isinstance(o, dict)]})
    out.sort(key=lambda x: -(x["used_w"] or 0))
    return out


_WORDS = re.compile(r"[^A-Za-z0-9 .,:'()/-]")


def events_view(rows):
    """The controller's event log, newest first: what happened in words, when, and its own short message if it has one."""
    out = []
    for e in (rows or []):
        if not isinstance(e, dict):
            continue
        key = str(e.get("key") or e.get("event") or e.get("type") or "")
        kind = re.sub(r"_\d+$", "", key).replace("_", " ").title() or "Event"
        msg = e.get("message") or e.get("msg") or e.get("title")
        out.append({"kind": kind, "when": e.get("timestamp") or e.get("time") or e.get("datetime"),
                    "category": str(e.get("category") or "").title() or None,
                    "severity": str(e.get("severity") or "").title() or None,
                    "message": _WORDS.sub("", str(msg))[:160] if isinstance(msg, str) else None})
    out.sort(key=lambda x: x["when"] if isinstance(x["when"], (int, float)) else 0, reverse=True)
    return out
