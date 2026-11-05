"""NETWORK → DEVICES, CLIENTS, WI-FI — register 1225 (the mockup's other Network sections; Dave 6 Nov: "sure go").
Run: python3 tests/the_network_devices_clients_wifi_bench.py

DRIVEN: the REAL proos.unifinet views over controller-shaped tables (shaped as Dave's console answered them).
  1. NO Wi-Fi password, key or secret ever leaves Core — whatever the controller's record carries
  2. devices: gateway first, then switches, then access points; online, firmware, an update waiting, clients, uplink
  3. clients: a wired client by switch and port, a Wi-Fi client by network, access point and signal
  4. Wi-Fi: each network's security in words, its bands, and how many clients are on it now
  5. the route only reads, and sends the views — never the raw records (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import unifinet as un                                  # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


GW, SW, AP = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03"
DEV = [
    {"mac": AP, "name": "Hallway", "model": "UALR6v2", "type": "uap", "state": 1, "version": "6.7.57", "num_sta": 24,
     "uptime": 1572480, "uplink": {"uplink_mac": SW, "uplink_remote_port": 3}},
    {"mac": SW, "name": "AV-Rack - USW-48", "model": "USL48P", "type": "usw", "state": 1, "version": "7.5.15", "num_sta": 25,
     "upgradable": True, "upgrade_to_firmware": "7.6.1", "port_table": [{"port_idx": i} for i in range(1, 53)],
     "uplink": {"uplink_mac": GW, "uplink_remote_port": 11}},
    {"mac": GW, "name": "Home", "model": "UDMPROMAX", "type": "udm", "state": 1, "version": "6.0.17", "num_sta": 89,
     "temperatures": [{"name": "CPU", "value": 54.2}], "port_table": [{"port_idx": 1}]},
]
CLI = [
    {"mac": "cc:01", "name": "UC-Display-SE-21", "is_wired": True, "sw_mac": SW, "sw_port": 9, "ip": "192.168.8.30", "network": "Default", "uptime": 3600},
    {"mac": "cc:02", "hostname": "Alfresco - LHS - Shelly Mini G4", "is_wired": False, "essid": "Home IoT", "ap_mac": AP, "signal": -54,
     "radio": "ng", "ip": "192.168.8.81", "network": "Default"},
    {"mac": "cc:03", "name": "iPhone", "is_wired": False, "essid": "Home", "ap_mac": AP, "signal": -61, "ip": "192.168.8.82"},
]
WL = [
    {"_id": "w1", "name": "Home IoT", "enabled": True, "security": "wpapsk", "wpa_mode": "wpa2", "wlan_bands": ["2g"],
     "x_passphrase": "SUPER-SECRET-PSK", "private_preshared_keys": [{"password": "PPSK-SECRET"}], "radius_secret": "RAD-SECRET"},
    {"_id": "w2", "name": "Home", "enabled": True, "security": "wpapsk", "wlan_bands": ["2g", "5g"], "x_passphrase": "HOME-PSK",
     "is_guest": False, "hide_ssid": False},
]

dv, cv, wv = un.devices_view(DEV), un.clients_view(CLI, DEV), un.wifi_view(WL, CLI)
blob = json.dumps([dv, cv, wv])
ok(not any(s in blob for s in ("SUPER-SECRET-PSK", "PPSK-SECRET", "RAD-SECRET", "HOME-PSK", "passphrase", "password", "secret")),
   "1 · no Wi-Fi password, key or secret leaves Core — only named fields")
ok([d["kind"] for d in dv] == ["Gateway", "Switch", "Access Point"] and dv[1]["upgradable"] and dv[1]["upgrade_to"] == "7.6.1"
   and dv[1]["uplink"] == "Home" and dv[1]["uplink_port"] == 11 and dv[2]["uplink"] == "AV-Rack - USW-48" and dv[0]["temperature"] == 54.2
   and dv[1]["ports"] == 52 and dv[2]["ports"] is None and dv[0]["clients"] == 89,
   "2 · devices: gateway, switch, access point; update waiting, uplink by name and port, temperature, port count")
by = {c["mac"]: c for c in cv}
ok(by["cc:01"]["wired"] and by["cc:01"]["switch"] == "AV-Rack - USW-48" and by["cc:01"]["port"] == 9
   and by["cc:02"]["wifi"] == "Home IoT" and by["cc:02"]["ap"] == "Hallway" and by["cc:02"]["signal"] == -54
   and by["cc:02"]["name"] == "Alfresco - LHS - Shelly Mini G4" and by["cc:02"]["switch"] is None,
   "3 · clients: wired by switch and port; Wi-Fi by network, access point and signal")
w = {x["name"]: x for x in wv}
ok(w["Home IoT"]["security"] == "WPA Personal" and w["Home IoT"]["bands"] == ["2.4 GHz"] and w["Home IoT"]["clients"] == 1
   and w["Home"]["bands"] == ["2.4 GHz", "5 GHz"] and w["Home"]["clients"] == 1,
   "4 · Wi-Fi: security in words, bands, clients on it now")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "network":')
# (pin narrowed, register 1227: the Wi-Fi on/off route now follows the reading route; the READING route still only reads)
route = src[i:src.index('if method == "POST" and sub == "network" and sub2 == "wifi":', i)]
ok("devices_view(" in route and "clients_view(" in route and "wifi_view(" in route and "call_service" not in route
   and '"wifi": (wl' not in route and "POST" not in route,
   "5 · the route only reads and sends the named-field views, never the raw records")

# REGISTER 1227 — Wi-Fi on/off through the platform's own switch
ents = [{"platform": "unifi", "unique_id": "wlan-w1", "entity_id": "switch.home_iot", "disabled_by": None},
        {"platform": "unifi", "unique_id": "wlan-w2", "entity_id": "switch.home", "disabled_by": "integration"},
        {"platform": "other", "unique_id": "wlan-w1", "entity_id": "switch.not_unifi"}]
wv2 = {x["name"]: x for x in un.wifi_view(WL, CLI, ents)}
ok(wv2["Home IoT"]["control"] == {"entity_id": "switch.home_iot", "enabled": True}
   and wv2["Home"]["control"] == {"entity_id": "switch.home", "enabled": False} and "SUPER-SECRET-PSK" not in json.dumps(wv2),
   "6 · each Wi-Fi network is joined to the platform's own Wi-Fi switch by its unique id (and still carries no password)")
j = src.index('if method == "POST" and sub == "network" and sub2 == "wifi":')
act = src[j:src.index('if method == "GET" and sub == "ports" and sub2 == "where":', j)]
# Pin rewritten, not weakened (register 1228): the switch is now found by the SAME exact-id join the page is drawn
# from (wlan_controls + wlan_control_for, which keep platform == unifi and "wlan-") instead of a second copy of it.
ok('_un.wlan_controls(' in act and '_un.wlan_control_for(' in act and "> 1:" in act
   and '_client.call_service("switch", "turn_on" if on else "turn_off", e["entity_id"])' in act
   and 'bool(row.get("enabled")) == on' in act and '"confirmed": False' in act and 'e.get("disabled_by")' in act,
   "7 · on/off presses only the platform's own Wi-Fi switch (the same exact-id join), refuses one the platform has "
   "switched off or two networks claim, and says 'confirmed' only when the controller reports the change")

# REGISTER 1228 — MEASURED on Dave's box: the platform's switch is wlan-<24-char id>; the record's _id is a different
# 36-char id. The join finds the platform's id EXACTLY wherever the record keeps it — never by name — and reports
# only the field's name.
P1, P2 = "6a1b2c3d4e5f60718293" + "68f7", "6a1b2c3d4e5f60718293" + "139f"
WL3 = [
    {"_id": "0b8e4f7a-1111-4c2a-9d3e-aaaaaaaa4a1c", "name": "Home", "enabled": True, "external_id": P1, "x_passphrase": "HOME-PSK"},
    {"_id": "0b8e4f7a-2222-4c2a-9d3e-bbbbbbbb4d67", "name": "Home IoT", "enabled": True, "external_id": P2, "x_passphrase": "IOT-PSK"},
    {"_id": "0b8e4f7a-3333-4c2a-9d3e-cccccccc0001", "name": "Guest", "enabled": True, "x_passphrase": "G-PSK"},
]
ents3 = [{"platform": "unifi", "unique_id": "wlan-" + P1, "entity_id": "switch.home"},
         {"platform": "unifi", "unique_id": "wlan-" + P2, "entity_id": "switch.home_iot"},
         {"platform": "unifi", "unique_id": "wlan-Guest", "entity_id": "switch.guest_by_name"}]
v3 = {x["name"]: x for x in un.wifi_view(WL3, CLI, ents3)}
dup = [dict(WL3[0]), dict(WL3[1], external_id=P1)]
v4 = un.wifi_view(dup, CLI, ents3)
ok(v3["Home"]["control"]["entity_id"] == "switch.home" and v3["Home IoT"]["control"]["entity_id"] == "switch.home_iot"
   and v3["Home"]["control_found_in"] == "external_id" and v3["Guest"]["control"] is None
   and "PSK" not in json.dumps(v3) and all(x["control"] is None for x in v4),
   "8 · a record whose _id differs from the platform's still finds its switch by the exact id it carries; a name is "
   "never a match; a switch two networks claim is nobody's; no password leaves")

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
