"""THE ASSIST AI DECIDES HOW A CONVERSATION IS HELD — register 1288.
Run: python3 tests/the_assist_ai_decides_the_conversation_bench.py

Dave 10 Nov 15:50: "can we have both options in Pro like we have now but by changing the AI it would take over the
assist". ChatGPT → Talk Live; Claude → the ordinary microphone listens again after each answer (conversation).

DRIVEN: the real realtime.available_for / conversation_for on assist.save_config, and the status route's source.
  1. Assist on OpenAI → Talk Live allowed, conversation not
  2. Assist on Claude → conversation allowed, Talk Live not (in words)
  3. the SAME home switch turns conversation off; on again → allowed
  4. the SAME per-person switch turns it off for one person only
  5. nobody signed in → refused
  6. the status route reports "conversation" beside "available"
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_cvb_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import realtime as RT, assist as A  # noqa: E402

A._CFG_PATH = os.path.join(os.environ["PROOS_DATA_DIR"], "assist.json")
passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


bec = {"id": "u_bec", "name": "Bec"}
dave = {"id": "u_dave", "name": "Dave"}

A.save_config({"provider": "openai", "model": "gpt-6-astra", "api_key": "sk-test"})
ok(RT.available_for(A.load_config(), bec)[0] is True, "1 · on OpenAI → Talk Live allowed")
ok(RT.conversation_for(A.load_config(), bec)[0] is False, "  …and the conversation mode is not")

A.save_config({"provider": "claude", "model": "claude-opus-5", "api_key": "sk-ant-test"})
cv = RT.conversation_for(A.load_config(), bec)
ok(cv == (True, ""), "2 · on Claude → conversation allowed")
tl = RT.available_for(A.load_config(), bec)
ok(tl[0] is False and "OpenAI" in tl[1], "  …and Talk Live refused, in words")

A.save_config({"realtime_enabled": False})
r = RT.conversation_for(A.load_config(), bec)
ok(r[0] is False and "home" in r[1], "3 · the home switch off → conversation off, in words")
A.save_config({"realtime_enabled": True})
ok(RT.conversation_for(A.load_config(), bec)[0] is True, "  …on again → allowed")

A.save_config({"realtime_off_for": ["u_bec"]})
r = RT.conversation_for(A.load_config(), bec)
ok(r[0] is False and "you" in r[1], "4 · off for Bec → Bec refused")
ok(RT.conversation_for(A.load_config(), dave)[0] is True, "  …Dave still allowed")

ok(RT.conversation_for(A.load_config(), {})[0] is False, "5 · nobody signed in → refused")

src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.find('if parts == ["assist", "realtime", "status"]:')
seg = src[i:i + 1200]
ok(i > 0 and "conversation_for(" in seg and '"conversation": bool(_cv)' in seg,
   "6 · the status route reports conversation beside available")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
