"""A PLATFORM WARNING THAT KEEPS REPEATING GOES TO ASSIST — register 1182 (3 Nov 2026).
Run: python3 tests/a_repeating_platform_warning_goes_to_assist_bench.py

Read 2 Nov: the Samsung integration's "took longer than the scheduled update interval" — 379 times since 25 Oct, mirrored
into the Master Log and never acted on. Dave 3 Nov 08:58: "yes" to 10 repeats an hour (the audit's item 4).

DRIVEN: the real proos/autofix.py with the platform's own system_log rows (name, message, level, count).
  1. a warning whose platform count rises by 10 within an hour is returned, with how many times; then its history
     restarts (not returned again on the next read unless it rises 10 more)
  2. a rise of 9 in an hour is not; a slow trickle (2 an hour, like the Samsung's average) is not
  3. INFO lines are not; a count that goes DOWN (the platform restarted) starts again, never a false rise
  4. it is worded for Assist as a Health problem — who is warning, the platform's own message, how many times
  5. WIRING (SPELLING — the read runs on Core's thread): the platform-log read checks every read, journals it, and hands it
     to Assist only when allowed today (once per message per day)
"""
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_rep_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import autofix as AF  # noqa: E402

SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


MSG = "Updating samsungtv_smart media_player took longer than the scheduled update interval 0:00:05"


def row(count, level="WARNING", name="homeassistant.helpers.entity", msg=MSG):
    return {"name": name, "message": [msg], "level": level, "count": count}


t0 = 1_800_000_000.0
AF._seen_counts.clear()
ok(AF.repeating([row(100)], t0) == [], "  (first reading: nothing to compare yet)")
ok(AF.repeating([row(105)], t0 + 1200) == [], "2 · a rise of 5 in 20 min is not")
got = AF.repeating([row(110)], t0 + 3000)
ok(len(got) == 1 and got[0]["rose"] == 10, "1 · its own count rose by 10 within the hour → returned (rose %s)" % (got[0]["rose"] if got else None))
ok(AF.repeating([row(111)], t0 + 3060) == [], "  …then its history restarts — not returned again for one more")

AF._seen_counts.clear()
AF.repeating([row(100)], t0)
ok(AF.repeating([row(109)], t0 + 3500) == [], "2 · a rise of 9 within the hour is not")
AF._seen_counts.clear()
trickle = [AF.repeating([row(100 + 2 * h)], t0 + 3600 * h) for h in range(0, 8)]
ok(not any(trickle), "  …nor a slow trickle of 2 an hour (the Samsung's 379-in-8-days average)")

AF._seen_counts.clear()
AF.repeating([row(1, level="INFO")], t0)
ok(AF.repeating([row(50, level="INFO")], t0 + 600) == [], "3 · INFO lines are not")
AF._seen_counts.clear()
AF.repeating([row(400)], t0)
ok(AF.repeating([row(3)], t0 + 600) == [] and AF.repeating([row(12)], t0 + 1200) == [],
   "  …a count that went DOWN (the platform restarted) starts again — never a false rise")
ok(len(AF.repeating([row(13)], t0 + 1300)) == 1, "  …and counts from there (3 → 13 in the hour)")

AF._seen_counts.clear()
AF.repeating([row(100)], t0)
r = AF.repeating([row(120)], t0 + 600)[0]
inc = AF.repeat_incident(r)
ok(inc["kind"] == "platform_warning_repeating" and inc["title"] == "homeassistant.helpers.entity keeps warning"
   and MSG in inc["cause"] and "20 times in the last hour" in inc["cause"] and inc["subject"].startswith("log:"),
   "4 · worded for Assist: who, the platform's own message, 20 times in the hour")
ok("Updating samsungtv_smart" in AF.health_brief(inc), "  …and Assist's brief carries it")

pl = SRV[SRV.index("def _platform_log_read():"):SRV.index("def _platform_faults_loop():")]
# re-pinned 1335: the pause switch is asked first; the once-a-day rule is kept
ok("for row in _autofix_mod.repeating(rows):" in pl and '"platform_warning_repeating"' in pl
   and 'if _health_auto_on() and _autofix_mod.allowed(inc["subject"]):' in pl and "target=_health_to_assist" in pl,
   "5 · WIRING: every platform-log read checks it, journals it, hands it to Assist once a day (SPELLING)")
ok(AF.REPEAT_PER_HOUR == 10, "  …at Dave's 10 an hour")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
