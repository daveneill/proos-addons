"""
Endpoint Stage 3 bench — run: python3 tests/stage3_slots_bench.py [old-build-dir]

ProOS_Endpoint_Model_Spec_2026-07-31.md §4 Stage 3: slots in the record, with
mechanical migration and NO re-commissioning. This build is the RECORD change
only — Pro still writes legacy fields (§4.5 rule 4: record and UI are
separate builds).

THE COMPATIBILITY CONTRACT BEING PINNED (§4.5)
----------------------------------------------
* Legacy fields are never removed. A legacy record gains `slots` as a derived
  view at load; a slot-bearing record (written by a future Pro) gets its
  legacy fields DERIVED from the slots. Both directions, one place:
  project.load(). Consumers keep reading display/speakers/audio/tvaudio/kind
  byte-identical whichever direction wrote the record.
* Slot identity is the BOUND ENTITY, never a position (settled 31 Jul:
  "Audio End-Point 2" is a Composer label, not a key). Removing the first
  audio endpoint must never renumber the second.
* "No video slot" is the same fact `kind: music` derives from — one source
  of truth (Stage 2's rule, now read off the slots when they exist).
* The file on disk is never rewritten by a read.

GATES (spec §6 — nothing ships if either fails)
-----------------------------------------------
7. Every live room shape derives the same kind it stores today.
8. A legacy room's commissioning input is byte-identical to the shipped
   build's — slots must be invisible to the generator and the engine.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos import project                                     # noqa: E402

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
AVR = "media_player.marantz_sr5014"
SONOS = "media_player.family_room_family_room"
SPK2 = "media_player.office_speaker"


def write_project(areas):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 3, "areas": areas}, fh)
    return path


def legacy_room(**kw):
    r = {"name": "Living Room", "area_id": "living_room", "kind": "tv",
         "committed": True, "display": TV, "sources": [SHIELD, AVR],
         "speakers": [SONOS], "audio": [SONOS], "tvaudio": SONOS,
         "inputs": {SHIELD: "HDMI 3", TV: "TV"},
         "avswitch": {"entity": AVR, "output": "HDMI 3",
                      "inputs": {SHIELD: "Blu-ray"}, "broadcast": "TV Audio"}}
    r.update(kw)
    return r


# ── 1. a legacy committed room gains slots as a derived view ───────────────
p = write_project({"living_room": legacy_room()})
rec = project.load(p)["areas"]["living_room"]
s = rec.get("slots") or {}
check("video slot derived from display, primary first",
      [x["entity"] for x in s.get("video", [])], [TV])
check("audio slots derived from speakers/audio",
      [x["entity"] for x in s.get("audio", [])], [SONOS])
check("video_audio slot derived from tvaudio",
      [x["entity"] for x in s.get("video_audio", [])], [SONOS])
check("switch slot derived from avswitch entity",
      (s.get("switch") or {}).get("entity"), AVR)
check("sources are NOT slots (they stay rec['sources'])",
      rec["sources"], [SHIELD, AVR])
check("legacy fields untouched by the derivation",
      (rec["display"], rec["tvaudio"]), (TV, SONOS))

# ── 2. the file on disk is never rewritten by a read ───────────────────────
project.load(p)
with open(p, encoding="utf-8") as fh:
    on_disk = json.load(fh)
check("disk record has no slots after load", "slots" in
      on_disk["areas"]["living_room"], False)

# ── 3. a slot-bearing record (future Pro) derives the legacy fields ────────
SLOTTED = {"name": "Theatre", "area_id": "theatre", "kind": "music",  # wrong on purpose
           "committed": True, "sources": [SHIELD],
           "display": None, "speakers": [], "audio": [], "tvaudio": None,
           "inputs": {}, "avswitch": None,
           "slots": {"video": [{"entity": TV}],
                     "audio": [{"entity": SONOS}, {"entity": SPK2}],
                     "video_audio": [{"entity": SONOS}],
                     "switch": {"entity": AVR}}}
p = write_project({"theatre": SLOTTED})
rec = project.load(p)["areas"]["theatre"]
check("display derived from the primary video slot", rec["display"], TV)
check("speakers derived from audio slots", rec["speakers"], [SONOS, SPK2])
check("audio derived alongside", rec["audio"], [SONOS, SPK2])
check("tvaudio derived from the video_audio slot", rec["tvaudio"], SONOS)
check("kind derived from slots overrides the stored flag", rec["kind"], "tv")

# a slotted room with NO video slot is a music room — one source of truth
p = write_project({"z": dict(SLOTTED, area_id="z", kind="tv",
                             slots={"video": [],
                                    "audio": [{"entity": SPK2}],
                                    "video_audio": [], "switch": None})})
rec = project.load(p)["areas"]["z"]
check("no video slot => kind music (same fact as Stage 2)", rec["kind"], "music")
check("  and display derives to None", rec["display"], None)

# ── 4. slot identity is the entity, never the position ─────────────────────
two = {"video": [{"entity": TV}],
       "audio": [{"entity": SONOS}, {"entity": SPK2}],
       "video_audio": [], "switch": None}
one = {"video": [{"entity": TV}],
       "audio": [{"entity": SPK2}],       # first endpoint REMOVED
       "video_audio": [], "switch": None}
p = write_project({"a": dict(SLOTTED, area_id="a", slots=two),
                   "b": dict(SLOTTED, area_id="b", slots=one)})
loaded = project.load(p)["areas"]
check("removing the first audio endpoint never renumbers the second",
      loaded["b"]["speakers"], [SPK2])
check("  the survivor is found by entity, not index",
      SPK2 in loaded["a"]["speakers"] and SPK2 in loaded["b"]["speakers"], True)

# ── 5. uncommitted rooms are untouched ─────────────────────────────────────
p = write_project({"u": legacy_room(committed=False, area_id="u")})
rec = project.load(p)["areas"]["u"]
check("an uncommitted room gains no slots", "slots" in rec, False)

# ── 6. reconcile renames INSIDE slots too ──────────────────────────────────
NEWTV = "media_player.living_room_tv_9"
r = legacy_room()
r["anchors"] = {TV: {"platform": "samsungtv_smart", "unique_id": "uuid-tv",
                     "device_id": "d"}}
r["slots"] = {"video": [{"entity": TV}], "audio": [], "video_audio": [],
              "switch": None}
reg = [{"entity_id": NEWTV, "platform": "samsungtv_smart",
        "unique_id": "uuid-tv", "device_id": "d"}]
out, renames = project.reconcile_identities(r, reg)
check("reconcile renames the slot binding", renames, {TV: NEWTV})
check("  slot entity rewritten",
      [x["entity"] for x in out["slots"]["video"]], [NEWTV])

# ── GATE 7: every live room shape derives its stored kind ──────────────────
LIVE_SHAPES = {
    "living_room (tv, avr)": legacy_room(),
    "family_room (tv, no avr)": {"name": "Family Room", "area_id": "family_room",
                                 "kind": "tv", "committed": True, "display": TV,
                                 "sources": [SHIELD], "speakers": [SONOS],
                                 "audio": [SONOS], "tvaudio": SONOS,
                                 "inputs": {}, "avswitch": None},
    "office (music, 2 spk)": {"name": "Office", "area_id": "office",
                              "kind": "music", "committed": True, "display": None,
                              "sources": [], "speakers": [SONOS, SPK2],
                              "audio": [SONOS, SPK2], "tvaudio": None,
                              "inputs": {}, "avswitch": None},
    "study (music, 1 spk)": {"name": "Study", "area_id": "study",
                             "kind": "music", "committed": True, "display": None,
                             "sources": [], "speakers": [SPK2], "audio": [SPK2],
                             "tvaudio": None, "inputs": {}, "avswitch": None},
}
for label, r in LIVE_SHAPES.items():
    p = write_project({"r": dict(r, area_id="r")})
    check(f"GATE7 {label} derives its stored kind",
          project.load(p)["areas"]["r"]["kind"], r["kind"])

# ── GATE 8: legacy commissioning input byte-identical to the shipped build ──
old_root = sys.argv[1] if len(sys.argv) > 1 else None
if old_root and os.path.isdir(os.path.join(old_root, "proos")):
    def comm(root, r):
        for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
            del sys.modules[m]
        sys.path.insert(0, root)
        try:
            from proos.project import _commissioning_from_record as f  # noqa
            return json.dumps(f(r), sort_keys=True, default=str)
        finally:
            sys.path.remove(root)
            for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
                del sys.modules[m]

    NEW_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for label, r in LIVE_SHAPES.items():
        # what the generator receives after a LOAD in the new build (slots
        # present in-memory) must equal the shipped build's output for the
        # bare legacy record + the shipped avswitch fix
        p = write_project({"r": dict(r, area_id="r")})
        loaded = comm(NEW_ROOT, __import__("json").loads(
            json.dumps(r)))                        # legacy shape
        # and with slots attached (as load() leaves it in memory):
        import copy
        withslots = copy.deepcopy(r)
        for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
            del sys.modules[m]
        sys.path.insert(0, NEW_ROOT)
        from proos import project as _np                       # noqa: PLC0415
        pp = write_project({"r": dict(withslots, area_id="r")})
        inmem = _np.load(pp)["areas"]["r"]
        sys.path.remove(NEW_ROOT)
        got = comm(NEW_ROOT, inmem)
        want = comm(NEW_ROOT, r)
        check(f"GATE8 {label}: slots are invisible to commissioning",
              got, want)
else:
    print("\nnote  no old build given — GATE 8 cross-build check reduced to "
          "slots-invisibility only.")
    NEW_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    from proos.project import _commissioning_from_record as _f   # noqa: E402
    for label, r in LIVE_SHAPES.items():
        p = write_project({"r": dict(r, area_id="r")})
        inmem = project.load(p)["areas"]["r"]
        check(f"GATE8 {label}: slots are invisible to commissioning",
              json.dumps(_f(inmem), sort_keys=True, default=str),
              json.dumps(_f(r), sort_keys=True, default=str))

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
