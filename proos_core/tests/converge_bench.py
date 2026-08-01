"""
Intent-convergence bench — run: python3 tests/converge_bench.py

THE PRODUCT RULE (Dave, standing, restated 1 Aug 2026)
------------------------------------------------------
CEC stays ON. Manufacturers ship it on and every reset turns it back on, so a
product that requires it off is fighting the tide — that is Control4's answer
because their control is fire-and-forget. ProOS is STATE-BASED: it knows what
the room was set to, so when CEC (or anything) yanks a device away, ProOS
pulls the room back to the committed state.

MOTIVATING FAILURE (recorder, 1 Aug 09:01:53–09:02:16 — measured)
-----------------------------------------------------------------
Watch Apple TV fired; 4 s later the Marantz yanked itself to Blu-ray (CEC
reacting to the Shield rest step); the verdict truthfully went VERIFIED
watch_shield_2; the card showed Shield until the amp settled. The existing
converger could not help twice over: it stands down on VERIFIED verdicts, and
its only intent signal (display input) is useless in an AVR room where every
source shares HDMI 3.

THE RULES BEING PINNED
----------------------
1. The strongest intent signal is the activity script the user JUST FIRED.
   Within the intent window (180 s), if the room's verdict contradicts that
   intent, the converger re-fires the intent script — even against a
   "verified" verdict: verified means the evidence is real, not that it is
   what the user asked for.
2. TV Off is an intent too: a room that pops back on inside the window gets
   pulled back off (the Bedroom's "turned itself back on" case).
3. OUTSIDE the window, reality wins: a user who walks up and changes the room
   by hand is respected. ProOS converges to ITS OWN commands, never against
   a human's later choice.
4. A still-RUNNING intent script is hands-off; throttle one attempt per room
   per 120 s; everything journalled loudly (silent repair is how trust dies).
5. The evidence-based fallback (unverified + unambiguous route) survives for
   rooms with no recent intent.
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proos.ctlbridge import ActivityPublisher                 # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(f"{name}: wanted {want!r} got {got!r}")
        print("FAIL  " + name + f"  wanted {want!r} got {got!r}")


ATV_S = "script.proos_living_room_watch_apple_tv"
SHD_S = "script.proos_living_room_watch_shield"
OFF_S = "script.proos_living_room_tv_off"


def act(key, script):
    a = types.SimpleNamespace()
    a.key, a._script = key, script
    a.route = {"select_source": "HDMI 3"}
    a.source_eid = "media_player.x_" + key
    a.summary = lambda snap: types.SimpleNamespace(ok=False)
    return a


A_ATV = act("watch_apple_tv", ATV_S)
A_SHD = act("watch_shield", SHD_S)
A_OFF = act("tv_off", OFF_S)


def mkpub():
    fired = []
    client = types.SimpleNamespace(call_service=lambda d, s, e: fired.append(e))
    ctrl = types.SimpleNamespace(client=client,
                                 _script_entity_for=lambda a: a._script,
                                 activities={"tv_off": A_OFF})
    pub = ActivityPublisher(types.SimpleNamespace(_req=lambda *a, **k: {},
                                                  render_template=lambda *a, **k: "[]"),
                            types.SimpleNamespace(load=lambda: {}),
                            lambda room: ctrl, enabled=lambda: True,
                            witnesses={})
    return pub, ctrl, fired


def snap(*, atv_fired_ago=None, shd_fired_ago=None, off_fired_ago=None,
         running=None):
    now = datetime.now(timezone.utc)
    s = {}
    for eid, ago in ((ATV_S, atv_fired_ago), (SHD_S, shd_fired_ago),
                     (OFF_S, off_fired_ago)):
        attrs = {}
        if ago is not None:
            attrs["last_triggered"] = (now - timedelta(seconds=ago)).isoformat()
        s[eid] = {"state": "on" if eid == running else "off",
                  "attributes": attrs}
    return s


def converge(pub, ctrl, *, active, verified, disp_src="HDMI 3", snapd=None):
    pub._maybe_converge("living_room", ctrl, [A_ATV, A_SHD], None,
                        snapd or {}, active, verified, disp_src, False)


# ── 1. THE CEC YANK: verified-but-wrong is pulled back inside the window ───
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True,
         snapd=snap(atv_fired_ago=30))
check("verified wrong-source verdict is pulled back to the fired intent",
      fired, [ATV_S])

# ── 2. verdict matching the intent does nothing ────────────────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_ATV, verified=True,
         snapd=snap(atv_fired_ago=30))
check("a room already on its intent is left alone", fired, [])

# ── 3. OUTSIDE the window reality wins ─────────────────────────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True,
         snapd=snap(atv_fired_ago=600))
check("beyond the intent window a verified room is respected", fired, [])

# ── 4. the LATEST fired script is the intent ───────────────────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_ATV, verified=True,
         snapd=snap(atv_fired_ago=100, shd_fired_ago=20))
check("a newer fired intent (shield) wins over an older one",
      fired, [SHD_S])

# ── 5. TV OFF is an intent: the Bedroom pop-back-on case ───────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True,
         snapd=snap(off_fired_ago=30))
check("a room that pops back on inside the off-intent window is pulled off",
      fired, [OFF_S])

# ── 6. a still-running intent script is hands-off ──────────────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True,
         snapd=snap(atv_fired_ago=10, running=ATV_S))
check("a running intent script is never re-fired", fired, [])

# ── 7. throttle: one attempt per room per 120 s ────────────────────────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True, snapd=snap(atv_fired_ago=30))
converge(pub, ctrl, active=A_SHD, verified=True, snapd=snap(atv_fired_ago=40))
check("second attempt inside the throttle window is suppressed",
      fired, [ATV_S])

# ── 8. no recent intent -> the old evidence path still stands down on
#      verified, and still converges an unverified unambiguous room ─────────
pub, ctrl, fired = mkpub()
converge(pub, ctrl, active=A_SHD, verified=True, snapd=snap())
check("no intent + verified -> untouched (old behaviour)", fired, [])

pub, ctrl, fired = mkpub()
A_ATV2 = act("watch_apple_tv", ATV_S)
A_ATV2.route = {"select_source": "HDMI 2"}
pub._maybe_converge("living_room", ctrl, [A_ATV2, A_SHD], None,
                    snap(), None, False, "HDMI 2", False)
check("no intent + unverified + unambiguous route -> old path converges",
      fired, [ATV_S])

print(f"\n{PASS} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAIL", f)
sys.exit(1 if FAIL else 0)
