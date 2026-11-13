"""ROOM MICS — "Replies Through" per room mic, and the mic opening again after a question (register 970vv).
Run: python3 tests/room_mic_replies_bench.py

Dave, 24 Oct 2026, 09:10: a choice per room mic, Room Speakers or This Device ("devices we build might be a mic
only"). Read on his Office Voice board 09:04: a question spoken through the room speaker left the board idle — its
software reopens only after playing a reply itself. The platform's start_conversation reopened it by hand (read).

DRIVEN (the real module, a fake clock and fake states):
 1. the choice: unset = Room Speakers; This Device is stored on the room record; Room Speakers clears it;
    anything else, or a non-mic, is refused
 2. a reply that ends in a question is a question; nothing else is
 3. the room's mics: by the entity's own room or its device's room; switched-off ones left out; the device's name
 4. after a question: when the mic's turn ends it is reopened ONCE; a mic that reopened by itself is left alone; a
    mic stuck busy is not touched; a refusal is said; music on the speaker cannot hold it up (970ww)
 4b. the question's length is read from its own audio (WAV, MP3, FLAC); the hold is speaker start-up + that length
 5. the moment of silence is a real 0.3 s sound file, written once
WIRING (source read — a spelling test of the wiring, said so):
 6. a room-mic turn set to This Device is not spoken through the room; a question through the room speaker starts
    the reopen; the door hands an unspoken reply to the mic to say itself
"""
import os
import sys
import tempfile
import wave

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


M = "assist_satellite.office_voice"
# 1
rec = {"area_id": "office"}
check("1 · unset is Room Speakers", rm.replies_for(rec, M), "room")
rm.set_replies(rec, M, "device")
check("  …This Device is stored on the room record", (rec.get("mic_replies"), rm.replies_for(rec, M)),
      ({M: "device"}, "device"))
rm.set_replies(rec, M, "room")
check("  …Room Speakers clears it (the default is stored as absent)", ("mic_replies" in rec, rm.replies_for(rec, M)),
      (False, "room"))


def refused(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


check("  …anything else is refused", refused(lambda: rm.set_replies(rec, M, "loud")))
check("  …a thing that is not a room mic is refused", refused(lambda: rm.set_replies(rec, "light.office", "device")))
check("  …an unknown stored value reads as Room Speakers", rm.replies_for({"mic_replies": {M: "x"}}, M), "room")

# 2
check("2 · 'Did you mean Netflix, or something else?' is a question", rm.asks("Did you mean Netflix, or something else?  "))
check("  …'Office, playing Favourite Songs.' is not", rm.asks("Office, playing Favourite Songs."), False)

# 3
ents = [{"entity_id": M, "device_id": "d1", "area_id": None},
        {"entity_id": "assist_satellite.panel", "device_id": "d2", "area_id": "office"},
        {"entity_id": "assist_satellite.kitchen", "device_id": "d3", "area_id": None},
        {"entity_id": "assist_satellite.old", "device_id": "d1", "area_id": "office", "disabled_by": "user"},
        {"entity_id": "light.office", "device_id": "d1", "area_id": "office"}]
devs = [{"id": "d1", "area_id": "office", "name": "satellite1-cfeacc", "name_by_user": "Office Voice"},
        {"id": "d2", "area_id": "kitchen", "name": "ProOS Panel"},
        {"id": "d3", "area_id": "kitchen", "name": "Kitchen Mic"}]
got = rm.mics_in_room("office", ents, devs)
check("3 · the room's mics: by its device's room, or its own room (which wins over the device's)",
      [m["entity"] for m in got], [M, "assist_satellite.panel"])
check("  …named after the device as the installer named it", got[0]["name"], "Office Voice")


# 4 · a fake clock and fake states
class World:
    def __init__(self, script):
        self.t = 0.0
        self.script = script          # entity -> list of (from_time, state)
        self.calls = []

    def now(self):
        return self.t

    def sleep(self, d):
        self.t += d

    def state_of(self, e):
        st = ""
        for at, s in self.script.get(e, []):
            if self.t >= at:
                st = s
        return st

    def start(self, m):
        self.calls.append((round(self.t, 1), m))


w = World({M: [(0, "processing"), (4.2, "idle")]})
r = rm.reopen(w.state_of, w.start, M, sleep=w.sleep, now=w.now)
check("4 · when the mic's turn ends (the door held it while the question played), it is reopened once",
      (r["done"], len(w.calls)), ("reopened", 1))
check("  …not before the turn had ended, and within half a second of it", 4.2 <= w.calls[0][0] <= 4.7)
w = World({M: [(0, "processing"), (4.0, "idle"), (4.1, "listening")]})
r = rm.reopen(w.state_of, w.start, M, sleep=w.sleep, now=w.now)
check("  …a mic that reopened by itself (the G-Flip panel's) is left alone", (r["done"], w.calls),
      ("mic already listening", []))
w = World({M: [(0, "responding")]})
r = rm.reopen(w.state_of, w.start, M, sleep=w.sleep, now=w.now)
check("  …a mic stuck mid-turn is not touched", (r["done"], w.calls), ("mic still busy", []))


def boom(m):
    raise RuntimeError("HTTP 400: busy")


w = World({M: [(0, "idle")]})
r = rm.reopen(w.state_of, boom, M, sleep=w.sleep, now=w.now)
check("  …a refusal is said, never raised", r["done"].startswith("could not reopen: HTTP 400"))
check("  …music on the room speaker cannot hold it up (the speaker is no longer watched — 970ww)",
      "speaker" not in rm.reopen.__code__.co_varnames)

# 4b · how long the question takes — measured from the audio itself (970ww)
import io as _io  # noqa: E402
import math as _m  # noqa: E402
import struct as _st  # noqa: E402


def wav(secs, rate=22050):
    b = _io.BytesIO()
    x = wave.open(b, "wb")
    x.setnchannels(1)
    x.setsampwidth(2)
    x.setframerate(rate)
    x.writeframes(_st.pack("<h", 0) * int(secs * rate))
    x.close()
    return b.getvalue()


check("4b · a 2.6 s WAV reads 2.6 s", round(rm.audio_seconds("audio/wav", wav(2.6)), 2), 2.6)


def mp3(frames, br_idx=9, sr_idx=0):
    # MPEG-1 layer III, 128 kbps, 44.1 kHz: 417/418-byte frames of 1152 samples
    out = b"ID3" + bytes([4, 0, 0, 0, 0, 0, 5]) + b"\0" * 5
    for k in range(frames):
        pad = 1 if k % 3 == 0 else 0
        size = int(144 * 128000 / 44100) + pad
        hdr = bytes([0xFF, 0xFB, (br_idx << 4) | (sr_idx << 2) | (pad << 1), 0x64])
        out += hdr + b"\0" * (size - 4)
    return out


got = rm.audio_seconds("audio/mpeg", mp3(100))
check("  …an MP3 is read frame by frame: 100 frames = 2.61 s", round(got, 2), round(100 * 1152 / 44100.0, 2))
fl = b"fLaC" + bytes([0x00, 0, 0, 34]) + bytes(10) + bytes([0x05, 0x62, 0x20, 0x00, 0x00, 0x00, 0xAC, 0x44]) + bytes(16)
check("  …a FLAC is read from its STREAMINFO: 44100 samples at 22050 Hz = 2.0 s", rm.audio_seconds("audio/flac", fl), 2.0)
check("  …something unreadable claims nothing", rm.audio_seconds("audio/x", b"hello there"), None)
check("  …the hold is the speaker starting up plus the question: 2.6 s → 3.6 s (the old estimate for 8 words: 4.7 s)",
      rm.hold_seconds(2.6), 3.6)
check("  …never longer than the door's ceiling, nothing for nothing", (rm.hold_seconds(40), rm.hold_seconds(None)),
      (15.0, None))

# 5
d = tempfile.mkdtemp(prefix="proos_rm_")
rm.ensure_silence(d)
p = os.path.join(d, rm.SILENCE_NAME)
wf = wave.open(p)
check("5 · the silence is a real sound file of 0.3 s", round(wf.getnframes() / wf.getframerate(), 2), 0.3)
m0 = os.stat(p).st_mtime_ns
rm.ensure_silence(d)
check("  …written once, left alone after", os.stat(p).st_mtime_ns, m0)
check("  …served where the platform resolves it", rm.SILENCE_URL, "/local/proos_silence.wav")

# 6 · wiring (source read)
from proos import door  # noqa: E402
CONV = door._CONV
SRV = open(os.path.join(HERE, "..", "server.py")).read()
fin = SRV[SRV.index("def _finish(_res):"):SRV.index("# THE PANEL'S SCREEN SHOWS WHAT IT HEARD")]
check("6 · wiring: This Device is not spoken through the room",
      'replies_for(_mrec, _mic) == "device"' in fin and fin.index('== "device"') < fin.index("_assist_speak("))
# RE-PINNED WITH CAUSE, REGISTER 1289: the reopen now asks listens_again(reply, conversation) — a question still
# reopens on its own (driven in a_room_mic_holds_a_conversation_bench, check 1), and in a conversation so does any answer.
check("  …wiring: a question through the room speaker starts the reopen",
      "_rmic.listens_again(" in fin and "target=_mic_reopen" in fin and rm.listens_again("Which lamp?", False))
check("  …wiring: the reopen asks the platform's start_conversation with the silence, no chime",
      '"start_conversation", m,' in SRV and '"preannounce": False' in SRV)
check("  …wiring: the measured length rides to the door, and the door holds the turn that long (build 5)",
      '_sp["say_s"] = _rmic.hold_seconds(_secs)' in fin and 'say = (res.get("spoke") or {}).get("say_s")' in CONV
      and door.DOOR_BUILD in ("5", "6", "7", "8", "9", "10", "11", "12", "13"))   # 1330d/e/h: builds 11-13 (the Matter Hub door) keep it   # 1326: build 10 (Repairs) keeps it   # 1325: build 9 (ProOS's AI tools) keeps the measured hold   # 1306: build 8 (ProOS's sensors) keeps it too   # re-pinned 1259: build 6 (the ProOS announcement) keeps the measured hold; 1262: build 7 (its minute check on the platform's loop) keeps it too
check("  …wiring: the door hands an unspoken reply to the mic to say itself (and keeps it listening after a question)",
      'resp.async_set_speech(res.get("reply")' in CONV and "continue_conversation=asks" in CONV)
check("  …wiring: the setting is changed only by the trade and journaled",
      'parts[2] == "mic-replies"' in SRV and '"mic_replies", {' in SRV)

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
