"""
Power-timeline bench — run: python3 tests/powerlog_bench.py

SPEC (Dave, 2 Aug 2026): "see at a glance all the times this TV or media
player turned on or off... if it's happening at times when clients did not
prompt it then it's an issue with the media player, not the system." The
timeline attributes and never judges.

RE-PINNED WITH CAUSE, register 1324 (12 Nov 2026, plan item B8 — "who did it" from the platform's own record):
rule 2 used to be "proos = a room script ran within 120 s before (or 5 s after)" — a guess from timing. The platform
records the cause of every state change (its logbook: the action and the account behind it, or nothing at all).
Read on Dave's box 12 Nov: the Bedroom TV's ON at 04:01 UTC = call_service script.turn_on by "Supervisor" (the
account every app on the box uses); OFF at 04:17 = no cause; ON at 04:50 = script.turn_on by "Dave". Rule 2 now pins
the platform's record; the 120 s window is gone (the old "a script 5 minutes earlier vouches for nothing" and "3 s
before the script" pins described the window itself and leave with it).

RULES PINNED
------------
1. Only POWER transitions count: playing->paused is not an event.
   RE-PINNED 2 Oct 2026 (register 659): unavailable/unknown is its OWN row,
   "no_reading", attributed to no one.
2. (1324) Each change's cause is the platform's record of THAT change (its time, to the ms): a person by name; "an app
   on the box" for the shared Supervisor account (never guessed to be ProOS); an automation by name; HomeKit & co. as
   integrations; no cause = the device itself; NO RECORD = unknown — never "the device did it".
3. Newest first; empty inputs are empty timelines, never errors.
4. (1324) DRIVEN end to end: fetch_log reads history for the timeline, the logbook for causes, the platform's accounts
   for names — and an unreadable logbook makes every cause unknown, not "external".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos import powerlog as P                                   # noqa: E402
from proos.powerlog import transitions                            # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


T = 1_794_456_000.0
USERS = {"f7214f": {"name": "Supervisor", "system": True}, "e20c29": {"name": "Dave", "system": False}}


def row(ts, state, **ctx):
    from datetime import datetime, timezone
    r = {"when": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "state": state}
    r.update(ctx)
    return r


# ── 1. only power transitions count ────────────────────────────────────────
check("playing->paused is not a power event",
      transitions([(T, "playing"), (T + 10, "paused"), (T + 20, "idle")]), [])
check("a dropout is its own row, never OFF (register 659)",
      transitions([(T, "on"), (T + 10, "unavailable"), (T + 20, "on")]),
      [{"ts": T + 10, "to": "no_reading"}, {"ts": T + 20, "to": "on"}])
check("off → unknown → off is two rows, not silence",
      [t["to"] for t in transitions([(T, "off"), (T + 10, "unknown"), (T + 20, "off")])],
      ["no_reading", "off"])
check("standby still reads as off (a power state, not a dropout)",
      transitions([(T, "on"), (T + 10, "standby")]), [{"ts": T + 10, "to": "off"}])

# ── 2. the cause is the platform's record (the rows read on Dave's box, 12 Nov) ─────────────────────────────────────
trans = transitions([(T, "off"), (T + 100.123, "on"), (T + 1060.5, "off"), (T + 3000.25, "on"), (T + 3400, "off"),
                     (T + 3600, "on"), (T + 3700, "unavailable")])
rows = [row(T + 100.123, "on", context_event_type="call_service", context_domain="script", context_service="turn_on",
            context_user_id="f7214f"),
        row(T + 1060.5, "off"),                                                     # no cause recorded
        row(T + 3000.25, "on", context_event_type="call_service", context_domain="script", context_service="turn_on",
            context_user_id="e20c29"),
        row(T + 3400, "off", context_event_type="automation_triggered", context_name="Goodnight",
            context_entity_id="automation.goodnight"),
        row(T + 3600, "on", context_event_type="homekit_state_change", context_domain="homekit")]
log = P.attribute_from_logbook(trans, rows, USERS)
check("the Supervisor account is 'an app on the box' — never guessed to be ProOS",
      [(e["by"], e["who"], e["how"]) for e in log if abs(e["ts"] - (T + 100.123)) < 0.01],
      [("app", "an app on the box (Supervisor)", "script.turn_on")])
check("no cause recorded = the device itself", [e["by"] for e in log if abs(e["ts"] - (T + 1060.5)) < 0.01], ["device"])
check("a person by name, with the action", [(e["by"], e["who"], e["how"]) for e in log if abs(e["ts"] - (T + 3000.25)) < 0.01],
      [("person", "Dave", "script.turn_on")])
check("an automation by name", [(e["by"], e["who"]) for e in log if e["ts"] == T + 3400], [("automation", "Goodnight")])
check("HomeKit (or Google …) as the integration", [(e["by"], e["who"]) for e in log if e["ts"] == T + 3600], [("integration", "homekit")])
check("a dropout is still attributed to no one", [e["by"] for e in log if e["to"] == "no_reading"], [None])
check("a change with NO logbook entry is 'unknown' — never 'the device did it'",
      P.attribute_from_logbook(transitions([(T, "off"), (T + 50, "on")]), [], USERS)[0]["by"], "unknown")
check("an account since removed is still a person, said so", P.cause_of({"context_event_type": "call_service", "context_user_id": "gone"}, USERS)["who"],
      "an account no longer on the box")

# ── 3. newest first; empty inputs ─────────────────────────────────────────────
check("newest first", log[0]["ts"] > log[-1]["ts"], True)
check("no history, no timeline", P.attribute_from_logbook([], [], {}), [])
check("null-safe", P.attribute_from_logbook(None, None, None), [])

# ── 4. driven end to end through fetch_log ─────────────────────────────────────
from datetime import datetime, timezone                              # noqa: E402


class Client:
    def __init__(self, logbook_ok=True):
        self.paths, self.ok = [], logbook_ok

    def _req(self, m, path):
        self.paths.append(path)
        if path.startswith("/api/history/"):
            return [[{"entity_id": "media_player.bedroom_tv", "state": s,
                      "last_changed": datetime.fromtimestamp(ts, timezone.utc).isoformat()}
                     for ts, s in ((T, "off"), (T + 100.123, "on"), (T + 1060.5, "off"))]]
        if path.startswith("/api/logbook/"):
            if not self.ok:
                raise OSError("logbook unavailable")
            return rows[:2]
        return []


ws = lambda t, **k: [{"id": "f7214f", "name": "Supervisor", "system_generated": True}] if t == "config/auth/list" else []
c = Client()
out = P.fetch_log(c, lambda: {}, "media_player.bedroom_tv", 48, ws_read=ws)
check("fetch_log: history for the timeline, the logbook for the causes, the accounts for names",
      [(e["to"], e["by"]) for e in out["log"]], [("off", "device"), ("on", "app")])
check("…the logbook is asked for this device only, with an end time", [p for p in c.paths if p.startswith("/api/logbook/")
                                                                       and "entity=media_player.bedroom_tv" in p and "end_time=" in p] != [], True)
check("…and no room script is fetched any more (the 120 s window is gone)", any("script.proos_" in p for p in c.paths), False)
out = P.fetch_log(Client(logbook_ok=False), lambda: {}, "media_player.bedroom_tv", 48, ws_read=ws)
check("an unreadable logbook makes every cause unknown — not 'external' (and says causes were not read)",
      ([e["by"] for e in out["log"]], out["causes_read"]), (["unknown", "unknown"], False))
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proos", "powerlog.py")).read()
check("(SOURCE) the window is gone from the module", ("def attribute(" in src, "window" in src.split('"""', 2)[2].split("# 1324:")[0]),
      (False, False))

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
