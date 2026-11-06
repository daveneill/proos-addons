"""NETWORK → NETWORKS · INTERNET · GUESTS · FIREWALL & RULES · POWER · EVENTS — register 1237 (Dave, 6 Nov: "okay go").
Run: python3 tests/the_network_sections_bench.py

DRIVEN: the REAL proos.networkviews over controller-shaped records (measured on Dave's console 6 Nov).
  1. NOTHING secret leaves Core: VPN private keys, pre-shared keys, RADIUS secrets, portal passwords, payment keys
  2. networks: Internet first, then networks, guest, VPNs; VLAN, subnet, DHCP range; on/off
  3. internet: status, provider, address, latency, uptime, last speed test; each internet connection by name
  4. firewall: your rules before UniFi's built-in ones, each with action and zone → zone BY NAME; port forwards; counts
  5. power: per switch, PoE drawn of its budget and the busiest ports; a switch with no PoE is left out
  6. guests and events in words (CLIENT_ROAMED_2 → Client Roamed), newest first
  7. the route only reads (Events is the console's own log search), through ProOS's own user, sending only the views (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import networkviews as nv                               # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


NC = [{"_id": "n1", "name": "Default", "purpose": "corporate", "ip_subnet": "192.168.8.1/24", "dhcpd_enabled": True, "dhcpd_start": "192.168.8.6", "dhcpd_stop": "192.168.8.254"},
      {"_id": "n2", "name": "WireGuard Server", "purpose": "remote-user-vpn", "x_wireguard_private_key": "WG-PRIVATE-SECRET", "ip_subnet": "192.168.3.1/24"},
      {"_id": "n3", "name": "Office VPN", "purpose": "vpn-client", "x_ipsec_pre_shared_key": "PSK-SECRET", "x_openvpn_password": "OVPN-SECRET"},
      {"_id": "n4", "name": "Internet 1", "purpose": "wan", "wan_networkgroup": "WAN", "wan_type": "dhcp", "x_pppoe_password": "PPPOE-SECRET"},
      {"_id": "n5", "name": "IoT", "purpose": "corporate", "vlan_enabled": True, "vlan": 20, "ip_subnet": "10.0.20.1/24", "enabled": False}]
HL = [{"subsystem": "wan", "status": "ok", "isp_name": "Aussie Broadband", "wan_ip": "203.0.113.9", "gw_name": "Home"},
      {"subsystem": "www", "status": "ok", "latency": 7, "uptime": 90061, "xput_down": 941.2, "xput_up": 48.7}]
GA = [{"portal_enabled": True, "auth": "password", "x_password": "GUEST-SECRET", "radius_secret": "RAD-SECRET", "x_stripe_api_key": "STRIPE-SECRET", "expire": 480}]
WL = [{"name": "Home", "is_guest": False, "x_passphrase": "HOME-PSK"}, {"name": "Visitors", "is_guest": True, "x_passphrase": "VIS-PSK"}]
GS = [{"name": "Phone", "authorized": True, "start": 1000, "end": 4600}]
FZ = [{"_id": "z1", "name": "Internal"}, {"_id": "z2", "name": "External"}, {"_id": "z3", "name": "Hotspot"}]
FP = [{"name": "Allow Return Traffic", "action": "ALLOW", "predefined": True, "index": 1, "source": {"zone_id": "z2"}, "destination": {"zone_id": "z1"}},
      {"name": "Block IoT to LAN", "action": "BLOCK", "predefined": False, "index": 10000, "enabled": True, "protocol": "all",
       "source": {"zone_id": "z3"}, "destination": {"zone_id": "z1"}}]
PF = [{"name": "Plex", "enabled": True, "dst_port": "32400", "fwd": "192.168.8.20", "fwd_port": "32400", "proto": "tcp", "src": "any"}]
DV = [{"name": "AV-Rack - USW-48", "model": "USL48P", "total_max_power": 195, "port_table": [{"port_idx": 1, "poe_power": "7.0"}, {"port_idx": 3, "poe_power": "12.4", "name": "AP Lounge"}, {"port_idx": 5}]},
      {"name": "Bedroom - TV", "model": "USMINI", "port_table": [{"port_idx": 1}]}]
EV = [{"key": "CLIENT_ROAMED_2", "timestamp": 2000, "category": "client", "message": "iPhone roamed from Hallway to Lounge"},
      {"key": "CLIENT_CONNECTED_WIRED_2", "timestamp": 3000}]

views = [nv.networks_view(NC), nv.internet_view(HL, NC), nv.guests_view(GS, GA, WL), nv.firewall_view(FP, FZ, PF, []), nv.power_view(DV), nv.events_view(EV)]
blob = json.dumps(views)
ok(not any(x in blob for x in ("SECRET", "PSK", "x_", "password", "private")), "1 · no VPN key, pre-shared key, RADIUS or portal secret, payment key or Wi-Fi password leaves Core")
n = views[0]
ok([x["kind"] for x in n] == ["Internet", "Network", "Network", "VPN Server", "VPN Client"] and n[1]["dhcp_range"] == "192.168.8.6 – 192.168.8.254"
   and n[2]["vlan"] == 20 and not n[2]["enabled"] and n[0]["wan_slot"] == "WAN" and n[0]["subnet"] is None,
   "2 · networks: Internet first, then networks and VPNs; VLAN, subnet, DHCP range; on/off")
i = views[1]
ok(i["status"] == "ok" and i["isp"] == "Aussie Broadband" and i["address"] == "203.0.113.9" and i["latency_ms"] == 7 and i["uptime_s"] == 90061
   and i["down_mbps"] == 941.2 and i["wans"] == [{"name": "Internet 1", "slot": "WAN", "type": "dhcp", "enabled": True}],
   "3 · internet: status, provider, address, latency, uptime, speed test; each connection by name")
f = views[3]
ok([r["name"] for r in f["rules"]] == ["Block IoT to LAN", "Allow Return Traffic"] and f["rules"][0]["from"] == "Hotspot" and f["rules"][0]["to"] == "Internal"
   and f["rules"][0]["action"] == "Block" and f["rules"][0]["protocol"] == "Any" and f["custom"] == 1 and f["built_in"] == 1
   and f["port_forwards"][0]["to"] == "192.168.8.20" and f["port_forwards"][0]["from"] == "Anywhere" and f["zones"] == ["External", "Hotspot", "Internal"],
   "4 · firewall: your rules first, action and zone → zone by name; port forwards; counts")
p = views[4]
ok(len(p) == 1 and p[0]["used_w"] == 19.4 and p[0]["budget_w"] == 195 and p[0]["top"][0] == {"port": 3, "name": "AP Lounge", "watts": 12.4},
   "5 · power: PoE drawn of the budget, busiest ports first; a switch without PoE left out")
g, e = views[2], views[5]
ok(g["guest_wifi"] == ["Visitors"] and g["portal"] and g["how"] == "Password" and g["guests_now"][0]["minutes_left"] == 60
   and [x["kind"] for x in e] == ["Client Connected Wired", "Client Roamed"] and e[1]["message"].startswith("iPhone roamed"),
   "6 · guests and events in words, newest first")
# 1237b — MEASURED on Dave's box: a switch with no PoE read "0 W of 0 W"; no speed test read "0 Mbps / 0 Mbps"; log entries
# had no message, so an event never said who.
DV2 = [{"name": "USW-24-G2", "total_max_power": 0, "port_table": [{"port_idx": 1, "poe_power": "0"}]}]
I2 = nv.internet_view([{"subsystem": "www", "status": "ok", "xput_down": 0, "xput_up": 0}], [])
E2 = nv.events_view([{"key": "CLIENT_ROAMED_2", "timestamp": 5, "parameters": {"CLIENT": {"name": "Dave's iPhone", "id": "aa:bb"}, "CHANNEL": {"name": "153"}, "NUM": 3}}])
shape = nv.event_fields([{"key": "K", "timestamp": 1, "parameters": {"CLIENT": {"name": "x", "ip": "1.2.3.4"}}}])
E3 = nv.events_view([{"key": "CLIENT_ROAMED_2", "timestamp": 6, "message_raw": "{CLIENT} roamed from {DEVICE_FROM} to {DEVICE_TO} on channel {CHANNEL}.",
                       "parameters": {"CLIENT": {"name": "iPhone 4707"}, "DEVICE_FROM": {"name": "Hallway"}, "DEVICE_TO": {"name": "Lounge"},
                                      "CHANNEL": {"name": "153"}, "CHANNEL_WIDTH": {"name": "80"}}}])
ok(E3[0]["who"] == ["iPhone 4707", "Hallway", "Lounge"] and E3[0]["message"] == "iPhone 4707 roamed from Hallway to Lounge on channel 153.",
   "9 · (1237c, measured) only the who/where details name an entry — not channel numbers; the controller's own sentence fills its blanks")
ok(nv.power_view(DV2) == [] and I2["down_mbps"] is None and I2["up_mbps"] is None and E2[0]["who"] == ["Dave's iPhone"]
   and shape == {"fields": ["key", "parameters", "timestamp"], "parameters": ["CLIENT"]} and "1.2.3.4" not in json.dumps(shape),
   "8 · no-PoE switch left out; no speed test is none; an event names who from its details; the log's shape reports names only")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
a = src.index('if method == "GET" and sub == "netsec" and not sub2:')
route = src[a:src.index('if method == "GET" and sub == "ports" and sub2 == "where":', a)]
ok(route.count("send_json(") == 1 and '"POST", V + "system-log/all"' in route and "call_service" not in route
   and all(x in route for x in ("_nv.networks_view(", "_nv.internet_view(", "_nv.guests_view(", "_nv.firewall_view(", "_nv.power_view(", "_nv.events_view("))
   and '"networks": nc' not in route and '"firewall": fp' not in route,
   "7 · the route only reads (the one POST is the console's own log search) and sends only the views")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
