"""
Room-kind derivation bench — run: python3 tests/kind_derivation_bench.py

Stage 2 of ProOS_Endpoint_Model_Spec_2026-07-31.md.

MOTIVATING FAILURE (live house, 1 Aug 2026 — measured, not inferred)
--------------------------------------------------------------------
The Office: two speakers committed (Sonos + HomePod), no display, Source
turned off on both, room committed. It published nothing.

Cause: `kind` is computed at SUGGEST time from CANDIDATES —

    kind = "tv" if (disp_c or src_c) else "music"

— and the HomePod is apple_tv-platform, so it is a source *candidate* whatever
the installer commits it as. The stored flag said "tv"; a tv room with no
display publishes nothing at all. Turning Source off in Pro cannot fix it,
because membership doesn't feed the flag — discovery does.

THE RULE BEING PINNED (endpoint spec §3 / §4.5)
-----------------------------------------------
`kind` is DERIVED from what is COMMITTED, at read time, in ONE place —
`project.load()`. A committed room with no display is a music room, whatever
the stored flag says. Nine modules read `rec["kind"]` (assist, credentials,
ctlbridge, healthmon, journal, musicstat, project, watcher, server); none of
them derives it, so none of them can drift.

"No video endpoint ⇒ music" is the same fact the endpoint model will later
read off the slots; this stage moves the truth source, Stage 3 moves the
storage.

GATES (spec §6, scenarios 4/5/7)
--------------------------------
- Every room shape on the live box derives the kind it currently stores.
- The derived record is what consumers see; the file on disk is untouched.
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


def write_project(areas):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 3, "areas": areas}, fh)
    return path


def room(*, kind, display=None, sources=(), speakers=(), audio=(),
         committed=True, name="X", area_id="x"):
    return {"name": name, "area_id": area_id, "kind": kind,
            "committed": committed, "display": display,
            "sources": list(sources), "speakers": list(speakers),
            "audio": list(audio), "inputs": {}, "avswitch": None}


# ── 1. THE OFFICE: stored 'tv', committed contents say music ───────────────
p = write_project({"Office": room(kind="tv", speakers=["media_player.s1",
                                                       "media_player.s2"],
                                  name="Office", area_id="office")})
rec = project.load(p)["areas"]["Office"]
check("stored kind=tv, no display, speakers only -> derived music",
      rec["kind"], "music")

# ── 2. every live room shape derives what it stores (gate, spec §6 #7) ─────
SHAPES = {
    # Bedroom / Family Room / Living Room: display + sources -> tv
    "tv room": room(kind="tv", display="media_player.tv",
                    sources=["media_player.atv"]),
    # Study / Ryan's Room: one speaker, stored music -> music
    "music room": room(kind="music", speakers=["media_player.spk"]),
    # a display-only room (display, no sources) is still a tv room
    "display only": room(kind="tv", display="media_player.tv"),
}
for label, r in SHAPES.items():
    p = write_project({"R": dict(r, name="R", area_id="r")})
    check(f"{label} derives its stored kind",
          project.load(p)["areas"]["R"]["kind"], r["kind"])

# ── 3. the reverse poison: stored music but a display was committed ────────
p = write_project({"R": room(kind="music", display="media_player.tv",
                             name="R", area_id="r")})
check("a committed display always makes a tv room",
      project.load(p)["areas"]["R"]["kind"], "tv")

# ── 4. sources-without-display does NOT make it a tv room ──────────────────
# The Office failure exactly: a source candidate (or even a committed source)
# with no display cannot produce watch activities -- the room's evidence is
# its speakers. kind follows the DISPLAY, the one thing a watch activity
# cannot exist without.
p = write_project({"R": room(kind="tv", sources=["media_player.hp"],
                             speakers=["media_player.hp"],
                             name="R", area_id="r")})
check("sources but no display -> music (a watch activity needs a display)",
      project.load(p)["areas"]["R"]["kind"], "music")

# ── 5. uncommitted rooms keep their stored flag ────────────────────────────
# Suggest-time kind is a UI hint over CANDIDATES and is allowed to say 'tv'
# for an empty room in a house full of TVs; deriving from empty membership
# would flip every suggestion to music before the installer adds anything.
p = write_project({"R": room(kind="tv", committed=False,
                             name="R", area_id="r")})
check("an uncommitted room keeps its suggest-time kind",
      project.load(p)["areas"]["R"]["kind"], "tv")

# ── 6. derivation happens at load; the FILE is untouched ───────────────────
p = write_project({"Office": room(kind="tv",
                                  speakers=["media_player.s1"],
                                  name="Office", area_id="office")})
project.load(p)
with open(p, encoding="utf-8") as fh:
    on_disk = json.load(fh)
check("the stored file is not rewritten by load",
      on_disk["areas"]["Office"]["kind"], "tv")

# ── 7. malformed records cannot break load ─────────────────────────────────
p = write_project({"A": None, "B": {"committed": True},
                   "C": room(kind="music", speakers=["media_player.x"],
                             name="C", area_id="c")})
out = project.load(p)
check("None record survives load", "A" in out["areas"], True)
check("bare record survives load", "B" in out["areas"], True)
check("and the real room still derives", out["areas"]["C"]["kind"], "music")

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
