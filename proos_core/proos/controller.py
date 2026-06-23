"""
Room controller -- the async brain per room.

Holds the discovered cluster + generated activities, and runs reconciles in a
background thread so the API can return immediately. Tracks one live run per
room; a new intent supersedes an in-flight one (cooperative cancel -- the
reconciler checks the flag between attempts).
"""
from __future__ import annotations
import threading
import time

from .reconciler import Reconciler, Outcome
from .discovery import discover_av
from .activities import build_watch_activities


# ---- Commissioning overlay -------------------------------------------------
# Per-room, per-source bindings that enhance discovered activities: an explicit
# route (when CEC isn't enough), a confirmed wake, a reachability witness. This
# is config data, not logic -- shaped exactly as a future ProOS commissioning
# source (an installer UI, or a Control4 add-on that contributes discrete
# routing) will provide it. The ENGINE below is the permanent part; this dict is
# just the current (empty) data.
#
# Empty by default: with nothing here, EVERY room auto-configures from discovery
# alone -- brand-agnostic CEC wake-only activities, no Control4, no per-brand
# codes. This is the base product. When a Control4 add-on (or installer UI) is
# present, it populates entries of the shape:
#   area -> source_eid (or "__broadcast__") -> {route, wake, reachability_sensor}
# to upgrade those rooms to discrete routing -- without touching this engine.
COMMISSIONING: dict = {}


def apply_commissioning(area: str, acts: list) -> list:
    """Overlay COMMISSIONING bindings onto discovered activities (in place).

    Never creates activities -- only enhances ones discovery already produced,
    so an absent source simply leaves its binding unused (no phantoms). Matched
    activities get their route/wake/witness set and the provisional flag cleared.
    """
    room = COMMISSIONING.get(area)
    if not room:
        return acts
    for a in acts:
        src = getattr(a, "source_eid", None)
        binding = room.get(src) if src else room.get("__broadcast__")
        if not binding:
            continue
        if binding.get("route"):
            a.route = binding["route"]
        if binding.get("wake"):
            a.wake = binding["wake"]
        if binding.get("reachability_sensor"):
            a.reachability_sensor = binding["reachability_sensor"]
        a.provisional = False  # commissioned
    return acts


class Run:
    def __init__(self, activity):
        self.activity_key = activity.key
        self.label = activity.label
        self.status = "reconciling"
        self.summary = ""
        self.targets: list[dict] = []
        self.transcript: list[str] = []
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> dict:
        return {
            "activity": self.activity_key,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "targets": self.targets,
            "transcript": self.transcript,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self.finished_at is None,
        }


class RoomController:
    def __init__(self, client, area: str, reconciler_kwargs: dict | None = None,
                 reachability: dict | None = None):
        self.client = client
        self.area = area
        self.reconciler_kwargs = reconciler_kwargs or {}
        self.reachability = reachability or {}
        self._lock = threading.Lock()
        self.run: Run | None = None
        self.asserted = None     # the activity this room is currently SET to (desired state)
        self.activities: dict = {}
        self.refresh()

    def refresh(self):
        cluster = discover_av(self.client, self.area)
        self.cluster = cluster
        # Brand-agnostic discovery: every discovered source becomes a PROVISIONAL
        # CEC wake-only activity. Works with zero commissioning -- no Control4, no
        # discrete codes -- on any room with a display + sources. Empty HA -> no
        # display -> no activities (empty->empty holds by construction).
        acts = build_watch_activities(cluster, reachability=self.reachability)
        # Overlay per-room commissioning on top: an explicit route (C4 / discrete),
        # a confirmed wake, a reachability witness -- and clear the provisional
        # flag. The overlay never creates activities, only enhances discovered
        # ones, so it can't produce phantoms: if a source isn't discovered, its
        # binding simply doesn't apply. The overlay lives in a config-shaped dict
        # today (COMMISSIONING, below) and will move verbatim to the add-on config
        # / installer UI without changing this engine.
        acts = apply_commissioning(self.area, acts)
        self.activities = {a.key: a for a in acts}

    def list_activities(self) -> list[dict]:
        snap = self.client.snapshot(
            list({e for a in self.activities.values() for e in a.entity_ids()})
        ) if self.activities else {}
        return [
            {"key": a.key, "label": a.label, "verdict": a.summary(snap).detail,
             "provisional": getattr(a, "provisional", False)}
            for a in self.activities.values()
        ]

    def start(self, activity_key: str) -> dict:
        activity = self.activities.get(activity_key)
        if not activity:
            raise KeyError(activity_key)

        with self._lock:
            # Switching is no longer special. Routing is a discrete, idempotent
            # input command fired by the display target every reconcile, so a
            # switch is just a normal reconcile that re-sends the new input code.
            # (The old CEC sleep-then-wake _force_route hack is gone -- it was
            # built on a wrong assumption about this hardware.)
            if self.run and self.run.finished_at is None:
                self.run.cancel()
            run = Run(activity)
            self.run = run
            self.asserted = activity  # the user's intent IS the desired state now

        rec = Reconciler(self.client, log=lambda line: run.transcript.append(line),
                         **self.reconciler_kwargs)

        def worker():
            # ROUTE FIRST: fire the discrete input code once, every activation.
            # Idempotent and power-independent -- the lever we proved live. This
            # is what actually switches the TV's input; the reconcile below just
            # converges power + advisory source/competitor state around it.
            route = getattr(activity, "route", None)
            # The select_source route fires either form: Control4 uses
            # c4_entity/c4_source; the native tuner ('Watch TV') uses
            # select_entity/select_source on the display itself. Both resolve to
            # one media_player.select_source call. Verification stays on the
            # independent witnesses below, never on the device's own echo.
            sel_entity = route and (route.get("c4_entity") or route.get("select_entity"))
            sel_source = route and (route.get("c4_source") or route.get("select_source"))
            if sel_entity and sel_source:
                run.transcript.append(
                    f"↪ routing display via select_source -> {sel_source}")
                try:
                    self.client.call_service(
                        "media_player", "select_source", sel_entity,
                        {"source": sel_source})
                except Exception as e:
                    run.transcript.append(f"   select_source route failed: {e}")
            elif route and route.get("tv_remote") and route.get("hdmi_code"):
                run.transcript.append(
                    f"↪ routing display to {route['hdmi_code']} via {route['tv_remote']}")
                try:
                    self.client.call_service(
                        "remote", "send_command", route["tv_remote"],
                        {"command": route["hdmi_code"]})
                except Exception as e:
                    run.transcript.append(f"   route command failed: {e}")

            # WAKE the source once, unconditionally. The source's own state is
            # the thing we can't trust (tvOS-26 reports phantom 'paused' while
            # asleep), so we never gate the wake on it. remote.turn_on is the
            # path that actually reaches the Apple TV; re-asserting an awake
            # device is harmless. This is what was missing -- the phantom
            # 'paused' made the old conditional wake skip entirely.
            # WAKE the source once, unconditionally. The source's own state is
            # the thing we can't trust (tvOS-26 phantom 'paused' while asleep),
            # so we never gate the wake on it. The wake carries its own domain/
            # service/entity because the path that actually reaches each device
            # differs: the Apple TV wakes via media_player.turn_on on its
            # media_player (remote.turn_on on the remote entity is a no-op on
            # this box) -- confirmed live. Shield/others can use their own.
            wake = getattr(activity, "wake", None)
            if wake and wake.get("entity"):
                run.transcript.append(
                    f"↪ waking source via {wake['domain']}.{wake['service']} {wake['entity']}")
                try:
                    self.client.call_service(
                        wake["domain"], wake["service"], wake["entity"])
                except Exception as e:
                    run.transcript.append(f"   wake command failed: {e}")

            def on_state(update):
                run.status = update["outcome"]
                run.summary = update["summary"]
                run.targets = update["targets"]
            result = rec.reconcile(activity, should_cancel=run.cancelled, on_state=on_state)
            run.status = result.outcome.value
            run.summary = result.summary
            run.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return run.to_dict()

    def heal(self) -> dict:
        """Re-reconcile the room back to its asserted desired state."""
        if not self.asserted:
            raise KeyError("no asserted activity to restore")
        return self.start(self.asserted.key)

    def recover(self, recover_wait: float = 12.0) -> dict:
        """
        Self-healing ladder for the asserted activity:
          1. find 'should be on' targets that are failing,
          2. reload their integration (fixes the stale control channel we hit
             tonight: reachable + available but not responding to commands),
          3. wait for the channel to re-establish,
          4. re-reconcile,
          5. if still degraded, escalate with a human-readable action.

        Drift (a competing source left on) needs no reload -- step 1 finds no
        stale 'on' target, so it falls straight through to the re-reconcile,
        which turns the competitor off.
        """
        if not self.asserted:
            raise KeyError("no asserted activity to recover")
        activity = self.asserted

        with self._lock:
            if self.run and self.run.finished_at is None:
                self.run.cancel()
            run = Run(activity)
            run.label = f"Recover · {activity.label}"
            self.run = run

        def worker():
            snap = self.client.snapshot(activity.entity_ids())
            # 'should be on' targets that aren't validating = candidates to reload.
            stale = [t for t in activity.targets
                     if t.name in ("display", "source") and not t.validate(snap).ok]
            reloaded: set[str] = set()
            for t in stale:
                try:
                    entry = self.client.resolve_config_entry(t.entity_id)
                except Exception:
                    entry = None
                if entry and entry not in reloaded:
                    run.transcript.append(f"↻ reloading integration for {t.entity_id} (entry {entry})")
                    try:
                        self.client.reload_integration(entry)
                        reloaded.add(entry)
                    except Exception as e:
                        run.transcript.append(f"   reload failed: {e}")
            if reloaded:
                run.transcript.append(f"   waiting {recover_wait:.0f}s for the control channel to re-establish…")
                time.sleep(recover_wait)

            # Re-assert the input on recovery too -- a device that dropped and
            # came back may have left the panel on the wrong input.
            route = getattr(activity, "route", None)
            sel_entity = route and (route.get("c4_entity") or route.get("select_entity"))
            sel_source = route and (route.get("c4_source") or route.get("select_source"))
            if sel_entity and sel_source:
                run.transcript.append(
                    f"↪ re-routing display via select_source -> {sel_source}")
                try:
                    self.client.call_service("media_player", "select_source",
                        sel_entity, {"source": sel_source})
                except Exception as e:
                    run.transcript.append(f"   select_source route failed: {e}")
            elif route and route.get("tv_remote") and route.get("hdmi_code"):
                run.transcript.append(f"↪ re-routing display to {route['hdmi_code']}")
                try:
                    self.client.call_service("remote", "send_command",
                        route["tv_remote"], {"command": route["hdmi_code"]})
                except Exception as e:
                    run.transcript.append(f"   route command failed: {e}")
            wake = getattr(activity, "wake", None)
            if wake and wake.get("entity"):
                run.transcript.append(
                    f"↪ re-waking source via {wake['domain']}.{wake['service']} {wake['entity']}")
                try:
                    self.client.call_service(
                        wake["domain"], wake["service"], wake["entity"])
                except Exception as e:
                    run.transcript.append(f"   wake command failed: {e}")

            rec = Reconciler(self.client, log=lambda line: run.transcript.append(line),
                             **self.reconciler_kwargs)

            def on_state(update):
                run.status = update["outcome"]
                run.summary = update["summary"]
                run.targets = update["targets"]

            result = rec.reconcile(activity, should_cancel=run.cancelled, on_state=on_state)
            run.status = result.outcome.value
            if result.outcome.value == "degraded" and reloaded:
                # Reload didn't bring it back -> escalate to a human action.
                run.summary = (f"Recovery incomplete: a device didn't respond even after "
                               f"reloading its integration. It may need a power cycle or a "
                               f"press on its remote. ({result.summary})")
            else:
                run.summary = result.summary
            run.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return run.to_dict()

    def status(self) -> dict:
        return self.run.to_dict() if self.run else {"status": "idle"}
