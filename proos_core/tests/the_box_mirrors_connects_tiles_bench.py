"""THE BOX MIRRORS PROOS CONNECT'S APP TILE LIBRARY — register 1350 (15 Nov 2026).
Run: python3 tests/the_box_mirrors_connects_tiles_bench.py

Dave 15 Nov 08:22: "Only from Connect as it keeps the dashboard our premium look not cheap logos thrown on buttons by an
installer that look bad." 08:23 "yes" to the plan.

DRIVEN END TO END — the real ProOS Connect relay (connect/relay: relay.py + tiles.py + Core's appart.py) over real HTTP,
and the box's real mirror (proos/tilesync.py + proos/appart.py) pointed at it:
  1. until Dave switches the library on for every box, a box keeps what it has (nothing removed, nothing fetched)
  2. switched on: the box takes every tile Connect has, removes the ones Connect hasn't, and copies the brand store,
     hidden tiles and retired apps — the app's name resolves on the box at once
  3. a tile replaced on Connect arrives on the box; one deleted on Connect leaves it; an unchanged tile is not fetched again
  4. a tile whose bytes are not what Connect listed is refused (kept out, said as failed)
  5. the box reports its wordmark apps to Connect only when that list changes; a room page's ask carries what was typed
  6. the move: the box's own tiles and taught apps are OFFERED, wait on Connect, and reach the library only when Dave adds them
  7. a suspended home gets no library; the admin tile API answers only a signed-in admin
  8. the box's old tile write doors all answer 410 in words (SOURCE); the mirror runs in Core's Connect loop (SOURCE)
"""
import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "..")
RELAY_DIR = os.path.join(CORE, "..", "connect", "relay")
CX = tempfile.mkdtemp(prefix="cx_tiles_")
BOX = tempfile.mkdtemp(prefix="box_tiles_")
os.environ["PROOS_CONNECT_DIR"] = CX
os.environ["PROOS_DATA_DIR"] = BOX
sys.path.insert(0, CORE)
sys.path.insert(0, RELAY_DIR)
import relay  # noqa: E402
import tiles as CT  # noqa: E402
from proos import appart as A, connectkeys as CK, tilesync as TS  # noqa: E402

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:%d" % srv.server_address[1]
st = relay.load()
st["boxes"]["px-home"] = {"secret_hash": relay._h("sec"), "status": "approved", "plan": "active", "name": "Home A"}
st["boxes"]["px-off"] = {"secret_hash": relay._h("sec2"), "status": "approved", "plan": "suspended", "name": "Home Off"}
relay.save(st)
relay._sessions["adm"] = 9e9
CK._url = lambda: B
CK._creds = lambda: {"X-ProOS-Box": "px-home", "Authorization": "Bearer sec"}
PNG = open(os.path.join(CORE, "appart", "netflix.png"), "rb").read()
PNG2 = open(os.path.join(CORE, "appart", "7plus.png"), "rb").read()


def admin(path, body):
    req = urllib.request.Request(B + "/admin/tiles/api" + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Cookie": "pc=adm"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def b64(b):
    return base64.b64encode(b).decode()


# the box starts with tiles of its own (an installer's upload from before)
os.makedirs(A._UPLOADED, exist_ok=True)
open(os.path.join(A._UPLOADED, "my_logo.png"), "wb").write(PNG2)
A.add_brand("My App", "myapp.com.au", packages=["com.my.app"])

# 1
admin("/apps/art/upload", {"name": "Stan", "image": b64(PNG)})
r = TS.sync()
ok(r == {"live": False} and os.path.isfile(os.path.join(A._UPLOADED, "my_logo.png"))
   and not os.path.isfile(os.path.join(A._UPLOADED, "stan.png")),
   "1 · library not on for every box yet: the box keeps what it has, takes nothing")

# 6 (before switching on) — the move
off = TS.offer()
st = relay.load()
ok(off.get("ok") and off["names"] == ["my_logo"] and st["tiles"]["offers"]["px-home"]["home"] == "Home A"
   and not CT.tile_file("my_logo"),
   "6 · the box's own tiles are OFFERED and wait on Connect — not in the library until Dave adds them")
code, res = admin("/tiles/offer", {"box": "px-home", "take": True})
ok(code == 200 and res["added"] == ["my_logo"] and res["brands"] == ["my_app"] and CT.tile_file("my_logo") == PNG2
   and "px-home" not in (relay.load()["tiles"].get("offers") or {}),
   "  …Add to Library: the tiles and the app it was taught are in the library; the offer is gone")

# 2
admin("/apps/art/delete", {"slug": "netflix"})          # a shipped tile hidden on Connect
admin("/apps/brands/add", {"name": "Binge", "domain": "binge.com.au", "ids": ["au.com.binge"]})
admin("/tiles/live", {"on": True})
open(os.path.join(A._UPLOADED, "junk_upload.png"), "wb").write(PNG2)       # something only this box has
r = TS.sync()
have = sorted(A._ls(A._UPLOADED))
ok(r["live"] and have == ["my_logo", "stan"] and sorted(r["removed"]) == ["junk_upload"],
   "2 · switched on: the box has exactly Connect's tiles — Stan arrives, the box-only upload goes")
ok("netflix" in A.hidden() and A.canonical("au.com.binge")["name"] == "Binge" and A.tile_path("netflix") is None,
   "  …and Connect's hidden tiles and app records — Binge resolves from its device id at once, Netflix's tile is hidden")

# 3
admin("/apps/art/upload", {"name": "Stan", "image": b64(PNG2)})
admin("/apps/art/delete", {"slug": "my_logo"})
r = TS.sync()
ok(r["fetched"] == ["stan"] and r["removed"] == ["my_logo"]
   and open(os.path.join(A._UPLOADED, "stan.png"), "rb").read() == PNG2,
   "3 · replaced on Connect → replaced on the box; deleted on Connect → gone from the box")
r = TS.sync()
ok(r["fetched"] == [] and r["removed"] == [], "  …and nothing unchanged is fetched again")

# 4
admin("/apps/art/upload", {"name": "Kayo", "image": b64(PNG)})
r = TS.sync(get_bytes=lambda p: b"not the tile Connect listed" * 10 if "kayo" in p else TS._get_bytes(p))
ok(r["failed"] == ["kayo"] and not os.path.isfile(os.path.join(A._UPLOADED, "kayo.png")),
   "4 · bytes that are not what Connect listed are refused — kept out and said as failed")

# 5
sent = []
r1 = TS.report_wanted(["Stan", "Some New App"], post=lambda p, b: sent.append((p, b)) or {})
r2 = TS.report_wanted(["Stan", "Some New App"], post=lambda p, b: sent.append((p, b)) or {})
ok(r1["sent"] == 1 and r2["sent"] == 0 and sent[0][1]["apps"] == [{"name": "Some New App"}],
   "5 · the box tells Connect its wordmark apps — once per change, not every ten minutes")
TS.ask_for_app("Fetch Kids", "fetchtv.com.au", ["com.fetch.kids"], "fetch_kids_tv")
w = CT.wanted_now(relay.load())
ok(w.get("fetch_kids", {}).get("domain") == "fetchtv.com.au" and w["fetch_kids"]["ids"] == ["com.fetch.kids"]
   and w["fetch_kids"]["homes"] == ["Home A"],
   "  …a room page's ask reaches Connect's list with the domain, device id and the home")

# 7
CK._creds = lambda: {"X-ProOS-Box": "px-off", "Authorization": "Bearer sec2"}
try:
    TS.sync()
    ok(False, "7 · a suspended home gets no library")
except urllib.error.HTTPError as e:
    ok(e.code == 403, "7 · a suspended home gets no library")
req = urllib.request.Request(B + "/admin/tiles/api/apps/art/upload", method="POST",
                             data=json.dumps({"name": "x", "image": b64(PNG)}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
    ok(False, "  …the admin tile API answers only a signed-in admin")
except urllib.error.HTTPError as e:
    ok(e.code == 403, "  …the admin tile API answers only a signed-in admin")

# 8
src = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
i = src.index('["apps", "art", "source"], ["apps", "art", "import"], ["apps", "art", "upload"]')
blk = src[i:i + 700]
ok(all(w in blk for w in ('"clear"', '"restore"', '"delete"', '["apps", "brands", "add"]', '["apps", "brands", "remove"]',
                          '["apps", "brands", "restore"]')) and "self._send(410" in blk and "managed on ProOS Connect" in blk,
   "8 · (SOURCE) every old tile write door on the box answers 410, in words")
ok("_ts_boot.sync()" in src and "_ts_boot.report_wanted(_site_app_names())" in src,
   "  …(SOURCE) the mirror and the wordmark report run in Core's Connect loop")
ok(open(os.path.join(RELAY_DIR, "appart.py"), "rb").read() == open(os.path.join(CORE, "proos", "appart.py"), "rb").read(),
   "  …Connect runs Core's own tile module — the same file, byte for byte (no second copy that drifts)")
ok(sorted(os.listdir(os.path.join(RELAY_DIR, "appart"))) == sorted(os.listdir(os.path.join(CORE, "appart"))),
   "  …and carries the same ProOS pack")

srv.shutdown()
print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
