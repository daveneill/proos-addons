"""THE CONSOLE PROBE — register 1238: measure an app before building it (Dave, 6 Nov: "finish all the build").
Run: python3 tests/the_console_probe_bench.py

DRIVEN: the REAL uniconsole.shape_of over answers full of values, and the real PROBE_PATHS.
  1. a FIXED list of read addresses per app; an app not on it has none
  2. shape_of returns a count, field names and inner field names — never a value, whatever the answer holds
  3. the route takes its addresses only from that list, only reads, and sends only the shape (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import uniconsole as uc                                 # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


ok(all(p.startswith("/proxy/connect/") for p in uc.PROBE_PATHS["connect"]) and uc.PROBE_PATHS.get("anything") is None,
   "1 · a fixed list of read addresses per app; nothing else")
ans = {"data": [{"id": "DEV-SECRET-ID", "name": "Kitchen Display", "token": "TOKEN-SECRET", "status": {"power": True, "volume": 40, "ip": "192.168.8.30"}}]}
n, fields, inner = uc.shape_of(ans)
blob = json.dumps([n, fields, inner])
ok(n == 1 and fields == ["id", "name", "status", "token"] and inner == {"status": ["ip", "power", "volume"]}
   and not any(x in blob for x in ("SECRET", "Kitchen", "192.168", "40", "true")) and uc.shape_of(None) == (None, [], {}),
   "2 · shape only — a count and field names; no value from the answer")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "probe" and not sub2:')
r = src[i:src.index('if method == "GET" and sub == "ports" and sub2 == "where":', i)]
ok("paths = _uc.PROBE_PATHS.get(app)" in r and "_uc.get_json(_creds, ph, pth)" in r and "_uc.shape_of(j)" in r
   and "send_json" not in r and "call_service" not in r and '"data"' not in r,
   "3 · the route uses only the fixed list, only reads, sends only the shape")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
