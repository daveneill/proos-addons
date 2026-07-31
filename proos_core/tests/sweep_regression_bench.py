"""
Sweep regression bench — run:
    python3 tests/sweep_regression_bench.py /path/to/old/proos

The gate for the music-room status change (ProOS_Room_Status_Spec §4):

    "the three AV rooms must publish byte-identical verdict attributes before
     and after. If they don't, it isn't this change."

Drives ActivityPublisher._sweep_rooms() in BOTH the shipped build and the new
one, over the same fake project + snapshot, and compares every POST the two
make for the TV room. Any difference at all -- state, attribute, ordering of
keys once normalised -- fails.

Also asserts the thing the change is FOR: the music room publishes in the new
build and published nothing in the old one.

Pass the old package's parent directory as argv[1]; with no argument it runs
the new build only and just checks the music room appears (useful in CI where
the previous release isn't unpacked).
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_ROOT = os.path.join(HERE, "..")

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"\n        wanted {want!r}\n        got    {got!r}")


# ── the fixture: one TV room, one music room, one snapshot ─────────────────
TV_DISP = "media_player.bedroom_tv"
TV_ATV = "media_player.bedroom_apple_tv"
MUSIC_SPK = "media_player.study_study"
OFFICE_SONOS = "media_player.office_office"
OFFICE_HOMEPOD = "media_player.office_2_office_2"      # apple_tv platform

PROJECT = {"areas": {
    "Bedroom": {"name": "Bedroom", "area_id": "bedroom", "kind": "tv",
                "committed": True, "display": TV_DISP, "sources": [TV_ATV],
                "audio": [], "speakers": []},
    "Study": {"name": "Study", "area_id": "study", "kind": "music",
              "committed": True, "display": None, "sources": [],
              "audio": [], "speakers": [MUSIC_SPK]},
    # The live Office: a committed MUSIC room that live discovery invents
    # activities for, because it holds an apple_tv-platform speaker (HomePod)
    # and controller._committed_cluster() only builds from the record when the
    # record has a display. Measured 31 Jul 2026 -- the room froze reporting
    # 'playing' while both its speakers were paused. A committed music room
    # must be answered from its record, never from discovery's guess.
    "Office": {"name": "Office", "area_id": "office", "kind": "music",
               "committed": True, "display": None, "sources": [],
               "audio": [], "speakers": [OFFICE_SONOS, OFFICE_HOMEPOD]},
}}

SNAP = {
    TV_DISP: {"state": "on", "attributes": {"source": "HDMI 1"}},
    TV_ATV: {"state": "playing", "attributes": {}},
    MUSIC_SPK: {"state": "playing",
                "attributes": {"media_title": "Kind of Blue",
                               "source": "Spotify"}},
    # Both Office speakers paused — the room must report idle, not stay
    # frozen on its last 'playing'.
    OFFICE_SONOS: {"state": "paused", "attributes": {}},
    OFFICE_HOMEPOD: {"state": "paused", "attributes": {}},
}


def make_activity():
    a = types.SimpleNamespace()
    a.key, a.source_eid = "watch_apple_tv", TV_ATV
    a.route = {"select_source": "HDMI 1"}
    a.audio_witness = None
    a.provisional = False
    a.label = "Watch Apple TV"
    a.targets = [types.SimpleNamespace(entity_id=TV_DISP)]
    a.summary = lambda snap: types.SimpleNamespace(ok=True)
    return a


class FakeClient:
    """Captures every POST /api/states the bridge makes."""

    def __init__(self):
        self.posts = []

    def _req(self, method, path, payload=None):
        self.posts.append((method, path, payload))
        return {}

    def render_template(self, *_a, **_k):
        return "[]"


def get_controller(area_name):
    ctrl = types.SimpleNamespace()
    # Bedroom is a real AV room. Office is a MUSIC room that live discovery
    # has invented an activity for (the HomePod is apple_tv-platform, a
    # certified Source integration) — reproducing the live freeze. A committed
    # music room must ignore this entirely.
    ctrl.activities = ({"watch_apple_tv": make_activity()}
                       if area_name in ("Bedroom", "Office") else {})
    return ctrl


def run_sweep(root):
    """Import ctlbridge from `root`, run one sweep, return captured posts."""
    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
        del sys.modules[mod]
    sys.path.insert(0, root)
    try:
        from proos.ctlbridge import ActivityPublisher       # noqa: PLC0415
        client = FakeClient()
        project_mod = types.SimpleNamespace(load=lambda: PROJECT)
        pub = ActivityPublisher(client, project_mod, get_controller,
                                enabled=lambda: True, witnesses={})
        pub._sweep_rooms(SNAP)
        return client.posts
    finally:
        sys.path.remove(root)


def by_area(posts):
    out = {}
    for _m, path, payload in posts:
        out[path.rsplit("/", 1)[-1]] = payload
    return out


def norm(payload):
    """Stable form for comparison — key order can't matter, values must."""
    return json.dumps(payload, sort_keys=True, default=str)


new = by_area(run_sweep(os.path.abspath(NEW_ROOT)))

# ── what the change is FOR ─────────────────────────────────────────────────
check("music room now publishes", "sensor.proos_activity_study" in new, True)
if "sensor.proos_activity_study" in new:
    m = new["sensor.proos_activity_study"]
    check("  music room state", m["state"], "playing")
    check("  produced by the music producer", m["attributes"]["kind"], "music")
    check("  now-playing carried", m["attributes"].get("media_title"),
          "Kind of Blue")
    check("  never marked provisional", m["attributes"]["provisional"], False)

check("tv room still publishes", "sensor.proos_activity_bedroom" in new, True)

# ── the freeze: a music room discovery invented activities for ─────────────
check("music room with invented activities still publishes",
      "sensor.proos_activity_office" in new, True)
if "sensor.proos_activity_office" in new:
    o = new["sensor.proos_activity_office"]
    check("  both speakers paused -> idle, not a stale 'playing'",
          o["state"], "idle")
    check("  answered by the music producer, not the ladder",
          o["attributes"]["kind"], "music")
    check("  both committed speakers counted",
          o["attributes"].get("members"), [OFFICE_SONOS, OFFICE_HOMEPOD])

# ── THE GATE: byte-identical AV output against the shipped build ───────────
old_root = sys.argv[1] if len(sys.argv) > 1 else None
if old_root and os.path.isdir(os.path.join(old_root, "proos")):
    old = by_area(run_sweep(os.path.abspath(old_root)))
    check("REGRESSION: tv room payload byte-identical to shipped build",
          norm(new.get("sensor.proos_activity_bedroom")),
          norm(old.get("sensor.proos_activity_bedroom")))
    check("  shipped build published nothing for the music room",
          "sensor.proos_activity_study" in old, False)
    check("  and the change adds exactly one publication",
          len(new) - len(old), 1)
else:
    print("\nnote  no old build given — regression gate SKIPPED."
          "\n      run: python3 tests/sweep_regression_bench.py <old-build-dir>")

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
