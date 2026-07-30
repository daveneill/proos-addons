"""
Display published-app memory bench — run: python3 tests/display_apps_bench.py

MOTIVATING FAILURE (live house, 30 Jul 2026)
--------------------------------------------
Family Room has a Samsung Frame. Resting in Art Mode it publishes:

    source_list = ["HDMI 1","HDMI 2","HDMI 3","HDMI 4","TV"]      (5, no apps)

against the same TV's full list when it is not:

    source_list = [... 28 entries: Netflix, Disney+, 7plus, 9Now, ABC iview ...]

appctl.candidates() reads source_list live, so the room reported NO launchable
apps for as long as the panel sat on artwork. Art Mode toggled 129 times in two
days, so this was most of the time. Breaks START_HERE §5: "Keep all built-in TV
apps — ProOS may be installed in a home with only the Samsung TV."

THE RULE BEING PINNED
---------------------
ProOS may replay a device's OWN last published app list. It may never invent
one. A stripped list must not overwrite a good one, and a device we have never
heard from gets nothing — silence, not a guess.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="proos_disp_apps_")
os.environ["PROOS_DATA_DIR"] = _TMP          # before appctl resolves any path

from proos import appctl                      # noqa: E402

TV = "media_player.family_room_tv_2"
FULL = ["HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "TV", "Netflix", "Disney+",
        "Prime Video", "7plus", "9Now", "ABC iview", "YouTube"]
ART = ["HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "TV"]

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
    """Minimal stand-in: one entity whose source_list we control."""

    def __init__(self):
        self.lists = {}

    def set(self, eid, source_list, state="on"):
        self.lists[eid] = {"state": state,
                           "attributes": {"source_list": list(source_list),
                                          "friendly_name": "Family Room TV"}}

    # appctl._state() calls whatever the module uses; patched in below.
    def state(self, eid):
        return self.lists.get(eid, {})


client = FakeClient()
appctl._state = lambda c, eid: client.state(eid)          # noqa: SLF001
appctl._platforms = lambda c: {}                          # noqa: SLF001

class FakeProject:
    """appctl takes the project MODULE and calls .load() on it, not a dict."""

    def __init__(self, areas):
        self._areas = areas

    def load(self):
        return {"areas": self._areas}


PROJECT = FakeProject({"family_room": {"committed": True, "area_id": "family_room",
                                       "name": "Family Room", "display": TV,
                                       "sources": [], "inputs": {}}})


def apps_now():
    c = appctl.candidates(client, PROJECT, "family_room")
    return (c[0]["apps"], c[0].get("apps_remembered")) if c else ([], None)


# 1 — a device we have never heard from, reporting nothing, gets NOTHING
client.set(TV, ART)
apps, remembered = apps_now()
check("unknown device + stripped list -> no apps, nothing invented", apps, [])
check("  and it is not labelled remembered", remembered, False)

# 2 — the TV publishes its real list: apps are served, inputs filtered out
client.set(TV, FULL)
apps, remembered = apps_now()
check("full list -> real apps, inputs dropped",
      apps, ["Netflix", "Disney+", "Prime Video", "7plus", "9Now", "ABC iview", "YouTube"])
check("  served live, not from memory", remembered, False)

# 3 — Art Mode: the TV strips its apps. We replay ITS OWN last words.
client.set(TV, ART)
apps, remembered = apps_now()
check("Art Mode -> the TV's own last published apps are replayed",
      apps, ["Netflix", "Disney+", "Prime Video", "7plus", "9Now", "ABC iview", "YouTube"])
check("  and the caller is told it came from memory", remembered, True)

# 4 — the stripped list must NOT have overwritten the good one
check("a stripped list never overwrites the remembered one",
      appctl._remembered_apps(TV),
      ["Netflix", "Disney+", "Prime Video", "7plus", "9Now", "ABC iview", "YouTube"])

# 5 — TV fully off, no source_list at all: still replay
client.set(TV, [], state="off")
apps, remembered = apps_now()
check("TV off with no source_list -> apps still offered", len(apps), 7)

# 6 — an app genuinely removed from the TV: a fresh publish wins
client.set(TV, ["HDMI 1", "TV", "Netflix", "YouTube"])
apps, _ = apps_now()
check("a real change overwrites memory (app uninstalled)", apps, ["Netflix", "YouTube"])
client.set(TV, ART)
apps, _ = apps_now()
check("  and the NEW list is what gets replayed", apps, ["Netflix", "YouTube"])

# 7 — memory is per entity: another room's TV is not borrowed from
OTHER = "media_player.study_tv"
client.set(OTHER, ART)
c = appctl.candidates(client, FakeProject({"study": {"committed": True, "area_id": "study",
                                                    "name": "Study", "display": OTHER,
                                                    "sources": [], "inputs": {}}}), "study")
check("memory never leaks between devices", c[0]["apps"] if c else None, [])


# ── 8-11: SWEEP-DRIVEN learning (the fix for "memory stayed empty forever") ──
# The Frame is in Art Mode every time anyone opens the room, so request-driven
# recording never fired. Learning must happen off the sweep snapshot instead.
import json as _json
_json.dump({}, open(os.path.join(_TMP, "display_apps.json"), "w"))   # forget everything

SNAP_FULL = {TV: {"state": "on", "attributes": {"source_list": FULL}},
             "media_player.a_speaker": {"state": "playing", "attributes": {}},
             "sensor.not_a_player": {"state": "1", "attributes": {"source_list": FULL}}}
SNAP_ART = {TV: {"state": "on", "attributes": {"source_list": ART}}}

n = appctl.observe_snapshot(SNAP_ART)
check("sweep sees only inputs -> learns nothing", n, 0)
check("  memory still empty", appctl._remembered_apps(TV), [])

n = appctl.observe_snapshot(SNAP_FULL)
check("sweep sees the real list -> learns it without anyone asking", n, 1)
check("  and only media_players are considered",
      appctl._remembered_apps("sensor.not_a_player"), [])

n = appctl.observe_snapshot(SNAP_FULL)
check("an unchanged list is not rewritten every sweep", n, 0)

n = appctl.observe_snapshot(SNAP_ART)
check("Art Mode sweep cannot erase what was learned", n, 0)
client.set(TV, ART)
apps, remembered = apps_now()
check("room now serves the swept list while the panel is on artwork",
      apps, ["Netflix", "Disney+", "Prime Video", "7plus", "9Now", "ABC iview", "YouTube"])
check("  flagged as remembered", remembered, True)

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
