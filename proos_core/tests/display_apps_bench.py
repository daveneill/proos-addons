"""
Published source-list memory bench — run: python3 tests/display_apps_bench.py

MOTIVATING FAILURE (live house, 30 Jul 2026 — measured, not inferred)
---------------------------------------------------------------------
Every display and every Apple TV published a full source_list earlier in the
day and NONE of them did by late afternoon:

    media_player.living_room_tv_2          11:38  34 entries  ->  16:20   5 inputs
    media_player.bedroom_tv_2              12:57  29 entries  ->  16:22   5 inputs
    media_player.family_room_..._apple_tv  14:29  39 apps     ->  16:21   ABSENT

The recorder confirms `source_list` is missing from most updates entirely — it
flickers in and out as the vendor integrations refresh. A Frame resting in Art
Mode is one trigger, not the cause. Whether the remote showed apps was down to
which update it happened to catch.

Nothing in ProOS causes that and nothing in ProOS can stop it, so the list has
to be remembered or the room is a coin toss.

THE RULES BEING PINNED
----------------------
1. Store the FULL published list VERBATIM — inputs and apps, device's own order.
   Dave, 30 Jul: "It had it all before inputs and apps so needs to be that again."
   Replaying anything less would change how the remote looks.
2. Never invent. A device we have never heard from gets nothing.
3. A list of nothing but inputs must never overwrite a good one.
4. A fresh publish always wins — an app genuinely removed disappears.
5. Memory never leaks between devices.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="proos_disp_apps_")
os.environ["PROOS_DATA_DIR"] = _TMP          # before appctl resolves any path

from proos import appctl                      # noqa: E402

TV = "media_player.family_room_tv_2"
OTHER = "media_player.study_tv"
# Real payloads read out of Dave's Home Assistant on 30 Jul 2026.
FULL = ["HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "TV", "Internet", "Foxtel",
        "Apple TV", "Netflix", "SmartThings", "Kayo Sports", "Disney+",
        "Prime Video", "7plus", "9Now", "10", "e-Manual", "YouTube"]
INPUTS_ONLY = ["HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "TV"]

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name)


class FakeClient:
    def __init__(self):
        self.st = {}

    def set(self, eid, source_list, state="on"):
        attrs = {"friendly_name": "Family Room TV"}
        if source_list is not None:
            attrs["source_list"] = list(source_list)
        self.st[eid] = {"state": state, "attributes": attrs}


class FakeProject:
    """appctl takes the project MODULE and calls .load() on it, not a dict."""

    def __init__(self, areas):
        self._areas = areas

    def load(self):
        return {"areas": self._areas}


client = FakeClient()
appctl._state = lambda c, eid: client.st.get(eid, {})     # noqa: SLF001
appctl._platforms = lambda c: {}                          # noqa: SLF001

PROJECT = FakeProject({"family_room": {"committed": True, "area_id": "family_room",
                                       "name": "Family Room", "display": TV,
                                       "sources": [], "inputs": {}}})


def room():
    c = appctl.candidates(client, PROJECT, "family_room")
    return c[0] if c else {}


# ── 1. a device we have never heard from gets NOTHING invented ──
client.set(TV, INPUTS_ONLY)
r = room()
check("unknown device, inputs only -> apps empty", r.get("apps"), [])
check("  full list is just what it published", r.get("apps_full"), INPUTS_ONLY)
check("  not flagged as remembered", r.get("apps_remembered"), False)

# ── 2. the device publishes properly: live wins, and it is recorded ──
client.set(TV, FULL)
r = room()
check("live list wins when it carries apps", r.get("apps_full"), FULL)
check("  served live, not from memory", r.get("apps_remembered"), False)
check("  apps view has inputs filtered", "HDMI 1" in (r.get("apps") or []), False)
check("  apps view keeps real apps", "Netflix" in (r.get("apps") or []), True)

# ── 3. the attribute vanishes entirely -> replay VERBATIM, inputs and all ──
client.set(TV, None, state="off")
r = room()
check("attribute absent -> full list replayed verbatim", r.get("apps_full"), FULL)
check("  flagged as remembered", r.get("apps_remembered"), True)

# ── 4. inputs-only (Art Mode) -> replay, and memory is NOT overwritten ──
client.set(TV, INPUTS_ONLY)
r = room()
check("inputs-only -> full list replayed verbatim", r.get("apps_full"), FULL)
check("  an inputs-only list never overwrites memory",
      appctl._remembered_list(TV), FULL)

# ── 5. a fresh publish always wins (an app was uninstalled) ──
SHORTER = ["HDMI 1", "TV", "Netflix", "YouTube"]
client.set(TV, SHORTER)
r = room()
check("a real change overwrites memory", r.get("apps_full"), SHORTER)
client.set(TV, None, state="off")
check("  and the NEW list is what gets replayed", room().get("apps_full"), SHORTER)

# ── 6. memory never leaks between devices ──
client.set(OTHER, INPUTS_ONLY)
c = appctl.candidates(client, FakeProject({"study": {
    "committed": True, "area_id": "study", "name": "Study",
    "display": OTHER, "sources": [], "inputs": {}}}), "study")
check("memory never leaks between devices",
      (c[0].get("apps_full") if c else None), INPUTS_ONLY)

# ── 7. SWEEP-DRIVEN learning: continuous, not on request ──
json.dump({}, open(os.path.join(_TMP, "display_apps.json"), "w"))   # forget all

check("sweep with inputs only learns nothing",
      appctl.observe_snapshot({TV: {"state": "on",
                                    "attributes": {"source_list": INPUTS_ONLY}}}), 0)
check("  memory still empty", appctl._remembered_list(TV), [])

check("sweep with a real list learns it, nobody having asked",
      appctl.observe_snapshot({
          TV: {"state": "on", "attributes": {"source_list": FULL}},
          "media_player.a_speaker": {"state": "playing", "attributes": {}},
          "sensor.not_a_player": {"state": "1",
                                  "attributes": {"source_list": FULL}}}), 1)
check("  only media_players are considered",
      appctl._remembered_list("sensor.not_a_player"), [])
check("  stored verbatim, inputs included", appctl._remembered_list(TV), FULL)

check("an unchanged list is not rewritten every sweep",
      appctl.observe_snapshot({TV: {"state": "on",
                                    "attributes": {"source_list": FULL}}}), 0)
check("an Art Mode sweep cannot erase what was learned",
      appctl.observe_snapshot({TV: {"state": "on",
                                    "attributes": {"source_list": INPUTS_ONLY}}}), 0)
client.set(TV, INPUTS_ONLY)
check("room serves the swept list while the panel publishes inputs only",
      room().get("apps_full"), FULL)

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
