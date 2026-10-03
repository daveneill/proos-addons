"""The box's own network, as the platform's Supervisor states it — plan item 12, T4.

The Network Change plan (13 Sep 2026), build 4: "a Network section … the box's hostname,
address, gateway, DNS, DHCP or static — readable by an installer … Beside it, one line per
outside system Core talks to: reachable or not, and why." Builds 1–3 shipped (registers
437–439); this is the reading half of build 4. Nothing here is kept: every call reads the
Supervisor's own answer (the mirror ruling). Setting a static address is not built here.

Pure: `summarise(network_info, host_info, systems)` → the facts Pro shows. A field the
platform did not give is None — never a guess.
"""


def _strip(d):
    return (d or {}).get("data") if isinstance((d or {}).get("data"), dict) else (d or {})


def summarise(network_info, host_info=None, systems=None) -> dict:
    ni, hi = _strip(network_info), _strip(host_info)
    itfs = []
    for itf in (ni.get("interfaces") or []):
        v4 = itf.get("ipv4") or {}
        method = v4.get("method")
        itfs.append({
            "name": itf.get("interface"),
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
    itfs.sort(key=lambda i: (not i["primary"], not i.get("connected"), str(i["name"])))
    return {
        "hostname": hi.get("hostname"),
        "interfaces": itfs,
        "internet": ni.get("host_internet"),
        "systems": list(systems or []),
    }
