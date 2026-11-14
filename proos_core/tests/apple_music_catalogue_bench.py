"""APPLE MUSIC'S OWN CATALOGUE FOR THE HOME'S SONOS — music design stage 4, register 759.
Run: python3 tests/apple_music_catalogue_bench.py

Proven from Dave's Mac (742): Apple's catalogue search with the Protech key, and the Sonos playing an Apple
song by its catalogue id with its own Apple sign-in. Dave's ruling 3 (11 Oct): the installer pastes a TOKEN
(never the key) now; ProOS Connect later. DRIVEN: applemusic with Apple's answers stood in (shapes as Apple
documents them), and speakermusic's Apple road with the speaker stood in. Apple from the box is a MANUAL GATE.
"""
import base64
import io
import json
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_apple_")

from proos import applemusic as AM   # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


def tok(exp, kid="K55WJWFRSD", iss="7XG3S6MMW8"):
    e = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return "%s.%s.signature" % (e({"alg": "ES256", "kid": kid}), e({"iss": iss, "iat": 0, "exp": exp}))


# 1 · the token
check("1 · no token yet: said so", AM.status(), {"set": False})
for bad, why in (("hello", "isn't an Apple Music token"), (tok(time.time() - 60), "already expired")):
    try:
        AM.save_token(bad)
        got = None
    except AM.AppleMusicError as e:
        got = why in str(e)
    check("1 · refused, with the reason: %s" % why, got, True)
good = tok(time.time() + 180 * 86400)
st = AM.save_token("  " + good[:20] + "\n" + good[20:] + "  ")       # pasted with a line break and spaces
check("1 · a good token is stored; the status says whose key and when it ends",
      (st["set"], st["key_id"], st["team_id"], round(st["days_left"])), (True, "K55WJWFRSD", "7XG3S6MMW8", 180))
check("1 · …and NEVER carries the token back out", good in json.dumps(st) or "signature" in json.dumps(st), False)
check("1 · ready to search", AM.ready(), True)

# 2 · Apple's answers → the same shape as a speaker service's items
SENT = []
ANSWERS = {}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen(req, timeout=None):
    SENT.append((req.full_url, req.headers.get("Authorization")))
    for k, v in ANSWERS.items():
        if k in req.full_url:
            return Resp(json.dumps(v).encode())
    return Resp(b'{"data": []}')


AM.urllib.request.urlopen = urlopen


def obj(kind, cid, name, artist=None):
    a = {"name": name, "artwork": {"url": "https://is1.mzstatic.com/x/{w}x{h}bb.jpg"}}
    if artist:
        a["artistName"] = artist
    return {"id": cid, "type": kind, "attributes": a}


ANSWERS["/search"] = {"results": {
    "songs": {"data": [obj("songs", "1293671142", "Relaxing", "Ocean Beach Waves Consort")]},
    "playlists": {"data": [obj("playlists", "pl.4f2b", "Spa")]},
    "artists": {"data": [obj("artists", "4488522", "P!nk")]}}}
res = AM.search("relaxing", "au")
check("2 · searched in the home's store, with the token as Apple asks", ("/catalog/au/search" in SENT[-1][0], SENT[-1][1] == "Bearer " + good), (True, True))
check("2 · Apple's own groups, in order", [g["category"] for g in res], ["songs", "artists", "playlists"])
s0 = res[0]["items"][0]
check("2 · a song: plays, doesn't open; artwork at a real size", (s0["id"], s0["play"], s0["open"], s0["art"]),
      ("apple:songs:1293671142", True, False, "https://is1.mzstatic.com/x/300x300bb.jpg"))
check("2 · an artist opens (to their top songs) and isn't played as itself", (res[1]["items"][0]["open"], res[1]["items"][0]["play"]), (True, False))
ANSWERS["/charts"] = {"results": {"playlists": [{"name": "Top Playlists", "data": [obj("playlists", "pl.1", "Today's Hits")]}],
                                  "songs": [{"name": "Top Songs", "data": [obj("songs", "9", "Song")]}]}}
ch = AM.charts("au")
check("2 · the home page for Apple Music is Apple's own charts, with Apple's own names", [c["title"] for c in ch], ["Top Playlists", "Top Songs"])
ANSWERS["/playlists/pl.4f2b/tracks"] = {"data": [obj("songs", "1", "A"), obj("songs", "2", "B")]}
ANSWERS["/artists/4488522/view/top-songs"] = {"data": [obj("songs", "3", "So What")]}
check("2 · a playlist's songs", [c["id"] for c in AM.tracks("apple:playlists:pl.4f2b", "au")], ["apple:songs:1", "apple:songs:2"])
check("2 · an artist's top songs", [c["title"] for c in AM.tracks("apple:artists:4488522", "au")], ["So What"])
ANSWERS.clear()


def http_err(req, timeout=None):
    raise AM.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)


AM.urllib.request.urlopen = http_err
try:
    AM.search("x", "au")
    msg = ""
except AM.AppleMusicError as e:
    msg = str(e)
check("2 · Apple refusing the token is said plainly (it may have expired)", "refused the token" in msg, True)
AM.urllib.request.urlopen = urlopen


class C:
    def _req(self, m, p, b=None):
        return {"country": "AU", "time_zone": "Australia/Melbourne"}


check("2 · the store is the home's own country, as the platform holds it", AM.storefront(C()), "au")

# 3 · speakermusic's Apple road: Apple finds, the SPEAKER plays song by song with its own Apple sign-in
from proos import speakermusic as S  # noqa: E402

ADDED = []


class FakeBrowser:
    def __init__(self, name, account=None, device=None, time_zone=None):
        self.name, self.account = name, account


stub_b = types.ModuleType("soco.music_services.browser")
stub_b.MusicServiceBrowser = FakeBrowser
stub_p = types.ModuleType("soco.music_services.browser.playback")
stub_p.build_uri = lambda br, iid, typ, mime: "x-sonos-http:%s.mp4?sid=204&flags=8232&sn=3" % iid.replace(":", "%3a")
stub_p.build_metadata = lambda br, iid, title, typ, mime="", uri="": "<DIDL><dc:title>%s</dc:title></DIDL>" % title
for n, m in (("soco", types.ModuleType("soco")), ("soco.music_services", types.ModuleType("soco.music_services")),
             ("soco.music_services.browser", stub_b), ("soco.music_services.browser.playback", stub_p)):
    sys.modules[n] = m


class AVT:
    def AddURIToQueue(self, args):
        ADDED.append(dict(args))
        return {"NumTracksAdded": 1}


class Sp:
    household_id = "HH"
    avTransport = AVT()
    ip_address = "192.168.8.10"

    def clear_queue(self):
        pass

    def play_from_queue(self, i):
        pass

    def get_current_transport_info(self):
        return {"current_transport_state": "PLAYING"}


class Acct:
    def __init__(self, sid, serial, nick=""):
        self.service_id, self.serial_number, self.nickname, self.token = sid, serial, nick, "t"


S._ACCOUNTS["HH"] = ([Acct(12, 2, "Dave"), Acct(204, 3, "Dave")], 0.0)
S._NAMES["HH"] = {12: "Spotify", 204: "Apple Music"}
S._CLIENT["c"] = C()
ANSWERS["/search"] = {"results": {"playlists": {"data": [obj("playlists", "pl.4f2b", "Spa")]}}}
ANSWERS["/playlists/pl.4f2b/tracks"] = {"data": [obj("songs", "11", "One"), obj("songs", "12", "Two")]}
ANSWERS["/charts"] = {"results": {"playlists": [{"name": "Top Playlists", "data": [obj("playlists", "pl.1", "Hits")]}]}}
sp = Sp()
check("3 · Apple Music's home page through the speaker service is Apple's charts",
      [x["title"] for x in S.home(sp, 204)["sections"]], ["Top Playlists"])
check("3 · searching Apple Music goes to Apple's catalogue, not the speaker", S.search(sp, 204, "spa")["results"][0]["items"][0]["id"], "apple:playlists:pl.4f2b")
check("3 · opening an Apple playlist lists its songs from Apple", [c["title"] for c in S.open_item(sp, 204, "apple:playlists:pl.4f2b")["items"]], ["One", "Two"])
ANSWERS["/catalog/au/playlists/pl.4f2b"] = {"data": [obj("playlists", "pl.4f2b", "Spa")]}   # register 837: the playlist itself
p = S.play(sp, 204, "apple:playlists:pl.4f2b")
time.sleep(0.3)
check("837 · the answer names the playlist from Apple's own catalogue — \"Spa\", never its catalogue id", p.get("title"), "Spa")
check("837 · an id Apple doesn't know gives no name (the answer falls back, nothing made up)", AM.name("apple:playlists:pl.none", "au"), None)
check("3 · playing it: the speaker's own Apple song addresses, each with its title, as its own queue",
      ([a["EnqueuedURI"] for a in ADDED], all("<dc:title>" in a["EnqueuedURIMetaData"] for a in ADDED), p["ok"], p["queued"]),
      (["x-sonos-http:song%3a11.mp4?sid=204&flags=8232&sn=3", "x-sonos-http:song%3a12.mp4?sid=204&flags=8232&sn=3"], True, True, 2))
check("3 · Assist doesn't choose Apple Music for a spoken request while there's no token (it can't be searched)",
      S.pick_service(S._ACCOUNTS["HH"][0], S._NAMES["HH"], "x?sid=204&sn=3", skip=(204,)), (12, 2))

# READ
zl = open(os.path.join(ROOT, "zipfiles.txt"), encoding="utf-8").read().split()
check("READ · the new module is in the zip's own list (register 741's lesson)", "proos/applemusic.py" in zl, True)
srv = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("READ · both token routes are installer-only",
      srv.count('if parts == ["speaker", "apple-token"]:') == 2 and
      all("installer access required" in srv[i:i + 700] for i in [k for k in range(len(srv)) if srv.startswith('if parts == ["speaker", "apple-token"]:', k)]), True)
pro = open(os.path.join(ROOT, "apps", "pro.html"), encoding="utf-8").read()
# RE-PINNED, register 1348: the token is made on ProOS Connect; Pro has no paste box, and shows Apple Music among what
# Connect carries for this box. Core's own token route stays (it reads Connect's token).
check("READ · Pro has no token paste (1348); its Connect panel lists Apple Music", ('id="am-token"' in pro, "'Apple Music'" in pro), (False, True))
src = open(os.path.join(ROOT, "proos", "applemusic.py"), encoding="utf-8").read()
check("READ · the module never takes the key — only a token", (".p8" in src.replace("p8 key", ""), "PRIVATE KEY" in src), (False, False))

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
