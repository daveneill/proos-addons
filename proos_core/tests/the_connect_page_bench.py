"""UNIFI CONNECT — register 1239 (Dave, 6 Nov: "connect is already there so finish all the build please").
Run: python3 tests/the_connect_page_bench.py

DRIVEN: the REAL networkviews.connect_view over a record shaped as Dave's console answered the 1238 probe.
  1. each display: name, model, online, firmware, and its live state (screen, brightness, volume, mode, rotation, sleep,
     auto reload, CEC) and the actions it supports by name
  2. NOTHING else leaves Core: extraInfo's addresses, MACs and fingerprint, the controller id, the app list, hostnames
  3. an action name that is not a plain word is dropped
  4. the route only reads and sends the view (SOURCE)
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


D = [{"id": "c1", "name": "UC-Display-SE-21", "online": True, "firmwareVersion": "1.2.3", "ip": "192.168.8.30", "mac": "AA:BB",
      "hostname": "uc-display-secret-host", "featureFlags": {"brightness": True, "volume": True},
      "extraInfo": {"ethIP": "192.168.8.30", "fingerprint": "FP-SECRET", "controllerID": "CTRL-SECRET", "wifiMac": "CC:DD"},
      "type": {"fullName": "UniFi Connect Display SE 21", "supportedActions": [{"name": "display_on", "id": "u1"}, {"name": "brightness"}, "reboot", {"name": "<script>"}]},
      "shadow": {"display": True, "brightness": 80, "volume": 35, "rotate": 0, "mode": "web_page", "sleepMode": False,
                 "autoReload": True, "isSupportCec": True, "appList": [{"pkg": "com.secret.app"}], "currentHomePage": "https://secret.example/page"}}]
v = nv.connect_view(D)[0]
ok(v["name"] == "UC-Display-SE-21" and v["model"] == "UniFi Connect Display SE 21" and v["online"] and v["firmware"] == "1.2.3"
   and v["screen_on"] is True and v["brightness"] == 80 and v["volume"] == 35 and v["mode"] == "web_page" and v["rotate"] == 0
   and v["sleep"] is False and v["auto_reload"] is True and v["cec"] and v["actions"] == ["display_on", "brightness", "reboot"],
   "1 · each display with its live state and the actions it supports by name")
blob = json.dumps(v)
ok(not any(x in blob for x in ("192.168", "SECRET", "secret", "AA:BB", "CC:DD", "com.", "https")), "2 · no address, MAC, fingerprint, controller id, app or page leaves Core")
ok("<script>" not in blob, "3 · an action name that is not a plain word is dropped")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "connect" and not sub2:')
r = src[i:src.index('if method == "GET" and sub == "probe" and not sub2:', i)]
ok('_uc.get_json(_creds, ph, "/proxy/connect/api/v2/devices?shadow=true")' in r and "_nv.connect_view(" in r
   and "send_json" not in r and "call_service" not in r and '"displays": rows' not in r,
   "4 · the route only reads and sends the view")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
