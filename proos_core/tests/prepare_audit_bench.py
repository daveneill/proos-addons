"""
Preparation-audit bench — run: python3 tests/prepare_audit_bench.py

Matrix #13, first shippable slice. The factory reset of 31 Jul silently
dropped THREE required settings, each found by hours of log archaeology
instead of seconds at commissioning:

  1. the Family Room Frame's `app_list` (panel doesn't answer
     ed.installedApp.get -- 13+ requests, zero replies, measured)
  2. `ip_control_art_mode` (absent => False, art readback fell to the ws
     client which misreports on this panel -- TV Off flipped back in 136 ms
     and the room read Watch Apple TV all night)
  3. UniFi `allow_bandwidth_sensors` (absent => False, zero data-rate
     sensors, every source raised "no network witness")

THE RULES BEING PINNED
----------------------
* The audit is CERTIFICATION DATA, not code: per-integration facts in a
  table (the netevidence.PROVIDERS pattern). Adding a brand is a table
  entry, never an engine change. No check ever asks "is this a Samsung?" --
  it asks the facts table for the device's integration.
* Observation first, options only where observation cannot tell
  (netevidence's own principle). App enumeration is judged from the
  published source_list; art readback health cannot be observed from
  entities, so the option is read from the entry's diagnostics.
* The audit ADVISES, it never blocks. Green means committed -- unchanged.
  Wrong config generates wrong behaviour; MISSING config generates a named,
  fixable advisory (signal-graph spec, three-states rule).
* A device is never faulted for a capability it doesn't claim
  (Certification Standard): a non-Frame skips art checks, a compatible
  display gets no samsungtv checks at all, a music room audits nothing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.prepare import audit_room                          # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


def ids(res, ok=None):
    out = [c["id"] for c in res["checks"] if ok is None or c["ok"] is ok]
    return sorted(out)


def by_id(res, cid):
    for c in res["checks"]:
        if c["id"] == cid:
            return c
    return None


TV = "media_player.family_room_tv"

INPUTS_ONLY = ["HDMI 1", "HDMI 2", "HDMI 3", "HDMI 4", "TV"]
WITH_APPS = INPUTS_ONLY + ["Netflix", "YouTube", "Disney+"]


def rec(display=TV, kind="tv", committed=True):
    return {"name": "Family Room", "area_id": "family_room", "kind": kind,
            "committed": committed, "display": display, "sources": [],
            "speakers": [], "audio": [],
            "meta": {TV: {"integration": "samsungtv_smart"}}}


def snap(source_list=WITH_APPS, art_switch=True, state="on"):
    s = {TV: {"state": state,
              "attributes": {"source_list": list(source_list) if source_list is not None else None}}}
    if s[TV]["attributes"]["source_list"] is None:
        del s[TV]["attributes"]["source_list"]
    if art_switch:
        s["switch.family_room_tv_art_mode"] = {"state": "off", "attributes": {}}
    return s


def entry(*, frame=True, ip=True, art=True, wol=True, ping=True, app_list=None):
    # ip: True = token present (the fork's own enable rule), False = unpaired.
    # art/wol: True = option set correctly, False = set WRONG, 'absent' =
    # missing from the diagnostics view (fork exports only 4 keys) -> UNKNOWN.
    data = {"is_frame_tv": frame, "ip_control_port": 1516 if frame else 1515}
    if ip:
        data["ip_control_token"] = "tok"
    options = {}
    if art is True:
        options["ip_control_art_mode"] = True
    elif art is False:
        options["ip_control_art_mode"] = False
    if wol is True:
        options["power_on_method"] = "1"     # options round-trip as strings
    elif wol is False:
        options["power_on_method"] = "2"
    if ping:
        options["ping_port"] = 9197
    if app_list is not None:
        options["app_list"] = app_list
    return {"data": data, "options": options}


GOOD = dict(record=rec(), snap=snap(), entry=entry())


# ── 1. a fully prepared room passes every check ────────────────────────────
res = audit_room(**GOOD)
check("a prepared room raises no advisories", ids(res, ok=False), [])
check("  and actually ran checks", len(res["checks"]) > 4, True)

# ── 2. THIS WEEK #1: ip_control_art_mode wrong / invisible on a Frame ──────
res = audit_room(record=rec(), snap=snap(), entry=entry(art=False))
c = by_id(res, "art_readback")
check("art option explicitly off is caught", c and c["ok"] is False, True)
check("  and the advisory names the fix",
      bool(c and "ip_control_art_mode" in (c.get("fix") or "")), True)
# the fork's diagnostics export only 4 option keys -- an ABSENT option is
# UNKNOWN with the reason, never a failure (audited live 1 Aug: the first
# render false-alarmed 3 checks on a freshly repaired house)
res = audit_room(record=rec(), snap=snap(), entry=entry(art='absent'))
c = by_id(res, "art_readback")
check("absent-from-diagnostics art option is UNKNOWN, not failed",
      c and c["ok"] is None, True)
check("  with the not-visible note",
      bool(c and "not visible" in (c.get("note") or "")), True)

# ── 3. THIS WEEK #2: panel publishes inputs only, no committed app_list ────
res = audit_room(record=rec(), snap=snap(source_list=INPUTS_ONLY),
                 entry=entry(app_list={}))
c = by_id(res, "app_enumeration")
check("silent app enumeration is caught", c and not c["ok"], True)
check("  advisory says to commit the list manually",
      bool(c and "app_list" in (c.get("fix") or "")), True)

# a committed app_list SATISFIES the check even with an inputs-only panel
res = audit_room(record=rec(), snap=snap(source_list=INPUTS_ONLY),
                 entry=entry(app_list={"Netflix": "111"}))
check("a committed app_list satisfies enumeration",
      by_id(res, "app_enumeration")["ok"], True)

# a panel that enumerates satisfies it with no app_list at all
res = audit_room(record=rec(), snap=snap(source_list=WITH_APPS),
                 entry=entry(app_list={}))
check("a panel that enumerates satisfies it natively",
      by_id(res, "app_enumeration")["ok"], True)

# TV OFF -> the list is unreadable, and the audit must SAY SO, not fail it
# (matrix #14: a source_list read while the TV is off is empty; judging
# enumeration from that would fault every sleeping panel)
res = audit_room(record=rec(), snap=snap(source_list=None, state="off"),
                 entry=entry(app_list={}))
c = by_id(res, "app_enumeration")
check("an off panel is 'unknown', never failed",
      c and c["ok"] is None, True)

# ── 4. remaining samsung facts ─────────────────────────────────────────────
res = audit_room(record=rec(), snap=snap(), entry=entry(ip=False))
check("IP control unpaired (no token) is caught",
      by_id(res, "ip_control")["ok"], False)

res = audit_room(record=rec(), snap=snap(), entry=entry(wol=False))
check("power-on method not WOL is caught",
      by_id(res, "power_on_wol")["ok"], False)
res = audit_room(record=rec(), snap=snap(), entry=entry(wol='absent'))
check("absent power-on method is UNKNOWN, not failed",
      by_id(res, "power_on_wol")["ok"], None)
# string/native tolerance: "1" and 1 are the same committed value
e = entry(); e["options"]["power_on_method"] = 1
res = audit_room(record=rec(), snap=snap(), entry=e)
check("integer power_on_method still passes (string-compare)",
      by_id(res, "power_on_wol")["ok"], True)

res = audit_room(record=rec(), snap=snap(art_switch=False), entry=entry())
check("a Frame with no art switch entity is caught",
      by_id(res, "art_switch")["ok"], False)

# ── 5. capability, never brand ─────────────────────────────────────────────
# a non-Frame Samsung: art checks don't exist for it
res = audit_room(record=rec(), snap=snap(art_switch=False),
                 entry=entry(frame=False))
check("a non-Frame has no art checks at all",
      [i for i in ids(res) if i.startswith("art")], [])

# a compatible (non-samsung) display: no samsung facts apply
r2 = rec()
r2["meta"][TV] = {"integration": "webostv"}
res = audit_room(record=r2, snap=snap(), entry=None)
check("a compatible display gets no samsung checks",
      [i for i in ids(res) if i in ("ip_control", "power_on_wol")], [])
check("  and is not faulted for it", ids(res, ok=False), [])

# a music room audits nothing (no display)
res = audit_room(record=rec(display=None, kind="music"), snap={}, entry=None)
check("a music room audits nothing", res["checks"], [])

# an uncommitted room audits nothing
res = audit_room(record=rec(committed=False), snap=snap(), entry=entry())
check("an uncommitted room audits nothing", res["checks"], [])

# ── 6. missing diagnostics degrade to unknown, never to failure ────────────
res = audit_room(record=rec(), snap=snap(), entry=None)
opts = [by_id(res, i) for i in ("ip_control", "art_readback", "power_on_wol")]
check("unreadable entry -> option checks are unknown, not failed",
      all(c is None or c["ok"] is None for c in opts), True)

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
