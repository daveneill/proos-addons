"""
Proof-run bench — run: python3 tests/proofrun_bench.py

The room proof: fire every stored activity and MEASURE that control and the
verdict both land. This is the missing half of Commissioning Flow Stage 4
Verify ("green means reachable + scripted + watched, proven, not assumed" —
project.verify checks reachability and script presence but never fires), and
the remaining piece of matrix #7 ("the Stage-4 test-fire"). It is also the
pitch's close, made a button: commission a room, prove it, watch the verdict
follow.

THE RULES BEING PINNED
----------------------
1. Success is the VERDICT, not the command. A watch_source step passes only
   when the room's verdict reaches watch_* AND its `source` attribute names
   the fired source; watch_tv passes on state watch_tv; tv_off on state off.
   No duplication of the engine's key vocabulary — the source attribute IS
   the contract.
2. A failed step never aborts the run (fault isolation: the report must show
   WHICH activities fail, not just the first), and the run ALWAYS ends by
   firing the off step — a proof run never leaves the room blaring.
3. Seconds-to-verdict is recorded per step: this is the performance number
   ("2-10 seconds good path" per What It Cannot Do §2) made measurable per
   room, per activity.
4. The engine is pure — fire/read/clock injected — so it benches offline and
   the server wires the real ones.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.proofrun import run_proof                          # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


ATV = "media_player.bedroom_apple_tv"
SHD = "media_player.bedroom_shield"

PLAN = [
    {"script": "script.proos_bedroom_watch_apple_tv", "alias": "Watch Apple TV",
     "kind": "watch_source", "source": ATV},
    {"script": "script.proos_bedroom_watch_shield", "alias": "Watch Shield",
     "kind": "watch_source", "source": SHD},
    {"script": "script.proos_bedroom_watch_tv", "alias": "Watch TV",
     "kind": "watch_tv"},
    {"script": "script.proos_bedroom_tv_off", "alias": "TV Off",
     "kind": "tv_off"},
]


class Fake:
    """Injected world: scripted verdict timeline per fired script."""

    def __init__(self, outcomes, latency=4.0):
        self.outcomes = outcomes          # script -> (state, source) after latency
        self.latency = latency
        self.fired = []
        self.t = 0.0
        self._pending = None

    def fire(self, script):
        self.fired.append(script)
        out = self.outcomes.get(script)
        self._pending = (self.t + self.latency, out) if out else None

    def read(self):
        # a matured outcome PERSISTS as the room's state (like the real
        # sensor); the resting state before anything matures is the previous
        # one, so a step with no outcome simply never converges.
        if self._pending and self.t >= self._pending[0]:
            self.cur = self._pending[1]
            self._pending = None
        st, src = getattr(self, "cur", ("off", None))
        return {"state": st, "source": src, "verified": True}

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ── 1. the happy path: every activity converges, seconds measured ──────────
w = Fake({
    "script.proos_bedroom_watch_apple_tv": ("watch_apple_tv", ATV),
    "script.proos_bedroom_watch_shield": ("watch_shield", SHD),
    "script.proos_bedroom_watch_tv": ("watch_tv", None),
    "script.proos_bedroom_tv_off": ("off", None),
})
res = run_proof(PLAN, w.fire, w.read, now=w.now, sleep=w.sleep,
                timeout_s=30, settle_s=2, poll_s=1)
check("all four steps pass", [r["ok"] for r in res["steps"]],
      [True, True, True, True])
check("seconds-to-verdict recorded and plausible (watch steps)",
      all(3 <= r["seconds"] <= 8 for r in res["steps"]
          if r["ok"] and r["kind"] != "tv_off"), True)
check("summary counts", (res["passed"], res["total"]), (4, 4))
check("every script actually fired", w.fired, [s["script"] for s in PLAN])
check("run ends with the off step", w.fired[-1].endswith("_tv_off"), True)

# ── 2. success is the verdict SOURCE, not just any watch state ─────────────
w = Fake({
    # shield script wrongly lands the room on the APPLE TV
    "script.proos_bedroom_watch_shield": ("watch_apple_tv", ATV),
    "script.proos_bedroom_tv_off": ("off", None),
})
res = run_proof([PLAN[1], PLAN[3]], w.fire, w.read, now=w.now, sleep=w.sleep,
                timeout_s=10, settle_s=1, poll_s=1)
check("a watch that lands on the WRONG source fails",
      res["steps"][0]["ok"], False)
check("  and the report names what it saw instead",
      res["steps"][0]["final"]["source"], ATV)

# ── 3. fault isolation + the room is never left on ─────────────────────────
w = Fake({
    # apple tv never converges at all; shield, watch_tv and off all work
    "script.proos_bedroom_watch_shield": ("watch_shield", SHD),
    "script.proos_bedroom_watch_tv": ("watch_tv", None),
    "script.proos_bedroom_tv_off": ("off", None),
})
res = run_proof(PLAN, w.fire, w.read, now=w.now, sleep=w.sleep,
                timeout_s=6, settle_s=1, poll_s=1)
check("a timed-out step fails without aborting the run",
      [r["ok"] for r in res["steps"]], [False, True, True, True])
check("  timeout recorded as the elapsed wait",
      res["steps"][0]["seconds"] >= 6, True)
check("  later steps still measured", res["steps"][1]["ok"], True)
check("the off step still fired after failures",
      w.fired[-1].endswith("_tv_off"), True)

# ── 4. tv_off always runs LAST whatever order the plan came in ─────────────
w = Fake({"script.proos_bedroom_tv_off": ("off", None),
          "script.proos_bedroom_watch_tv": ("watch_tv", None)})
res = run_proof([PLAN[3], PLAN[2]], w.fire, w.read, now=w.now, sleep=w.sleep,
                timeout_s=6, settle_s=1, poll_s=1)
check("tv_off is reordered to the end",
      w.fired[-1].endswith("_tv_off"), True)
check("  and the watch step still ran first",
      w.fired[0].endswith("_watch_tv"), True)

# ── 5. an empty plan is an empty report, not an error ──────────────────────
res = run_proof([], lambda s: None, lambda: {}, now=lambda: 0,
                sleep=lambda s: None, timeout_s=5, settle_s=1, poll_s=1)
check("empty plan -> empty report", (res["passed"], res["total"]), (0, 0))

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
