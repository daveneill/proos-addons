"""THE ROOM'S VOICE VOLUME — how loud ProOS speaks in a commissioned room, apart from the music (registers 970xx, 970zz).
Run: python3 tests/room_voice_volume_bench.py

Dave, 24 Oct 2026: 10:10 "the volume the voice comes through the speakers is low … make it part of pro as a room
ajustment"; 10:15 "too loud i would say 25"; 10:35 "25% is good default"; 10:38 "It needs to be brand agnostic … if
its cable of transmitting the voice replies then it defaults at that volume and can be changed for the room"; 10:44
"apple tv and shields are media devices not audio … these settings only apply to a room that knows its audio endpoint
… and is commisioned".

DRIVEN (the real module; the real _assist_speak with a recording platform client):
 1. the setting: unset = 25 %; 10-100 or the speaker's own stored on the room; null = back to 25; anything else refused
 2. only a COMMISSIONED room's committed AUDIO endpoint has one — not a room that is not commissioned, not one with no
    audio endpoint, and never its TV or streamer
 3. how the level is reached is what the speaker reports, not its brand: announce (read: Sonos) · swap (it can set a
    volume) · none
 4. announce: play_media as an announcement with extra.volume = the level; the music is not touched
 5. swap: the level is set, the reply spoken, and the speaker's own level put back after the reply's measured length —
    unless someone changed it meanwhile; a failed reply puts it back at once
 6. none: spoken at its own level, said; the speaker's own: spoken exactly as before
 7. the platform's own text-to-speech media id
WIRING (source read, named so):
 8. Pro offers Voice Volume (Speaker's Own, 10-100 %, 25 marked Default) and a Test, only where the room has an audio
    endpoint; the route refuses a room without one, is the trade's, and is journaled
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import roommic as rm  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


def refused(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


SPK = "media_player.office_office"
# 1
rec = {"area_id": "office", "committed": True, "audio": [SPK]}
check("1 · unset is 25 % (Dave: \"25% is good default\")", rm.voice_volume(rec), 25)
rm.set_voice_volume(rec, 40)
check("  …40 % is stored on the room", rm.voice_volume(rec), 40)
rm.set_voice_volume(rec, "own")
check("  …the speaker's own is a choice of its own", (rec.get("voice_volume"), rm.voice_volume(rec)), ("own", None))
rm.set_voice_volume(rec, None)
check("  …null goes back to the default (stored as absent)", ("voice_volume" in rec, rm.voice_volume(rec)), (False, 25))
check("  …0.5, 5, 101, a word or true are refused",
      all(refused(lambda v=v: rm.set_voice_volume({}, v)) for v in (0.5, 5, 101, "loud", True)))

# 2
check("2 · a commissioned room's committed audio endpoint is the voice's speaker", rm.audio_endpoint(rec), SPK)
check("  …a room that is not commissioned has none", rm.audio_endpoint(dict(rec, committed=False)), None)
check("  …a commissioned room with no audio endpoint has none", rm.audio_endpoint({"committed": True, "audio": []}), None)
bed = {"committed": True, "audio": ["media_player.bedroom_bedroom"], "display": "media_player.bedroom_tv",
       "sources": ["media_player.bedroom_bedroom_apple_tv"]}
check("  …never the TV or the streamer — the audio endpoint only", rm.audio_endpoint(bed), "media_player.bedroom_bedroom")

# 3
check("3 · what the speaker reports decides: Sonos with announce → announce", rm.method_for("sonos", 8321599), "announce")
check("  …any brand that can set its volume → swap", rm.method_for("unifiprotect", 4 | 512), "swap")
check("  …Sonos that does not report announce → swap, not assumed", rm.method_for("sonos", 4), "swap")
check("  …a speaker that cannot set a volume → none", rm.method_for("androidtv_remote", 0), None)

# 4-6 · the real _assist_speak with a recording client
SRC = open(os.path.join(HERE, "..", "server.py")).read()
# RE-PINNED WITH CAUSE, REGISTER 1289: _assist_speak gained whole=False (a room mic's answer said in full); the
# default road below is unchanged, so the slice now starts at the name without the closing bracket.
code = SRC[SRC.index("def _assist_speak(where, reply"):SRC.index("# ── THE VOICE IS MADE WHILE THE ANSWER IS STILL BEING WRITTEN")]
CALLS, THREADS, JOURNAL = [], [], []


class Client:
    def __init__(self, plat, feats, vol=0.12):
        self.plat, self.feats, self.vol = plat, feats, vol
        self.fail_speak = False

    def call_service(self, d, s, e, data=None):
        CALLS.append((d, s, e, data))
        if (d, s) == ("media_player", "volume_set"):
            self.vol = data["volume_level"]
        if (d, s) == ("tts", "speak") and self.fail_speak:
            raise RuntimeError("HTTP 500: engine down")

    def entity_registry(self):
        return [{"entity_id": SPK, "platform": self.plat}]

    def snapshot(self, ids):
        return {SPK: {"state": "paused", "attributes": {"supported_features": self.feats, "volume_level": self.vol}}}

    def area_registry(self):
        return [{"area_id": "office", "name": "Office"}]


class _T:
    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        THREADS.append(self.target)


class _J:
    @staticmethod
    def emit(room, kind, data):
        JOURNAL.append((room, kind, data))


import struct  # noqa: E402
import wave  # noqa: E402
import io  # noqa: E402


def _wav(secs):
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(struct.pack("<h", 0) * int(secs * 16000))
    w.close()
    return b.getvalue()


SLEPT = []
_rv_defaults = rm.restore_volume.__defaults__
rm.restore_volume.__defaults__ = (SLEPT.append,)   # restore_volume's own wait, recorded not slept
ns = {"_speak_trim": lambda m: (m, False), "_speak_plain": lambda m: m,   # 1290: whole by default (re-pinned with cause)
       "_tts_engine": lambda: "tts.proos_voice",
      "_voice_choice": lambda: ("tts.proos_voice", "en_GB-ryan", "en_GB"), "__name__": "bench",
      "threading": type("T", (), {"Thread": _T}), "_journal_mod": _J,
      "_glass_voice_audio": lambda t: ("audio/wav", _wav(2.6), {})}
exec(compile(code, "server_assist_speak", "exec"), ns)


def speak(r, client):
    CALLS.clear()
    THREADS.clear()
    JOURNAL.clear()
    ns["_assist_room_rec"] = lambda aid: r
    ns["_client"] = client
    return ns["_assist_speak"]({"area_id": "office"}, "Do you mean music, or something to watch?")


# 4 · announce
out = speak(dict(rec), Client("sonos", 8321599))
d, s, e, data = CALLS[0]
check("4 · Sonos at the default: play_media as an announcement at volume 25, nothing else touched",
      (d, s, e, data.get("announce"), data.get("extra"), len(CALLS), out.get("volume_how")),
      ("media_player", "play_media", SPK, True, {"volume": 25}, 1, "announce"))
check("  …with the platform's own id for the same words and voice",
      parse_qs(urlparse(data["media_content_id"]).query)["message"], ["Do you mean music, or something to watch?"])

# 5 · swap
c = Client("unifiprotect", 4 | 512, vol=0.6)
out = speak(dict(rec, voice_volume=30), c)
check("5 · a speaker that can only set its volume: set to 30 %, then spoken",
      [(x[0], x[1], (x[3] or {}).get("volume_level")) for x in CALLS],
      [("media_player", "volume_set", 0.3), ("tts", "speak", None)])
check("  …and a put-back is waiting", (out.get("volume_how"), len(THREADS)), ("swap", 1))
THREADS[0]()
check("  …after the reply's measured length (2.6 s + 1.0 s start), its own 60 % is put back",
      (SLEPT[-1], CALLS[-1][1], CALLS[-1][3], JOURNAL[-1][2]["done"]), (3.6, "volume_set", {"volume_level": 0.6}, "restored"))
c = Client("unifiprotect", 4 | 512, vol=0.6)
speak(dict(rec, voice_volume=30), c)
c.vol = 0.45                                       # someone turned it during the reply
THREADS[0]()
check("  …but a level someone changed during the reply is left as they set it",
      (JOURNAL[-1][2]["done"], c.vol), ("changed by someone during the reply — left", 0.45))
c = Client("unifiprotect", 4 | 512, vol=0.6)
c.fail_speak = True
out = speak(dict(rec, voice_volume=30), c)
check("  …a reply that fails puts its level back at once", (out.get("sent"), CALLS[-1][1], c.vol), (False, "volume_set", 0.6))

# 6 · none, and the speaker's own
out = speak(dict(rec), Client("androidtv_remote", 0))
check("6 · a speaker that cannot set a volume: spoken at its own level, and said",
      ([x[:2] for x in CALLS], out.get("volume_ignored")),
      ([("tts", "speak")], "this speaker cannot set a volume — it speaks at its own level"))
out = speak(dict(rec, voice_volume="own"), Client("sonos", 8321599))
check("  …the speaker's own: spoken exactly as before (tts.speak)", [x[:2] for x in CALLS], [("tts", "speak")])
out = speak(dict(rec, committed=False), Client("sonos", 8321599))
check("  …a room that is not commissioned speaks nothing", (CALLS, out.get("why")), ([], "room not commissioned"))
rm.restore_volume.__defaults__ = _rv_defaults

# 7
mid = rm.tts_media_id("tts.proos_voice", "Which room?", "en_AU", {"voice": "en_AU-amy"})
u, q = urlparse(mid), parse_qs(urlparse(mid).query)
check("7 · the platform's own text-to-speech id: engine, words, language, voice",
      (u.scheme, u.netloc, u.path, q["message"], q["language"], json.loads(q["tts_options"][0])),
      ("media-source", "tts", "/tts.proos_voice", ["Which room?"], ["en_AU"], {"voice": "en_AU-amy"}))

# 8 · wiring
PRO = open(os.path.join(HERE, "..", "apps", "pro.html"), "rb").read().decode("utf-8", "replace")
check("8 · wiring: Pro offers Voice Volume — Speaker's Own, 10-100 %, 25 marked Default — and a Test",
      "Voice Volume" in PRO and "Speaker\\u2019s Own" in PRO and "' (Default)'" in PRO and "v2RoomVoiceVolTest()" in PRO)
check("  …wiring: only where the room has an audio endpoint", "Commission this room with an audio endpoint" in PRO)
check("  …wiring: the route refuses a room without one, is the trade's, and is journaled",
      'parts[2] == "voice-volume"' in SRC and "not commissioned with an audio endpoint" in SRC and '"voice_volume", {' in SRC)

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
