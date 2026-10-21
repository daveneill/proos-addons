"""The box's own network, as the platform's Supervisor states it — plan item 12, T4.

The Network Change plan (13 Sep 2026), build 4: "a Network section … the box's hostname,
address, gateway, DNS, DHCP or static — readable by an installer … Beside it, one line per
outside system Core talks to: reachable or not, and why." Builds 1–3 shipped (registers
437–439); this is the reading half of build 4. Nothing here is kept: every call reads the
Supervisor's own answer (the mirror ruling).

SETTING IT (register 969z, Dave 21 Oct 16:05: "There is no way to ever modify these settings if needed to change to
static or change DNS addresses like most boxes and the Gflip has 2 x LAN connections"; 16:07: one image for every
device — "show the number of LAN ports on the unit"). Every port the box reports is listed, named LAN 1, LAN 2 … in
the order the box names its wired ports, Wi-Fi as Wi-Fi; nothing is per device. A change goes through the
Supervisor's own /network/interface/<name>/update (the platform's network control, the one its own screens use);
`update_body` builds and checks it, `restore_body` rebuilds a port's settings exactly as they were read.

Pure: `summarise(network_info, host_info, systems)` → the facts Pro shows. A field the
platform did not give is None — never a guess.
"""


def _strip(d):
    return (d or {}).get("data") if isinstance((d or {}).get("data"), dict) else (d or {})


def _labels(raw_itfs):
    """LAN 1, LAN 2 … in the order the box names its wired ports; Wi-Fi as Wi-Fi (Wi-Fi 2 for a second)."""
    out, wired, wifi = {}, [], []
    for itf in raw_itfs or []:
        (wifi if itf.get("type") == "wireless" else wired).append(str(itf.get("interface") or ""))
    for i, n in enumerate(sorted(wired)):
        out[n] = "LAN %d" % (i + 1) if len(wired) > 1 else "LAN"
    for i, n in enumerate(sorted(wifi)):
        out[n] = "Wi-Fi" if i == 0 else "Wi-Fi %d" % (i + 1)
    return out


def summarise(network_info, host_info=None, systems=None) -> dict:
    ni, hi = _strip(network_info), _strip(host_info)
    itfs = []
    labels = _labels(ni.get("interfaces") or [])
    for itf in (ni.get("interfaces") or []):
        v4 = itf.get("ipv4") or {}
        method = v4.get("method")
        _a0 = str((v4.get("address") or [""])[0] or "")
        itfs.append({
            "name": itf.get("interface"),
            "label": labels.get(str(itf.get("interface") or "")) or itf.get("interface"),
            "enabled": itf.get("enabled"),
            "mac": itf.get("mac"),
            "method": method,
            "prefix": int(_a0.split("/")[1]) if "/" in _a0 and _a0.split("/")[1].isdigit() else None,
            "kind": itf.get("type"),
            "primary": bool(itf.get("primary")),
            "connected": itf.get("connected"),
            # the platform's own words: auto = the router hands the address out (DHCP)
            "addressing": ({"auto": "DHCP", "static": "Static", "disabled": "Off"}.get(method, method)
                           if method else None),
            "addresses": [str(a).split("/")[0] for a in (v4.get("address") or [])],
            "gateway": v4.get("gateway"),
            "dns": list(v4.get("nameservers") or []),
            "vlan": (itf.get("vlan") or {}).get("id") if isinstance(itf.get("vlan"), dict) else None,
        })
    itfs.sort(key=lambda i: (0 if str(i.get("label", "")).startswith("LAN") else 1, str(i.get("label") or i["name"])))
    return {
        "hostname": hi.get("hostname"),
        "interfaces": itfs,
        "internet": ni.get("host_internet"),
        "systems": list(systems or []),
    }


# ── changing a port (register 969z) ───────────────────────────────────────────────────────────────────────────────────
import ipaddress as _ip


def _v4(s):
    return _ip.IPv4Address(str(s).strip())


def update_body(form: dict) -> dict:
    """The Supervisor body for one port's new settings, checked. Raises ValueError with a plain sentence.

    form: {"method": "auto"|"static"|"disabled", "address", "prefix" (8–30), "gateway", "dns": [up to two]}.
    Automatic keeps the router's address and may name its own DNS; Static needs an address, a mask and a gateway on the
    same network; Off switches the port off."""
    m = str((form or {}).get("method") or "")
    if m not in ("auto", "static", "disabled"):
        raise ValueError("Choose Automatic, Static or Off.")
    if m == "disabled":
        return {"enabled": False}
    dns = []
    for d in (form.get("dns") or [])[:2]:
        if str(d or "").strip():
            try:
                dns.append(str(_v4(d)))
            except ValueError:
                raise ValueError("%s is not a DNS server address." % str(d).strip()[:40])
    body = {"enabled": True, "ipv4": {"method": m, "nameservers": dns}}
    if m == "static":
        try:
            addr = _v4(form.get("address"))
        except ValueError:
            raise ValueError("The address is not a valid IP address.")
        try:
            prefix = int(form.get("prefix"))
        except (TypeError, ValueError):
            raise ValueError("Choose the subnet mask.")
        if not 8 <= prefix <= 30:
            raise ValueError("Choose the subnet mask.")
        net = _ip.IPv4Network("%s/%d" % (addr, prefix), strict=False)
        if addr in (net.network_address, net.broadcast_address):
            raise ValueError("%s is the network's own address, not one a box can use." % addr)
        try:
            gw = _v4(form.get("gateway"))
        except ValueError:
            raise ValueError("The gateway is not a valid IP address.")
        if gw not in net:
            raise ValueError("The gateway %s is not on the same network as %s/%d." % (gw, addr, prefix))
        if gw == addr:
            raise ValueError("The gateway cannot be the box's own address.")
        if not dns:
            raise ValueError("A static address needs at least one DNS server (often the gateway).")
        body["ipv4"].update({"address": ["%s/%d" % (addr, prefix)], "gateway": str(gw)})
    return body


def restore_body(raw_itf: dict) -> dict:
    """A port's settings exactly as the Supervisor stated them — used to put them back."""
    raw = raw_itf or {}
    if raw.get("enabled") is False:
        return {"enabled": False}
    v4 = raw.get("ipv4") or {}
    m = v4.get("method") or "auto"
    body = {"enabled": True, "ipv4": {"method": m, "nameservers": list(v4.get("nameservers") or [])}}
    if m == "static":
        body["ipv4"]["address"] = list(v4.get("address") or [])
        body["ipv4"]["gateway"] = v4.get("gateway")
    return body


def raw_interface(network_info, name):
    for itf in (_strip(network_info).get("interfaces") or []):
        if itf.get("interface") == name:
            return itf
    return None
