"""
The reconciliation engine.

Command-based: send commands -> hope.
ProOS: loop { read -> delta -> command delta -> wait -> re-validate } -> settle.

Settles into one of:
  ACHIEVED    - reached desired state; validated, not assumed.
  RECONCILING - mid-flight (what the dashboard polls during a run).
  DEGRADED    - tried N times, still wrong; which target failed and why.
  SUPERSEDED  - a newer intent arrived and cancelled this run.

Two optional hooks make it usable as an async service:
  should_cancel() -> bool   checked each attempt; True = stop and SUPERSEDE.
  on_state(update)          called each attempt with live per-target state, so a
                            status endpoint can show progress without re-reading HA.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time

from .model import Activity
from .ha_client import HAClient, Snapshot


class Outcome(str, Enum):
    ACHIEVED = "achieved"
    RECONCILING = "reconciling"
    DEGRADED = "degraded"
    SUPERSEDED = "superseded"


@dataclass
class TargetReport:
    name: str
    entity_id: str
    ok: bool
    detail: str
    advisory: bool


@dataclass
class Result:
    home_id: str
    activity: str
    label: str
    outcome: Outcome
    summary: str
    attempts: int
    targets: list[TargetReport] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(self, client: HAClient, *, max_attempts: int = 16,
                 settle_seconds: float = 3.0, redrive_every: int = 3, log=None):
        self.client = client
        self.max_attempts = max_attempts
        self.settle_seconds = settle_seconds
        self.redrive_every = redrive_every
        self._log = log or (lambda line: None)

    def _targets_now(self, activity: Activity, snap: Snapshot) -> list[TargetReport]:
        out = []
        for t in activity.targets:
            c = t.validate(snap)
            out.append(TargetReport(t.name, t.entity_id, c.ok, c.detail, t.advisory))
        return out

    def reconcile(self, activity: Activity, *, should_cancel=None, on_state=None) -> Result:
        eids = activity.entity_ids()
        transcript: list[str] = []

        def say(line: str):
            transcript.append(line)
            self._log(line)

        def emit(outcome: Outcome, snap: Snapshot):
            if on_state:
                on_state({
                    "outcome": outcome.value,
                    "summary": activity.summary(snap).detail,
                    "targets": [tr.__dict__ for tr in self._targets_now(activity, snap)],
                    "transcript": list(transcript),
                })

        say(f"▶ Reconcile [{activity.room} · {activity.label}] on home '{self.client.home_id}'")

        attempt = 0
        driven_at: dict[str, int] = {}
        cancelled = False
        while attempt < self.max_attempts:
            if should_cancel and should_cancel():
                cancelled = True
                say("✖ superseded by a newer request")
                break
            attempt += 1
            snap = self.client.snapshot(eids)
            emit(Outcome.RECONCILING, snap)

            checks = {t.name: t.validate(snap) for t in activity.targets}
            unmet = [t for t in activity.targets if not checks[t.name].ok]
            if not unmet:
                say(f"  ✓ attempt {attempt}: all targets satisfied")
                break

            drivable, blocked = [], []
            for t in unmet:
                (drivable if all(checks[d].ok for d in t.after) else blocked).append(t)

            say(f"  • attempt {attempt}: {len(drivable)} active, "
                f"{len(blocked)} waiting on prerequisites")
            for t in blocked:
                say(f"      ⏸ {t.name} waits for {[d for d in t.after if not checks[d].ok]}")
            for t in drivable:
                recently = t.name in driven_at and (attempt - driven_at[t.name]) < self.redrive_every
                if recently:
                    say(f"      ⏳ {t.name} converging ({checks[t.name].detail})")
                    continue
                cmds = t.drive(snap)
                if cmds:
                    driven_at[t.name] = attempt
                for cmd in cmds:
                    say(f"      → {cmd}")
                    self.client.call_service(cmd.domain, cmd.service, cmd.entity_id, cmd.data)
            if self.settle_seconds:
                time.sleep(self.settle_seconds)

        final = self.client.snapshot(eids)
        reports = self._targets_now(activity, final)
        hard_fail = any((not r.ok and not r.advisory) for r in reports)
        summary = activity.summary(final)

        if cancelled:
            outcome = Outcome.SUPERSEDED
        elif summary.ok and not hard_fail:
            outcome = Outcome.ACHIEVED
        else:
            outcome = Outcome.DEGRADED
        say(f"■ {outcome.value.upper()}: {summary.detail}  (after {attempt} attempt(s))")
        emit(outcome, final)

        return Result(
            home_id=self.client.home_id, activity=activity.key, label=activity.label,
            outcome=outcome, summary=summary.detail, attempts=attempt,
            targets=reports, transcript=transcript,
        )
