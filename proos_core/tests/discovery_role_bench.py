"""
Discovery role bench — run: python3 tests/discovery_role_bench.py

Matrix #2, the DISCOVERY half (the Pro chip-gate half shipped in 2026080110).

MOTIVATING FAILURE (live house, 31 Jul – 1 Aug 2026 — measured)
---------------------------------------------------------------
The HomePod auto-role keys on device_class:

    if role == "source" and integration == "apple_tv" and dc == "speaker":
        role = "audio"

Its own comment says a real Apple TV is dc "tv" **or None** — and the live
Office HomePod reports dc **null** (entity registry, 31 Jul). So the HomePod
looked exactly like a real Apple TV, discovery bucketed it a SOURCE, live
discovery invented a Watch activity for the Office, the room took the AV
path, and its status froze (fixed downstream in 1.0.259/1.0.260 — this is
the root).

THE RULE BEING PINNED (Certification Standard, apple_tv register row)
---------------------------------------------------------------------
An apple_tv device qualifies as a Source only when its paired
`remote.<oid>` entity is present — the register's own on-box check, and how
sleep/wake works. A HomePod has none. Capability, not device_class, not name.

Fail-open: when remote presence is UNKNOWN (registry unreadable), the role
stands — never re-role on missing evidence (confirm, don't assume).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.discovery import role_for                          # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


# ── THE DEFECT: HomePod with dc null ───────────────────────────────────────
check("HomePod (apple_tv, dc None, NO remote) -> audio",
      role_for("apple_tv", None, has_remote=False), "audio")

# ── the existing dc rule still catches dc 'speaker' regardless of remote ───
check("HomePod (apple_tv, dc speaker, remote unknown) -> audio",
      role_for("apple_tv", "speaker", has_remote=None), "audio")

# ── a real Apple TV keeps Source — the remote is the discriminator ─────────
check("Apple TV (apple_tv, dc None, remote present) -> source",
      role_for("apple_tv", None, has_remote=True), "source")
check("Apple TV (apple_tv, dc tv, remote present) -> source",
      role_for("apple_tv", "tv", has_remote=True), "source")

# ── FAIL-OPEN: unknown remote presence never re-roles ──────────────────────
check("apple_tv with UNKNOWN remote presence keeps its role (fail-open)",
      role_for("apple_tv", None, has_remote=None), "source")

# ── other integrations are untouched ───────────────────────────────────────
check("androidtv_remote stays source", role_for("androidtv_remote", "tv", None),
      "source")
check("samsungtv_smart stays display", role_for("samsungtv_smart", "tv", None),
      "display")
check("sonos stays audio", role_for("sonos", "speaker", None), "audio")
check("denonavr keeps its mapped role",
      role_for("denonavr", "receiver", None),
      role_for("denonavr", "receiver", True))

# ── the unmapped-integration dc fallback survives ──────────────────────────
check("unmapped dc-tv device falls back to display",
      role_for("webostv_new_thing", "tv", None), "display")
check("unmapped non-tv device has no role",
      role_for("webostv_new_thing", None, None), None)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
