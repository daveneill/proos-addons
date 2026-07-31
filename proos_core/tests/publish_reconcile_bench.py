"""
Publish-reconcile bench — run: python3 tests/publish_reconcile_bench.py [old-build-dir]

Matrix #19 (gap list, 31 Jul 2026): verdicts do not republish after a Home
Assistant restart.

THE MECHANISM
-------------
`ctlbridge._publish()` skips the POST when its in-memory `_last` cache says
the state is unchanged:

    if self._last.get(eid) == state:
        return

`_last` is never reconciled with HA. The verdict sensors are bare POSTed
states — HA does not restore them across a restart — so after every HA
restart the sensors are simply GONE, while Core's cache still says "off".
Nothing republishes until the room's activity actually changes. Every home,
every restart, silently. The documented workaround was "restart ProOS Core
after any HA restart".

THE FIX BEING PINNED
--------------------
The sweep already fetches EVERY state in one call — including the verdict
sensors themselves. So reconcile the cache against the sweep's own snapshot:
if HA's copy of a sensor is missing or disagrees, the cache entry is dropped
and the next publish goes through. Zero extra HA traffic, no timers, no
restart detection — the snapshot is already the truth, so use it.

Publish-on-change is preserved: a sensor present and agreeing publishes
nothing, exactly as today.
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_ROOT = os.path.abspath(os.path.join(HERE, ".."))

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


TV_DISP = "media_player.bedroom_tv"
TV_ATV = "media_player.bedroom_apple_tv"
SPK = "media_player.study_study"
TV_SENSOR = "sensor.proos_activity_bedroom"
MU_SENSOR = "sensor.proos_activity_study"

PROJECT = {"areas": {
    "Bedroom": {"name": "Bedroom", "area_id": "bedroom", "kind": "tv",
                "committed": True, "display": TV_DISP, "sources": [TV_ATV],
                "audio": [], "speakers": []},
    "Study": {"name": "Study", "area_id": "study", "kind": "music",
              "committed": True, "display": None, "sources": [],
              "audio": [], "speakers": [SPK]},
}}


def devices():
    """The room devices, in the state they hold across every scenario."""
    return {
        TV_DISP: {"state": "on", "attributes": {"source": "HDMI 1"}},
        TV_ATV: {"state": "playing", "attributes": {}},
        SPK: {"state": "playing", "attributes": {"media_title": "X"}},
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
    def __init__(self):
        self.posts = []

    def _req(self, method, path, payload=None):
        self.posts.append(path.rsplit("/", 1)[-1])
        return {}

    def render_template(self, *_a, **_k):
        return "[]"


def get_controller(area_name):
    ctrl = types.SimpleNamespace()
    ctrl.activities = ({"watch_apple_tv": make_activity()}
                       if area_name == "Bedroom" else {})
    return ctrl


def build(root):
    for m in [m for m in list(sys.modules) if m.split(".")[0] == "proos"]:
        del sys.modules[m]
    sys.path.insert(0, root)
    from proos.ctlbridge import ActivityPublisher                # noqa: PLC0415
    sys.path.remove(root)
    client = FakeClient()
    pub = ActivityPublisher(client, types.SimpleNamespace(load=lambda: PROJECT),
                            get_controller, enabled=lambda: True, witnesses={})
    return pub, client


def sweep(pub, snap):
    """One sweep the way ctlbridge.sweep() runs it: reconcile (if the build
    has it) + rooms, against the same snapshot."""
    if hasattr(pub, "_reconcile_published"):
        pub._reconcile_published(snap)
    pub._sweep_rooms(snap)


def published_states(pub, snap):
    """What HA would now hold: mirror each POST into the snapshot."""
    return snap


pub, client = build(NEW_ROOT)

# ── 1. first sweep publishes both rooms ────────────────────────────────────
snap = devices()
sweep(pub, snap)
check("first sweep publishes the TV room", TV_SENSOR in client.posts, True)
check("first sweep publishes the music room", MU_SENSOR in client.posts, True)

# ── 2. steady state: sensors present and agreeing -> publish nothing ───────
client.posts.clear()
snap = devices()
snap[TV_SENSOR] = {"state": "watch_apple_tv", "attributes": {}}
snap[MU_SENSOR] = {"state": "playing", "attributes": {}}
sweep(pub, snap)
check("agreeing sensors are not republished (publish-on-change kept)",
      client.posts, [])

# ── 3. THE DEFECT: HA restarted, sensors gone, room unchanged ──────────────
client.posts.clear()
snap = devices()                      # no verdict sensors in the snapshot
sweep(pub, snap)
check("HA restart (sensor missing) -> TV verdict republished",
      TV_SENSOR in client.posts, True)
check("HA restart -> music status republished", MU_SENSOR in client.posts, True)

# ── 4. sensor present but disagreeing (e.g. 'unknown') -> republished ──────
client.posts.clear()
snap = devices()
snap[TV_SENSOR] = {"state": "unknown", "attributes": {}}
snap[MU_SENSOR] = {"state": "playing", "attributes": {}}
sweep(pub, snap)
check("a sensor HA holds as 'unknown' is republished",
      TV_SENSOR in client.posts, True)
check("  while the agreeing one stays quiet", MU_SENSOR in client.posts, False)

# ── 5. and steady state again after recovery ───────────────────────────────
client.posts.clear()
snap = devices()
snap[TV_SENSOR] = {"state": "watch_apple_tv", "attributes": {}}
snap[MU_SENSOR] = {"state": "playing", "attributes": {}}
sweep(pub, snap)
check("recovered: nothing republishes", client.posts, [])

# ── REGRESSION GATE: the shipped build reproduces the defect ───────────────
old_root = sys.argv[1] if len(sys.argv) > 1 else None
if old_root and os.path.isdir(os.path.join(old_root, "proos")):
    opub, oclient = build(os.path.abspath(old_root))
    sweep(opub, devices())
    oclient.posts.clear()
    sweep(opub, devices())            # sensors missing -- the restart case
    check("SHIPPED build fails to republish after restart (defect reproduced)",
          TV_SENSOR in oclient.posts, False)
else:
    print("\nnote  no old build given — defect-reproduction gate SKIPPED."
          "\n      run: python3 tests/publish_reconcile_bench.py <old-build-dir>")

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
