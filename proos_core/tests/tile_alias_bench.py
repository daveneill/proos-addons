"""
Tile-alias bench — run: python3 tests/tile_alias_bench.py

MOTIVATING FAILURE (live house, 30 Jul 2026)
--------------------------------------------
appart/aliases.json contained:

    "tv": "apple_tv"

added so Apple's own app — which is literally called "TV" — would get the Apple
TV graphic. But "TV" is not a name unique to Apple. It is also:

  * a Samsung display's tuner input   (source_list: [... "TV"])
  * Android TV's own live-television app
  * an AVR's "TV Audio" style input, once slugified

so the Shield's remote rendered an Apple TV logo, and so would live TV. One
alias mis-served three devices.

THE RULE BEING PINNED (ProOS_App_Tile_Pack_Spec.md)
---------------------------------------------------
An alias key must be a VARIANT NAME OF ONE SERVICE (`disney_plus -> disney`),
never a generic word. A generic key collides across devices and mis-serves
every one of them.

Platform-specific apps resolve by PACKAGE ID instead — packages.json already
maps `com.apple.atve.androidtv.appletv -> apple_tv`, and tile_path() gives the
package id precedence over the name. A name with no graphic falls to the
neutral wordmark, which is the documented behaviour and is always better than
confidently showing the wrong logo.
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
APPART = os.path.join(ROOT, "appart")

# Words that appear in real devices' source_list as INPUTS or generic entries.
# An alias keyed on any of these cannot be about one service, by definition.
# Kept deliberately tight: only words observed in this house's actual
# source_lists, plus the obvious input vocabulary. ("channels" is NOT here —
# Channels is a real product.)
GENERIC = {
    "tv", "live_tv", "app", "apps", "input", "source", "media", "video",
    "audio", "music", "radio", "home", "tuner", "antenna", "cable", "dtv",
    "av", "aux", "line_in", "hdmi", "hdmi_1", "hdmi_2", "hdmi_3", "hdmi_4",
    "screen_mirroring", "internet", "gallery", "settings", "search",
}

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}{(' — ' + detail) if detail else ''}")
        print("FAIL  " + name + ((" — " + detail) if detail else ""))


with open(os.path.join(APPART, "aliases.json"), encoding="utf-8") as fh:
    aliases = {k: v for k, v in json.load(fh).items()
               if isinstance(v, str) and not k.startswith("_")}
with open(os.path.join(APPART, "packages.json"), encoding="utf-8") as fh:
    packages = {k: v for k, v in json.load(fh).items()
                if isinstance(v, str) and not k.startswith("_")}

print(f"{len(aliases)} aliases, {len(packages)} package mappings\n")

# 1 — a generic key is ALLOWED, and here is why (corrected 30 Jul 2026 after
#     Dave pushed back, correctly). "TV" is a real app name on an Apple TV, and
#     `tv -> apple_tv` is a deliberate decision made when Core was set up with
#     brandfetch. The same string is ALSO a Samsung tuner input -- but that
#     ambiguity cannot be resolved inside the tile map, because the map only
#     ever sees a string. It is resolved by the CALLER: a display INPUT must
#     never be sent to the tile service in the first place. Core already draws
#     that line (appart.is_app / appart._INPUT_RE); the dashboard does not, which
#     is why HDMI 1-4 and TV appeared in an APPS row. Tested in the D3 work, not
#     here. This check is left as a REPORT so a future generic key is visible
#     rather than silent.
generic_keys = sorted(k for k in aliases if k in GENERIC)
if generic_keys:
    print("note  generic alias keys present (allowed, resolved by the caller): "
          + ", ".join(f"{k!r} -> {aliases[k]!r}" for k in generic_keys))

# 2 — self-mapping is harmless (it resolves to the same slug) and one exists
#     deliberately. Reported, not failed. My earlier claim that it "hides a
#     missing tile" was wrong: nvidia_games has a real fetched tile on the box.
selfref = sorted(k for k, v in aliases.items() if k == v)
if selfref:
    print("note  self-mapping aliases (no-op, harmless): " + ", ".join(selfref))

# 3 — alias keys must be slug-shaped, or they can never match
badshape = sorted(k for k in aliases
                  if k != k.lower().strip() or " " in k or "-" in k)
check("every alias key is slug-shaped", not badshape, ", ".join(badshape))

# 4 — the Apple TV app must still resolve WITHOUT the name alias, by package id
check("Apple's TV app resolves by package id, not by name",
      packages.get("com.apple.atve.androidtv.appletv") == "apple_tv",
      "packages.json is missing the Apple TV package mapping")

# 5 — no alias may point at a target that is itself an alias key (chains don't
#     resolve: tile_path() applies the map exactly once)
chains = sorted(f"{k} -> {v} -> {aliases[v]}" for k, v in aliases.items()
                if v in aliases and v != k)
check("no alias chains (the map is applied once)", not chains, "; ".join(chains))

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
