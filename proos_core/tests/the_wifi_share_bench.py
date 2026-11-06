"""SHARE WI-FI — register 1242 (the mockup's "Share Wi-Fi", through the platform's own QR images, disabled as shipped).
Run: python3 tests/the_wifi_share_bench.py

DRIVEN: the REAL unifinet join over registry rows as measured on Dave's box (qr_code-<id>, disabled as shipped).
  1. the QR image is found by the same exact-id join as the network's on/off switch, under its own prefix; never by name
  2. the platform image fetch takes only the platform's own image addresses (SOURCE of live_ha.get_image)
  3. the route: GET sends only the platform's picture (never the password as text); POST enable only switches the
     platform's QR image on; a switched-off image says so (SOURCE)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import unifinet as un                                   # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


P = "6a1b2c3d4e5f60718293" + "68f7"
ents = [{"platform": "unifi", "unique_id": "wlan-" + P, "entity_id": "switch.home"},
        {"platform": "unifi", "unique_id": "qr_code-" + P, "entity_id": "image.home", "disabled_by": "integration"},
        {"platform": "unifi", "unique_id": "qr_code-Home", "entity_id": "image.by_name"}]
rec = {"_id": "0b8e4f7a-1111-4c2a-9d3e-aaaaaaaa4a1c", "name": "Home", "legacy_id": P, "x_passphrase": "HOME-PSK"}
qr, where = un.wlan_control_for(rec, un.wlan_controls(ents, "qr_code-"))
sw, _w = un.wlan_control_for(rec, un.wlan_controls(ents))
ok(qr == {"entity_id": "image.home", "enabled": False} and where == "legacy_id" and sw["entity_id"] == "switch.home",
   "1 · the QR image is found by the same exact-id join as the switch, under its own prefix; never by name")
lh = open(os.path.join(HERE, "..", "proos", "live_ha.py"), encoding="utf-8").read()
g = lh[lh.index("def get_image(self, path: str):"):lh.index("def all_states(self)")]
ok('if not path.startswith("/api/image_proxy/"):' in g and 'if not ct.startswith("image/"):' in g,
   "2 · the platform image fetch takes only the platform's image addresses and only an image answer")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if sub == "wifiqr" and method in ("GET", "POST"):')
r = src[i:src.index('if method == "POST" and sub == "connect" and sub2 == "act":', i)]
ok('_un.wlan_controls(ents, "qr_code-")' in r and '_client.enable_entity(qr["entity_id"])' in r and r.count("enable_entity(") == 1
   and '_client.get_image("/api/image_proxy/" + qr["entity_id"])' in r and "x_passphrase" not in r and "call_service" not in r
   and '"disabled": True' in r,
   "3 · GET sends only the platform's picture; POST enable only switches its QR image on; switched off says so")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
