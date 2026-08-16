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
from . import discovery
from . import roomdevices as _roomdev

# ---------------------------------------------------------------------------
# Watch-dict schema (reference only). The live watch list is derived ENTIRELY
# from HA's registries by discover_watches() below -- there is NO hardcoded list.
# WATCHES is intentionally EMPTY: if discovery finds nothing watchable, awareness
# watches nothing (an empty home is not a fault, and must never invent devices).
# Each watch that discover_watches emits carries these fields:
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
# Intentionally empty — the watch list is built live from HA's registries by
# discover_watches(). Nothing is hardcoded, so awareness can never invent a device
# that isn't really configured on this box.
WATCHES = []

UNAVAILABLE = {"unavailable", "unknown", "none", "", None}
# Liveness-sensor states that mean "the device answered" (reachable = True).
_REACH_UP = {"on", "home"}
# A-6 (Dave's ruling, 16 Aug 2026). These are the states in which the witness
# makes a POSITIVE STATEMENT that the device is not on the network. `not_home`
# is a UniFi controller saying "this client is not associated"; `off` is a
# connectivity sensor saying the integration has lost its hold. Both are
# evidence.
_REACH_DOWN = {"off", "not_home"}
# ...and these are the states in which the witness says NOTHING. It has not
# testified that the device is gone; it has failed to testify at all.
#
# `unavailable` and `unknown` used to live in _REACH_DOWN, and the difference
# matters enormously on a real box. Read on Dave's home, 16 Aug: ALL 111 device
# trackers come from ONE UniFi config entry, and ProOS deliberately prefers that
# controller's view over a TCP probe for every watched device. So the moment
# that entry unloads — a controller reboot, a firmware update, a failed reauth,
# a changed password — every witness in the house goes `unavailable` in the same
# instant. Reading that as "the device is gone" turned one dead integration into
# a red offline fault on every television in the home, each one telling the
# installer to go and check a power cable that was never unplugged.
#
# That is Shape 2 — an empty answer read as a real answer — for the fourth time
# (registers 143, 148, 151). The witness layer going quiet is now handled where
# it belongs: as ONE named fault about the integration, raised by healthmon's
# witness_blackout check, which reads the mute list this module publishes.
_REACH_MUTE = {"unavailable", "unknown", "none", ""}
# ...and the two sets must never overlap. Caught while packaging 1.0.423:
# _REACH_MUTE was DEFINED and never consulted — the guard was a comment
# wearing a constant's clothes, and moving "unavailable" back into
# _REACH_DOWN would have silently undone Dave's ruling with both sets still
# sitting there looking correct. `_resolve_reach` now consults it explicitly,
# and this assertion makes the contradiction impossible to ship: a state
# cannot be both a positive statement and a silence.
assert not (_REACH_UP & _REACH_DOWN) and not (_REACH_DOWN & _REACH_MUTE) \
    and not (_REACH_UP & _REACH_MUTE), \
    "a witness state cannot be both evidence and silence"

# CLASS FACT (Dave, 9 Aug 2026 — brand-agnostic, known from the INTEGRATION at
# the moment a device is added, never from a brand string): panels legitimately
# LEAVE the network when powered down — WoL/MAC is what wakes them, so their
# radios sleep in standby. Off + witness-gone is RESTING for a display. Every
# other AV class (streamers, audio, lighting, climate) stays connected when
# "off", so witness-gone remains a fault for them. The kind here is derived
# from the integration's device class when the watch is built — a new brand of
# TV gets this behaviour with no code change, exactly like CEC auto-wake.
#
# RESTATED BY DAVE, 16 AUG 2026, while A-8 was being built: *"We said a while
# back that TVs use a MAC address to turn on, as when they are off the network
# port is asleep."* Still true, and **A-8 does not weaken it**. A TV that is
# off on a HEALTHY network is resting, exactly as before — its port is asleep,
# that is normal and expected, and ProOS stays silent about it. A dark TV never
# raises anything on its own account.
#
# What A-8 adds is the precondition that was always implied and never written
# down: this exemption is a statement about a panel that left the network ON
# ITS OWN. It cannot also cover a panel whose switch has been unplugged,
# because from here the two look identical — a sleeping port and a dead port
# both refuse the probe — and answering "normal" is simply picking the more
# comfortable of two indistinguishable explanations. See _offnet_ok in tick().
OFFNET_WHEN_OFF = ("display",)

# CLASS FACT (Dave, 9 Aug 2026): LIGHTING lives on physical switches — a smart
# bulb switched off at the wall or on the lamp itself loses ALL power, so its
# radio dies: unavailable + witness-gone, the exact shape of a dead device —
# and it happens every day, on purpose. The evidence cannot tell wall-switch
# from failure, so the honest calm answer is "no power" (standby), never a red
# fault. Nothing is hidden: the pill and device_liveness still show the state.
# A bulb that is wedged (unavailable but witness PRESENT) or contradicted
# (believed-on but witness GONE) still faults. Brand-agnostic, by class.
NO_POWER_IS_NORMAL = ("lighting",)

OK = "ok"
PENDING = "pending"   # unhealthy but within debounce -> amber pill
FAULT = "fault"       # unhealthy past debounce        -> red pill
STANDBY = "standby"   # power-aware device that's simply off -> calm, not a fault

# Fault verdicts (only meaningful once status == FAULT):
V_OFFLINE = "offline"          # device does not answer -> genuinely down
V_INTEGRATION = "integration"  # device answers -> HA lost it (wedged)
# A-8 (Dave's switch test, 16 Aug 2026). The network path itself is broken, so
# ProOS cannot tell a resting panel from a cut-off one — and says exactly that
# rather than picking whichever answer is more comfortable.
V_NOPATH = "no_path"


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class _Runtime:
    """Per-item runtime memory. This is what HA itself lacks."""
    __slots__ = ("status", "state", "unhealthy_since", "last_event",
                 "last_change", "reachable", "verdict", "asked",
                 "recovery", "recovery_at", "recovery_n")

    def __init__(self):
        self.status = OK
        self.state = None
        self.unhealthy_since = None
        self.last_event = None       # "raised" | "resolved" | None
        self.last_change = None      # epoch secs of last transition
        self.reachable = None        # True/False/None -- last liveness reading
        # Did ProOS actually CONSULT the second signal this pass? A record of
        # what was done, not a claim about what kind of device this is. It is
        # the difference between "asked and got nothing" and "never asked",
        # and the surfaces must not merge them.
        #
        # THREE states, not two — the gate caught this the moment it was
        # written as a boolean. None means NO TICK HAS RUN YET, which is not
        # the same as "deliberately skipped": a watcher that has not started is
        # simply unknown, and counting it as confirmed would re-open the 8 Aug
        # lie this whole layer exists to close.
        self.asked = None
        self.verdict = None          # V_OFFLINE / V_INTEGRATION when in FAULT
        self.recovery = None         # None|attempting|recovered|failed|needs_approval
        self.recovery_at = None      # epoch of last recovery attempt
        self.recovery_n = 0          # attempts this fault episode


class Watcher:
    def __init__(self, client, watches=None, reachability=None, now=time.time,
                 recover_fn=None, audit_path="/data/watcher_audit.log"):
        self.client = client                       # HAClient: .snapshot(entity_ids)
        self.watches = watches if watches is not None else WATCHES
        self.reach_map = reachability or {}         # {entity_id: {sensor|ip spec}}
        self.now = now
        self.recover_fn = recover_fn                # callable(entity, action)->bool; server owns the HOW
        self.audit_path = audit_path                # tiered recovery is always audited
        self._rt = {w["entity"]: _Runtime() for w in self.watches}
        self._lock = threading.Lock()
        self._reach_cache = {}          # {entity: (ts, bool|None)} -- bound probing
        self._reach_ttl = 15.0          # seconds; an off device isn't re-probed each tick
        # A-6: {entity: sensor_eid} for every watch whose SENSOR witness was
        # consulted this pass and said nothing. Published on report() so
        # healthmon can tell "one integration went quiet" from "one device
        # died" — the whole point of Dave's ruling. Rebuilt every tick, so it
        # is always this sweep's truth and never a memory of an old outage.
        self._mute = {}
        # STAGE 1 BUILD 2 (16 Aug 2026): event-driven evaluation. The HA
        # stream pokes this the moment a WATCHED entity (or its witness
        # sensor) changes, and run_forever's wait returns immediately instead
        # of sleeping out the interval. The interval survives as the
        # reconciliation sweep, so with no stream the old cadence stands
        # unchanged — fail-open. Pokes landing during an evaluation coalesce
        # into ONE follow-up tick (the Event is a latch, not a queue).
        self._wake = threading.Event()
        self._watched_cache = frozenset(
            [w["entity"] for w in self.watches]
            + [s for s in self._sensor_entities()])

    def _port_of(self, entity):
        """The PHYSICAL switch-port reading for a watched device, or None.

        Step 2 of the rule inventory (Dave's law, 16 Aug 2026): before a rule
        can be deleted, the fact that replaces it has to be OBSERVED. This
        carries the reading — link, enable, speed, PoE watts, and whether the
        gear itself is online — so the question `OFFNET_WHEN_OFF` stands in for
        can be answered from a real home instead of from my prediction about
        how a television's network port behaves in standby.

        **No verdict reads this yet, deliberately.** Replacing a guess with a
        guess about hardware would be the same mistake in a new hat.

        None means ProOS could not establish it: no controller configured, a
        wireless device, an unknown client, or gear not reporting. It never
        means "no port"."""
        fn = getattr(self, "port_fn", None)
        if not fn:
            return None
        try:
            return fn(entity)
        except Exception:                                        # noqa: BLE001
            return None

    def _gear_down(self):
        """Network gear ProOS currently believes is offline (A-8). Injected by
        the server as `gear_down_fn` — healthmon owns infrastructure, so the
        watcher asks it rather than growing a second opinion about the network.
        Fail-open to nothing: with no answer, ProOS has no evidence the network
        is broken, which is the assumption that changes the least."""
        fn = getattr(self, "gear_down_fn", None)
        if not fn:
            return []
        try:
            return list(fn() or [])
        except Exception:                                        # noqa: BLE001
            return []

    def set_watches(self, watches, allow_empty=False):
        """Swap the watch list at runtime (auto re-discovery) while preserving the
        _Runtime of any entity still watched -- so a device mid-fault keeps its
        debounce/verdict/since across a refresh. New entities start fresh; removed
        ones are dropped.

        An empty list is ignored by default (a transient empty discovery must not
        wipe live watches). Pass allow_empty=True to force-clear -- e.g. after a
        factory reset, when every watched device is genuinely gone and the state
        aura must not stay red on devices that no longer exist."""
        if not watches and not allow_empty:
            return
        watches = watches or []
        with self._lock:
            self.watches = watches
            self._rt = {w["entity"]: (self._rt.get(w["entity"]) or _Runtime())
                        for w in watches}

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
        round trip); ip specs probe directly (only called for real faults).

        None is not a failure of this function — it is the honest answer when
        the witness has not spoken (A-6). Every caller already treats None as
        "no evidence" and fails open; what changed on 16 Aug is only WHICH
        states arrive here as None."""
        if not spec:
            return None
        if spec.get("sensor"):
            st = (states.get(spec["sensor"]) or {}).get("state")
            if st in _REACH_UP:
                return True
            if st in _REACH_DOWN:
                return False
            if st in _REACH_MUTE:
                return None                # A-6: silence, not a verdict
            return None                    # a state we do not recognise at all
        if spec.get("ip"):
            try:
                return tcp_reachable(spec["ip"], int(spec.get("port", 7000)),
                                     float(spec.get("timeout", 1.0)))
            except Exception:
                return None
        return None

    def _reach_cached(self, w, spec, states, now):
        """Sensor specs read from the batch snapshot (free); IP probes are cached
        for _reach_ttl so an off/power-aware device isn't probed every tick."""
        if spec.get("sensor"):
            return self._resolve_reach(spec, states)
        if spec.get("ip"):
            ent = w["entity"]
            c = self._reach_cache.get(ent)
            if c and (now - c[0]) < self._reach_ttl:
                return c[1]
            val = self._resolve_reach(spec, states)
            self._reach_cache[ent] = (now, val)
            return val
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

    def _awareness_excluded(self, now):
        """Installer exclusions (Room Devices toggle), cached 10s so the tick
        loop doesn't re-read the store every pass. Fail-open."""
        c = getattr(self, "_excl_cache", None)
        if c and (now - c[0]) < 10:
            return c[1]
        try:
            excl = _roomdev.awareness_excluded()
        except Exception:
            excl = set()
        self._excl_cache = (now, excl)
        return excl

    def tick(self):
        """One evaluation pass over every watched item. One HA round trip
        (watched entities + any sensor-type liveness entities, batched)."""
        now = self.now()
        _excl = self._awareness_excluded(now)
        ents = [w["entity"] for w in self.watches]
        ents += [e for e in self._sensor_entities() if e not in ents]
        # Keep the wake filter current: watches and reach_map both change at
        # runtime (rediscovery, reach merges), and a stale filter would make
        # the stream deaf to a newly watched device.
        self._watched_cache = frozenset(ents)
        try:
            states = self.client.snapshot(ents)     # {eid: {state, attributes, ...}}
        except Exception as e:
            print(f"[watcher] snapshot failed: {e}", flush=True)
            return
        with self._lock:
            for w in self.watches:
                ent = w["entity"]
                if ent in _excl:
                    # Excluded by the installer: not evaluated, not counted.
                    # It reappears in report() under excluded_items — visible,
                    # never silently absent.
                    continue
                rt = self._rt[ent]
                state = (states.get(ent) or {}).get("state")
                rt.state = state
                state_healthy = self._is_healthy(w, state)
                pa = bool(w.get("power_aware"))
                spec = self._reach_spec(w)
                # Verify Don't Assume: a device with a liveness signal that
                # doesn't answer the network is NOT healthy, whatever HA reports —
                # integration state lags, goes stale, or settles to 'off' within
                # seconds of a real outage. The independent probe is ground truth.
                #
                # THE SWITCH TEST (Dave, 8 Aug 2026 — watcher_liveness_bench):
                # power-aware devices USED to be exempt here ("off + unreachable
                # is a normal resting state"), so when the switch feeding the
                # Bedroom died and everything settled to 'off', the second signal
                # was bound but never consulted and Health said All Systems
                # Normal. The exemption is retired: a switched-off device
                # normally STAYS on the network, so off + witness-GONE is a
                # fault, off + witness-PRESENT is a normal off room, and no
                # witness / witness-unknown fails open exactly as before.
                live_reachable = None
                # Don't let the liveness probe DOWNGRADE a camera that HA already
                # reports as up. A UniFi/Protect camera's state comes from the NVR
                # (recording/idle = genuinely connected), so it can't be secretly
                # offline the way an Apple TV can — and the generic TCP probe uses a
                # streaming-box port a camera never answers, which was flagging an
                # online (recording) camera as offline. NOTE: this only skips the
                # probe while the camera is HEALTHY; the two-signal wedge test still
                # runs below when a camera IS unavailable (reachable -> integration
                # wedged, unreachable -> genuinely offline).
                _cam_up = (w.get("kind") == "camera") and state_healthy
                rt.asked = bool(spec) and not _cam_up
                if spec and not _cam_up:
                    live_reachable = self._reach_cached(w, spec, states, now)
                    # A-6: a SENSOR witness we actually asked, that said
                    # nothing. Only sensor specs are recorded — an ip probe
                    # returning None is a network timeout, which is a fact
                    # about this one device, not about an integration. A
                    # camera we deliberately did not probe is not mute either;
                    # nobody asked it.
                    if spec.get("sensor") and live_reachable is None:
                        self._mute[ent] = spec["sensor"]
                    else:
                        self._mute.pop(ent, None)
                else:
                    self._mute.pop(ent, None)
                # Panels may leave the network when powered down (class fact
                # above) — off + witness-gone is resting for THEM only. The
                # witness still rules a panel that is unavailable or believed-on.
                #
                # A-8 — THE EXEMPTION HAS A PRECONDITION, AND IT WAS MISSING.
                # Dave's switch test, 16 Aug: he pulled the switch feeding the
                # Bedroom. The Bedroom TV's witness got it RIGHT — the probe
                # failed, the device was gone. ProOS threw that away, because a
                # display that is off and off-network is "resting". On the same
                # Health page, the infrastructure card named that very TV in its
                # list of devices the dead switch had taken down.
                #
                # ProOS held both facts and let the comfortable one win. That is
                # Shape 1 — one question answered in two places — and it is why
                # the 9 Aug claim that "the switch test still escalates" was
                # wrong: the room-level escalation needs EVERY witnessed device
                # lost, and a stale tracker on one box is enough to stop it.
                #
                # The class fact is still true: a panel really does leave the
                # network in standby. But it is only true WHEN THE NETWORK IS
                # UP. A panel is resting if it left the network on its own; if
                # the switch feeding the house is down, ProOS cannot tell
                # resting from cut off, and the honest verdict is neither
                # "normal" nor "offline" — it is "no network path".
                # ── THE MEASUREMENT REPLACES THE CLASS FACT ────────────────
                # Dave, 16 Aug 2026, after the port readings went live on his
                # box: deliver what you say you are going to deliver. This is
                # the delivery.
                #
                # When ProOS can SEE this device's own switch port and that
                # switch is healthy, no opinion about what kind of device it is
                # is needed. Two independent sources agree:
                #
                #   the integration says the device is off
                #   the switch says its port is there, enabled, and the switch
                #     itself is fine
                #
                # A device reporting itself off, on a port that exists, on a
                # switch that is up, is off. That is arithmetic over two facts
                # — no class list, no brand, no scenario. A television, a
                # streamer and an amplifier are all judged identically.
                _port = self._port_of(ent)
                # ── D-1 · THE GEAR QUESTION IS PER-DEVICE (failure record,
                # 16 Aug 2026). `_gear` above is the HOUSE-WIDE list, and using
                # it to judge single devices accused a Living Room TV of a
                # Bedroom switch failure. Where ProOS can see THIS device's own
                # switch, that reading decides — and per Dave's ruling the same
                # morning, it outranks a stale tracker: his live test showed
                # UniFi keeps saying 'home' for ~10 minutes after a switch
                # dies, and for that whole window the row lied "normal ·
                # confirmed two ways" beside "switch OFFLINE" on one line.
                #
                #     "Switch wins. If the device's own switch is offline, a
                #      stale tracker can't confirm anything."
                #
                # AND THE HOUSE-WIDE LIST IS GONE FROM DEVICE VERDICTS
                # ENTIRELY (1.0.435, same day). 1.0.434 kept it as a
                # "fallback" for devices with no port reading — and within the
                # hour it accused the Living Room TV a third time while HA's
                # own activity log showed nothing for that device. Dave:
                #
                #     "if it was reading actual not virtual it would not be
                #      able to be wrong" · "we agreed no rules and it kept
                #      writing another rule"
                #
                # A fallback that states something unmeasured is a rule. A
                # device with no port reading is judged on ITS OWN readings
                # only. The dead switch is still reported — healthmon's infra
                # card and monitor's room card read the SWITCH itself — but no
                # device verdict borrows house-wide evidence ever again.
                # Benched: watcher_scoped_gear_bench §5, red-first.
                _my_gear_down = bool(_port) and _port.get("gear_online") is False
                _switch_seen = bool(_port) and _port.get("gear_online") is not False
                if _my_gear_down:
                    # A-8's precondition, now per-device: with its own switch
                    # dead, ProOS cannot tell resting from cut off — no
                    # exemption, no "normal".
                    _offnet_ok = False
                elif _switch_seen:
                    _offnet_ok = state in ("off", "standby")
                else:
                    # NOTHING TO MEASURE. No controller, no remembered port, or
                    # the switch itself is not reporting. `OFFNET_WHEN_OFF` now
                    # does ONE job and only this one: the fallback for a home
                    # where the port cannot be read at all. It is no longer the
                    # answer — it is what is left when there is no answer, which
                    # is case ① of the rule inventory (no tool exists) rather
                    # than case ② (a tool exists and was not used).
                    # 1.0.435: the `and not _gear` condition that used to hang
                    # off this line is deleted — it judged this device on the
                    # whole house's gear, which is how the Living Room TV was
                    # accused of a Bedroom switch three times.
                    _offnet_ok = (w.get("kind") in OFFNET_WHEN_OFF
                                  and state in ("off", "standby"))
                healthy = (state_healthy and not _my_gear_down
                           and not (live_reachable is False
                                    and not _offnet_ok))
                if healthy:
                    if rt.status == FAULT:
                        rt.last_event = "resolved"
                        rt.last_change = now
                        self._audit(ent, "resolved", rt.verdict or "")
                        if rt.recovery == "attempting":
                            rt.recovery = "recovered"
                            self._audit(ent, "recovered")
                    rt.unhealthy_since = None
                    rt.status = OK
                    rt.verdict = None
                    # Keep the witness answer even when healthy (W4): "normal"
                    # must be EARNED, so the surfaces report what was actually
                    # confirmed on the network, not just that nothing faulted.
                    rt.reachable = live_reachable
                    if rt.recovery in (None, "recovered"):
                        rt.recovery_n = 0    # clear episode once genuinely healthy
                else:
                    if rt.unhealthy_since is None:
                        rt.unhealthy_since = now
                    debounced = (now - rt.unhealthy_since) >= w.get("fault_after", 30)
                    # Power-aware devices probe immediately (to tell off from
                    # wedged); always-on already have live_reachable from above.
                    reachable = live_reachable
                    if reachable is None and spec and (pa or debounced):
                        reachable = self._reach_cached(w, spec, states, now)
                    rt.reachable = reachable
                    _wall_switched = (reachable is False and not state_healthy
                                      and w.get("kind") in NO_POWER_IS_NORMAL)
                    # D-1: a device whose OWN switch is dead never falls into
                    # the resting branch — resting is a claim about a device
                    # that left the network on its own, and with the path down
                    # ProOS cannot tell. It falls through to the debounced
                    # no-path verdict below.
                    if pa and not _my_gear_down and (reachable is None
                                                     or _wall_switched):
                        # No witness / witness can't say: a resting state — fail
                        # open, never invent a fault. (A witness that positively
                        # says GONE falls through to the debounced offline fault
                        # below — the switch test.) EXCEPT lighting with no
                        # power at all (class fact above): the wall switch is a
                        # daily, deliberate act — calm standby, not an alarm.
                        if rt.status == FAULT:
                            rt.last_event = "resolved"
                            rt.last_change = now
                        rt.status = STANDBY
                        rt.verdict = "standby"
                    elif debounced:
                        # Past debounce: a real fault. Diagnose why with the signal.
                        # D-1 + Dave's ruling (16 Aug): the device's OWN switch
                        # being offline outranks everything virtual — including
                        # a tracker still positively saying 'home' (stale for
                        # ~10 min on his box). No path, name the gear, never
                        # blame the device.
                        if _my_gear_down:
                            rt.verdict = V_NOPATH
                        else:
                            # 1.0.435: no house-wide V_NOPATH fallback here any
                            # more. `no_path` is a physical claim about THIS
                            # device's path, and it may only ever come from
                            # this device's own port reading above. With no
                            # reading, the verdict states only what this
                            # device's own witnesses measured.
                            rt.verdict = (V_INTEGRATION if reachable is True
                                          else V_OFFLINE)
                        if rt.status != FAULT:
                            rt.last_event = "raised"
                            rt.last_change = now
                            rt.recovery = None        # fresh episode: allow recovery
                            rt.recovery_n = 0
                            self._audit(ent, "fault", rt.verdict or "")
                        rt.status = FAULT
                        # Recovery hook: only integration-wedged (device reachable,
                        # HA lost it) is auto-recoverable. Offline is never actioned.
                        self._maybe_recover(w, rt, ent, now)
                    else:
                        rt.status = PENDING
                        rt.verdict = None
        # ── STAGE 1 BUILD 3 (16 Aug 2026): TELL THE GLASS ──────────────────
        # When anything this watcher REPORTS has changed shape — status,
        # verdict, state, witness answer, recovery — publish ONE nudge via
        # publish_fn (the SSE bus, injected by the server). The surfaces
        # refetch immediately instead of discovering it on a 15s poll, and
        # the Live page stops being a photograph. A quiet house publishes
        # NOTHING: the bus carries news, not noise. A broken hook never
        # breaks evaluation — fail-open, like every injection here.
        try:
            _sig = tuple(sorted(
                (e, r.status, r.verdict, r.state, r.reachable,
                 r.recovery, r.last_event)
                for e, r in self._rt.items()))
        except Exception:                                        # noqa: BLE001
            _sig = None
        if _sig is not None and _sig != getattr(self, "_pub_sig", ()):
            self._pub_sig = _sig
            _fn = getattr(self, "publish_fn", None)
            if _fn:
                try:
                    _fn()
                except Exception:                                # noqa: BLE001
                    pass

    def _audit(self, ent, event, detail=""):
        """Every recovery attempt/outcome is logged, reusing the terminal-audit
        pattern. Failure to write must never break the watch loop."""
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(f"{_iso(self.now())}\t{ent}\t{event}\t{detail}\n")
        except Exception:
            pass

    def _run_recovery(self, ent, action):
        """Runs the actual action off the tick thread so a slow reload never
        stalls monitoring."""
        try:
            ok = bool(self.recover_fn(ent, action)) if self.recover_fn else False
            self._audit(ent, "attempt_done" if ok else "attempt_error", action)
        except Exception as e:
            self._audit(ent, "attempt_exception", str(e))

    def _maybe_recover(self, w, rt, ent, now):
        """Tiered recovery. Only fires for integration-wedged faults. 'safe' auto-
        runs (rate-limited, audited, escalates on failure); 'risky' never auto-runs
        -- it surfaces for human approval. Apple TV uses a single safe action."""
        rec = w.get("recover")
        if not rec or rt.verdict != V_INTEGRATION:
            return
        tier = rec.get("tier", "safe")
        if tier == "risky":
            if rt.recovery not in ("needs_approval", "attempting"):
                rt.recovery = "needs_approval"
                self._audit(ent, "needs_approval", rec.get("action", ""))
            return
        if tier != "safe" or not self.recover_fn or rt.recovery == "recovered":
            return
        cooldown = rec.get("cooldown", 900)   # >= 15 min between retries
        grace = rec.get("grace", 45)          # wait this long before judging failure
        maxn = rec.get("max", 3)
        if rt.recovery == "attempting":
            if rt.recovery_at and (now - rt.recovery_at) >= grace:
                rt.recovery = "failed"
                self._audit(ent, "failed", rec.get("action", ""))
            return
        if rt.recovery == "failed":
            if rt.recovery_n >= maxn:
                return                        # give up auto; guidance escalates to manual
            if rt.recovery_at and (now - rt.recovery_at) < cooldown:
                return                        # respect cooldown before retrying
        # Attempt.
        rt.recovery = "attempting"
        rt.recovery_at = now
        rt.recovery_n += 1
        self._audit(ent, "attempt", rec.get("action", ""))
        threading.Thread(target=self._run_recovery,
                         args=(ent, rec.get("action")), daemon=True).start()

    def _guidance(self, w, rt):
        if rt.status != FAULT:
            return None
        if rt.verdict == V_NOPATH:
            # A-8. Say what is actually known and what is NOT. For a panel this
            # is the whole point: ProOS cannot tell a sleeping network port
            # from a dead one, and pretending otherwise in either direction is
            # what lost Dave's trust in the awareness layer.
            #
            # D-1: name the DEVICE'S OWN gear when its port reading is the
            # thing that convicted; the house-wide list is only the fallback
            # wording for a no-reading home.
            _p = self._port_of(w["entity"]) or {}
            if _p.get("gear_online") is False and _p.get("gear"):
                _who = str(_p["gear"])
            else:
                _g = self._gear_down()
                _who = (_g[0] if len(_g) == 1
                        else "%d pieces of network gear" % len(_g))
            if w.get("kind") in OFFNET_WHEN_OFF:
                return (f"{w['name']} cannot be checked: {_who} is offline, so "
                        f"there is no network path to it. A TV that is simply "
                        f"switched off also drops off the network — its port "
                        f"sleeps until it is woken — so ProOS cannot tell the "
                        f"two apart while the network is down, and will not "
                        f"guess. Restore the network first; this clears by "
                        f"itself when it comes back.")
            return (f"{w['name']} has no network path: {_who} is offline. The "
                    f"device itself has not been shown to be faulty. Fix the "
                    f"network first — this clears on its own once it returns.")
        if rt.verdict == V_INTEGRATION:
            if rt.recovery == "attempting":
                return (f"{w['name']} lost its connection -- ProOS is restarting "
                        "the integration automatically.")
            if rt.recovery == "failed":
                if w.get("cert_display"):
                    # Reachable certified TV, safe reload exhausted -> re-pair, not restart.
                    return (f"{w['name']} is on the network but not responding, and an "
                            "automatic restart didn't fix it -- this usually means the TV "
                            "was reset. Re-pair its input control (accept the Allow prompt "
                            "on the TV) and re-link SmartThings, and check the TV's IP "
                            "hasn't changed.")
                return (f"{w['name']} is online, but automatic recovery didn't "
                        "restore it. Its integration may need a manual restart.")
            if rt.recovery == "needs_approval":
                return (f"{w['name']} is online but unresponsive. Approve a "
                        "restart to recover it.")
            return w.get("guidance_wedged") or (
                f"{w['name']} is online, but its connection was lost. Restarting "
                "its integration will restore it -- the device itself is fine.")
        return w.get("guidance")

    def report(self):
        """Read-only payload for GET /watchers."""
        RECOVERED_TTL = 1800   # 'recovered' reverts to plain nominal after 30 min
        now = self.now()
        _excl = self._awareness_excluded(now)
        with self._lock:
            items, overall = [], OK
            excluded_items = []
            first_fault = first_pending = first_verdict = None
            for w in self.watches:
                if w["entity"] in _excl:
                    excluded_items.append({"name": w["name"],
                                           "area": w.get("area"),
                                           "entity": w["entity"]})
                    continue
                rt = self._rt[w["entity"]]
                is_fault = rt.status == FAULT
                # A-1 (audit, 15 Aug). `has_signal` only ever meant "a second
                # signal is BOUND to this device" — it was never a claim that
                # the signal had ANSWERED. Health rendered it as
                # "16 devices watched · 16 two-signal", and Dave, reading his
                # own product: "I don't believe a word of it."
                #
                # He was right not to. A binding is a wire; a wire is not a
                # witness. `signal` states which of the three a device actually
                # is, so the surface can stop averaging them into one number:
                #
                #   confirmed — the second signal has TESTIFIED (reachable
                #               resolved True or False this pass). This is the
                #               only state that earns "confirmed two ways".
                #   silent    — bound, but it has never answered. Watched on
                #               state alone; the wedge test cannot run.
                #   none      — nothing bound. State-only, and honest about it.
                #
                # Note this is deliberately the LIVE reading, not a sticky
                # "it worked once" flag: a witness that has gone quiet must
                # stop being counted the moment it goes quiet, or the number
                # drifts back into fiction — which is exactly how "31
                # two-signal" was displayed on 8 Aug with three of those
                # devices unreachable.
                _bound = bool(self._reach_spec(w))
                _port = self._port_of(w["entity"])
                # ── A-13 · THE PORT IS A SECOND SIGNAL, AND A BETTER ONE ────
                # Dave, 16 Aug 2026, looking at his camera list: every row read
                #
                #   CAMERA · SECOND SIGNAL NOT ANSWERING
                #   AV-Rack — USW-48 port 6 · link up · 100 Mbps · PoE 5.98 W
                #
                # ProOS printed the answer and denied having it, on one line.
                # Shape 1 again — one question answered in two places — and
                # this time the two places are a claim and a measurement.
                #
                # A switch reporting LINK UP and watts flowing into a port is
                # not a hint that the device is present. It is the strongest
                # evidence in the building: the electricity is measured at the
                # thing the cable is plugged into. It outranks a tracker and
                # it outranks a probe.
                #
                # So a live port CONFIRMS. And the surfaces say which signal
                # did the confirming, because "how do you know" is the question
                # this whole layer exists to answer.
                _port_live = bool(_port and _port.get("link") is True
                                  and _port.get("gear_online") is not False)
                # D-1 + Dave's ruling (16 Aug): a witness answering through a
                # switch that is OFFLINE has no path to be right on — its
                # positive claim is void, not evidence. For the ~10 minutes a
                # UniFi tracker stays stale after a switch dies, these rows
                # said "confirmed two ways" an inch above "switch OFFLINE".
                # They now say the signal is cut off with the switch. The
                # tracker's raw answer stays in `reachable` — a record is
                # never erased, it just stops being counted as confirmation.
                _gear_off = bool(_port and _port.get("gear_online") is False)
                # "NOT ANSWERING" vs "NEVER ASKED" — and this is a RECORD, not
                # a rule.
                #
                # Dave, 16 Aug 2026, on the first version of this line: *"This
                # better not be a rule change."* It was. I had written
                # `kind == "camera"` here — a device-class condition, inline,
                # in the observation layer, hours after building a gate to stop
                # exactly that. The ledger check missed it because it only
                # scanned module-level constants, so the enforcement had a hole
                # and I walked through it.
                #
                # Corrected: `rt.asked` records whether ProOS actually
                # CONSULTED the second signal this pass. That is a fact about
                # what was done, decided where the decision is made, and it
                # needs no opinion about what kind of device this is.
                _unasked = bool(_bound and rt.asked is False
                                and rt.reachable is None)
                if _port_live:
                    _sig, _by = "confirmed", "port"
                elif _gear_off and _bound:
                    _sig, _by = "cut", None
                elif rt.reachable is not None:
                    _sig, _by = "confirmed", "witness"
                elif _unasked:
                    _sig, _by = "unasked", None
                elif _bound:
                    _sig, _by = "silent", None
                else:
                    _sig, _by = "none", None
                items.append({
                    "name": w["name"],
                    # Stage 2 build 2: the identity, so consumers (monitor's
                    # room view, and later the surfaces) join by entity id,
                    # never by display name.
                    "entity": w["entity"],
                    "kind": w.get("kind"),
                    "area": w.get("area"),
                    "has_signal": _bound,
                    "port": _port,
                    "signal": _sig,
                    "signal_by": _by,
                    "status": ("standby" if rt.status == STANDBY
                               else "amber" if rt.status == PENDING else rt.status),
                    "verdict": rt.verdict if is_fault else (
                        "standby" if rt.status == STANDBY
                        else PENDING if rt.status == PENDING else OK),
                    "reachable": rt.reachable,
                    "recovery": (None if (rt.recovery == "recovered" and rt.last_change
                                          and (now - rt.last_change) > RECOVERED_TTL)
                                 else rt.recovery),
                    "state": rt.state,
                    "guidance": self._guidance(w, rt),
                    "since": _iso(rt.last_change or rt.unhealthy_since),
                    "last_event": (None if (rt.last_event == "resolved" and rt.last_change
                                            and (now - rt.last_change) > RECOVERED_TTL)
                                   else rt.last_event),
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
                summary = "All Systems Normal"
            # ── STAGE 4 BUILD 1 (16 Aug 2026): THE HERO'S WORDS LIVE HERE ──
            # Pro's home-screen hero used to compose its own verdict ladder
            # on the glass: "All Systems Normal" as a DEFAULT (even over an
            # empty list, even over fault shapes it didn't recognise),
            # "N issues — self-healing" for faults nothing was healing
            # (including FAILED recoveries), and an anonymous "Checking…" —
            # while ignoring the summary this report already served. Words
            # are verdicts, and verdicts belong beside the readings that
            # earn them. `hero` serves every word, every count, the pin and
            # the tone; the glass only places them
            # (watcher_hero_words_bench.py / pro_hero_mirror_bench.js).
            _healthy = sum(1 for i in items if i["status"] == OK)
            _standby = sum(1 for i in items if i["status"] == "standby")
            _hfaults = [i for i in items if i["status"] == FAULT]
            _pending = [i for i in items if i["status"] == "amber"]
            _two = sum(1 for i in items if i["signal"] == "confirmed")
            _alone = len(items) - _two
            # "Restoring" is a FACT about an attempt in progress — never a
            # promise. recovery=="attempting" is set only while the safe
            # recovery hook is actually running (and report() has already
            # TTL-nulled stale recovered/resolved marks above, so the pin
            # dies with its evidence).
            _working = next((i for i in items
                             if i["recovery"] == "attempting"), None)
            _healed = sorted((i for i in items
                              if i["recovery"] == "recovered"
                              or i["last_event"] == "resolved"),
                             key=lambda i: i["since"] or "", reverse=True)
            if _working is not None:
                _tone = "noticing"
                _headline = "Restoring %s…" % _working["name"]
            elif _hfaults:
                _tone = "attention"
                if len(_hfaults) == 1:
                    _tail = ("integration needs a restart"
                             if _hfaults[0]["verdict"] == V_INTEGRATION
                             else "needs attention")
                    _headline = "%s — %s" % (_hfaults[0]["name"], _tail)
                else:
                    _headline = "%d devices need attention" % len(_hfaults)
            elif _pending:
                _tone = "noticing"
                _headline = ("Confirming %s…" % _pending[0]["name"]
                             if len(_pending) == 1
                             else "Confirming %d devices…" % len(_pending))
            elif not items:
                # An empty list is not a calm house — it is an unwatched one.
                _tone = "empty"
                _headline = "No devices are being watched yet"
            else:
                _tone, _headline = "calm", "All Systems Normal"
            if items:
                _sub = "%d device%s watched" % (
                    len(items), "" if len(items) == 1 else "s")
                if _two:
                    _sub += " · %d confirmed two ways" % _two
                if _alone:
                    _sub += " · %d on state alone" % _alone
            else:
                _sub = "commit a room to start watching"
            _pin = None
            if _working is not None:
                _pin = {"kind": "restoring",
                        "text": ("%s — restarting its connection"
                                 % _working["name"]),
                        "since": None}
            elif _healed:
                _pin = {"kind": "recovered",
                        "text": "%s recovered on its own" % _healed[0]["name"],
                        "since": _healed[0]["since"]}
            hero = {"headline": _headline, "tone": _tone, "sub": _sub,
                    "pin": _pin,
                    "stats": [{"n": _healthy, "label": "healthy",
                               "tone": "good"},
                              {"n": _standby, "label": "standby",
                               "tone": "dim"},
                              {"n": len(_hfaults), "label": "faults",
                               "tone": "alert" if _hfaults else "dim"}]}
            # A-6: the witnesses that were ASKED and said nothing this pass.
            # healthmon reads this to tell one dead integration from forty
            # dead devices. Carried as (device, its witness sensor) pairs
            # because naming the integration means resolving the SENSOR's
            # config entry, not the device's.
            mute = [{"name": w["name"], "entity": w["entity"],
                     "sensor": self._mute[w["entity"]]}
                    for w in self.watches
                    if w["entity"] in self._mute and w["entity"] not in _excl]
            # THE TOOLBOX REPORTS ON ITSELF (Dave, 16 Aug 2026). The physical
            # port line shipped, and on his box nothing appeared — no line, no
            # explanation, nothing to act on. That silence is the same defect
            # the whole layer is being rebuilt to remove: a thing that cannot
            # answer must SAY it cannot answer.
            #
            # So the report states whether the physical toolbox is connected
            # and, when it is not, why — in one sentence an installer can act
            # on without opening a log.
            phys = {"available": False,
                    "why": "no network controller is connected to ProOS"}
            try:
                fn = getattr(self, "physical_status_fn", None)
                if fn:
                    phys = fn() or phys
            except Exception:                                    # noqa: BLE001
                phys = {"available": False,
                        "why": "the network controller could not be reached"}
            return {"status": overall, "summary": summary, "hero": hero,
                    "items": items,
                    "excluded_items": excluded_items,
                    "excluded": len(excluded_items),
                    "witness_mute": mute,
                    "witness_bound": sum(1 for i in items if i["has_signal"]),
                    "physical": phys}

    # ── STAGE 1 BUILD 2: the stream wakes the thinking ─────────────────────
    def poke(self):
        """Wake the evaluation loop NOW. Called by the HA event stream when a
        watched entity changes; safe from any thread; a latch, so a storm of
        events during one evaluation collapses into a single follow-up tick."""
        self._wake.set()

    def poke_if_watched(self, entity_id):
        """The stream's hook: wake only for entities this watcher actually
        evaluates (the device itself or its witness sensor). A busy house's
        unrelated events must never burn evaluation passes."""
        if entity_id in self._watched_cache:
            self._wake.set()

    def run_forever(self, interval=5):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    print(f"[watcher] tick error: {e}", flush=True)
                # Event-driven with the interval as the reconciliation sweep:
                # a poke returns this wait immediately; with no stream (or a
                # quiet house) it times out and the old cadence stands.
                self._wake.wait(interval)
                self._wake.clear()
        t = threading.Thread(target=loop, name="proos-watcher", daemon=True)
        t.start()
        return t


# ── automatic watch discovery ────────────────────────────────────────────────
# The watch list is derived from HA's registries so the whole home is covered
# with no per-home configuration: add a device, it gets watched. We deliberately
# watch only ALWAYS-ON device classes -- cameras, networked speakers, and
# streaming boxes that stay network-present in standby. TVs and AV receivers are
# powered off by design, so watching them would fault every time they're turned
# off (a false alarm). They still get a reachability signal in the map, ready for
# a future power-aware watch that can tell "off" from "broken".
_SRC_PLATFORMS = {"apple_tv", "androidtv_remote", "firetv", "roku"}   # remote-primary
_AUDIO_PLATFORMS = {"sonos", "heos", "bluesound"}                      # always-on speakers
_CAMERA_PLATFORMS = {"unifiprotect", "generic", "onvif", "reolink", "amcrest", "hikvision"}
# Power-aware classes: watched, but "off" is a resting state, not a fault. Only a
# network-present-but-lost device faults (wedged integration).
_DISPLAY_PLATFORMS = {"samsungtv", "samsungtv_smart", "webostv", "bravia_tv",
                      "philips_js", "lg_netcast", "vizio", "androidtv"}
_AVR_PLATFORMS = {"denonavr", "onkyo", "yamaha_musiccast", "arcam_fmj"}
# Non‑AV certified classes. Lighting/climate are power‑aware (off/idle is normal — only a
# network‑present‑but‑lost device faults). A security panel is NOT power‑aware: it must
# always be available, so an unreachable panel is a real, high‑priority fault.
_LIGHT_PLATFORMS = {"lifx", "shelly", "wiz"}
_CLIMATE_PLATFORMS = {"coolmaster"}
_SECURITY_PLATFORMS = {"elkm1"}

_INTEG_NAME = {
    "apple_tv": "Apple TV", "androidtv_remote": "Android TV", "firetv": "Fire TV",
    "roku": "Roku", "sonos": "Sonos", "heos": "HEOS", "bluesound": "BluOS",
    "unifiprotect": "UniFi Protect", "generic": "camera", "onvif": "ONVIF",
    "reolink": "Reolink", "samsungtv": "Samsung TV", "samsungtv_smart": "Samsung TV",
    "webostv": "LG webOS", "bravia_tv": "Sony", "denonavr": "Denon", "onkyo": "Onkyo",
    "yamaha_musiccast": "Yamaha", "philips_js": "Philips",
    "lifx": "LIFX", "shelly": "Shelly", "wiz": "WiZ", "coolmaster": "Cool Automation",
    "elkm1": "Elk M1",
}
_KIND_ORDER = {"media": 0, "audio": 1, "display": 2, "camera": 3,
               "lighting": 4, "climate": 5, "security": 6}


def _pretty(entity_id):
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def _best_camera(cams):
    """Prefer the main high-res channel; skip package/sub/low-res duplicates."""
    def score(c):
        e = c["entity_id"]
        s = 0
        if "high_resolution" in e:
            s -= 2
        if any(x in e for x in ("package", "sub", "low_resolution", "doorbell_chime")):
            s += 3
        return (s, len(e))
    return sorted(cams, key=score)[0]


def _mk_watch(name, entity, kind, fault_after, platform, power_aware=False):
    # Friendly, ProOS-facing label -- the resident/installer sees a category, not
    # the technical HA integration name (a Shield is "streaming", not "Android TV
    # Remote"). Falls back to the device-type name, then the raw platform.
    _FRIENDLY = {"media": "streaming", "audio": "audio", "display": "TV",
                 "camera": "camera", "lighting": "lighting", "climate": "climate",
                 "security": "alarm"}
    integ = _FRIENDLY.get(kind) or _INTEG_NAME.get(
        platform, (platform or "the").replace("_", " ").title())
    if kind == "security":
        # An alarm panel must always be present — unreachable is a real, high‑priority fault.
        g = (f"{name} is not responding. Check the panel's power and network/hub "
             f"(the security system may be offline).")
        gw = (f"{name} panel is on the network but its connection was lost. Reload the "
              f"{_INTEG_NAME.get(platform, 'panel')} integration — the panel itself is fine.")
    elif power_aware:
        # Off is normal for these; the only fault we raise is a wedged integration.
        g = (f"{name} reports off but has also dropped off the network — a "
             f"switched-off device normally stays connected. Check its power, "
             f"cable, and the switch or access point feeding it.")
        gw = (f"{name} is on the network, but its connection was lost. "
              f"Restart the {integ} integration -- the device itself is fine.")
    elif kind == "camera":
        nvr = _INTEG_NAME.get(platform) or "the camera"
        g = (f"{name} is offline. Check the camera's power and network connection "
             f"(Wi-Fi or cable), then the {nvr} recorder if other cameras are down too.")
        gw = (f"The {nvr} controller is online but {name} isn't reporting. Reload the "
              f"{nvr} integration; if it stays down the camera itself has dropped off, "
              f"so check the camera.")
    elif kind == "audio":
        g = f"{name} dropped off the network. Check its power and Wi-Fi/Ethernet."
        gw = (f"{name} is online, but its connection was lost. Restart the {integ} "
              "integration -- no need to touch the device.")
    else:  # media (streaming source box)
        g = (f"{name} lost network or was powered off. Check Wi-Fi/power; if it "
             f"persists, restart the {integ} integration.")
        gw = (f"{name} is online, but its connection was lost. "
              f"Restart the {integ} integration -- no need to touch the device.")
    w = {"name": name, "kind": kind, "entity": entity, "healthy_when": "available",
         "ignore_states": [], "fault_after": fault_after, "platform": platform,
         "guidance": g, "guidance_wedged": gw}
    if power_aware:
        w["power_aware"] = True
    # SAFE auto-recovery -- but only where it's EARNED (the degradation contract):
    #   * certified AV integrations   -> self-heal (validated: reload restores it,
    #                                    no re-auth prompt, no destructive side effect)
    #   * cameras                     -> self-heal (their certified/managed path)
    #   * compatible AV integrations  -> WATCHED for verdicts, but NO auto-recover:
    #                                    they never claimed safe self-heal, so we
    #                                    surface a manual-restart hint instead of
    #                                    silently reloading an unvalidated driver.
    # A device is never faulted for a capability it doesn't claim; recovery is the
    # capability, gated here. Certifying a new brand turns this on with no code change.
    recoverable = (kind == "camera") or (platform in discovery.CERTIFIED_INTEGRATIONS)
    if recoverable:
        w["recover"] = {"tier": "safe", "action": "reload_integration",
                        "cooldown": 900, "grace": 45, "max": 3}
    # A certified display that goes reachable-but-wedged and won't come back after a
    # safe reload is the signature of a TV factory-reset (the on-box pairing was
    # cleared -- a reload can't fix what needs re-pairing). Flag it so guidance can
    # say "re-pair", not the generic "restart the integration".
    if kind == "display" and platform in discovery.CERTIFIED_INTEGRATIONS:
        w["cert_display"] = True
    return w


def _classify_device(ents, name, entry_domain):
    """One physical device -> at most one watch (or None). camera > source-remote
    > networked-audio. Everything else (TVs, AVRs, helpers) yields None."""
    def platform(en):
        return en.get("platform") or entry_domain.get(en.get("config_entry_id"))

    cams = [e for e in ents if e["entity_id"].startswith("camera.")
            and platform(e) in _CAMERA_PLATFORMS]
    if cams:
        p = _best_camera(cams)
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "camera", 60, platform(p))

    rems = [e for e in ents if e["entity_id"].startswith("remote.")
            and platform(e) in _SRC_PLATFORMS]
    if rems:
        p = rems[0]
        # POWER-AWARE like TVs/AVRs: a streaming box that's asleep closes its
        # ports and stops answering the liveness probe, which was reporting a
        # perfectly healthy resting Shield as "offline". Only a wedged
        # integration (reachable but HA lost it) faults.
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "media", 30, platform(p), power_aware=True)

    auds = [e for e in ents if e["entity_id"].startswith("media_player.")
            and platform(e) in _AUDIO_PLATFORMS]
    if auds:
        p = auds[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "audio", 30, platform(p))

    # Power-aware: TVs and AV receivers. Off is normal (standby), only a wedged
    # integration faults. fault_after is longer -- TVs are slow to (dis)appear.
    disp = [e for e in ents if e["entity_id"].startswith("media_player.")
            and platform(e) in _DISPLAY_PLATFORMS]
    if disp:
        p = disp[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "display", 45, platform(p), power_aware=True)
    avr = [e for e in ents if e["entity_id"].startswith("media_player.")
           and platform(e) in _AVR_PLATFORMS]
    if avr:
        p = avr[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "audio", 45, platform(p), power_aware=True)
    # ── Non‑AV certified classes ──────────────────────────────────────────────
    # Lighting: a bulb/relay dropping OFF THE NETWORK is a real fault; being switched
    # off is not (power_aware). Climate likewise (idle/off is normal). Security panel
    # is always‑on — unreachable faults immediately.
    lights = [e for e in ents if e["entity_id"].startswith("light.")
              and platform(e) in _LIGHT_PLATFORMS]
    if lights:
        p = lights[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "lighting", 60, platform(p), power_aware=True)
    clim = [e for e in ents if e["entity_id"].startswith("climate.")
            and platform(e) in _CLIMATE_PLATFORMS]
    if clim:
        p = clim[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "climate", 60, platform(p), power_aware=True)
    sec = [e for e in ents if e["entity_id"].startswith("alarm_control_panel.")
           and platform(e) in _SECURITY_PLATFORMS]
    if sec:
        p = sec[0]
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "security", 60, platform(p))
    return None


def _room_from_name(name, area_names):
    """Infer a room from a device name that leads with it -- 'Bedroom Apple TV'
    -> 'Bedroom'. Prefix match wins; else a whole-word match; longest area name
    wins so 'Living Room' beats a hypothetical 'Room'."""
    if not name:
        return None
    low = " " + name.lower() + " "
    best = None
    for an in area_names:
        a = (an or "").lower().strip()
        if not a:
            continue
        prefix = name.lower().startswith(a)
        word = (" " + a + " ") in low
        if prefix or word:
            rank = (1 if prefix else 0, len(a))
            if best is None or rank > best[0]:
                best = (rank, an)
    return best[1] if best else None


def discover_watches(storage_dir=None, client=None):
    """Derive the watch list from HA's registries (LIVE via client when given,
    else on-disk). One watch per always-on physical device, deduped, sorted.
    Empty list on any failure (caller keeps its current/fallback watches)."""
    if storage_dir is None:
        import os as _os
        storage_dir = ("/homeassistant" if _os.path.isdir("/homeassistant/.storage") else "/config") + "/.storage"
    from . import netmap
    entries, devices, entities = netmap.load_registries(storage_dir, client)
    areas = netmap.load_areas(storage_dir, client)
    entry_domain = {e.get("entry_id"): e.get("domain") for e in entries}
    dev_name = {}
    dev_area = {}
    for d in devices:
        did = d.get("id")
        if did:
            dev_name[did] = d.get("name_by_user") or d.get("name")
            dev_area[did] = d.get("area_id")
    ent_area = {e.get("entity_id"): e.get("area_id") for e in entities}
    # Controller host per config entry (UniFi Protect / Reolink NVR, etc.). A
    # camera's wedge probe targets the RECORDER, not the camera's own IP — cameras
    # often don't answer a TCP probe and may sit on an isolated VLAN, whereas the
    # controller is reachable and is the true "is the integration up" signal.
    entry_host = {}
    for en in entries:
        data = en.get("data") or {}
        host = data.get("host") or data.get("ip_address") or data.get("ip")
        if host and en.get("entry_id"):
            entry_host[en["entry_id"]] = host
    ent_entry = {e.get("entity_id"): e.get("config_entry_id") for e in entities}
    by_dev = {}
    for e in entities:
        eid, did = e.get("entity_id"), e.get("device_id")
        if not eid or e.get("disabled_by") or not did:
            continue
        by_dev.setdefault(did, []).append(e)

    watches, seen = [], set()
    area_names = list(areas.values())
    for did, ents in by_dev.items():
        w = _classify_device(ents, dev_name.get(did), entry_domain)
        if w and w["entity"] not in seen:
            # Camera wedge signal = the controller/NVR (HTTPS). NVR answers but the
            # camera is unavailable -> wedged integration; NVR unreachable -> offline.
            if w["kind"] == "camera":
                host = entry_host.get(ent_entry.get(w["entity"]))
                if host:
                    w["reach"] = {"ip": host, "port": 443}
            # ProOS: use the device's own connection-health binary_sensor as the second
            # signal. A ProOS certified driver exposes `<device>_connection` -- an
            # ALWAYS-available connectivity sensor keyed off the integration's live
            # connection. It reports directly whether the integration holds the device
            # (a cleaner ground truth than an IP probe), so state-unavailable + connection
            # 'on' = wedged integration (self-heal), + 'off' = genuinely offline. Only set
            # when nothing more specific (camera NVR IP) already claimed the slot.
            if "reach" not in w:
                for e in ents:
                    ce = e.get("entity_id") or ""
                    if "proos_connection" in str(e.get("unique_id") or "") \
                            and ce.startswith("binary_sensor."):
                        w["reach"] = {"sensor": ce}
                        break
            # Room resolution, most trustworthy first:
            #  1) the device's own area (what the installer sets on the device),
            #  2) inferred from the device name ('Bedroom Apple TV' -> Bedroom).
            # The entity-level area override is deliberately NOT used: a stale one
            # is exactly what mis-filed unassigned cameras into a single room.
            room = (areas.get(dev_area.get(did))
                    or _room_from_name(w["name"], area_names))
            w["area"] = room or "Unassigned"
            w["_did"] = did
            watches.append(w)
            seen.add(w["entity"])

    # ── Collapse TWINS: one physical device, two integrations ────────────────
    # A device that answers on the SAME MAC under two integrations (a Denon/Marantz AVR
    # that shows as both `denonavr` and `heos`) is ONE certified device, not two watches.
    # Dedupe by hardware MAC (safe — cameras that share an NVR *IP* keep their own MACs, so
    # they're never collapsed), keeping the richer control path (more certified capabilities;
    # the AVR beats the streaming-only twin).
    # HARDWARE KEY, not just MAC (Dave, 4 Aug, live): the Marantz pair carries
    # NO connections at all under either integration —
    #   denonavr: identifiers [["denonavr","Marantz SR5014-BHL36191003541"]]
    #   heos:     serial_number "BHL36191003541"
    # — so the MAC dedupe could never fire, both twins were watched, and the
    # HEOS twin (not power-aware; its ProOS connection witness goes off when
    # the AVR powers down) reported "Marantz SR5014 needs a look" while the
    # AVR was perfectly fine and simply off.
    #
    # The SERIAL is the shared hardware id. Still id-level, never a name:
    # a MAC match, or one device's serial appearing inside the other's
    # hardware id. Serials are long and distinctive, so an 8-character floor
    # makes a coincidental match implausible; anything shorter is ignored.
    def _hw_keys(d):
        macs, ids = set(), set()
        for pair in (d.get("connections") or []):
            if (isinstance(pair, (list, tuple)) and len(pair) > 1
                    and str(pair[0]).lower() in ("mac", "mac_address")):
                macs.add(str(pair[1]).lower().replace("-", ":"))
        for pair in (d.get("identifiers") or []):
            if isinstance(pair, (list, tuple)) and len(pair) > 1:
                ids.add(str(pair[1]).lower())
        sn = str(d.get("serial_number") or "").strip().lower()
        if len(sn) >= 8:
            ids.add(sn)
        return macs, ids, (sn if len(sn) >= 8 else None)

    dev_mac, dev_ids, dev_sn = {}, {}, {}
    for d in devices:
        did = d.get("id")
        macs, ids, sn = _hw_keys(d)
        if macs:
            dev_mac[did] = sorted(macs)[0]
        dev_ids[did] = ids
        if sn:
            dev_sn[did] = sn

    def _same_device(a_did, b_did):
        """One physical device seen twice? MAC match, or one's serial inside
        the other's hardware ids. Ids only — never a name or a model."""
        if not a_did or not b_did or a_did == b_did:
            return a_did == b_did
        ma, mb = dev_mac.get(a_did), dev_mac.get(b_did)
        if ma and mb:
            return ma == mb
        for x, y in ((a_did, b_did), (b_did, a_did)):
            sn = dev_sn.get(x)
            if sn and any(sn in i for i in dev_ids.get(y, ())):
                return True
        return False

    def _rank(w):
        return len(discovery.CERTIFIED_CAPABILITIES.get(w.get("platform"), ()))

    out, kept = [], []            # kept: one surviving watch per physical device
    for w in watches:
        did = w.get("_did")
        prev = None
        if did:
            for k in kept:
                if _same_device(did, k.get("_did")):
                    prev = k
                    break
        if prev is None:
            kept.append(w)
            out.append(w)
        elif _rank(w) > _rank(prev):
            # Winner keeps its own second signal, but inherits the twin's if it has
            # none -- e.g. a Marantz seen as denonavr (winner) + heos: whichever twin
            # carries the `<device>_connection` sensor, the surviving watch uses it.
            if "reach" not in w and prev.get("reach"):
                w["reach"] = prev["reach"]
            out[out.index(prev)] = w
            kept[kept.index(prev)] = w
        else:
            # Same physical device, poorer path -> drop, but hand its second signal
            # up to the kept watch if that one lacks one.
            if "reach" not in prev and w.get("reach"):
                prev["reach"] = w["reach"]
    watches = out
    for w in watches:
        w.pop("_did", None)

    watches.sort(key=lambda w: (_KIND_ORDER.get(w["kind"], 9), w["name"].lower()))
    return watches
