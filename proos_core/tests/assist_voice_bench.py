"""Assist speaks in the room you're standing in — and says SENT, never
played. Run: python3 tests/assist_voice_bench.py

DAVE'S ASK (16 Aug 2026): "if using assist I could type my questions and
it could respond through the audio in the room that I am in — and be
able to disable it in the room settings if not required." The room
context already rides every chat turn (assistWhere, register 107-era);
this build gives the reply a voice: Core asks the box's OWN speech
engine (the first tts.* entity — a reading, refreshed on a TTL, never a
hardcoded engine name) to speak through the room's COMMITTED speaker.
Off per room in the room's settings; every no is SAID with its reason.

DRIVEN — the real _assist_speak / _tts_engine / routes with fakes:
 1. room + committed speaker + engine → tts.speak SENT to the room's
    first committed audio member; the reply says sent/via/engine/room
 2. the claim is SENT, never "played" — commanded-state law
 3. no room context → silent, said, NO service call
 4. the room's toggle off → silent, said
 5. no committed speaker → silent, said
 6. no speech engine on the box → silent, said
 7. a long reply is CUT for speech AND the cut is said (the full text
    stays on screen)
 8. the engine reading is cached — a second speak pays no second
    full-state read
 9. GET /rooms/<a>/assist-voice reports enabled + capability honestly
10. POST toggle: anonymous/homeowner refused; installer sets it; the
    change is journaled to the room, attributed
"""
import io
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ.setdefault("PROOS_DATA_DIR", tempfile.mkdtemp(prefix="proos_avc_"))

import server as srv                                             # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


class FakeClient:
    def __init__(self, tts=True):
        self.calls, self.dumps = [], 0
        self.tts = tts

    def _req(self, method, path, payload=None):
        if path == "/api/states":
            self.dumps += 1
            return ([{"entity_id": "tts.piper"}] if self.tts else []) \
                + [{"entity_id": "light.x"}]
        return []

    def call_service(self, domain, service, entity_id, data=None):
        self.calls.append((domain, service, entity_id, dict(data or {})))


PROJ = {"areas": {"bedroom": {
    "committed": True, "area_id": "bedroom", "name": "Bedroom",
    "audio": ["media_player.bedroom_sonos"],
    "display": "media_player.bedroom_tv"}}}


class FakeProject:
    def __init__(self, proj):
        self.proj, self.saved = proj, []

    def load(self, *a, **k):
        return self.proj

    def save(self, proj, *a, **k):
        self.saved.append(proj)
        return proj


# The feature not existing IS the old state — stub the pieces so the
# bench runs RED on the old file instead of crashing.
if not hasattr(srv, "_tts_cache"):
    srv._tts_cache = {"ts": 0.0, "engine": None}
if not hasattr(srv, "_assist_speak"):
    srv._assist_speak = lambda *a, **k: {}


def rig(tts=True, proj=None):
    srv._client = FakeClient(tts)
    srv.project = FakeProject(proj if proj is not None else
                              {"areas": {k: dict(v) for k, v in
                                         PROJ["areas"].items()}})
    srv._tts_cache.update({"ts": 0.0, "engine": None})
    return srv._client, srv.project


# 1-2 · the happy path says SENT
c, _ = rig()
out = srv._assist_speak({"area_id": "bedroom"}, "All quiet in here.")
check("the reply is SENT to the room's committed speaker",
      (out.get("sent"), out.get("via"), out.get("engine"), out.get("room")),
      (True, "media_player.bedroom_sonos", "tts.piper", "Bedroom"))
check("  …the service call is the engine's own speak",
      c.calls, [("tts", "speak", "tts.piper",
                 {"media_player_entity_id": "media_player.bedroom_sonos",
                  "message": "All quiet in here.",
                  # RE-PINNED 18 Oct 2026, register 933: on Piper the product's
                  # local voice rides with its language (Dave's 9 Oct ruling)
                  "options": {"voice": srv.PROOS_LOCAL_VOICE},
                  "language": srv.PROOS_LOCAL_VOICE_LANG})])
check("  …the claim is SENT, never played",
      "played" in str(out).lower(), False)

# 3 · no room context
c, _ = rig()
out = srv._assist_speak({}, "hello")
check("no room context: silent, said, no call",
      (out.get("sent"), bool(out.get("why")), c.calls), (False, True, []))

# 4 · the room said no
c, p = rig()
p.proj["areas"]["bedroom"]["assist_voice"] = False
out = srv._assist_speak({"area_id": "bedroom"}, "hello")
check("the room's toggle off: silent, said",
      (out.get("sent"), "off" in str(out.get("why")), c.calls),
      (False, True, []))

# 5 · no committed speaker
c, p = rig()
p.proj["areas"]["bedroom"]["audio"] = []
out = srv._assist_speak({"area_id": "bedroom"}, "hello")
check("no committed speaker: silent, said",
      (out.get("sent"), "speaker" in str(out.get("why")), c.calls),
      (False, True, []))

# 6 · no engine on the box
c, _ = rig(tts=False)
out = srv._assist_speak({"area_id": "bedroom"}, "hello")
check("no speech engine: silent, said",
      (out.get("sent"), "engine" in str(out.get("why")), c.calls),
      (False, True, []))

# 7 · a long reply is cut for speech, and the cut is said
# RE-PINNED WITH CAUSE, REGISTER 483. This drove the room speaker with
# "word " * 200 -- a thousand characters with no full stop anywhere -- and
# required the spoken text to come back under 500. Both halves were the old
# 400-char rule written down: 400 characters is about seventy words, and it
# cut NORMAL replies in half (Dave: "quite often on a long response it stops
# mid way and says rest is on screen"). The ceiling is now 1,000 with 200 of
# slack, so that string is said whole and this check would have proved
# nothing. THE INTENT IS UNCHANGED AND IS WHAT IS PINNED -- a reply too long
# to say is cut, and the home says so -- with an input that is unambiguously
# too long, written in sentences like a real reply. Two things are pinned
# here that were NOT pinned before: where it stops (on a full stop, never
# mid-sentence, which is the defect itself) and that an answer of ordinary
# length is not cut at all.
c, _ = rig()
out = srv._assist_speak({"area_id": "bedroom"},
                        "The home has twenty-two committed devices across nine rooms. " * 40)
spoken = c.calls[0][3]["message"] if c.calls else ""
# RE-PINNED WITH CAUSE, REGISTER 1290 — Dave, 10 Nov 21:09: "Everything in full everywhere". He listens through the
# room speakers, not the screen, so the cut and "the rest is on screen" lost the answer. The behaviour pinned is now
# its reverse: a long answer is said WHOLE, with no tail; the trimmer survives only for a caller that asks (whole=False).
check("a long reply is SAID WHOLE, with no 'rest is on screen'",
      (out.get("cut"), len(spoken) > srv._SPEAK_CAP + 40,
       "on screen" in spoken), (None, True, False))
check("  \u2026and it stops on a full stop, never in the middle of a sentence",
      spoken.split("\u2014")[0].rstrip().endswith("."))
c, _ = rig()
out = srv._assist_speak({"area_id": "bedroom"},
                        "The Bedroom is playing 774 ABC Melbourne, and everything else is quiet.")
check("  \u2026while an answer of ordinary length is spoken whole, uncut",
      (out.get("cut"), c.calls[0][3]["message"].endswith("quiet.")), (None, True))

# 7b · a REFUSED speak never claims sent (added when a mutation survived:
# nothing drove a failing service call — a check that cannot fail is not
# a check)
c, _ = rig()
c.call_service = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("engine refused"))
out = srv._assist_speak({"area_id": "bedroom"}, "hello")
check("a refused speak never claims sent — the refusal is said",
      (out.get("sent"), "refused" in str(out.get("why"))), (False, True))

# 8 · the engine reading is cached
c, _ = rig()
srv._assist_speak({"area_id": "bedroom"}, "one")
srv._assist_speak({"area_id": "bedroom"}, "two")
check("a second speak pays no second full-state read", c.dumps, 1)

# 9-10 · the routes
def hit(method, path, user, body=b"{}"):
    h = object.__new__(srv.Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    resp = {}
    h._send = lambda code, obj=None, **k: resp.update(code=code, obj=obj)
    h._auth = lambda: True
    h._user = user
    (h.do_GET if method == "GET" else h.do_POST)()
    return resp


class FakeUsers:
    @staticmethod
    def is_tech(uid):
        return False


class FakeJournal:
    events = []

    @classmethod
    def emit(cls, room, etype, data=None):
        cls.events.append((room, etype, dict(data or {})))


c, p = rig()
srv.users = FakeUsers()
srv._journal_mod = FakeJournal
r = hit("GET", "/rooms/bedroom/assist-voice", None)
check("GET reports enabled + capability honestly",
      (r.get("code"), (r.get("obj") or {}).get("enabled"),
       (r.get("obj") or {}).get("speaker"), (r.get("obj") or {}).get("engine")),
      (200, True, True, True))

r = hit("POST", "/rooms/bedroom/assist-voice", None,
        b'{"enabled": false}')
check("POST: anonymous refused", r.get("code"), 403)
r = hit("POST", "/rooms/bedroom/assist-voice", {"id": "h", "name": "Home"},
        b'{"enabled": false}')
check("POST: a homeowner is refused (room settings are the trade's)",
      r.get("code"), 403)
FakeJournal.events.clear()
r = hit("POST", "/rooms/bedroom/assist-voice",
        {"id": "i", "name": "Installer", "is_admin": True},
        b'{"enabled": false}')
check("POST: the installer sets it and it persists",
      (r.get("code"), p.proj["areas"]["bedroom"].get("assist_voice"),
       len(p.saved)), (200, False, 1))
check("  …and the change is journaled to the room, attributed",
      FakeJournal.events,
      [("bedroom", "assist_voice", {"enabled": False, "by": "Installer"})])

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + str(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
