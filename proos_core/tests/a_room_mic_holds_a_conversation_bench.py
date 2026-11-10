"""A ROOM MIC HOLDS A CONVERSATION — register 1289.
Run: python3 tests/a_room_mic_holds_a_conversation_bench.py

Dave 10 Nov 20:02: "how can we have the room mics 2 way conversation like the app"; 20:13 "I have not got speakers in
the mic yet it uses the room speakers for the reply"; 20:15 "Okay" to: reopen after every answer when Assist is on
Claude, say the whole answer, wait for the whole answer however long, end on quiet or a goodbye.

DRIVEN (the real roommic module, a fake clock and fake mic):
  1. in a conversation the mic opens again after ANY answer; outside one only after a question; never after nothing
  2. the goodbyes end it; requests that start the same way do not
  3. the wait is the speaker starting + the answer's measured length — past the old 15 s ceiling — and capped
  4. reopen waits out an answer still playing after the mic's own turn ended, then opens it once
  5. a mic someone woke meanwhile (listening) is left alone
WIRING (server.py read — a spelling test of the wiring, said so):
  6. a room-mic turn asks realtime.conversation_for; a goodbye is answered "Okay." by the mic and nothing is said
     through the room; the room mic's answer is said whole and measured whole; the reopen carries "until"
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import roommic as rm  # noqa: E402

P = F = 0


def ok(c, m):
    global P, F
    if c:
        P += 1
        print("  ok  " + m)
    else:
        F += 1
        print("  FAIL " + m)


ok(rm.listens_again("The kitchen lights are on.", True), "1 · conversation: an ordinary answer reopens the mic")
ok(not rm.listens_again("The kitchen lights are on.", False), "  …outside a conversation it does not")
ok(rm.listens_again("Which lamp?", False), "  …a question still does, as before")
ok(not rm.listens_again("", True), "  …nothing said → nothing reopened")

for t in ("thanks", "Thank you.", "stop", "that's all", "okay bye", "no thanks jarvis", "never mind"):
    ok(rm.is_goodbye(t), '2 · "%s" ends the conversation' % t)
for t in ("stop the music", "thanks, turn the lights off", "what time is it"):
    ok(not rm.is_goodbye(t), '  …"%s" is a request' % t)

ok(rm.wait_until(30.0, 100.0) == 100.0 + rm.SPEAKER_START_S + 30.0, "3 · a 30 s answer is waited out in full (past 15 s)")
ok(rm.wait_until(1000.0, 0.0) == rm.CONVERSE_CAP_S, "  …and the wait is capped")
ok(rm.wait_until(None, 5.0) is None and rm.wait_until(0, 5.0) is None, "  …no measurement → no wait invented")


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


c = Clock()
states = {"m": "idle"}
started = []
res = rm.reopen(lambda e: states[e], lambda m: started.append((m, c.t)), "m", sleep=c.sleep, now=c.now, until=20.0)
ok(res.get("done") == "reopened" and len(started) == 1, "4 · the mic is opened again once")
ok(started and started[0][1] >= 20.0, "  …only after the answer has finished playing (%.1f s)" % (started[0][1] if started else -1))
ok(res.get("held_s") == 20.0, "  …and the wait is journalled")

c = Clock()
started = []


def woke(e):
    return "listening" if c.t >= 5 else "idle"


res = rm.reopen(woke, lambda m: started.append(m), "m", sleep=c.sleep, now=c.now, until=10.0)
ok(not started and res.get("done") == "mic already listening", "5 · a mic someone woke meanwhile is left alone")

src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.find("REGISTER 1289 — A ROOM MIC HOLDS A CONVERSATION")
seg = src[i:i + 1800]
ok(i > 0 and "conversation_for(_assist.load_config(), uinfo)" in seg and "is_goodbye(_text)" in seg
   and '"reply": "Okay."' in seg and '"sent": False' in seg, "6 · a room-mic turn asks the switch; a goodbye is \"Okay.\" by the mic")
ok('def _assist_speak(where, reply, whole=True):' in src and '_assist_speak(where, _res.get("reply"))' in src,
   "  …a room mic's answer is said whole (1290: every answer is, by default)")
ok('_glass_voice_audio(_res.get("reply"), whole=True)' in src and '_sp["until"] = _rmic.wait_until(_secs, _t_said)' in src,
   "  …measured whole, and the reopen waits for it")
ok("_rmic.listens_again(_res.get(\"reply\"), _cv_room)" in src and 'until=(spoke or {}).get("until")' in src,
   "  …and the reopen follows the conversation, not only a question")
print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
