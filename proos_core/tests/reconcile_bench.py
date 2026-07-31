"""
Identity-reconcile bench — run: python3 tests/reconcile_bench.py

Matrix #12 — "Entity renamed/removed after commit: define the
reconcile-on-rescan behaviour." The last unbuilt piece of the identity
cluster (#5 shipped as discovery role_for; the drift ALARMS already existed
in healthmon; #11's suggest path already keys by area_id).

THE FAILURE THIS PREVENTS (lived, 28-31 Jul)
--------------------------------------------
Delete-and-re-add an integration and HA can mint new entity ids (the _2
twins). Committed records then rot silently: scripts drive corpses, verdicts
read corpses. "This wrecked Family and Living Rooms for most of today and
nobody was told" (What It Cannot Do §3). healthmon now ALARMS on it; this
build makes rescan REPAIR it.

THE RULES BEING PINNED
----------------------
1. Identity is the registry's (platform, unique_id) — the one key HA holds
   stable across re-pairs. Anchors are captured AT COMMIT, from the registry,
   never derived from names (the identity standard).
2. Reconcile is mechanical and total: display, sources, speakers, audio,
   tvaudio, inputs keys, avswitch entity + its inputs keys, and the anchors
   themselves all move together. A half-renamed record is worse than a stale
   one.
3. Fail-open everywhere: no anchors -> no-op. unique_id vanished from the
   registry -> untouched (that is the ALARM's job, not reconcile's). Old id
   still live in the registry -> untouched (nothing actually renamed).
4. Reconcile REPORTS what it did — the renames map goes to the caller for
   journalling. Silent repair is how trust dies.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.project import capture_anchors, reconcile_identities   # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


OLD = "media_player.family_room_shield_2"
NEW = "media_player.family_room_shield_3"
TV = "media_player.family_room_tv"
SONOS = "media_player.family_room_family_room"


def registry(shield_eid=OLD, include_shield=True):
    rows = [
        {"entity_id": TV, "platform": "samsungtv_smart",
         "unique_id": "uuid-tv", "device_id": "dev-tv"},
        {"entity_id": SONOS, "platform": "sonos",
         "unique_id": "RINCON_1", "device_id": "dev-sonos"},
    ]
    if include_shield:
        rows.append({"entity_id": shield_eid, "platform": "androidtv_remote",
                     "unique_id": "48:b0:2d:00:70:06", "device_id": "dev-sh"})
    return rows


def rec(shield=OLD, anchors="auto"):
    r = {"name": "Family Room", "area_id": "family_room", "kind": "tv",
         "committed": True, "display": TV, "sources": [shield],
         "speakers": [SONOS], "audio": [SONOS], "tvaudio": SONOS,
         "inputs": {shield: "HDMI 4", TV: "TV"},
         "avswitch": None}
    if anchors == "auto":
        r["anchors"] = {
            TV: {"platform": "samsungtv_smart", "unique_id": "uuid-tv",
                 "device_id": "dev-tv"},
            shield: {"platform": "androidtv_remote",
                     "unique_id": "48:b0:2d:00:70:06", "device_id": "dev-sh"},
            SONOS: {"platform": "sonos", "unique_id": "RINCON_1",
                    "device_id": "dev-sonos"},
        }
    elif anchors is not None:
        r["anchors"] = anchors
    return r


# ── 1. capture_anchors: registry truth, committed members only ─────────────
r = rec(anchors=None)
a = capture_anchors(r, registry())
check("anchors captured for every committed member",
      sorted(a), sorted([TV, OLD, SONOS]))
check("  keyed by (platform, unique_id, device_id)",
      a[OLD], {"platform": "androidtv_remote",
               "unique_id": "48:b0:2d:00:70:06", "device_id": "dev-sh"})
r2 = dict(rec(anchors=None), committed=False)
check("an uncommitted room captures nothing",
      capture_anchors(r2, registry()), {})
check("an entity absent from the registry gets no anchor (never invented)",
      OLD in capture_anchors(rec(anchors=None), registry(include_shield=False)),
      False)

# ── 2. THE REPAIR: re-pair renamed the shield ──────────────────────────────
r = rec()
out, renames = reconcile_identities(r, registry(shield_eid=NEW))
check("the rename is detected via unique_id", renames, {OLD: NEW})
check("sources rewritten", out["sources"], [NEW])
check("inputs keys rewritten", sorted(out["inputs"]), sorted([NEW, TV]))
check("  input VALUE survives the move", out["inputs"][NEW], "HDMI 4")
check("anchors follow the rename", NEW in out["anchors"], True)
check("  and the stale anchor is gone", OLD in out["anchors"], False)
check("untouched members stay untouched", out["display"], TV)

# avswitch entity + its per-source inputs move too
r = rec()
r["avswitch"] = {"entity": OLD, "output": "HDMI 3", "inputs": {OLD: "Game"}}
out, renames = reconcile_identities(r, registry(shield_eid=NEW))
check("avswitch entity rewritten", out["avswitch"]["entity"], NEW)
check("avswitch input keys rewritten", out["avswitch"]["inputs"], {NEW: "Game"})

# ── 3. FAIL-OPEN ───────────────────────────────────────────────────────────
out, renames = reconcile_identities(rec(anchors=None), registry(shield_eid=NEW))
check("no anchors -> no-op", renames, {})
check("  record untouched", out["sources"], [OLD])

out, renames = reconcile_identities(rec(), registry(include_shield=False))
check("unique_id gone from registry -> untouched (the alarm's job)",
      renames, {})
check("  record untouched", out["sources"], [OLD])

out, renames = reconcile_identities(rec(), registry(shield_eid=OLD))
check("old id still live -> nothing renamed", renames, {})

out, renames = reconcile_identities(rec(), None)
check("unreadable registry -> no-op", renames, {})

r3 = dict(rec(), committed=False)
out, renames = reconcile_identities(r3, registry(shield_eid=NEW))
check("uncommitted rooms are never reconciled", renames, {})

# ── 4. a rename NEVER creates a duplicate member ───────────────────────────
r = rec()
r["sources"] = [OLD, NEW]              # pathological: both ids somehow present
out, renames = reconcile_identities(r, registry(shield_eid=NEW))
check("rename into an existing member de-duplicates",
      out["sources"].count(NEW), 1)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
