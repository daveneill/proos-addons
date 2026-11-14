"""THE REALTIME VOICE USES PROOS ASSIST — one brain, a new mouth — register 1148 (1 Nov 2026).
Run: python3 tests/realtime_voice_uses_proos_assist_bench.py

Dave 15:32: "build the prototype so we can see as no matter what way we go it will need the ProOS assist to be rock
solid to communicate with". The provider's realtime model hears and speaks; every instruction and every tool is
ProOS Assist's own.

DRIVEN: the real proos/realtime.py with the provider stood in (the request Core sends is recorded).
  1. the session request carries Assist's OWN instructions for this person and room (the house rules, the room's
     devices with ids) under a voice preface, and Assist's OWN tools for this person — the deferred-group door left out
  2. the home's key goes only to the provider's session endpoint; what comes back to the page is the short-lived key
  3. a non-OpenAI or key-less Assist is refused in words
  4. a tool the model calls runs through Assist's ToolRunner.run — the same refusals apply (a hidden device stays hidden)
  5. WIRING: Core's route admits whom Talk Live is on for (1153); Pro's prototype talks to the provider with the short-lived key and sends
     every function call back to Core; the module ships
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_rt_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import realtime as RT, assist as A  # noqa: E402

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


SENT = []


def post(url, body, headers):
    SENT.append((url, body, headers))
    return {"value": "ek_short_lived", "expires_at": 123}


class Runner:
    user = {"id": "u", "name": "Dave", "is_owner": True}

    def __init__(self):
        self.ran = []

    def run(self, name, args):
        self.ran.append((name, args))
        if args.get("entity_id") == "light.hidden":
            return {"error": "that device is hidden from Assist — it can be made available again in Pro"}
        return {"ok": True}


A._active_tools = lambda runner: [{"name": "area_control", "description": "d", "input_schema": {"type": "object"}},
                                  dict(A._MORE_TOOLS)]
A._system_prompt = lambda user, home, where, eng: "DOCTRINE for %s in %s | HOUSE RULES: lamps means lamps | %s" % (
    user.get("name"), (where or {}).get("area_name"), "Bec's Lamp [light.bedroom_becs_lamp] on 20%")
where = {"area_id": "bedroom", "area_name": "Bedroom", "confidence": "certain"}
r = RT.new_session(A, {"provider": "openai", "api_key": "sk-home"}, Runner(), Runner.user, "Home", where, post=post)
url, body, hdr = SENT[-1]
inst = body["session"]["instructions"]
ok(inst.startswith("YOU ARE SPEAKING OUT LOUD") and "DOCTRINE for Dave in Bedroom" in inst and "HOUSE RULES" in inst
   and "[light.bedroom_becs_lamp]" in inst, "1 · the session carries Assist's own instructions, house rules and the room under a voice preface")
names = [t["name"] for t in body["session"]["tools"]]
# RE-PINNED, register 1159 (Dave 21:30: Talk Live said it could not check who is president): the session also carries
# web_search — the provider's own search, answered by Core (driven in realtime_is_for_everyone_switchable checks 7–8).
ok(names == ["area_control", "web_search"] and body["session"]["tools"][0]["type"] == "function",
   "  …and Assist's own tools for this person plus the web, the deferred-group door left out")
ok(url == RT.CLIENT_SECRETS and hdr.get("Authorization") == "Bearer sk-home" and r.get("value") == "ek_short_lived"
   and "sk-home" not in str(r), "2 · the home's key goes only to the provider; the page gets the short-lived key")
sess = body["session"]
ok(sess.get("output_modalities") == ["text"] and "output" not in (sess.get("audio") or {}) and r.get("voice") == "ryan",
   "1b · by DEFAULT the model answers in text — the home's own voice (Ryan) speaks it; no provider voice (register 1150)")
RT.new_session(A, {"provider": "openai", "api_key": "sk-home"}, Runner(), Runner.user, "Home", where, post=post, voice="provider")
s2 = SENT[-1][1]["session"]
ok("output_modalities" not in s2 and (s2.get("audio") or {}).get("output", {}).get("voice") == RT.DEFAULT_VOICE,
   "  …and the OpenAI voice only when asked for")
ok("error" in RT.new_session(A, {"provider": "claude", "api_key": "x"}, Runner(), Runner.user, "", {}, post=post)
   and "error" in RT.new_session(A, {"provider": "openai"}, Runner(), Runner.user, "", {}, post=post),
   "3 · a non-OpenAI or key-less Assist is refused in words")
rn = Runner()
o = RT.run_tool(rn, "device_control", '{"entity_id":"light.hidden","action":"turn_on"}')
ok(rn.ran == [("device_control", {"entity_id": "light.hidden", "action": "turn_on"})] and "hidden from Assist" in o.get("error", ""),
   "4 · a tool the model calls runs through Assist's ToolRunner — a hidden device stays hidden")
srv = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
pro = open(os.path.join(HERE, "..", "..", "deploy", "pro.html"), encoding="utf-8").read()
seg = srv.split('if parts in (["assist", "realtime", "session"], ["assist", "realtime", "tool"]):', 1)[1][:1200]
# RE-PINNED, register 1153 (Dave 18:41: everyone in the home, switchable): the routes are no longer owner/tech only —
# they admit exactly whom realtime.available_for admits (driven in realtime_is_for_everyone_switchable_bench).
ok("_rt.available_for(_cfg, u or {})" in seg and "return self._send(403, {\"error\": _why" in seg,
   "5 · Core's realtime routes admit exactly whom Talk Live is on for")
# RE-PINNED, register 1348 (Dave 14 Nov 15:04): Pro's "Talk to Assist — Realtime (Prototype)" is off Pro — the live voice
# for everyone is Talk Live in the ProOS app, which carries the same road. The three pins move to the app (the
# prototype's own names rt… became lv… there), and Pro is pinned to carry no prototype.
dash = open(os.path.join(HERE, "..", "..", "deploy", "dashboard.html"), encoding="utf-8").read()
ok("api.openai.com/v1/realtime/calls" in dash and "'Bearer '+s.value" in dash and "/assist/realtime/tool" in dash
   and "function_call_output" in dash, "  …the app's Talk Live calls the provider with the short-lived key and sends every tool call back to Core")
ok("function lvMaybeReply(lv)" in dash and "lv.pending>0" in dash and "case 'response.done'" in dash,
   "  …and asks for ONE reply after all of a turn's tool calls answered (seen on the box: two calls, two asks, refused)")
ok("/assist/speak" in dash and "function lvHush(lv)" in dash and "speech_started': lvHush(lv)" in dash
   and "Talk to Assist — Realtime (Prototype)" not in pro,
   "  …the app speaks through Core's own voice and hushes when you speak; Pro carries no prototype (1348)")
ok("proos/realtime.py" in open(os.path.join(HERE, "..", "zipfiles.txt")).read(), "  …and the module ships")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
