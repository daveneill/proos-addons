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
        self.activities = {a.key: a for a in build_watch_activities(cluster)}

        # ---- TEMPORARY HARDCODE (2026-06-22) -------------------------------
        # Discrete HDMI codes can't be auto-discovered; the commissioning wizard
        # will map them per install. Until then, inject the confirmed Family Room
        # map directly so the live add-on serves Apple/Shield/Broadcast routing.
        # Confirmed by firing codes at remote.family_room_family_room_tv and
        # watching the panel: Apple=KEY_HDMI1, Shield=KEY_HDMI3, Broadcast=KEY_TV.
        if self.area == "Family Room":
            from .activities import (make_watch_activity, TV, TV_REMOTE,
                                     APPLE, APPLE_REMOTE, SHIELD, SHIELD_REMOTE)
            hard = [
                make_watch_activity(area="Family Room", key="watch_apple",
                    source_label="Apple TV", display_eid=TV, tv_remote=TV_REMOTE,
                    hdmi_code="KEY_HDMI1", source_eid=APPLE,
                    wake_remote=APPLE_REMOTE, competing_eids=[SHIELD]),
                make_watch_activity(area="Family Room", key="watch_shield",
                    source_label="Shield", display_eid=TV, tv_remote=TV_REMOTE,
                    hdmi_code="KEY_HDMI3", source_eid=SHIELD,
                    wake_remote=SHIELD_REMOTE, competing_eids=[APPLE]),
                make_watch_activity(area="Family Room", key="watch_tv",
                    source_label="TV", display_eid=TV, tv_remote=TV_REMOTE,
                    hdmi_code="KEY_TV", source_eid=None,
                    competing_eids=[APPLE, SHIELD], broadcast=True),
            ]
            self.activities = {a.key: a for a in hard}

    def list_activities(self) -> list[dict]:
        snap = self.client.snapshot(
            list({e for a in self.activities.values() for e in a.entity_ids()})
        ) if self.activities else {}
        return [
            {"key": a.key, "label": a.label, "verdict": a.summary(snap).detail}
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
            if route and route.get("tv_remote") and route.get("hdmi_code"):
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
            wake = getattr(activity, "wake_remote", None)
            if wake:
                run.transcript.append(f"↪ waking source via {wake}")
                try:
                    self.client.call_service("remote", "turn_on", wake)
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
            if route and route.get("tv_remote") and route.get("hdmi_code"):
                run.transcript.append(f"↪ re-routing display to {route['hdmi_code']}")
                try:
                    self.client.call_service("remote", "send_command",
                        route["tv_remote"], {"command": route["hdmi_code"]})
                except Exception as e:
                    run.transcript.append(f"   route command failed: {e}")
            wake = getattr(activity, "wake_remote", None)
            if wake:
                run.transcript.append(f"↪ re-waking source via {wake}")
                try:
                    self.client.call_service("remote", "turn_on", wake)
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
