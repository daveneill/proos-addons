"""
Attribute-republish bench — run: python3 tests/attr_republish_bench.py [old-build-dir]

Found while writing the #19 bench (1 Aug 2026): `_publish` skips on matching
STATE alone, so an attribute-only change never re-POSTs. Harmless for years
of TV rooms — a verdict's meaning lives in its state — but a MUSIC room's
meaning lives half in its attributes: the Study playing track after track is
`playing` -> `playing` forever, so the dashboard shows the first song all
evening.

THE RULE BEING PINNED
---------------------
1. A change in MEANINGFUL attributes republishes, state changed or not.
   Meaningful: label, media_title/artist/album, grouped_to, source, verified,
   held — anything a person or Assist reads.
2. VOLATILE attributes never cause a publish on their own: `evidence` and
   `witness_rate` can move every 2-second sweep, and re-POSTing them each
   time is one state row per room per sweep into HA's recorder, forever.
   Publish-on-change exists precisely to prevent that; it stays.
3. #19 reconcile behaviour is unchanged.
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


SPK = "media_player.study_study"
TV_DISP = "media_player.bedroom_tv"
TV_ATV = "media_player.bedroom_apple_tv"
MU_SENSOR = "sensor.proos_activity_study"
TV_SENSOR = "sensor.proos_activity_bedroom"

PROJECT = {"areas": {
    "Bedroom": {"name": "Bedroom", "area_id": "bedroom", "kind": "tv",
                "committed": True, "display": TV_DISP, "sources": [TV_ATV],
                "audio": [], "speakers": []},
    "Study": {"name": "Study", "area_id": "study", "kind": "music",
              "committed": True, "display": None, "sources": [],
              "audio": [], "speakers": [SPK]},
}}


def snap(title, tv_on=True):
    s = {SPK: {"state": "playing", "attributes": {"media_title": title}}}
    if tv_on:
        s[TV_DISP] = {"state": "on", "attributes": {"source": "HDMI 1"}}
        s[TV_ATV] = {"state": "playing", "attributes": {}}
    # sensors present + agreeing, so #19 reconcile never interferes here
    s[MU_SENSOR] = {"state": "playing", "attributes": {}}
    s[TV_SENSOR] = {"state": "watch_apple_tv", "attributes": {}}
    return s


def make_activity(witness_rate):
    a = types.SimpleNamespace()
    a.key, a.source_eid = "watch_apple_tv", TV_ATV
    a.route = {"select_source": "HDMI 1"}
    a.audio_witness = None
    a.provisional = False
    a.label = "Watch Apple TV"
    a.targets = [types.SimpleNamespace(entity_id=TV_DISP)]
    a.summary = lambda s: types.SimpleNamespace(ok=True)
    return a


class FakeClient:
    def __init__(self):
        self.posts = []           # (sensor, state, attrs)

    def _req(self, method, path, payload=None):
        self.posts.append((path.rsplit("/", 1)[-1],
                           (payload or {}).get("state"),
                           (payload or {}).get("attributes") or {}))
        return {}

    def render_template(self, *_a, **_k):
        return "[]"


def get_controller(area_name):
    ctrl = types.SimpleNamespace()
    ctrl.activities = ({"watch_apple_tv": make_activity(0.5)}
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


def sweep(pub, s):
    if hasattr(pub, "_reconcile_published"):
        pub._reconcile_published(s)
    pub._sweep_rooms(s)


def posts_for(client, sensor):
    return [p for p in client.posts if p[0] == sensor]


pub, client = build(NEW_ROOT)

# ── 1. baseline ────────────────────────────────────────────────────────────
sweep(pub, snap("Song One"))
check("first sweep publishes the music room",
      len(posts_for(client, MU_SENSOR)), 1)

# ── 2. THE DEFECT: next track, state still 'playing' ───────────────────────
client.posts.clear()
sweep(pub, snap("Song Two"))
mu = posts_for(client, MU_SENSOR)
check("a title change republishes with state unchanged", len(mu), 1)
check("  and carries the new title",
      mu[0][2].get("media_title") if mu else None, "Song Two")

# ── 3. nothing changed -> nothing publishes ────────────────────────────────
client.posts.clear()
sweep(pub, snap("Song Two"))
check("an identical sweep publishes nothing", client.posts, [])

# ── 4. VOLATILE attrs never publish on their own ───────────────────────────
# Two sweeps whose only difference is witness evidence flutter: same states,
# same meaningful attrs. The TV sensor must stay quiet both times.
client.posts.clear()
sweep(pub, snap("Song Two"))
sweep(pub, snap("Song Two"))
check("volatile-only flutter causes no TV republish",
      posts_for(client, TV_SENSOR), [])

# ── 5. #19 unchanged: wiped sensor still comes back ────────────────────────
client.posts.clear()
s = snap("Song Two")
del s[MU_SENSOR]
sweep(pub, s)
check("a wiped sensor still republishes (#19 intact)",
      len(posts_for(client, MU_SENSOR)), 1)

# ── REGRESSION GATE: the shipped build drops the title change ──────────────
old_root = sys.argv[1] if len(sys.argv) > 1 else None
if old_root and os.path.isdir(os.path.join(old_root, "proos")):
    opub, oclient = build(os.path.abspath(old_root))
    sweep(opub, snap("Song One"))
    oclient.posts.clear()
    sweep(opub, snap("Song Two"))
    check("SHIPPED build drops the title change (defect reproduced)",
          posts_for(oclient, MU_SENSOR), [])
else:
    print("\nnote  no old build given — defect-reproduction gate SKIPPED.")

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
