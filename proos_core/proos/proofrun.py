"""
ProOS Core — the room proof (fire every activity, measure control AND state).

The missing half of Commissioning Flow Stage 4 Verify: project.verify checks
reachability and script presence but never fires anything, so "green" has
never meant "the room demonstrably works". This runs the room end to end —
every stored watch activity, then off — and judges each step by the VERDICT,
not the command: control worked AND awareness saw it, with seconds-to-verdict
recorded per step. That is 'faultless control and state' made a measurable,
repeatable test, and it is the pitch's close as a button (commission a room,
prove it, watch the verdict follow).

Rules (benched in tests/proofrun_bench.py):
  * Success is the verdict. watch_source passes only when the room reaches
    watch_* AND the verdict's `source` attribute names the fired source — no
    duplication of the engine's key vocabulary, the source attribute is the
    contract. watch_tv passes on state 'watch_tv'; tv_off on 'off'.
  * A failed step never aborts the run (the report must show WHICH activities
    fail), and the run ALWAYS ends with the off step — a proof never leaves
    the room blaring.
  * Pure engine: fire/read/clock injected. The server wires the real ones and
    runs it in a background thread (it drives real devices for minutes).
"""
from __future__ import annotations

import time as _time


def run_proof(plan, fire, read, *, now=_time.monotonic, sleep=_time.sleep,
              timeout_s=45, settle_s=4, poll_s=1.0, progress=None) -> dict:
    """Execute a room proof. Pure given injected fire/read/clock.

    plan  -- [{script, alias, kind: watch_source|watch_tv|tv_off,
               source?: entity_id}]
    fire  -- callable(script_entity_id)
    read  -- callable() -> {state, source, verified} for the room's verdict
    """
    # tv_off always runs LAST whatever order the plan arrived in: it is both
    # the off-state test and the guarantee the proof leaves the room off.
    steps = [s for s in (plan or []) if s.get("kind") != "tv_off"] + \
            [s for s in (plan or []) if s.get("kind") == "tv_off"]

    results = []
    for step in steps:
        if progress:
            try:
                progress(step)
            except Exception:
                pass
        started = now()
        try:
            fire(step["script"])
        except Exception as e:                                # noqa: BLE001
            results.append({"script": step["script"],
                            "alias": step.get("alias"),
                            "kind": step.get("kind"), "ok": False,
                            "seconds": 0.0, "final": {"error": str(e)}})
            continue

        def _passes(v):
            st = (v or {}).get("state") or ""
            if step["kind"] == "watch_source":
                return st.startswith("watch_") and st != "watch_tv" \
                    and (v or {}).get("source") == step.get("source")
            if step["kind"] == "watch_tv":
                return st == "watch_tv"
            return st == "off"                               # tv_off

        ok, final = False, {}
        while True:
            final = read() or {}
            if _passes(final):
                ok = True
                break
            if now() - started >= timeout_s:
                break
            sleep(poll_s)

        results.append({"script": step["script"], "alias": step.get("alias"),
                        "kind": step.get("kind"), "ok": ok,
                        "seconds": round(now() - started, 1),
                        "final": {"state": final.get("state"),
                                  "source": final.get("source"),
                                  "verified": final.get("verified")}})
        sleep(settle_s)          # let the room breathe between activities

    return {"steps": results,
            "passed": sum(1 for r in results if r["ok"]),
            "total": len(results)}
