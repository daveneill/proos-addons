"""A "never came on" card says WHICH DAY it was asked — REGISTER 1467 (one-list 10.35).  DRIVEN.
Run: python3 tests/a_never_came_on_card_says_the_day_bench.py

Dave's box, 25 Nov 2026: Health showed "Living Room — Living Room TV never came on · Asked for Watch Apple TV at 4:04 pm"
for a request made on 12 NOVEMBER. The card wrote the request's time with the clock alone, however old it was, so a
thirteen-day-old incident read as today's. Assist had to read the record to find the date. The card now says the day and
date whenever the request was not today (healthmon's own _when_day — nothing new is measured, no window, no constant).
Driven through the real _missed_check:
 1. asked today            -> the clock time only, as before
 2. asked thirteen days ago -> the clock time WITH the weekday, day and month
 3. the rest of the card is as it was (what was asked, the display, what it reads)
"""
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ.setdefault("PROOS_DATA_DIR", "/tmp/proos_bench_missed_day")
os.makedirs(os.environ["PROOS_DATA_DIR"], exist_ok=True)
from proos import healthmon as H  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


def card(fired_at):
    S = {"sensor.proos_activity_living_room": {"state": "off", "attributes": {
            "area": "Living Room", "missed_activity": {"key": "watch_apple_tv", "label": "Watch Apple TV",
                                                       "fired_at": fired_at, "display": "media_player.living_room_tv"}}},
         "media_player.living_room_tv": {"state": "off", "attributes": {"friendly_name": "Living Room TV"}}}
    seen = set()
    H._missed_check(S, seen)
    H._sweep_clears(seen)
    c = [i for i in H.incidents() if i["kind"] == "activity_missed"]
    out = c[0] if c else {}
    S["sensor.proos_activity_living_room"]["attributes"].pop("missed_activity")       # clear it for the next case
    seen = set(); H._missed_check(S, seen); H._sweep_clears(seen)
    return out


def clock(ts):
    return time.strftime("%-I:%M %p", time.localtime(ts)).lower()


def day(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %-d %b")


noon = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
c = card(noon)
check("1 · asked today: the clock time alone", c.get("cause", "").startswith("Asked for Watch Apple TV at %s; " % clock(noon)))
check("1 · …with no day in it", day(noon) in c.get("cause", ""), False)

old = noon - 13 * 86400 + 4 * 3600 + 240                       # thirteen days ago, 4:04 pm
c = card(old)
check("2 · asked thirteen days ago: the time WITH the weekday, day and month",
      c.get("cause", "").startswith("Asked for Watch Apple TV at %s %s; " % (clock(old), day(old))))
check("2 · …and it reads the way the box says it — 4:04 pm", clock(old), "4:04 pm")
yest = noon - 86400
c = card(yest)
check("2 · asked yesterday is not today either", ("%s %s" % (clock(yest), day(yest))) in c.get("cause", ""))

check("3 · the rest of the card is as it was", (c.get("title"), "Living Room TV never reported on" in c.get("cause", ""),
                                               'It reads "off"' in c.get("cause", "")),
      ("Living Room — Living Room TV never came on", True, True))

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)) + ("  " + repr(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
