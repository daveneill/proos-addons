"""THE PLATFORM IS NEVER NAMED IN A LOG LINE — register 1475 (25 Nov 2026).
Run: python3 tests/the_platform_is_never_named_in_a_log_line_bench.py

Dave, on his box, Master Log: "Homeassistant keeps warning — The platform logged …". "not happy that in logs I can see home
assistant"; "needs to be right can't have this leak". READ: ProOS writes that line itself (autofix.repeat_incident) and put
the warning part's own name in front — for the platform's core that name IS the platform's — and nothing filtered it for
anyone; the same title is handed to Assist. And ProOS's own log lines (the journal) left Core with no filter at all: only
the mirrored platform rows (1027) were filtered.

DRIVEN: the real proos/autofix.py and proos/platformlog.py.
  1. a repeating warning from the platform's own core reads "The platform keeps warning" — for every sign-in and for Assist
  2. an integration's warning still names the integration; the platform's name inside the quoted message reads ProOS
  3. for_glass: for anyone but the Developer every word in a log line has the name replaced — nested, in lists — and the
     record itself is not altered; identities (entity ids and the like) are left exactly as they are so nothing breaks
  4. the Developer gets every word as written (Dave, 27 Oct: "developer access gets everything word for word")
  5. WIRING (SPELLING): every door a log line leaves Core by passes through for_glass — a room's log, the Master Log,
     Assist's audit and the live stream
"""
import os
import re
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_named_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import autofix as AF  # noqa: E402
from proos import platformlog as PL  # noqa: E402

SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
NAME = PL._PLATFORM_NAME
A, B = "home", "assistant"                 # the name is never written whole in a module or a bench string (platform_words)
passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok    " + m)
    else:
        failed += 1
        print("  FAIL  " + m)


# 1
for nm in (A + B, A + B + ".helpers.entity", A + B + ".core", (A + B).title()):
    inc = AF.repeat_incident({"name": nm, "message": ["Error doing job: Task exception was never retrieved"], "rose": 21, "count": 22})
    ok(inc["title"] == "The platform keeps warning", "1 · a warning from the platform's own %s reads \"The platform keeps warning\" — got %r" % (nm.split(".")[-1], inc["title"]))
    ok(not NAME.search(inc["title"] + inc["cause"]), "1 · …and neither the title nor the cause names the platform")
# 2
inc = AF.repeat_incident({"name": A + B + ".components.unifiprotect", "message": [A.title() + " " + B.title() + " could not reach the camera"], "rose": 12, "count": 40})
ok(inc["title"] == "unifiprotect keeps warning", "2 · an integration's warning still names the integration — got %r" % inc["title"])
ok("ProOS could not reach the camera" in inc["cause"] and not NAME.search(inc["cause"]), "2 · the platform's name inside the quoted message reads ProOS")
ok("12 times in the last hour" in inc["cause"] and "40 times" in inc["cause"], "2 · the counts are untouched")
ok(inc["kind"] == "platform_warning_repeating" and inc["id"].startswith("log:"), "2 · the incident is otherwise what it was")
# 3
ev = {"ts": 1.0, "room": "site", "type": "platform_warning_repeating",
      "data": {"title": (A + B).title() + " keeps warning", "cause": "The platform logged \"" + A + "_" + B + " is slow\"",
               "entity_id": "update." + A + "_" + B + "_core_update", "lines": ["hassio said no", {"note": A.upper() + " " + B.upper()}], "rose": 21}}
before = repr(ev)
out = PL.for_glass([ev], developer=False)
d = out[0]["data"]
ok(d["title"] == "ProOS keeps warning" and "ProOS is slow" in d["cause"], "3 · the name is replaced in every word of the line")
ok(d["lines"][0] == "ProOS said no" and d["lines"][1]["note"] == "ProOS", "3 · …nested and in lists too")
ok(d["entity_id"] == "update." + A + "_" + B + "_core_update" and d["rose"] == 21 and out[0]["type"] == "platform_warning_repeating", "3 · identities and numbers are left exactly as they are")
ok(repr(ev) == before, "3 · the record itself is not altered")
ok(not NAME.search(repr({k: v for k, v in d.items() if k != "entity_id"})), "3 · nothing left in the line names the platform")
# 4
ok(PL.for_glass([ev], developer=True)[0]["data"]["title"] == (A + B).title() + " keeps warning", "4 · the Developer gets every word as written")
# 5
ok(len(re.findall(r"for_glass\(", SRV)) >= 4, "5 · four doors pass through for_glass (found %d)" % len(re.findall(r"for_glass\(", SRV)))
i = SRV.find('parts[2] == "journal"')
ok("for_glass(" in SRV[i:i + 900], "5 · a room's log")
i = SRV.find('parts == ["journal", "master"]')
ok("for_glass(" in SRV[i:i + 2600], "5 · the Master Log")
i = SRV.find('parts == ["assist", "audit"]')
ok("for_glass(" in SRV[i:i + 900], "5 · Assist's audit")
i = SRV.find("def _sse_stream(self):")
ok("for_glass(" in SRV[i:i + 2200], "5 · the live stream")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
