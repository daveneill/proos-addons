"""
ProOS Core -- Watcher (awareness layer)
=======================================

A small, curated health layer that knows the difference between *asleep* and
*broken*. It does NOT monitor the whole entity registry -- it watches a declared
list of things that matter (the wedge: scope has a floor), holds an opinion about
what healthy looks like for each, and raises a fault only after a fault persists
past a debounce window. It remembers transitions, so it can say "resolved" -- the
one thing HA's sea of `unavailable` entities never tells you.

Two-signal diagnosis (Verify Don't Assume)
------------------------------------------
State alone can't tell you *why* something is unavailable. A watched item may
carry an independent liveness signal -- a `ping` binary_sensor, a router/UniFi
device_tracker, or a direct TCP probe -- supplied either inline on the watch
(`"reach": {"sensor": ...}` / `{"ip": ...}`) or via the add-on's `reachability`
map (keyed by entity, same one the room layer uses). When a fault clears the
debounce, the Watcher consults that signal and classifies the fault:

  * device answers the network  -> "integration"  (HA lost it; restart the
                                    integration -- do NOT power-cycle the device)
  * device does not answer       -> "offline"      (genuinely down; check power/net)

Debounce still gates *whether* a fault is real, so brief blips never raise; the
second signal only refines *what kind* of fault a sustained one is. Reads state
(and any sensor-type liveness entities) in one HA round trip per tick.

Wiring in server.py:
  from proos.watcher import Watcher
  _watcher = Watcher(_client, reachability=(_cfg or {}).get("reachability"))
  _watcher.run_forever(interval=5)
  # GET /watchers -> _watcher.report()
"""
from __future__ import annotations
import time
import threading
from datetime import datetime, timezone

from .reachability import tcp_reachable

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
#   guidance       what to check when it's genuinely OFFLINE, in plain language
#   reach          (optional) independent liveness signal for this item:
#                    {"sensor": "binary_sensor.x"} | {"ip": "1.2.3.4", "port": 7000}
#                  If absent, the add-on 'reachability' map (keyed by entity) is
#                  consulted. With no signal at all, behaviour is state-only.
#   guidance_wedged (optional) what to check when the device is REACHABLE but HA
#                  has lost it (integration wedged). Auto-synthesised if omitted.
# ---------------------------------------------------------------------------
WATCHES = [
    {
        # Watch the REMOTE, not the media_player: the media_player reports a
        # phantom power state. The remote's on/off is the trustworthy signal.
        # NB: this remote blips to 'unavailable' ~120ms every ~15min (pyatv
        # Companion reconnect) -- fault_after absorbs those ~96 daily blips.
        # The independent second signal (device IP) is derived automatically from
        # HA's registries by netmap and merged into the reachability map, so a
        # *sustained* fault is diagnosed as wedged-integration vs truly offline
        # with no per-home configuration here.
        "name": "Family Room Apple TV",
        "kind": "media",
        "entity": "remote.family_room_apple_tv",
        "healthy_when": "available",
        "ignore_states": ["paused"],
        "fault_after": 30,
        "guidance": "Lost network or powered off. Check Wi-Fi, then restart the "
                    "Apple TV integration in Home Assistant if it persists.",
        "guidance_wedged": "The Apple TV is online, but Home Assistant has lost "
                           "its connection to it. Restart the Apple TV "
                           "integration in Home Assistant -- no need to touch "
                           "the device.",
    },
    {
        "name": "Family Room Shield",
        "kind": "media",
        "entity": "remote.family_room_shield_tv",
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 30,
        "guidance": "Shield is unreachable. Check it's powered and on the network.",
    },
    {
        "name": "Family Room Sonos",
        "kind": "media",
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
        "kind": "camera",
        "entity": "camera.services_front_door_high_resolution_channel",
        "healthy_when": "available",
        "ignore_states": [],
        "fault_after": 60,
        "guidance": "Front door camera is offline. Check the camera, then the "
                    "UniFi Protect NVR (192.168.19.159) if other cameras are down too.",
    },
]

UNAVAILABLE = {"unavailable", "unknown", "none", "", None}
# Liveness-sensor states that mean "the device answered" (reachable = True).
_REACH_UP = {"on", "home"}
_REACH_DOWN = {"off", "not_home", "unavailable", "unknown"}

OK = "ok"
PENDING = "pending"   # unhealthy but within debounce -> amber pill
FAULT = "fault"       # unhealthy past debounce        -> red pill

# Fault verdicts (only meaningful once status == FAULT):
V_OFFLINE = "offline"          # device does not answer -> genuinely down
V_INTEGRATION = "integration"  # device answers -> HA lost it (wedged)


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class _Runtime:
    """Per-item runtime memory. This is what HA itself lacks."""
    __slots__ = ("status", "state", "unhealthy_since", "last_event",
                 "last_change", "reachable", "verdict")

    def __init__(self):
        self.status = OK
        self.state = None
        self.unhealthy_since = None
        self.last_event = None       # "raised" | "resolved" | None
        self.last_change = None      # epoch secs of last transition
        self.reachable = None        # True/False/None -- last liveness reading
        self.verdict = None          # V_OFFLINE / V_INTEGRATION when in FAULT


class Watcher:
    def __init__(self, client, watches=None, reachability=None, now=time.time):
        self.client = client                       # HAClient: .snapshot(entity_ids)
        self.watches = watches if watches is not None else WATCHES
        self.reach_map = reachability or {}         # {entity_id: {sensor|ip spec}}
        self.now = now
        self._rt = {w["entity"]: _Runtime() for w in self.watches}
        self._lock = threading.Lock()

    # -- signals ------------------------------------------------------------
    def _reach_spec(self, w):
        """Inline reach wins; else the config map keyed by entity; else None."""
        return w.get("reach") or self.reach_map.get(w["entity"])

    def _sensor_entities(self):
        """Every sensor-type liveness entity, so they ride the same snapshot."""
        ents = set()
        for w in self.watches:
            spec = self._reach_spec(w)
            if spec and spec.get("sensor"):
                ents.add(spec["sensor"])
        return ents

    def _resolve_reach(self, spec, states):
        """True/False/None. Sensor specs read from the batch snapshot (no extra
        round trip); ip specs probe directly (only called for real faults)."""
        if not spec:
            return None
        if spec.get("sensor"):
            st = (states.get(spec["sensor"]) or {}).get("state")
            if st in _REACH_UP:
                return True
            if st in _REACH_DOWN:
                return False
            return None
        if spec.get("ip"):
            try:
                return tcp_reachable(spec["ip"], int(spec.get("port", 7000)),
                                     float(spec.get("timeout", 1.0)))
            except Exception:
                return None
        return None

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
        """One evaluation pass over every watched item. One HA round trip
        (watched entities + any sensor-type liveness entities, batched)."""
        now = self.now()
        ents = [w["entity"] for w in self.watches]
        ents += [e for e in self._sensor_entities() if e not in ents]
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
                    rt.verdict = None
                    rt.reachable = None
                else:
                    if rt.unhealthy_since is None:
                        rt.unhealthy_since = now
                    if now - rt.unhealthy_since >= w.get("fault_after", 30):
                        # Past debounce: a real fault. Now diagnose *why* with
                        # the independent signal, if this item has one.
                        spec = self._reach_spec(w)
                        rt.reachable = self._resolve_reach(spec, states) if spec else None
                        rt.verdict = (V_INTEGRATION if rt.reachable is True
                                      else V_OFFLINE)
                        if rt.status != FAULT:
                            rt.last_event = "raised"
                            rt.last_change = now
                        rt.status = FAULT
                    else:
                        rt.status = PENDING
                        rt.verdict = None

    def _guidance(self, w, rt):
        if rt.status != FAULT:
            return None
        if rt.verdict == V_INTEGRATION:
            return w.get("guidance_wedged") or (
                f"{w['name']} is online, but Home Assistant has lost its "
                "connection. Restart its integration in Home Assistant -- the "
                "device itself is fine.")
        return w.get("guidance")

    def report(self):
        """Read-only payload for GET /watchers."""
        with self._lock:
            items, overall = [], OK
            first_fault = first_pending = first_verdict = None
            for w in self.watches:
                rt = self._rt[w["entity"]]
                is_fault = rt.status == FAULT
                items.append({
                    "name": w["name"],
                    "kind": w.get("kind"),
                    "has_signal": bool(self._reach_spec(w)),
                    "status": "amber" if rt.status == PENDING else rt.status,
                    "verdict": rt.verdict if is_fault else (
                        PENDING if rt.status == PENDING else OK),
                    "reachable": rt.reachable if is_fault else None,
                    "state": rt.state,
                    "guidance": self._guidance(w, rt),
                    "since": _iso(rt.last_change or rt.unhealthy_since),
                    "last_event": rt.last_event,
                })
                if is_fault:
                    overall = FAULT
                    if first_fault is None:
                        first_fault, first_verdict = w["name"], rt.verdict
                elif rt.status == PENDING and overall != FAULT:
                    overall = "amber"
                    first_pending = first_pending or w["name"]
            if overall == FAULT:
                tail = ("integration needs a restart"
                        if first_verdict == V_INTEGRATION else "needs attention")
                summary = f"{first_fault} -- {tail}"
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
