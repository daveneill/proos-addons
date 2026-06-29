"""
ProOS Core -- Watcher (awareness layer)
=======================================

A small, curated health layer that knows the difference between *asleep* and
*broken*. It does NOT monitor the whole entity registry -- it watches a declared
list of things that matter (the wedge: scope has a floor), holds an opinion about
what healthy looks like for each, and raises a fault only after a fault persists
past a debounce window. It remembers transitions, so it can say "resolved" -- the
one thing HA's sea of `unavailable` entities never tells you.

Reads state through the existing HAClient (one round trip per tick via
client.snapshot), so it inherits add-on vs cloud transparently like the rest of
Core. Distinct from Monitor (which is per-room reconcile state); this is a flat
device-availability view.

Wiring in server.py:
  from proos.watcher import Watcher
  _watcher = Watcher(_client); _watcher.run_forever(interval=5)
  # GET /watchers -> _watcher.report()
"""
from __future__ import annotations
import time
import threading
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# The watched list. THIS is the wedge. Edit per job. Keep it short
# (10-15 items, not the registry). Entity IDs below are VERIFIED against the
# Family Room instance.
#
#   name           friendly label -- no entity, no IP, no protocol shown to user
#   entity         HA entity_id to read
#   healthy_when   "available" -> healthy unless unavailable/unknown
#                  ["s1","s2"] -> healthy only in these states (strict)
#   ignore_states  states ALWAYS treated as healthy (phantom-state escape hatch)
#   fault_after    seconds unhealthy before raising (debounce)
#   guidance       the one thing to check, in plain language
# ---------------------------------------------------------------------------
WATCHES = [
    {
        # Watch the REMOTE, not the media_player: the media_player reports a
        # phantom power state. The remote's on/off is the trustworthy signal.
        # NB: this remote blips to 'unavailable' ~120ms every ~15min (pyatv
        # Companion reconnect) -- fault_after absorbs those ~96 daily blips.
        # (A ping sensor via config 'reachability' would be even steadier --
        #  future upgrade: prefer the mapped sensor when present.)
        "name": "Family Room Apple TV",
        "entity": "remote.family_room_apple_tv",
        "healthy_when": "available",
        "ignore_states": ["paused"],
        "fault_after": 30,
        "guidance": "Lost network or powered off. Check Wi-Fi, then restart the "
                    "Apple TV integration in Home Assistant if it persists.",
    },
    {
        "name": "Family Room Shield",
        "entity": "remote.family_room_shield_tv",
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 30,
        "guidance": "Shield is unreachable. Check it's powered and on the network.",
    },
    {
        "name": "Family Room Sonos",
        "entity": "media_player.family_room",      # dc=speaker; 'paused' is fine
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 30,
        "guidance": "Sonos dropped off the network. Check power and Wi-Fi/Ethernet.",
    },
    {
        # Single anchor camera as a proxy for "is Protect/NVR up". A proper
        # "N-of-M cameras online" aggregate is the next step.
        "name": "Front Door Camera",
        "entity": "camera.services_front_door_high_resolution_channel",
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 60,
        "guidance": "Front door camera is offline. Check the camera, then the "
                    "UniFi Protect NVR (192.168.19.159) if other cameras are down too.",
    },
]

UNAVAILABLE = {"unavailable", "unknown", "none", "", None}

OK = "ok"
PENDING = "pending"   # unhealthy but within debounce -> amber pill
FAULT = "fault"       # unhealthy past debounce        -> red pill


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
        self.unhealthy_since = None
        self.last_event = None       # "raised" | "resolved" | None
        self.last_change = None      # epoch secs of last transition


class Watcher:
    def __init__(self, client, watches=None, now=time.time):
        self.client = client                       # HAClient: .snapshot(entity_ids)
        self.watches = watches if watches is not None else WATCHES
        self.now = now
        self._rt = {w["entity"]: _Runtime() for w in self.watches}
        self._lock = threading.Lock()

    @staticmethod
    def _is_healthy(w, state):
        if state in w.get("ignore_states", ()):     # phantom escape hatch wins
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
        """One evaluation pass over every watched item. One HA round trip."""
        now = self.now()
        ents = [w["entity"] for w in self.watches]
        try:
            states = self.client.snapshot(ents)     # {eid: {state, attributes, ...}}
        except Exception as e:
            print(f"[watcher] snapshot failed: {e}", flush=True)
            return
        with self._lock:
            for w in self.watches:
                ent = w["entity"]
                rt = self._rt[ent]
                state = (states.get(ent) or {}).get("state")
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
                    if now - rt.unhealthy_since >= w.get("fault_after", 30):
                        if rt.status != FAULT:
                            rt.last_event = "raised"
                            rt.last_change = now
                        rt.status = FAULT
                    else:
                        rt.status = PENDING

    def report(self):
        """Read-only payload for GET /watchers (named to avoid clashing with
        the HAClient's own snapshot())."""
        with self._lock:
            items, overall = [], OK
            first_fault = first_pending = None
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
                summary = f"{first_fault} -- needs attention"
            elif overall == "amber":
                summary = f"{first_pending} -- checking..."
            else:
                summary = "All systems nominal"
            return {"status": overall, "summary": summary, "items": items}

    def run_forever(self, interval=5):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    print(f"[watcher] tick error: {e}", flush=True)
                time.sleep(interval)
        t = threading.Thread(target=loop, name="proos-watcher", daemon=True)
        t.start()
        return t
