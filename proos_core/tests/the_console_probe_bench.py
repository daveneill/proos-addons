"""THE CONSOLE PROBE — register 1238: measure an app before building it (Dave, 6 Nov: "finish all the build").
Run: python3 tests/the_console_probe_bench.py  (SOURCE — the route is plumbing; what it may return is the point)
  1. a FIXED list of read addresses per app — nothing asked for by the caller is fetched
  2. it only reads (get_json), never sends, never calls the platform
  3. it returns SHAPE only: status, a count, field NAMES — never a value (no record, no field's content)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "GET" and sub == "probe" and not sub2:')
r = src[i:src.index('if method == "GET" and sub == "ports" and sub2 == "where":', i)]
PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    (PASS := PASS + 1) if c else FAIL.append(m)
    print(("  ok  " if c else "  FAIL ") + m)


ok('paths = {"connect": (' in r and '.get(app)' in r and 'if not paths:' in r, "1 · a fixed list of read addresses per app")
ok("_uc.get_json(_creds, ph, pth)" in r and "send_json" not in r and "call_service" not in r and "get_media" not in r,
   "2 · it only reads")
ok('"fields": sorted(first.keys())' in r and 'sorted(v.keys())' in r and '"count": len(d)' in r
   and '"data": d' not in r and '"first": first' not in r and "values()" not in r,
   "3 · it returns shape only — counts and field names, never a value")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
