"""
AV-switch routing bench — run: python3 tests/avswitch_route_bench.py

MOTIVATING FAILURE (live house, 31 Jul 2026 — measured on a freshly committed
room, not inferred)
---------------------------------------------------------------------------
Living Room signal path, as wired and as committed in Pro:

    Shield    -> Marantz "Blu-ray"
    Apple TV  -> Marantz "Apple TV"
    Marantz   -> Living Room TV "HDMI 3"        (Output -> display input)

Generated scripts:

    ProOS · Living Room · Watch Shield    ->  Select HDMI 3 ✓ then AVR Blu-ray ✓
    ProOS · Living Room · Watch Marantz   ->  NO display hop at all ✗

Press Watch Marantz and the TV powers on, the AVR powers on, and the screen
stays on whatever input it was already showing. Commissioning Flow matrix #7,
"one-touch lands on a black input".

Cause, in `project._routes_for` / `_commissioning_from_record`:

    for e in (sw.get("inputs") or {}):        # sources plugged INTO the switch
        if e and e != rec.get("display"):
            comm["routes"][e] = {"input": out_inp}

The AV switch is not plugged into itself, so it never appears in
`sw["inputs"]`, so it never gets a display route. The generator then does:

    route = routes.get(src.entity) or {}
    if input_name and ...: seq.append(_input_step(input_name))

No route entry => no step, silently, with no warning.

NOT a regression: generator.py is byte-identical (md5 dc7807daaf, 954 lines)
across 1.0.220 -> 1.0.256. This is a model gap, not a broken build.

THE RULE BEING PINNED (ProOS_Signal_Graph_Spec_2026-07-31.md §3)
----------------------------------------------------------------
The display input is a property of the signal path. Anything downstream of the
AV switch looks at the switch's committed output — INCLUDING the switch itself
when it is also committed as a source (its own tuner / streaming). The switch
has no input to select for itself; that is the only difference, and it falls
out of the model rather than needing a special case.

A source wired DIRECT to a display keeps its own display input untouched —
per-source, not per-room. That case is pinned here so the fix cannot quietly
turn "AV switch present" into "everything routes through it".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.project import _commissioning_from_record          # noqa: E402

TV = "media_player.living_room_tv"
SHIELD = "media_player.living_room_shield_2"
ATV = "media_player.living_room_apple_tv"
AVR = "media_player.marantz_sr5014"

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"\n        wanted {want!r} got {got!r}")


def rec(*, inputs=None, avswitch=None, display=TV):
    return {"name": "Living Room", "area_id": "living_room", "committed": True,
            "display": display, "sources": [SHIELD, ATV, AVR],
            "inputs": dict(inputs or {}), "avswitch": avswitch}


def routes_of(r):
    return _commissioning_from_record(r).get("routes") or {}


def inp(r, eid):
    return (routes_of(r).get(eid) or {}).get("input")


# ── the live Living Room, exactly as committed ─────────────────────────────
LIVE = rec(inputs={TV: "TV"},
           avswitch={"entity": AVR, "output": "HDMI 3",
                     "inputs": {SHIELD: "Blu-ray", ATV: "Apple TV"},
                     "broadcast": "TV Audio"})

# 1 — what already worked, and must keep working
check("a source through the switch looks at the switch's output",
      inp(LIVE, SHIELD), "HDMI 3")
check("  every switched source, not just the first", inp(LIVE, ATV), "HDMI 3")

# 2 — THE DEFECT
check("the switch committed as a source ALSO looks at its own output",
      inp(LIVE, AVR), "HDMI 3")

# 3 — the display is DELIBERATELY absent from routes, and must stay absent.
#     The TV's own tuner input does not travel here: project.py builds it as
#     `display_input` on the cluster (`inputs[display]` when the display is
#     committed as a source) and the generator reads `cluster.display_input`
#     for Watch TV. `_routes_for` filters `e != rec['display']` on purpose.
#     Pinned so the fix below cannot start writing a switch-output route over
#     the display's own tuner.
check("the display never appears in routes (its tuner travels separately)",
      inp(LIVE, TV), None)

# 4 — no output committed => nothing invented for anyone
NOOUT = rec(inputs={TV: "TV"},
            avswitch={"entity": AVR, "output": "",
                      "inputs": {SHIELD: "Blu-ray"}})
check("no committed output invents no display hop", inp(NOOUT, SHIELD), None)
check("  not for the switch either", inp(NOOUT, AVR), None)

# 5 — a room with NO AV switch is untouched (per-source, not per-room)
DIRECT = rec(inputs={TV: "TV", SHIELD: "HDMI 4", ATV: "HDMI 2"}, avswitch=None)
check("no switch: each source keeps its own display input",
      inp(DIRECT, SHIELD), "HDMI 4")
check("  and the other one too", inp(DIRECT, ATV), "HDMI 2")
check("  and the display stays out of routes", inp(DIRECT, TV), None)

# 6 — MIXED ROOM: a source wired direct to the display keeps its own input
#     even though the room has an AV switch. This is the case the current
#     commissioning UI cannot express; pinned so the fix can never assume
#     "switch present => everything routes through it".
MIXED = rec(inputs={TV: "TV", ATV: "HDMI 2"},
            avswitch={"entity": AVR, "output": "HDMI 3",
                      "inputs": {SHIELD: "Blu-ray"}})
check("mixed room: the switched source uses the switch output",
      inp(MIXED, SHIELD), "HDMI 3")
check("mixed room: the direct source keeps its OWN display input",
      inp(MIXED, ATV), "HDMI 2")
check("mixed room: the switch still routes to its own output",
      inp(MIXED, AVR), "HDMI 3")

# 7 — an explicit per-source route always beats the derived one
EXPLICIT = rec(inputs={TV: "TV", AVR: "HDMI 1"},
               avswitch={"entity": AVR, "output": "HDMI 3",
                         "inputs": {SHIELD: "Blu-ray"}})
check("an explicitly committed route for the switch is not overwritten",
      inp(EXPLICIT, AVR), "HDMI 1")

# 8 — the switch is never routed as if it were the display
ASDISP = rec(display=AVR, inputs={},
             avswitch={"entity": AVR, "output": "HDMI 3", "inputs": {}})
check("a switch that IS the display gets no self-route", inp(ASDISP, AVR), None)

# 9 — the audio plan is unchanged by any of this
aud = _commissioning_from_record(LIVE).get("audio") or {}
check("audio plan still names the switch", aud.get("entity"), AVR)
check("  with its per-source inputs", aud.get("inputs"),
      {SHIELD: "Blu-ray", ATV: "Apple TV"})
check("  and its broadcast input", aud.get("broadcast"), "TV Audio")
check("  and still powers it", aud.get("power"), True)

# ── REGRESSION GATE ────────────────────────────────────────────────────────
# Every room WITHOUT an AV switch must produce commissioning input identical to
# the shipped build, and the switched room must differ by exactly one added
# route. Pass the old build's package parent as argv[1]; skipped without it.
_old_root = sys.argv[1] if len(sys.argv) > 1 else None
if _old_root and os.path.isdir(os.path.join(_old_root, "proos")):
    import json

    def _old_comm(r):
        for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
            del sys.modules[m]
        sys.path.insert(0, _old_root)
        try:
            from proos.project import _commissioning_from_record as f  # noqa
            return json.dumps(f(r), sort_keys=True, default=str)
        finally:
            sys.path.remove(_old_root)
            for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
                del sys.modules[m]

    def _new_comm(r):
        for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
            del sys.modules[m]
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from proos.project import _commissioning_from_record as f      # noqa
        return json.dumps(f(r), sort_keys=True, default=str)

    # Family-Room / Bedroom shape: sources direct to the display, no switch.
    check("REGRESSION: a room with no AV switch is byte-identical",
          _new_comm(DIRECT), _old_comm(DIRECT))
    check("REGRESSION: a switch with no committed output is byte-identical",
          _new_comm(NOOUT), _old_comm(NOOUT))
    check("REGRESSION: an explicit switch route is byte-identical",
          _new_comm(EXPLICIT), _old_comm(EXPLICIT))

    old_live = json.loads(_old_comm(LIVE))
    new_live = json.loads(_new_comm(LIVE))
    added = set(new_live["routes"]) - set(old_live["routes"])
    check("the switched room gains exactly one route", added, {AVR})
    check("  and nothing else changed",
          {k: v for k, v in new_live.items() if k != "routes"},
          {k: v for k, v in old_live.items() if k != "routes"})
    check("  and no existing route was altered",
          {k: v for k, v in new_live["routes"].items() if k != AVR},
          old_live["routes"])
else:
    print("\nnote  no old build given — regression gate SKIPPED."
          "\n      run: python3 tests/avswitch_route_bench.py <old-build-dir>")

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
