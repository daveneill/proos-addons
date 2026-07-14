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
                    "Apple TV integration if it persists.",
        "guidance_wedged": "The Apple TV is online, but its connection was lost. "
                           "its connection to it. Restart the Apple TV "
                           "integration -- no need to touch "
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
STANDBY = "standby"   # power-aware device that's simply off -> calm, not a fault

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
                 "last_change", "reachable", "verdict",
                 "recovery", "recovery_at", "recovery_n")

    def __init__(self):
        self.status = OK
        self.state = None
        self.unhealthy_since = None
        self.last_event = None       # "raised" | "resolved" | None
        self.last_change = None      # epoch secs of last transition
        self.reachable = None        # True/False/None -- last liveness reading
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
                state_healthy = self._is_healthy(w, state)
                pa = bool(w.get("power_aware"))
                spec = self._reach_spec(w)
                # Verify Don't Assume: for an always-on device with a liveness
                # signal, a device that doesn't answer the network is NOT healthy
                # even if HA still reports it up -- integration state lags or goes
                # stale (an Apple TV remote reads 'on' for minutes after it dies;
                # a Samsung reads 'off' when unreachable). The independent probe is
                # ground truth. Power-aware devices are exempt: 'off + unreachable'
                # is a normal resting state (someone switched the TV off), so we
                # don't second-guess their state with the probe here.
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
                if spec and not pa and not _cam_up:
                    live_reachable = self._reach_cached(w, spec, states, now)
                healthy = state_healthy and not (live_reachable is False)
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
                    rt.reachable = None
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
                    if pa and reachable is not True:
                        # Off (unreachable) or can't confirm it's on the network:
                        # a resting state for a TV/AVR, NOT a fault.
                        if rt.status == FAULT:
                            rt.last_event = "resolved"
                            rt.last_change = now
                        rt.status = STANDBY
                        rt.verdict = "standby"
                    elif debounced:
                        # Past debounce: a real fault. Diagnose why with the signal.
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
        if rt.verdict == V_INTEGRATION:
            if rt.recovery == "attempting":
                return (f"{w['name']} lost its connection -- ProOS is restarting "
                        "the integration automatically.")
            if rt.recovery == "failed":
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
        with self._lock:
            items, overall = [], OK
            first_fault = first_pending = first_verdict = None
            for w in self.watches:
                rt = self._rt[w["entity"]]
                is_fault = rt.status == FAULT
                items.append({
                    "name": w["name"],
                    "kind": w.get("kind"),
                    "area": w.get("area"),
                    "has_signal": bool(self._reach_spec(w)),
                    "status": ("standby" if rt.status == STANDBY
                               else "amber" if rt.status == PENDING else rt.status),
                    "verdict": rt.verdict if is_fault else (
                        "standby" if rt.status == STANDBY
                        else PENDING if rt.status == PENDING else OK),
                    "reachable": rt.reachable if is_fault else None,
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

_INTEG_NAME = {
    "apple_tv": "Apple TV", "androidtv_remote": "Android TV", "firetv": "Fire TV",
    "roku": "Roku", "sonos": "Sonos", "heos": "HEOS", "bluesound": "BluOS",
    "unifiprotect": "UniFi Protect", "generic": "camera", "onvif": "ONVIF",
    "reolink": "Reolink", "samsungtv": "Samsung TV", "samsungtv_smart": "Samsung TV",
    "webostv": "LG webOS", "bravia_tv": "Sony", "denonavr": "Denon", "onkyo": "Onkyo",
    "yamaha_musiccast": "Yamaha", "philips_js": "Philips",
}
_KIND_ORDER = {"media": 0, "audio": 1, "display": 2, "camera": 3}


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
                 "camera": "camera"}
    integ = _FRIENDLY.get(kind) or _INTEG_NAME.get(
        platform, (platform or "the").replace("_", " ").title())
    if power_aware:
        # Off is normal for these; the only fault we raise is a wedged integration.
        g = f"{name} appears to be off."
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
         "ignore_states": [], "fault_after": fault_after,
         "guidance": g, "guidance_wedged": gw}
    if power_aware:
        w["power_aware"] = True
    # SAFE auto-recovery for every watched integration: if the device is reachable
    # but the integration wedged, reload the integration (never power-cycle the
    # device). Universally safe -- reload only restarts HA's config entry. The
    # verdict + power-aware logic guarantee it fires ONLY on genuine integration-
    # wedge, never on a device that's simply off (that's STANDBY, not a fault).
    w["recover"] = {"tier": "safe", "action": "reload_integration",
                    "cooldown": 900, "grace": 45, "max": 3}
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
        return _mk_watch(name or _pretty(p["entity_id"]), p["entity_id"],
                         "media", 30, platform(p))

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


def discover_watches(storage_dir="/homeassistant/.storage", client=None):
    """Derive the watch list from HA's registries (LIVE via client when given,
    else on-disk). One watch per always-on physical device, deduped, sorted.
    Empty list on any failure (caller keeps its current/fallback watches)."""
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
            # Room resolution, most trustworthy first:
            #  1) the device's own area (what the installer sets on the device),
            #  2) inferred from the device name ('Bedroom Apple TV' -> Bedroom).
            # The entity-level area override is deliberately NOT used: a stale one
            # is exactly what mis-filed unassigned cameras into a single room.
            room = (areas.get(dev_area.get(did))
                    or _room_from_name(w["name"], area_names))
            w["area"] = room or "Unassigned"
            watches.append(w)
            seen.add(w["entity"])
    watches.sort(key=lambda w: (_KIND_ORDER.get(w["kind"], 9), w["name"].lower()))
    return watches
