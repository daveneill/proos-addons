"""HEALTH'S AUTOMATIC HAND-OVERS TO ASSIST CAN BE PAUSED — register 1335 (13 Nov 2026).
Run: python3 tests/health_handovers_can_be_paused_bench.py

Read on Dave's box 13 Nov: 17 different problems went to the AI by themselves since 02:15 (once each, as 1164/1180/1182
built it). Dave 18:1x: "Pause it while we gauge". DRIVEN — the real assist settings in a scratch folder:
  1. on unless switched off; saved and read back through the same settings as Talk Live
  2. switching it off leaves everything else in the settings as it was (provider, model, Talk Live)
SOURCE — server.py (the three automatic roads, checked where they start):
  3. the watcher's automatic hand-over, Health's problems, and repeating platform warnings each ask the switch FIRST —
     before the once-a-day record is spent — and a paused problem is not marked handed, so switching on offers it then
  4. Fix in Pro is not gated (it calls _hand_to_assist directly)
"""
import os
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_hauto_")
from proos import assist  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


assist._CFG_PATH = os.path.join(os.environ["PROOS_DATA_DIR"], "assist.json")
check("1 · on when never set", (assist.health_auto(), assist.status(full=True)["health_auto"]), (True, True))
assist.save_config({"provider": "claude", "model": "claude-opus-5-5", "realtime_enabled": False})
st = assist.save_config({"health_auto": False})
check("   …off once switched off, read back", (assist.health_auto(), st["health_auto"]), (False, False))
check("2 · switching it leaves the rest as it was", (st["provider"], st["model"], st["realtime_enabled"]),
      ("claude", "claude-opus-5-5", False))
st = assist.save_config({"health_auto": True})
check("   …and on again", st["health_auto"], True)

src = open(os.path.join(CORE, "server.py"), encoding="utf-8").read()
w = src.split('if action == "assist":')[1][:1400]
h = src.split('if inc.get("kind") not in _autofix_mod.AUTO_KINDS or iid in _health_handed:')[1][:600]
lg = src.split("for row in _autofix_mod.repeating(rows):")[1][:1400]
check("3 · (SOURCE) each automatic road asks the switch before the once-a-day record is spent",
      (w.index("_health_auto_on()") < w.index("_autofix_mod.allowed(entity)"),
       h.index("_health_auto_on()") < h.index("_health_handed.add(iid)"),
       "_health_auto_on() and _autofix_mod.allowed(" in lg), (True, True, True))
fix = src.split('reason="Fix pressed in Pro"')[0][-600:]
check("4 · (SOURCE) Fix in Pro still hands over — not gated", "_health_auto_on" not in fix)
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
