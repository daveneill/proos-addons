"""THE SPEAKER'S OWN MUSIC SERVICES FROM CORE — PROOF STEP — register 741.
Run: python3 tests/speaker_music_from_core_bench.py

Register 740 proved on Dave's network (from his Mac) that a Sonos reads the home's own music accounts,
searches Spotify in ~1 s, serves the whole home page and library, and plays a playlist as its own queue
in 2.6 s. Dave, 7:38 am: "go ahead with step 2" — prove it from Core on the box. This bench pins what
can be pinned without a speaker: the two addresses are READ from the platform (never typed), a Core
without the library does nothing and says so, and the image/port/route/Pro button are wired. The real
proof is on the box, by Dave's tap in Pro — a manual gate, named as one.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
import tempfile  # noqa: E402
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_spm_disk_")   # register 778: a real, empty data folder

from proos import speakermusic as S  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


NET = {"interfaces": [
    {"interface": "hassio", "primary": False, "ipv4": {"address": ["172.30.32.1/23"]}},
    {"interface": "end0", "primary": True, "ipv4": {"address": ["192.168.8.240/24"]}}]}
check("1 · the box's own address comes from the Supervisor's network report (the primary interface)",
      S.box_address(NET), "192.168.8.240")
check("1 · …never the internal container network", S.box_address({"interfaces": [
    {"primary": False, "ipv4": {"address": ["172.30.32.1/23"]}}]}), None)
check("1 · …and nothing when the report is empty", S.box_address({}), None)


class C:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def render_template(self, t):
        self.asked.append(t)
        return self.answer


c = C("192.168.8.31\n")
check("2 · the speaker's address: the platform's MAC for that device → the controller's tracker for it",
      S.speaker_address(c, "media_player.office_office"), "192.168.8.31")
# RE-PINNED, register 931: the speaker's own integration record (configuration_url) is asked FIRST now; this
# stand-in answers a bare address to every question, which is no URL, so the controller's record is asked next —
# the same question as before, now the LAST one asked.
check("2 · …asked of the platform for exactly that entity, by its device's connections and the trackers",
      ("'media_player.office_office'" in c.asked[-1], "connections" in c.asked[-1], "device_tracker" in c.asked[-1]),
      (True, True, True))
check("2 · an unknown address is None, not a guess", S.speaker_address(C(""), "media_player.x"), None)

S.soco_ready = lambda: (False, "the speaker music library isn't installed in this Core (ImportError)")
out = S.proof(C("1.2.3.4"), NET, "media_player.office_office", say=lambda m: None)
check("3 · without the library: it stops, says so, and does nothing else",
      (out["ok"], out["lines"]), (False, ["STOP: the speaker music library isn't installed in this Core (ImportError)"]))

# 4 · REGISTER 741, SEEN ON THE BOX: the library's listener fixes its port when first imported (1400), so
# setting the config later changed nothing and the speaker was told to reply where the box is closed.
class L:
    def __init__(self, running, port):
        self.is_running, self.address, self.requested_port_number, self.stopped = running, (
            ("172.30.33.1", port) if running else ()), port, False

    def stop(self):
        self.stopped, self.is_running = True, False


class Ev:
    def __init__(self, lst):
        self.event_listener = lst


fresh = L(False, 1400)
S.listener_port(Ev(fresh))
check("4 · a listener made at import with the library's 1400 is told Core's published port before it starts",
      (fresh.requested_port_number, fresh.stopped), (S.EVENT_PORT, False))
wrong = L(True, 1400)
S.listener_port(Ev(wrong))
check("4 · one already running on the wrong port is stopped, so it restarts on Core's",
      (wrong.stopped, wrong.requested_port_number), (True, S.EVENT_PORT))
right = L(True, S.EVENT_PORT)
S.listener_port(Ev(right))
check("4 · one already on Core's port is left alone", right.stopped, False)
src = open(os.path.join(ROOT, "proos", "speakermusic.py"), encoding="utf-8").read()
body = src[src.find("def proof("):]
# RE-PINNED WITH CAUSE, REGISTER 748: stage 1 moved the speaker connection out of the proof into the
# service's own connect(), which the proof and every route now use — the rule is unchanged.
_conn = src[src.find("def connect("):src.find("def accounts_of(")]
check("4 · the speaker connection points the listener BEFORE it first talks to the speaker",
      -1 < _conn.find("listener_port(") < _conn.find("soco.SoCo(ip)"))
check("4 · …and the proof goes through that same connection", "connect(client, network_info, entity_id)" in body)
check("4 · …and logs the exact address it gives the speaker, so the box shows it",
      "Core asks the speaker to reply to" in body)

# 5 · REGISTER 744, SEEN ON THE BOX: the household's nine accounts arrived (step 2), then opening Spotify
# with no account made the library read them a second time, which never arrived. The accounts as the box
# read them (service 12 = Spotify, 204 = Apple Music):
class A:
    def __init__(self, sid, nick=""):
        self.service_id, self.nickname = sid, nick


BOX = [A(303), A(12, "Dave"), A(204, "Dave"), A(236, "dave.neill@live.com.au"), A(268), A(259),
       A(12, "Ryan’s Spotify Account"), A(12, "Bec"), A(12, "Jarrod")]
pick = S.choose_account(BOX, 204, "Dave")
check("5 · 'Dave' for Apple Music is Dave's APPLE account, not the first 'Dave' of any service",
      (pick.service_id, pick.nickname), (204, "Dave"))
check("5 · 'Bec' for Spotify is Bec's", S.choose_account(BOX, 12, "bec").nickname, "Bec")
check("5 · no name: that service's first account", S.choose_account(BOX, 12).nickname, "Dave")
check("5 · a name that isn't there is nobody — never someone else's account", S.choose_account(BOX, 12, "Sam"), None)
check("5 · a service the home hasn't set up has no account", S.choose_account(BOX, 999), None)
body = open(os.path.join(ROOT, "proos", "speakermusic.py"), encoding="utf-8").read()
body = body[body.find("def proof("):]
# RE-PINNED WITH CAUSE, REGISTER 748: the service is now opened in one place, _session(), for the
# proof and every route; the rule is unchanged — always with the account already read.
_all = open(os.path.join(ROOT, "proos", "speakermusic.py"), encoding="utf-8").read()
check("5 · the service is always opened WITH the account already read (no second read)",
      ("MusicServiceBrowser(name, account=acct, device=sp" in _all,
       _all.count("MusicServiceBrowser(")), (True, 1))

# 6 · REGISTER 745, SEEN ON THE BOX: reads from the speaker arrived, didn't, did, didn't, did, didn't.
class Speaker:
    def __init__(self, pattern):
        self.pattern, self.asked = list(pattern), 0

    def read(self):
        self.asked += 1
        if self.pattern.pop(0):
            return BOX
        raise RuntimeError("No ThirdPartyMediaServersX event arrived within 10 seconds")


S._ACCOUNTS.clear()
sp6 = Speaker([True, False, True, False])
got, how = S.household_accounts("Sonos_HH1", sp6.read, now=1000.0)
check("6 · the first read: the home's accounts, read from the speaker", (len(got), "read from the speaker" in how), (9, True))
# RE-PINNED WITH CAUSE, register 828 (Dave 13 Oct 15:08 "I also added another service to Sonos and it doesnt show up"):
# the accounts were kept for Core's LIFETIME, so a service added in the speaker's own app never appeared. The rule kept:
# within ACCOUNTS_FRESH_S the speaker is not asked again (these calls are now timed inside that window).
for k in range(3):
    got, how = S.household_accounts("Sonos_HH1", sp6.read, now=1000.0 + 100 * (k + 1))
check("6 · every later test inside the fresh window uses the accounts already read — the speaker is not asked again",
      (sp6.asked, len(got), how.startswith("kept from")), (1, 9, True))
NEW = BOX + [BOX[0]]
sp6.pattern = [False, True]
sp6_reads = {"n": 0}


def _read_new():
    sp6_reads["n"] += 1
    if sp6.pattern.pop(0):
        return NEW
    raise RuntimeError("No ThirdPartyMediaServersX event arrived within 10 seconds")


_t = 1000.0 + S.ACCOUNTS_FRESH_S + 1
# RE-PINNED, register 882 (read on the box 15 Oct: the 828 re-read ran on the person's own search and hung 10 s per service):
# a PERSON'S request never reads the speaker again once a list is kept — the background warmer is the one path that does
got, how = S.household_accounts("Sonos_HH1", _read_new, now=_t)
check("6 · 882: the kept list is old but this is a person's request: the kept list, NO read of the speaker",
      (sp6_reads["n"], len(got), "refreshed in the background" in how), (0, 9, True))
with S.refreshing():
    got, how = S.household_accounts("Sonos_HH1", _read_new, now=_t)
    check("6 · 828 (in the background): once the kept list is old, the speaker is asked again — a miss keeps the kept list (never empties it)",
          (sp6_reads["n"], len(got), "didn't answer" in how), (1, 9, True))
    got, how = S.household_accounts("Sonos_HH1", _read_new, now=_t + 10)
    check("6 · 828: …and it isn't asked again straight away (the retry waits)", (sp6_reads["n"], len(got)), (1, 9))
    got, how = S.household_accounts("Sonos_HH1", _read_new, now=_t + S.ACCOUNTS_RETRY_S + 1)
    check("6 · 828: the retry reads the speaker — a service added in the speaker's own app now appears",
          (sp6_reads["n"], len(got), "read again" in how), (2, 10, True))
check("6 · 882: outside the warmer the flag is off again", getattr(S._REFRESH, "on", False), False)
S._ACCOUNTS.clear()
sp6 = Speaker([False, True])
got, how = S.household_accounts("Sonos_HH1", sp6.read)
check("6 · a read that doesn't arrive is asked once more (the box's every-other pattern)",
      (sp6.asked, len(got), "second ask" in how), (2, 9, True))
S._ACCOUNTS.clear()
sp6 = Speaker([False, False])
try:
    S.household_accounts("Sonos_HH1", sp6.read)
    stopped = False
except RuntimeError:
    stopped = True
check("6 · two misses in a row is a stop, with the speaker's own error — never accounts made up", (stopped, sp6.asked), (True, 2))
check("6 · …and nothing is kept from a failed read", S._ACCOUNTS, {})
# RE-PINNED, register 778 (Dave 12 Oct 09:31, "any signins on both"): the module now keeps ONE file — ProOS's own
# sign-ins, made when the person signs in on the service's own page (775–777). The household's accounts still never
# touch disk: every write in the module is that one file's writer, and reading the accounts writes nothing.
import ast as _ast  # noqa: E402
_src = open(os.path.join(ROOT, "proos", "speakermusic.py"), encoding="utf-8").read()
_writers = sorted(n.name for n in _ast.walk(_ast.parse(_src)) if isinstance(n, _ast.FunctionDef)
                  and any(w in (_ast.get_source_segment(_src, n) or "") for w in ('"w"', "json.dump(", "_json.dump(")))
_before = set(os.listdir(os.environ["PROOS_DATA_DIR"]))
S._ACCOUNTS.clear()
S.household_accounts("Sonos_HH_DISK", Speaker([True]).read)
_after = set(os.listdir(os.environ["PROOS_DATA_DIR"]))
# RE-PINNED, register 799 (Dave 12 Oct 18:06, "go on both"): a SECOND file — the address each speaker last answered
# at with its own id (ProOS's own measurement, used only when the platform's tracker has none). Still never the
# household's accounts: the module's writers are exactly those two files' writers.
check("6 · the accounts are kept in memory only — reading them writes nothing; the module writes only its sign-ins and the speakers' confirmed addresses",
      (_writers, _after - _before), (["_links_write", "remember_address"], set()))

df = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
check("READ · the image installs SoCo at the pinned, proven commit", S.SOCO_COMMIT in df and "SoCo.git@" in df)
check("READ · …and a failed install never fails the image", "|| echo \"speaker music library unavailable" in df)
cfg = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8").read()
check("READ · the speaker's way in is a published port, and the module uses the same one",
      ('"%d/tcp": %d' % (S.EVENT_PORT, S.EVENT_PORT)) in cfg)
srv = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
i = srv.find('if parts == ["speaker", "proof"]:')
check("READ · Core's test route is installer-only", i > -1 and "installer access required" in srv[i:i + 600])
pro = open(os.path.join(ROOT, "apps", "pro.html"), encoding="utf-8").read()
# RE-PINNED, register 1348 (Dave 14 Nov 15:02–15:04): the test tools are off Pro's Assist page; Core's proof route stays,
# installer-only (above), for a session to drive.
check("READ · Pro no longer carries the Speaker Music Test (1348); Core's route stays", 'bld-sec-h">Speaker Music Test' not in pro)

# ══ 7 · MUSIC DESIGN STAGE 1 (register 748), driven with stand-ins for the speaker and the service ══
import time  # noqa: E402
import types  # noqa: E402

ADDED = []


class FakeItem:
    def __init__(self, iid, title, browse=False, raw=None, artist="", art="", typ="track"):
        self.item_id, self.title, self.can_browse = iid, title, browse
        self.raw, self.artist, self.album_art_uri, self.item_type = raw or {}, artist, art, typ


class Res:
    def __init__(self, items):
        self.items, self.index, self.total = items, 0, len(items)


TRACKS = [FakeItem("t%d" % n, "Track %d" % n) for n in range(5)]
PLAYLIST = FakeItem("pl1", "Stress Relief", browse=True, typ="playlist")


class FakeBrowser:
    available_search_categories = ["artists", "playlists"]

    def __init__(self, name, account=None, device=None, time_zone=None):
        self.name, self.account, self.time_zone = name, account, time_zone
        FakeBrowser.made.append(self)

    def get_metadata(self, item="root", index=0, count=100):
        if item == "root":
            return Res([FakeItem("sec1", "Recently played", browse=True), FakeItem("x", "A single")])
        if getattr(item, "item_id", item) in ("sec1", "pl1"):
            return Res(TRACKS[:count])
        return Res([])

    def search(self, cat, q, count=8):
        return Res([PLAYLIST] if cat == "playlists" else [])


FakeBrowser.made = []
_stub_b = types.ModuleType("soco.music_services.browser")
_stub_b.MusicServiceBrowser = FakeBrowser
_stub_p = types.ModuleType("soco.music_services.browser.playback")
_stub_p.resolve_item = lambda br, it: (it.item_id, "track", "audio/x-spotify", it.title)
_stub_p.build_uri = lambda br, iid, typ, mime: (("x-sonosapi-radio:%s?sid=236&flags=104&sn=4" % iid) if str(iid).startswith("ST:")
                                                else "x-sonos-spotify:%s?sid=12&sn=2" % iid)
_stub_p.build_metadata = lambda br, iid, title, typ, mime="", uri="": "<DIDL><dc:title>%s</dc:title></DIDL>" % title
for _n, _m in (("soco", types.ModuleType("soco")), ("soco.music_services", types.ModuleType("soco.music_services")),
               ("soco.music_services.browser", _stub_b), ("soco.music_services.browser.playback", _stub_p)):
    sys.modules[_n] = _m


class AVT:
    def AddURIToQueue(self, args):
        d = dict(args)
        ADDED.append(d)
        return {"NumTracksAdded": 1, "FirstTrackNumberEnqueued": 116 + len(ADDED)}


class FakeSpeaker:
    household_id = "Sonos_HH1"
    avTransport = AVT()

    def __init__(self, plays=True):
        self.plays, self.cleared, self.started = plays, False, None

    def clear_queue(self):
        self.cleared = True

    def play_from_queue(self, i):
        self.started = i

    def get_current_transport_info(self):
        return {"current_transport_state": "PLAYING" if self.plays else "TRANSITIONING"}

    def play_uri(self, uri, meta="", title=""):
        self.direct = (uri, title)

    on_queue, position = True, 3

    def get_current_media_info(self):
        return {"uri": "x-rincon-queue:RINCON_X#0" if self.on_queue else "x-sonosapi-radio:ST:1?sid=236"}

    def get_current_track_info(self):
        return {"playlist_position": str(self.position)}


S._ACCOUNTS.clear()
S._ACCOUNTS["Sonos_HH1"] = (BOX_SERIALS := [A(12, "Ryan"), A(12, "Dave"), A(204, "Dave")], 1000.0)
BOX_SERIALS[0].serial_number, BOX_SERIALS[1].serial_number, BOX_SERIALS[2].serial_number = 7, 2, 3
S._NAMES["Sonos_HH1"] = S.parse_service_names(
    '<Services><Service Id="12" Name="Spotify"/><Service Id="204" Name="Apple Music"/><Service Id="303" Name="Sonos Radio"/></Services>')
check("7 · the speaker's own names for its services are read from its own list",
      S._NAMES["Sonos_HH1"], {12: "Spotify", 204: "Apple Music", 303: "Sonos Radio"})
sv = S.list_services(S._ACCOUNTS["Sonos_HH1"][0], S._NAMES["Sonos_HH1"])
check("7 · services listed with their accounts; each defaults to its FIRST-ADDED account (Dave's ruling)",
      [(x["service"], x["default"], [a["name"] for a in x["accounts"]]) for x in sv],
      [("Spotify", 2, ["Dave", "Ryan"]), ("Apple Music", 3, ["Dave"])])
check("7 · …and nothing secret leaves: only service, account number and name",
      sorted(sv[0]["accounts"][0].keys()), ["account", "name"])
sp7 = FakeSpeaker()
h = S.home(sp7, 12, None, "Australia/Melbourne")
check("7 · the home page is the service's own sections, in its order, with their items",
      [(x["title"], len(x.get("items") or [])) for x in h["sections"]], [("Recently played", 5), ("A single", 0)])
check("7 · …opened with the first-added account and the home's time zone",
      (FakeBrowser.made[-1].account.nickname, FakeBrowser.made[-1].time_zone), ("Dave", "Australia/Melbourne"))
S.home(sp7, 12, 7)
check("7 · another account is the option (Ryan's #7) — its own session", FakeBrowser.made[-1].account.nickname, "Ryan")
try:
    S.home(sp7, 12, 99)
    refused = False
except S.SpeakerMusicError:
    refused = True
check("7 · an account number that isn't there is refused — never someone else's", refused, True)
r = S.search(sp7, 12, "stress", None)
check("7 · search runs in each of the SERVICE's own categories", [x["category"] for x in r["results"]], ["artists", "playlists"])
c = r["results"][1]["items"][0]
check("7 · a found playlist can be opened; whether it plays is the service's say (unknown here = None)",
      (c["title"], c["open"], c["play"]), ("Stress Relief", True, None))
check("7 · a service that says canPlay is believed", S.card(FakeItem("p", "P", browse=True, raw={"canPlay": "false"}))["play"], False)
ADDED[:] = []
p7 = S.play(sp7, 12, "pl1", None)
time.sleep(0.3)
check("7 · a playlist plays as the speaker's own queue: cleared, first track started, the rest added",
      (sp7.cleared, sp7.started, len(ADDED), p7["queued"]), (True, 0, 5, 5))
check("7 · REGISTER 748: every track reaches the speaker WITH its details, in the metadata's own field",
      all("<dc:title>Track" in a["EnqueuedURIMetaData"] for a in ADDED), True)
check("7 · the answer is the speaker's own state", (p7["ok"], p7["state"], p7["title"]), (True, "PLAYING", "Stress Relief"))
p8 = S.play(FakeSpeaker(plays=False), 12, "t1", None, wait_s=0.6)
check("7 · a speaker that doesn't start says so — never 'playing'", (p8["ok"], p8["state"]), (False, "TRANSITIONING"))
# REGISTER 782 (seen on the box, 10:38): a Pandora station from "My Stations" — Core queued it; the speaker said "UPnP
# Error 800". A station starts directly, as the same Pandora program did on the Office in 777 (PLAYING 5.8 s).
ADDED[:] = []
sp9 = FakeSpeaker()
S._keep(S._session(sp9, 12, None), FakeItem("ST:3819", "Deadmau5 Radio", typ="program"))
p9 = S.play(sp9, 12, "ST:3819", None)
check("7 · REGISTER 782: a station starts DIRECTLY on the speaker (its own address says it's a station) — never queued",
      (getattr(sp9, "direct", None), sp9.cleared, ADDED, p9["ok"], p9["title"]),
      (("x-sonosapi-radio:ST:3819?sid=236&flags=104&sn=4", "Deadmau5 Radio"), False, [], True, "Deadmau5 Radio"))
check("7 · …while a single TRACK still goes in the queue, prepared once", (S.is_stream("x-sonos-spotify:t1?sid=12"), S.is_stream("x-sonosapi-radio:x")), (False, True))
# REGISTER 785 (Dave 10:57 "go"): Play next / Add to queue, into the speaker's own queue, by its own position fields
ADDED[:] = []
sp10 = FakeSpeaker()
S._keep(S._session(sp10, 12, None), FakeItem("t9", "So What", typ="track"))
p10 = S.play(sp10, 12, "t9", None, mode="next")
check("7 · REGISTER 785: Play next goes straight after the track playing (its queue position 3 → 4), flagged as next; nothing restarted",
      ([(a["EnqueuedURI"], a["DesiredFirstTrackNumberEnqueued"], a["EnqueueAsNext"]) for a in ADDED], sp10.cleared, sp10.started, p10["ok"]),
      ([("x-sonos-spotify:t9?sid=12&sn=2", 4, 1)], False, None, True))
ADDED[:] = []
p11 = S.play(sp10, 12, "pl1", None, mode="add")
time.sleep(0.3)
check("7 · Add to queue puts a whole playlist at the END, in order; the answer is the speaker's own count",
      ([a["DesiredFirstTrackNumberEnqueued"] for a in ADDED], [a["EnqueuedURI"].split("?")[0][-2:] for a in ADDED], p11["queued"]),
      ([0, 0, 0, 0, 0], ["t0", "t1", "t2", "t3", "t4"], 5))
sp10.on_queue = False
ADDED[:] = []
p12 = S.play(sp10, 12, "t9", None, mode="next")
# RE-PINNED (register 788, Dave's ruling B, 12 Oct 11:41): Play next while a STATION plays — a station never ends, so the
# track used to wait in a queue nobody played (787). Now the station stops and the track plays now from the queue.
check("7 · DAVE'S RULING B: Play next while a station plays — added at the end, then PLAYED NOW from the queue at the number "
      "the speaker said it landed (117 → index 116); the answer is the speaker's state",
      (ADDED[0]["DesiredFirstTrackNumberEnqueued"], sp10.started, p12["on_queue"], p12["started"], p12["ok"]), (0, 116, False, True, True))
ADDED[:] = []
p13 = S.play(sp10, 12, "t9", None, mode="add")
check("7 · …while Add to queue during a station still only adds (said: it waits in the queue)", (p13.get("started"), p13["on_queue"]), (False, False))
S._keep(S._session(sp10, 12, None), FakeItem("ST:9", "Some Radio", typ="program"))
try:
    S.play(sp10, 12, "ST:9", None, mode="add")
    got = None
except S.SpeakerMusicError as e:
    got = str(e)
check("7 · a station can't be queued — said plainly, nothing sent", got, "a station can't go in the queue — play it instead")
# REGISTER 790 (queue build 2): the speaker's own queue — read with artwork made whole, current place, remove, move, play
class QItem:
    def __init__(self, t, c, art):
        self.title, self.creator, self.album_art_uri = t, c, art


class QAVT(AVT):
    def __init__(self):
        self.reorders = []

    def ReorderTracksInQueue(self, args):
        self.reorders.append(dict(args))


class QSpeaker(FakeSpeaker):
    ip_address = "192.168.8.10"

    def __init__(self):
        super().__init__()
        self.avTransport = QAVT()
        self.removed, self.from_q = [], None

    def get_queue(self, max_items=100):
        return [QItem("Over Now", "Iskarelyn", "/getaa?s=1&u=x"), QItem("Move", "Adam Port", "https://i.scdn.co/a.jpg"),
                QItem("Make Me Feel", "oskar med k", "")]

    def remove_from_queue(self, i):
        self.removed.append(i)

    def play_from_queue(self, i):
        self.from_q = i


qs = QSpeaker()
qq = S.queue(qs)
check("7 · REGISTER 790: the queue is the speaker's own, in order, each with its place as its id; the playing one marked "
      "(the speaker's playlist_position 3 → index 2)",
      ([(x["queue_item_id"], x["name"], x["artist"]) for x in qq["items"]], qq["current_index"]),
      ([("pos:0", "Over Now", "Iskarelyn"), ("pos:1", "Move", "Adam Port"), ("pos:2", "Make Me Feel", "oskar med k")], 2))
check("7 · …the speaker's own artwork addresses are made whole (its own address and port); others kept; none = none",
      [x["image"] for x in qq["items"]], ["http://192.168.8.10:1400/getaa?s=1&u=x", "https://i.scdn.co/a.jpg", None])
S.queue_remove(qs, "pos:1")
S.queue_play(qs, "pos:0")
S.queue_move(qs, "pos:0", 2)
S.queue_move(qs, "pos:2", -2)
check("7 · remove and play are the speaker's own, by place; a move uses its own ReorderTracksInQueue, counted from 1 "
      "(down two: 1 before 4; up two: 3 before 1)",
      (qs.removed, qs.from_q, [(r["StartingIndex"], r["InsertBefore"]) for r in qs.avTransport.reorders]), ([1], 0, [(1, 4), (3, 1)]))
try:
    S.queue_remove(qs, "nonsense")
    got = None
except S.SpeakerMusicError as e:
    got = str(e)
check("7 · an id that isn't a place in the queue is refused in words", got, "that isn't an item in this queue")
# REGISTER 792 (Dave 13:33: Clear "had to hit 4-5 times … it seemed to do it in stages"): Core was still adding the
# rest of a playlist when Clear emptied the queue. Clear now stops the fill first; an Add's fill stops on a Clear too.
class CSpeaker(QSpeaker):
    def __init__(self):
        super().__init__()
        self.cleared_n, self.q = 0, [1, 2, 3]

    def clear_queue(self):
        self.cleared_n += 1
        self.q = []

    def get_queue(self, max_items=100):
        return list(self.q)


cs = CSpeaker()
ADDED[:] = []
sess = S._session(cs, 12, None)
tracks = [FakeItem("t%d" % n, "T%d" % n) for n in range(6)]
seen = []


def prep_then_clear(t):
    seen.append(t.item_id)
    if t.item_id == "t2":
        S.queue_clear(cs)                     # the person presses Clear while the rest is still being added
    return ("x-sonos-spotify:%s" % t.item_id, "<DIDL/>")


S._enqueue(cs, sess, tracks, prep_then_clear, "add", "Some Playlist")
time.sleep(0.3)
check("7 · REGISTER 792: a Clear during an Add's fill STOPS the fill — nothing is added after the Clear",
      ([a["EnqueuedURI"].split(":")[1] for a in ADDED], cs.cleared_n), (["t0", "t1"], 1))
r = S.queue_clear(cs)
check("7 · …and Clear says what the speaker's own queue holds afterwards", (r["ok"], r["left"]), (True, 0))
# REGISTER 794: a service whose home is its content page also shows its classic tree's first level on top (the library)
_models = types.ModuleType("soco.music_services.browser.models")
_models._legacy_item = lambda rec, transport="smapi": FakeItem(rec["id"], rec["title"], browse=True, typ=rec.get("itemType"))
sys.modules["soco.music_services.browser.models"] = _models


class Scoped:
    def get_metadata(self, oid, index, count, recursive, so, sa):
        assert oid == "root"
        return {"items": [{"id": "featured_pls", "title": "Popular Playlists", "itemType": "container"},
                          {"id": "yourmusic_root", "title": "Your Music", "itemType": "collection"}]}


FakeBrowser._content_endpoint = "https://content.example/home"
FakeBrowser._scoped_client = lambda self: Scoped()
_kept_sessions = dict(S._SESSIONS)
S._SESSIONS.clear()
h2 = S.home(FakeSpeaker(), 12, None, "Australia/Melbourne")
check("7 · REGISTER 794: the service's own classic tree goes on top as one shelf — its own words and order, each a "
      "folder that opens (Your Music → the person's playlists)",
      (h2["sections"][0]["title"], [(i["title"], i["open"]) for i in h2["sections"][0]["items"]], h2["sections"][1]["title"]),
      ("Browse", [("Popular Playlists", True), ("Your Music", True)], "Recently played"))
del FakeBrowser._content_endpoint
del FakeBrowser._scoped_client
S._SESSIONS.clear()
S._SESSIONS.update(_kept_sessions)
check("7 · nothing in the module uses the library's add_uri_to_queue (it takes metadata as a POSITION)",
      ".add_uri_to_queue(" in open(os.path.join(ROOT, "proos", "speakermusic.py"), encoding="utf-8").read(), False)
# 8 · REGISTER 751, seen from the dashboard: search waited ~10 s behind a queue fill; categories one by one
import threading as _th  # noqa: E402
SLOW = {"search": 0.3, "prepare": 0.05}
FakeBrowser.available_search_categories = ["artists", "albums", "tracks", "playlists"]
_orig_search = FakeBrowser.search


def _slow_search(self, cat, q, count=8):
    time.sleep(SLOW["search"])
    return _orig_search(self, cat, q, count)


FakeBrowser.search = _slow_search
_orig_resolve = _stub_p.resolve_item


def _slow_resolve(br, it):
    time.sleep(SLOW["prepare"])
    return _orig_resolve(br, it)


_stub_p.resolve_item = _slow_resolve
BOX_SERIALS[1].token = "signed-in"
t0 = time.time()
r8 = S.search(FakeSpeaker(), 12, "relaxing", 2)
check("8 · a signed-in account's categories are searched side by side (4 × 0.3 s in well under 1.2 s)",
      (time.time() - t0 < 0.8, [x["category"] for x in r8["results"]]), (True, ["artists", "albums", "tracks", "playlists"]))
BOX_SERIALS[0].token = ""
t0 = time.time()
S.search(FakeSpeaker(), 12, "relaxing", 7)
check("8 · an account with no sign-in shares one connection, so its categories are asked in turn",
      time.time() - t0 >= 1.1, True)
TRACKS[:] = [FakeItem("t%d" % n, "Track %d" % n) for n in range(40)]       # a 40-track playlist: a 2 s fill
ADDED[:] = []
spf = FakeSpeaker()
S.play(spf, 12, "pl1", 2)
t0 = time.time()
S.search(spf, 12, "relaxing", 2)
check("8 · a search straight after a play is not held behind the queue fill", time.time() - t0 < 0.8, True)
time.sleep(0.3)
n_before = len(ADDED)
S.play(spf, 12, "t1", 2)                                    # a NEWER play on the same speaker
time.sleep(2.5)
check("8 · a newer play on the same speaker stops the older fill (its tracks never land in the new queue)",
      len(ADDED) < 40, True)
FakeBrowser.search, _stub_p.resolve_item = _orig_search, _orig_resolve

srv = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("READ · the routes exist: services, home, open, search (any signed-in) and play (household POST)",
      ('parts[2] in (\n                    "services", "home", "open", "search")' in srv,
       'if parts == ["speakers", "music", "play"]:' in srv), (True, True))
check("READ · the home's time zone comes from the platform's own setting", '"/api/config"' in srv[srv.find("def _home_time_zone"):srv.find("def _home_time_zone") + 700])

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
