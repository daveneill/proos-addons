"""
ProOS Core — Watcher (awareness layer)
======================================

A small, curated health layer that knows the difference between *asleep* and
*broken*. It does NOT monitor the whole entity registry — it watches a declared
list of things that matter (the wedge: scope has a floor), holds an opinion about
what healthy looks like for each, and raises a fault only after a fault persists
past a debounce window. It remembers transitions, so it can say "resolved" — the
one thing Home Assistant's sea of `unavailable` entities never tells you.

Design seams (all injectable for testing):
  - read_state(entity_id) -> str|None   : how we read HA state
  - now() -> float                      : monotonic-ish wall clock (epoch seconds)

The web server calls `snapshot()` for the /health payload (read-only).
A background loop calls `tick()` on an interval. They are deliberately separate:
state-before-control — evaluation is pure, exposure is a read.
"""

import json
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# The watched list. THIS is the wedge. Edit it per job. Keep it short
# (10–15 items, not the registry). Awareness of a defined set is shippable;
# awareness of the universe is a manifesto.
#
# Fields:
#   name              friendly label — no entity, no IP, no protocol shown to user
#   entity            HA entity_id to read
#   healthy_when      "available"  -> healthy unless unavailable/unknown
#                     ["s1","s2"]  -> healthy only in these states (strict)
#   ignore_states     states that are ALWAYS treated as healthy. This is where
#                     hard-won knowledge lives: the Apple TV's phantom "paused"
#                     while asleep is NOT a fault.
#   fault_after       seconds the item must stay unhealthy before we raise (debounce)
#   guidance          the one thing to check, in plain language
# ---------------------------------------------------------------------------
WATCHES = [
    {
        # Watch the REMOTE, not the media_player: the media_player reports a
        # phantom power state (off/paused while actually awake). The remote's
        # on/off is the trustworthy signal. NB: this remote blips to
        # 'unavailable' for ~120ms every ~15min (pyatv Companion reconnect) —
        # the fault_after debounce below is what absorbs those ~96 daily blips.
        "name": "Family Room Apple TV",
        "entity": "remote.family_room_apple_tv",      # verified
        "healthy_when": "available",                  # on OR off are both fine
        "ignore_states": ["paused"],                  # defensive — never a fault
        "fault_after": 30,
        "guidance": "Lost network or powered off. Check Wi-Fi, then restart the "
                    "Apple TV integration in Home Assistant if it persists.",
    },
    {
        "name": "Family Room Shield",
        "entity": "remote.family_room_shield_tv",     # verified
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 30,
        "guidance": "Shield is unreachable. Check it's powered and on the network.",
    },
    {
        "name": "Family Room Sonos",
        "entity": "media_player.family_room",         # verified (dc=speaker)
        "healthy_when": "available",                  # 'paused' is available — fine
        "ignore_states": [],
        "fault_after": 30,
        "guidance": "Sonos dropped off the network. Check power and Wi-Fi/Ethernet.",
    },
    {
        # Single anchor camera as a proxy for "is Protect/NVR up". A proper
        # "N-of-M cameras online" aggregate is the next step once watches can
        # be template-based; for now this honestly watches one camera.
        "name": "Front Door Camera",
        "entity": "camera.services_front_door_high_resolution_channel",  # verified
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 60,                            # cameras: longer debounce
        "guidance": "Front door camera is offline. Check the camera, then the "
                    "UniFi Protect NVR (192.168.19.159) if other cameras are down too.",
    },
]

# States that mean "the thing is gone", regardless of healthy_when.
UNAVAILABLE = {"unavailable", "unknown", "none", "", None}

# Status vocabulary
OK = "ok"
PENDING = "pending"   # unhealthy but within the debounce window  -> amber pill
FAULT = "fault"       # unhealthy past the debounce window         -> red pill


# ---------------------------------------------------------------------------
# Default state reader: HA REST via the Supervisor proxy. Zero extra deps.
# An add-on gets SUPERVISOR_TOKEN injected; the proxy is reachable at
# http://supervisor/core/api/states/<entity_id>.
# ---------------------------------------------------------------------------
def make_supervisor_reader(token, base="http://supervisor/core/api", timeout=5):
    def read_state(entity_id):
        url = f"{base}/states/{entity_id}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
                return data.get("state")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None          # entity doesn't exist -> treated as a fault
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            return None              # can't reach HA -> treated as unhealthy, debounced
    return read_state


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class _Runtime:
    """Per-item runtime memory. This is what HA itself lacks."""
    __slots__ = ("status", "state", "unhealthy_since", "last_event", "last_change")

    def __init__(self):
        self.status = OK
        self.state = None
        self.unhealthy_since = None   # epoch secs of first unhealthy read in current streak
        self.last_event = None        # "raised" | "resolved" | None
        self.last_change = None       # epoch secs of last raised/resolved transition


class Watcher:
    def __init__(self, watches=None, read_state=None, now=time.time):
        self.watches = watches if watches is not None else WATCHES
        self.read_state = read_state          # injected; required before tick()
        self.now = now
        self._rt = {w["entity"]: _Runtime() for w in self.watches}
        self._lock = threading.Lock()

    # -- pure evaluation of one item -------------------------------------
    @staticmethod
    def _is_healthy(w, state):
        # ignore_states wins over everything — this is the phantom-state escape hatch
        if state in w.get("ignore_states", ()):
            return True
        if state in UNAVAILABLE:
            return False
        hw = w.get("healthy_when", "available")
        if hw == "available":
            return True
        if isinstance(hw, (list, tuple, set)):
            return state in hw
        return state == hw

    def tick(self):
        """One evaluation pass over every watched item. Cheap. Idempotent."""
        if self.read_state is None:
            raise RuntimeError("Watcher.read_state not set")
        now = self.now()
        with self._lock:
            for w in self.watches:
                ent = w["entity"]
                rt = self._rt[ent]
                state = self.read_state(ent)
                rt.state = state
                healthy = self._is_healthy(w, state)

                if healthy:
                    if rt.status == FAULT:
                        rt.last_event = "resolved"
                        rt.last_change = now
                    rt.unhealthy_since = None
                    rt.status = OK
                else:
                    if rt.unhealthy_since is None:
                        rt.unhealthy_since = now
                    elapsed = now - rt.unhealthy_since
                    if elapsed >= w.get("fault_after", 30):
                        if rt.status != FAULT:
                            rt.last_event = "raised"
                            rt.last_change = now
                        rt.status = FAULT
                    else:
                        rt.status = PENDING

    # -- read-only exposure for /health ----------------------------------
    def snapshot(self):
        with self._lock:
            items = []
            overall = OK
            first_fault = None
            first_pending = None
            for w in self.watches:
                rt = self._rt[w["entity"]]
                is_fault = rt.status == FAULT
                items.append({
                    "name": w["name"],
                    "status": "amber" if rt.status == PENDING else rt.status,
                    "state": rt.state,
                    "guidance": w["guidance"] if is_fault else None,
                    "since": _iso(rt.last_change or rt.unhealthy_since),
                    "last_event": rt.last_event,
                })
                if is_fault:
                    overall = FAULT
                    first_fault = first_fault or w["name"]
                elif rt.status == PENDING and overall != FAULT:
                    overall = "amber"
                    first_pending = first_pending or w["name"]

            if overall == FAULT:
                summary = f"{first_fault} — needs attention"
            elif overall == "amber":
                summary = f"{first_pending} — checking…"
            else:
                summary = "All systems nominal"

            return {"status": overall, "summary": summary, "items": items}

    # -- background loop --------------------------------------------------
    def run_forever(self, interval=5):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:  # never let the loop die
                    print(f"[watcher] tick error: {e}", flush=True)
                time.sleep(interval)
        t = threading.Thread(target=loop, name="proos-watcher", daemon=True)
        t.start()
        return t
