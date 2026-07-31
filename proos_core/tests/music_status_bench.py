"""
Music-room status bench — run: python3 tests/music_status_bench.py

MOTIVATING FAILURE (live house, 31 Jul 2026 — measured after a clean factory
reset and re-commission, not inferred)
---------------------------------------------------------------------------
Three committed rooms report nothing at all:

    area          contents                              sensor.proos_activity_*
    family_room   display + sources + audio             present
    bedroom       display + sources + audio             present
    living_room   display + sources + audio             present
    ryans_room    1 media_player + sensors + switches   ABSENT
    office        3 media_players + sensors + switches  ABSENT
    study         1 media_player + sensors + switches   ABSENT

Cause: ctlbridge._sweep_rooms() does `if not acts: continue`. A music room has
no AV activities, so it falls out of the sweep before anything is published.
Matrix #4 records that music-only rooms are VALID, and `kind` has been on the
area record since 1.0.37 — but What It Cannot Do §7 states the limit plainly:
"audio-only listening verdicts [are not] first-class activities."

Committed means committed. A room that can be committed must be able to report.

THE RULES BEING PINNED (ProOS_Room_Status_Spec_2026-07-31.md §2, §5)
--------------------------------------------------------------------
1. One contract, two producers. A music room publishes the SAME sensor with the
   SAME attribute shape as an AV room. Consumers never branch on room type.
2. The music producer never runs the verdict ladder. Its evidence is direct.
3. States are `playing` / `idle` / `off`. `idle` covers paused AND stopped.
4. `off` is a PRESENTATION state — the room is cleared from the live surface.
   It is never "the speaker is powered down": a mains-powered speaker is never
   off, so reading power state here would be wrong on every brand.
5. A speaker joined to a group playing from another room reports `playing`,
   attributed "Playing (grouped to <origin>)", and takes its now-playing from
   the GROUP COORDINATOR, not from the joined speaker.
6. The AV path is untouched. Regression is the gate, not a nicety.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.musicstat import decide_music                      # noqa: E402

OFFICE = "media_player.office_office"
OFFICE_2 = "media_player.office_speaker"
STUDY = "media_player.study_study"
RYAN = "media_player.ryans_room_ryans_room"

# entity -> room name, as the committed project knows it
AREA_OF = {OFFICE: "Office", OFFICE_2: "Office", STUDY: "Study", RYAN: "Ryan's Room"}

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


def sp(state, *, title=None, source=None, group=None, volume=0.2):
    attrs = {"volume_level": volume}
    if title:
        attrs["media_title"] = title
    if source:
        attrs["source"] = source
    if group is not None:
        attrs["group_members"] = list(group)
    return {"state": state, "attributes": attrs}


def rec(speakers, name="Study", area_id="study", kind="music", committed=True):
    return {"name": name, "area_id": area_id, "kind": kind,
            "committed": committed, "speakers": list(speakers),
            "audio": [], "display": None, "sources": []}


def area_of(eid):
    return AREA_OF.get(eid)


# ── 1. the motivating failure: a committed music room reports SOMETHING ──
snap = {STUDY: sp("idle")}
d = decide_music(rec([STUDY]), snap, area_of)
check("a committed music room produces a verdict at all", d is not None, True)
check("  silent room is idle, not off", d["state"], "idle")

# ── 2. playing ──
snap = {STUDY: sp("playing", title="Black Star", source="Spotify")}
d = decide_music(rec([STUDY]), snap, area_of)
check("a playing room is playing", d["state"], "playing")
check("  label reads plainly", d["label"], "Playing")
check("  now-playing carried", d["media_title"], "Black Star")
check("  source carried", d["source"], STUDY)

# ── 3. idle covers paused AND stopped (spec §2) ──
for st in ("paused", "idle"):
    d = decide_music(rec([STUDY]), {STUDY: sp(st)}, area_of)
    check(f"'{st}' maps to idle", d["state"], "idle")

# ── 4. `off` is PRESENTATION, not power ────────────────────────────────────
# A mains-powered speaker reporting 'off'/'standby'/'unavailable' means the
# room is not live. It must NOT mean "someone unplugged it".
for st in ("off", "standby", "unavailable", "unknown"):
    d = decide_music(rec([STUDY]), {STUDY: sp(st)}, area_of)
    check(f"speaker '{st}' clears the room from live", d["state"], "off")
check("cleared room says so in the label",
      decide_music(rec([STUDY]), {STUDY: sp("off")}, area_of)["label"], "Off")

# ── 5. any speaker playing makes the room playing ──────────────────────────
snap = {OFFICE: sp("idle"), OFFICE_2: sp("playing", title="Blue Train")}
d = decide_music(rec([OFFICE, OFFICE_2], name="Office", area_id="office"),
                 snap, area_of)
check("one playing speaker makes the room playing", d["state"], "playing")
check("  and the playing one is the source", d["source"], OFFICE_2)

# ── 6. GROUPED ACROSS ROOMS (spec §2) ──────────────────────────────────────
# Study joined to a group coordinated from the Office. HA lists the coordinator
# FIRST in group_members. The Study is genuinely playing, but is not the origin.
grp = [OFFICE, STUDY]
snap = {OFFICE: sp("playing", title="Kind of Blue", source="Spotify", group=grp),
        STUDY: sp("playing", group=grp)}
d = decide_music(rec([STUDY]), snap, area_of)
check("a joined room is playing", d["state"], "playing")
check("  attributed to the origin room", d["label"], "Playing (grouped to Office)")
check("  grouped_to names the origin", d["grouped_to"], "Office")
check("  now-playing comes from the COORDINATOR",
      d["media_title"], "Kind of Blue")
check("  coordinator recorded", d["coordinator"], OFFICE)

# the coordinator's OWN room is not 'grouped to' itself
d = decide_music(rec([OFFICE], name="Office", area_id="office"), snap, area_of)
check("the origin room is not grouped to itself", d["grouped_to"], None)
check("  and reads plainly", d["label"], "Playing")

# a group of one is not a group
solo = [STUDY]
d = decide_music(rec([STUDY]), {STUDY: sp("playing", title="Solo", group=solo)},
                 area_of)
check("a one-member group is not a group", d["grouped_to"], None)

# grouped but the coordinator is in the SAME room -> not cross-room
same = [OFFICE, OFFICE_2]
snap = {OFFICE: sp("playing", title="Same Room", group=same),
        OFFICE_2: sp("playing", group=same)}
d = decide_music(rec([OFFICE, OFFICE_2], name="Office", area_id="office"),
                 snap, area_of)
check("grouping within one room is not 'grouped to'", d["grouped_to"], None)

# ── 7. never invent: a room whose speakers aren't in the snapshot ───────────
d = decide_music(rec([RYAN], name="Ryan's Room", area_id="ryans_room"), {},
                 area_of)
check("unknown speakers produce no verdict, not a fake one", d, None)

# ── 8. a room with no speakers committed produces nothing ──────────────────
check("a music room with no members produces nothing",
      decide_music(rec([]), {STUDY: sp("playing")}, area_of), None)

# ── 9. ATTRIBUTE SHAPE parity with the AV producer (spec §2 rule 1) ────────
# Consumers (Pro, dashboard, Assist) must never branch on room type, so the
# keys an AV verdict always carries must be present here too.
d = decide_music(rec([STUDY]), {STUDY: sp("playing", title="X")}, area_of)
for k in ("state", "label", "activity_key", "area", "verified", "held",
          "icon", "source", "audio_entity", "kind"):
    check(f"attribute '{k}' present for parity", k in d, True)
check("music evidence is direct, so verified is true", d["verified"], True)
check("music rooms are never 'held'", d["held"], False)
check("kind marks the producer", d["kind"], "music")
check("area carries the human name", d["area"], "Study")

d_off = decide_music(rec([STUDY]), {STUDY: sp("off")}, area_of)
check("activity_key is 'off' when cleared", d_off["activity_key"], "off")
check("activity_key is 'listen' when live", d["activity_key"], "listen")

# ── 10. a TV room is NOT this producer's business ──────────────────────────
check("a tv-kind room is refused outright",
      decide_music(rec([STUDY], kind="tv"), {STUDY: sp("playing")}, area_of),
      None)
check("an uncommitted room is refused outright",
      decide_music(rec([STUDY], committed=False), {STUDY: sp("playing")},
                   area_of),
      None)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
