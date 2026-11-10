"""THE HOME'S VOICE FOR A SCREEN — one voice, decided in Pro, played on the
iPad and the room speaker alike (register 346).
Run: python3 tests/glass_voice_bench.py

Dave, 6 Sep 2026, on the app saying its answers in Apple's device voice
(register 344): "sounds like a computer" — then: no setting on any iPad
or phone; "a voice we decide", set in Pro. So POST /assist/speak makes
the answer's audio with the SAME engine, voice and language the room
speaker uses (_voice_choice, register 345) through the platform's own
tts_get_url, fetches the file and hands the bytes to the page. Nothing
is invented: no engine → said; the engine refusing → said; empty text →
said; the journal records that words went to the voice engine, and how
many — never which.

DRIVEN — the real helper and route with fakes:
 1. the audio is made with the chosen engine + voice + LANGUAGE
 2. no voice chosen → the engine's default, no options invented
 3. the bytes come back with the engine's content type
 4. an answer longer than the speak cap is cut AND says so
 5. sign-in required; empty text refused with the reason
 6. no engine on the box → 502, said; the engine refusing → 502, said
 7. the journal says words went out — the count, never the words
"""
import io
import json
import os
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
_DATA = tempfile.mkdtemp(prefix="proos_gv_")
os.environ["PROOS_DATA_DIR"] = _DATA

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


if not hasattr(srv, "_glass_voice_audio"):                      # the OLD file runs RED
    srv._glass_voice_audio = lambda text: (_ for _ in ()).throw(ConnectionError("no such door"))


class FakeClient:
    def __init__(self, engines=("tts.cloud_say",), refuse=False):
        self.calls = []
        self.engines = list(engines)
        self.refuse = refuse

    def _req(self, method, path, payload=None):
        if path == "/api/states":
            return [{"entity_id": e} for e in self.engines]
        if path == "/api/config":
            return {"language": "en"}
        if path == "/api/tts_get_url":
            self.calls.append(dict(payload or {}))
            if self.refuse:
                raise RuntimeError("POST /api/tts_get_url -> HTTP 400: Unsupported voice")
            return {"url": "http://box/api/tts_proxy/abc.mp3", "path": "/api/tts_proxy/abc.mp3"}
        return {}


class FakeResp:
    def __init__(self, data, ctype):
        self.data, self.headers = data, {"Content-Type": ctype}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


FETCHED = []


def fake_urlopen(req, timeout=None):
    FETCHED.append((req.full_url, req.get_header("Authorization")))
    return FakeResp(b"ID3\x03mp3bytes", "audio/mpeg")


def fake_ws(msg_type, **fields):
    if msg_type == "tts/engine/voices" and fields.get("language") == "en-AU":
        return {"voices": [{"voice_id": "DarrenNeural", "name": "Darren"}]}
    raise RuntimeError("language not supported")


def rig(settings=None, **kw):
    srv._client = FakeClient(**kw)
    srv._ws_call = fake_ws
    srv._cfg = {"base_url": "http://box:8123", "token": "tok"}
    srv._tts_cache.update({"ts": 0.0, "engine": None, "engines": []})
    urllib.request.urlopen = fake_urlopen
    FETCHED.clear()
    vp = os.path.join(_DATA, "voice.json")
    if os.path.exists(vp):
        os.remove(vp)
    if settings is not None:
        with open(vp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
    return srv._client


# 1 · engine + voice + LANGUAGE make the audio
c = rig({"engine": "tts.cloud_say", "voice": "DarrenNeural", "language": "en-AU"})
ctype, data, out = srv._glass_voice_audio("Done — HBO Max is now open in the Bedroom.")
check("the audio is made with the chosen engine, voice AND language (the room speaker's own choice)",
      c.calls, [{"engine_id": "tts.cloud_say", "message": "Done — HBO Max is now open in the Bedroom.",
                 "language": "en-AU", "options": {"voice": "DarrenNeural"}}])
check("  …the file is fetched from the box with Core's own token, never the page's",
      FETCHED, [("http://box:8123/api/tts_proxy/abc.mp3", "Bearer tok")])
check("  …and the bytes come back with the engine's content type",
      (ctype, data, out.get("voice"), out.get("language"), out.get("words"), out.get("cut")),
      ("audio/mpeg", b"ID3\x03mp3bytes", "DarrenNeural", "en-AU", 10, False))

# 2 · no voice chosen → the engine's default, nothing invented
c = rig()
srv._glass_voice_audio("hello")
check("no voice chosen: the engine's default speaks — no options, no language invented",
      ("options" in c.calls[0], "language" in c.calls[0]), (False, False))

# 4 · the cap
# RE-PINNED WITH CAUSE, REGISTER 483. This drove the helper with "word " * 200
# -- one thousand characters and not a full stop in it -- which was a long
# answer under the old 400-char rule. The ceiling is now 1,000 with 200 of
# slack, because 400 characters cut a NORMAL reply in half (Dave: "quite often
# on a long response it stops mid way and says rest is on screen"), so that
# same string is now said whole and this check would have passed for nothing.
# The INTENT is unchanged and is what is pinned: an answer too long to say is
# cut, and the home says so. The input is now unambiguously too long, it is
# written in sentences like a real reply, AND the stopping point is pinned as
# well -- a spoken answer that stops mid-sentence is the defect itself.
c = rig()
LONG = "The home has twenty-two committed devices across nine rooms. " * 40
_, _, out = srv._glass_voice_audio(LONG)
said = c.calls[0]["message"]
TAIL = " \u2014 the rest is on screen."
# RE-PINNED WITH CAUSE, REGISTER 1290 — Dave, 10 Nov 21:09: "Everything in full everywhere". He listens through the
# room speakers, not the screen, so the cut and "the rest is on screen" lost the answer. The behaviour pinned is now
# its reverse: a long answer is said WHOLE, with no tail; the trimmer survives only for a caller that asks (whole=False).
check("a long answer is said WHOLE, with no 'rest is on screen'",
      (out.get("cut"), said.endswith(TAIL.lstrip()),
       len(said) > srv._SPEAK_CAP + 40), (False, False, True))
check("  \u2026and it ends where the answer ends, on its full stop",
      said.rstrip().endswith("."))
check("  \u2026the trimmer is still there for a caller that asks (whole=False)",
      srv._glass_voice_audio(LONG, whole=False)[2].get("cut"), True)
check("  \u2026while an answer of ordinary length is said whole, uncut",
      srv._glass_voice_audio("The Office is playing 774 ABC Melbourne.")[2].get("cut"), False)

# 6 · every no is said
rig(engines=())
try:
    srv._glass_voice_audio("hello")
    check("no engine on the box → refused, said", False)
except ConnectionError as e:
    check("no engine on the box → refused, said", "no speech engine" in str(e))
rig(refuse=True)
try:
    srv._glass_voice_audio("hello")
    check("the engine refusing → refused, with the engine's reason", False)
except ConnectionError as e:
    check("the engine refusing → refused, with the engine's reason", "Unsupported voice" in str(e))
rig()
try:
    srv._glass_voice_audio("   ")
    check("empty text → refused, said", False)
except ConnectionError as e:
    check("empty text → refused, said", str(e), "nothing to say")


# 5, 7 · the door
def hit(method, path, user, body=b"{}"):
    h = object.__new__(srv.Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    resp = {}
    h._send = lambda code, obj=None, **k: resp.update(code=code, obj=obj)
    h._send_bytes = lambda code, ctype, data: resp.update(code=code, ctype=ctype, data=data)
    h._auth = lambda: True
    h._user = user
    (h.do_GET if method == "GET" else h.do_POST)()
    return resp


class FakeJournal:
    events = []

    @classmethod
    def emit(cls, room, etype, data=None):
        cls.events.append((room, etype, dict(data or {})))


srv._journal_mod = FakeJournal
c = rig({"engine": "tts.cloud_say", "voice": "DarrenNeural", "language": "en-AU"})
r = hit("POST", "/assist/speak", None, b'{"text":"hello"}')
check("POST /assist/speak: sign-in required (the door's own gate refuses before the route)", r.get("code") in (401, 403))
FakeJournal.events.clear()
r = hit("POST", "/assist/speak", {"id": "u1", "name": "Dave"}, b'{"text":"Done, the lights are on."}')
check("  …a signed-in person gets the home's audio",
      (r.get("code"), r.get("ctype"), r.get("data")), (200, "audio/mpeg", b"ID3\x03mp3bytes"))
# RE-PINNED WITH CAUSE, REGISTER 487. The record gained one field: `early`,
# whether the home's voice had already been started on these words before the
# page asked for them (the head start). It is a yes or a no about TIMING and
# carries no part of the answer, so the law this check exists for -- HOW MANY,
# NEVER WHICH -- is untouched, and the exact-match discipline is kept rather
# than loosened to a subset.
check("  …and the journal says words went to the voice engine — how many, never which",
      FakeJournal.events,
      [("site", "speak_glass", {"engine": "tts.cloud_say", "voice": "DarrenNeural",
                                "language": "en-AU", "words": 5, "early": False,
                                "by": "Dave"})])
check("  …and whether it had a head start is a yes or a no, never a word of it",
      isinstance(FakeJournal.events[0][2].get("early"), bool))
check("  …with no word of the answer in it", "lights" in json.dumps(FakeJournal.events), False)
r = hit("POST", "/assist/speak", {"id": "u1"}, b'{"text":""}')
check("  …empty text → 502 with the reason", (r.get("code"), (r.get("obj") or {}).get("error")), (502, "nothing to say"))
rig(engines=())
r = hit("POST", "/assist/speak", {"id": "u1"}, b'{"text":"hello"}')
check("  …no engine → 502, said", (r.get("code"), "no speech engine" in str((r.get("obj") or {}).get("error"))), (502, True))

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + str(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
