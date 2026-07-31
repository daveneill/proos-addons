"""
Room-art pictures bench — run: python3 tests/roomart_pictures_bench.py

MOTIVATING FAILURE (found by Dave after the 31 Jul factory reset)
-----------------------------------------------------------------
A room saved without a background LOOKED fine — but only because each surface
was inventing its own default: Pro previews Core's curated table, the
dashboard falls back to its OWN older hard-coded map (a different image), and
nothing ever WRITES the area picture — so Pro's "Generate art for 1 room
without a background" kept counting it. Three curated tables, zero writers.
And the Home background survived the reset because system areas are kept
verbatim, picture included.

THE RULES BEING PINNED
----------------------
1. ONE writer: at commit, every committed room whose HA area has no picture
   gets Core's curated background WRITTEN to the area picture. From then on
   every surface reads the same image from the same place (the area
   registry), and the generate-count is honestly zero.
2. An existing picture is NEVER overwritten — an installer's or homeowner's
   upload always wins (roomart's documented precedence).
3. Two rooms of the same kind get DIFFERENT variants, deterministically.
4. Uncommitted rooms and areas missing from the registry are untouched.
5. On factory reset, system areas survive but their PICTURES are stripped —
   shipped look after every reset, including Home.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.roomart import pictures_needed, variants           # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


def areas(**pics):
    out = []
    for aid, pic in pics.items():
        out.append({"area_id": aid, "name": aid.replace("_", " ").title(),
                    "picture": pic})
    return out


def project(*rooms):
    return {"areas": {r[0]: {"name": r[1], "area_id": r[0],
                             "committed": r[2]} for r in rooms}}


# ── 1. a committed room with no picture gets the curated background ────────
need = pictures_needed(areas(living_room=None),
                       project(("living_room", "Living Room", True)))
check("empty-picture committed room gets an assignment",
      list(need), ["living_room"])
check("  and it is the curated photo for its kind",
      need["living_room"], variants("Living Room")[0])

# ── 2. an existing picture is NEVER overwritten ────────────────────────────
need = pictures_needed(areas(living_room="/api/image/uploaded.jpg"),
                       project(("living_room", "Living Room", True)))
check("an uploaded picture is untouched", need, {})

# ── 3. uncommitted rooms are untouched ─────────────────────────────────────
need = pictures_needed(areas(living_room=None),
                       project(("living_room", "Living Room", False)))
check("an uncommitted room is untouched", need, {})

# ── 4. an area missing from the registry is skipped, never invented ────────
need = pictures_needed(areas(),
                       project(("ghost_room", "Ghost Room", True)))
check("a room with no registry area is skipped", need, {})

# ── 5. two rooms of the same kind get DIFFERENT variants ───────────────────
need = pictures_needed(areas(bedroom=None, ryans_room=None),
                       project(("bedroom", "Bedroom", True),
                               ("ryans_room", "Ryans Bedroom", True)))
check("both bedrooms get pictures", sorted(need), ["bedroom", "ryans_room"])
check("  and they differ", need["bedroom"] != need["ryans_room"], True)

# deterministic: same inputs, same handout
need2 = pictures_needed(areas(bedroom=None, ryans_room=None),
                        project(("bedroom", "Bedroom", True),
                                ("ryans_room", "Ryans Bedroom", True)))
check("  deterministically", need, need2)

# ── 6. a variant already used by an EXISTING area picture is not repeated ──
first = variants("Bedroom")[0]
need = pictures_needed(areas(bedroom=first, ryans_room=None),
                       project(("bedroom", "Bedroom", True),
                               ("ryans_room", "Ryans Bedroom", True)))
check("an in-use curated URL is not handed out again",
      need.get("ryans_room") not in (None, first), True)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
