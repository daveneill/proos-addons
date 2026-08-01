"""
Sibling-rest bench — run: python3 tests/sibling_rest_bench.py

MOTIVATING FAILURE (live house, 1 Aug 2026 — recorder history, not inference)
-----------------------------------------------------------------------------
The Living Room Marantz was power-cycled BY PROOS during every watch:

    09:01:26 on · 09:02:00 off · 09:02:02 on · 09:02:19 off · 09:02:22 on
    · 09:02:40 off        (102 transitions in the recorder window)

Cause, visible in the generated `Watch Shield`: the audio steps power the AVR
on and select Blu-ray — then the SIBLING-REST step turns the AVR off, because
the Marantz is ALSO committed as a source ("watching this source means the
others go to rest"). One script switches the amp on and kills it. Dashboard
symptom: the media card flaps between the volume-slider (combined) card and
the plain card as the AVR bounces; the verdict holds/sticks on contradictory
evidence.

THE RULE BEING PINNED (same class as the 1.0.258 display-hop fix)
-----------------------------------------------------------------
The room's AUDIO DEVICE is infrastructure, never a sibling. A watch activity
must not rest the entity carrying the room's audio — resting it kills the
active source's sound. It still powers off in TV Off (that is the audio plan's
job, not sibling logic). Watching the switch itself still rests the real
sibling sources.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.generator import build_room_scripts                # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


TV = "media_player.living_room_tv"
SHIELD = "media_player.living_room_shield_2"
ATV = "media_player.living_room_apple_tv"
AVR = "media_player.marantz_sr5014"


class FakeClient:
    def render_template(self, *_a, **_k):
        return "[]"

    def _req(self, *_a, **_k):
        return {}


def dev(entity, integration, dc=None):
    d = types.SimpleNamespace()
    d.entity, d.integration, d.device_class = entity, integration, dc
    d.name = entity
    return d


def cluster(sources):
    c = types.SimpleNamespace()
    c.area, c.area_id = "Living Room", "living_room"
    c.display = dev(TV, "samsungtv_smart", "tv")
    c.sources = sources
    c.audio = [dev(AVR, "denonavr", "receiver")]
    c.display_is_source = True
    c.display_input = "TV"
    c.label_for = lambda d: {SHIELD: "Shield", ATV: "Apple TV",
                             AVR: "Marantz SR5014", TV: "TV"}.get(d.entity,
                                                                  d.entity)
    return c


COMM = {
    "routes": {SHIELD: {"input": "HDMI 3"}, ATV: {"input": "HDMI 3"},
               AVR: {"input": "HDMI 3"}},
    "audio": {"mode": "avr", "entity": AVR,
              "inputs": {SHIELD: "Blu-ray", ATV: "Apple TV"},
              "broadcast": "TV Audio", "power": True},
    "off_state": "full",
}


def steps_of(scripts, oid):
    return (scripts.get(oid) or {}).get("sequence") or []


def rests(steps, entity):
    """turn_off steps (media_player or remote) targeting the entity."""
    out = []
    for s in steps:
        cand = [s] + list(s.get("then") or [])
        for x in cand:
            act = x.get("action") or ""
            tgt = ((x.get("target") or {}).get("entity_id")) or ""
            if act.endswith("turn_off") and entity in str(tgt):
                out.append(x)
    return out


scripts = build_room_scripts(FakeClient(),
                             cluster([dev(SHIELD, "androidtv_remote", "tv"),
                                      dev(ATV, "apple_tv"),
                                      dev(AVR, "denonavr", "receiver")]),
                             COMM)

ws = steps_of(scripts, "proos_living_room_watch_shield")
check("watch_shield exists", len(ws) > 0, True)

# ── THE DEFECT ─────────────────────────────────────────────────────────────
check("Watch Shield NEVER rests the room's audio device (the AVR)",
      rests(ws, AVR), [])
check("  but still powers the AVR ON for audio",
      any((s.get("action") or "").endswith("turn_on")
          and AVR in str((s.get("target") or {}).get("entity_id") or "")
          for s in ws), True)
check("  and still rests the true sibling (Apple TV)",
      len(rests(ws, "living_room_apple_tv")) > 0, True)

wa = steps_of(scripts, "proos_living_room_watch_apple_tv")
check("Watch Apple TV never rests the AVR either", rests(wa, AVR), [])

wm = steps_of(scripts, "proos_living_room_watch_marantz_sr5014")
check("Watch Marantz still rests the Shield",
      len(rests(wm, "shield")) > 0, True)
check("  and never rests itself", rests(wm, AVR), [])

# ── TV Off is the audio plan's job and MUST still power the AVR off ────────
off = steps_of(scripts, "proos_living_room_tv_off")
check("TV Off still powers the AVR off", len(rests(off, AVR)) > 0, True)

# ── REGRESSION: a room whose AVR is NOT a source is untouched ──────────────
scripts2 = build_room_scripts(FakeClient(),
                              cluster([dev(SHIELD, "androidtv_remote", "tv"),
                                       dev(ATV, "apple_tv")]),
                              COMM)
ws2 = steps_of(scripts2, "proos_living_room_watch_shield")
check("no-AVR-source room: watch still rests the sibling",
      len(rests(ws2, "living_room_apple_tv")) > 0, True)
check("  and never touches the audio device with a rest",
      rests(ws2, AVR), [])

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
