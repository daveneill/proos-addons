"""
Operational monitoring -- the layer that answers "is everything actually working?"

The key design choice, forced by reality: this HA has 180+ unavailable/unknown
entities (mostly phone/tablet sensors that sleep). Surfacing those is the
traditional-system failure mode -- alert spam. ProOS instead watches ONLY the
devices Core orchestrates (the discovered cluster per room) and turns raw state
into diagnosed, human-readable health.

Two kinds of problem:
  * device fault  - a device Core controls is unavailable/unknown (offline).
  * state drift   - a room has an asserted activity but reality no longer matches
                    (someone grabbed a remote, a device dropped, etc).

Each issue carries a diagnosis: what, since when, a suggested action, and whether
ProOS could recover it automatically. That last flag is the seed of self-healing.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import threading
import time

from .reachability import resolve as resolve_reachability

DEAD = ("unavailable", "unknown")
# States where HA actively believes the device is present/in-use. If it's one of
# these but an independent probe says unreachable, that's a contradiction.
BELIEVES_PRESENT = ("on", "playing", "paused", "idle")

# Integration-specific guidance -- turns "offline" into "here's what to do".
SUGGESTED = {
    "sonos": "Power-cycle the speaker or check its Wi-Fi; Sonos drops off HA when it loses network.",
    "apple_tv": "Check the Apple TV's power and network; it may have lost its HA pairing.",
    "androidtv_remote": "Check the Shield's power and network connection.",
    "samsungtv": "Expected if the TV is fully powered down; if it should be on, check its network.",
    "samsungtv_smart": "Expected if the TV is fully powered down; if it should be on, check its network.",
    "heos": "Check the receiver's power and network connection.",
}


def _age_minutes(iso: str | None, now: float) -> float | None:
    if not iso:
        return None
    try:
        # HA timestamps are ISO8601 with offset; mock uses epoch floats.
        if isinstance(iso, (int, float)):
            return round((now - float(iso)) / 60, 1)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 60, 1)
    except Exception:
        return None


def _fmt_age(mins: float | None) -> str:
    if mins is None:
        return "an unknown time"
    if mins < 1:
        return "less than a minute"
    if mins < 60:
        return f"{int(mins)} min"
    return f"{mins/60:.1f} hr"


@dataclass
class Issue:
    kind: str    # 'device_fault' | 'unreachable' | 'presumed_dead' | 'room_offline'
    headline: str
    detail: str
    entity: str | None = None
    since_minutes: float | None = None
    suggested_action: str | None = None
    auto_recoverable: bool = False


@dataclass
class RoomHealth:
    area: str
    status: str                     # 'ok' | 'attention' | 'fault'
    summary: str
    asserted: str | None
    issues: list[Issue] = field(default_factory=list)
    checked_at: float = 0.0

    def to_dict(self):
        d = asdict(self)
        return d


def check_room(ctrl) -> RoomHealth:
    """Compute health for one room from a fresh snapshot of its devices only."""
    now = time.time()
    cluster = ctrl.cluster
    devices = ([cluster.display] if cluster.display else []) + cluster.sources + cluster.audio
    eids = [d.entity for d in devices]
    snap = ctrl.client.snapshot(eids) if eids else {}

    issues: list[Issue] = []

    # --- device faults (offline gear we actually control) ---
    for d in devices:
        rec = snap.get(d.entity, {})
        st = rec.get("state", "unavailable")
        if st in DEAD:
            age = _age_minutes(rec.get("last_changed"), now)
            issues.append(Issue(
                kind="device_fault",
                headline=f"{d.name} is offline",
                detail=(f"Last seen {_fmt_age(age)} ago; not responding. "
                        f"{'Device or network fault.' if st == 'unavailable' else 'State unknown.'}"),
                entity=d.entity,
                since_minutes=age,
                suggested_action=SUGGESTED.get(d.integration, "Check the device's power and network."),
                auto_recoverable=False,   # offline gear usually needs a human/network, not us
            ))

    # --- independent reachability: catch what HA's passive availability misses ---
    # THE SWITCH-TEST RULE (Dave, 8 Aug 2026 — device_offline_bench): the
    # witness is consulted WHATEVER the integration claims the power state is.
    # Dave pulled the switch feeding the Bedroom's TV, Shield and Apple TV; the
    # integrations settled to "off" within seconds, the old gate below only ran
    # for BELIEVES_PRESENT states, and the product said All Systems Normal while
    # holding the contradicting evidence. "off" is not evidence of health —
    # off + witness-PRESENT is a normal off room; off + witness-GONE is a device
    # that stopped answering, and the product says so. No witness bound = say
    # nothing (fail open, tenet 10). The UniFi tracker's own consider_home
    # window is the debounce; we do not second-guess it here.
    reach_map = getattr(ctrl, "reachability", {}) or {}
    witnessed, lost = [], []
    for d in devices:
        spec = reach_map.get(d.entity)
        if not spec:
            continue
        witnessed.append(d)
        reachable = resolve_reachability(spec, ctrl.client)
        if reachable is False:
            lost.append(d)
            st = snap.get(d.entity, {}).get("state", "unavailable")
            if st in BELIEVES_PRESENT:
                # HA still believes it's online, but it isn't.
                issues.append(Issue(
                    kind="unreachable",
                    headline=f"{d.name} is not responding on the network",
                    detail=(f"HA still reports '{st}', but {d.name} did not answer an "
                            f"independent network probe. Likely a power or network drop "
                            f"HA's integration hasn't detected yet."),
                    entity=d.entity,
                    suggested_action=SUGGESTED.get(d.integration, "Check the device's power and network."),
                    auto_recoverable=False,
                ))
            else:
                # Claims off/standby AND gone from the network: presumed dead.
                # Confirmed by the independent witness, never guessed from state.
                issues.append(Issue(
                    kind="presumed_dead",
                    headline=f"{d.name} is not answering on the network",
                    detail=(f"{d.name} reports '{st}', but it has also dropped off "
                            f"the network — its independent witness cannot see it. "
                            f"A switched-off device normally stays on the network; "
                            f"this looks like power, a cable or the network path."),
                    entity=d.entity,
                    suggested_action=SUGGESTED.get(d.integration, "Check the device's power and network."),
                    auto_recoverable=False,
                ))

    # --- whole-room escalation: a switch, not a string of coincidences ---
    # Every witnessed device in the room gone at once is infrastructure. Say it
    # ONCE, as the finding it is, instead of burying it in per-device faults.
    if len(witnessed) >= 2 and len(lost) == len(witnessed):
        issues.insert(0, Issue(
            kind="room_offline",
            headline=f"All of {getattr(ctrl, 'area', 'this room')}'s devices are off the network",
            detail=(f"All {len(lost)} witnessed devices in the room stopped answering "
                    f"the network together. That pattern is a network switch, access "
                    f"point or cable feeding the room — not {len(lost)} devices "
                    f"failing at once."),
            suggested_action="Check the network switch/PoE port or access point that feeds this room.",
            auto_recoverable=False,
        ))

    # (The pre-reset "state drift" branch lived here: it compared the room
    # to an ASSERTED activity and offered to restore it — control-era
    # thinking removed in the product reset of 2 Aug (rooms claim only
    # COMMANDED activities; external control is legitimate; nothing is
    # forced back). ctrl.asserted is no longer set, so the branch was
    # dead machinery — deleted 3 Aug.
    #
    # 4 Aug: that deletion also removed the line that BOUND `asserted`,
    # while two readers below still referenced it — so every check raised
    # NameError ("[monitor] Family Room check failed: name 'asserted' is
    # not defined", live log). The room's health never completed, which is
    # why the Pro aura stayed lit. Bound explicitly to None: a room has no
    # asserted activity in the state world, and the summary/field simply
    # say nothing.)
    asserted = None

    _FAULT_KINDS = ("device_fault", "unreachable", "presumed_dead", "room_offline")
    fault = any(i.kind in _FAULT_KINDS for i in issues)
    if fault:
        if any(i.kind == "room_offline" for i in issues):
            status = "fault"
            summary = "Room off the network — check its switch or access point"
        else:
            nfault = sum(i.kind in _FAULT_KINDS for i in issues)
            status, summary = "fault", f"{nfault} device(s) need attention"
    else:
        n = len(devices)
        status, summary = "ok", f"All {n} device(s) healthy" + (
            f" · {asserted.label}" if asserted else "")

    return RoomHealth(area=ctrl.area, status=status, summary=summary,
                      asserted=asserted.label if asserted else None,
                      issues=issues, checked_at=now)


class Monitor:
    """Background loop that keeps each room's health current; optional auto-heal."""

    def __init__(self, controllers: dict, interval: float = 20.0,
                 auto_heal: bool = False, log=print):
        self.controllers = controllers
        self.interval = interval
        self.auto_heal = auto_heal
        self._log = log
        self.cache: dict[str, RoomHealth] = {}
        self._last_heal: dict[str, float] = {}

    def check(self, area: str) -> RoomHealth | None:
        ctrl = self.controllers.get(area)
        if not ctrl:
            return None
        h = check_room(ctrl)
        self.cache[area] = h
        # auto-heal retired: the monitor is read-only -- it diagnoses and reports,
        # never drives devices. (auto_heal option kept but inert.)
        return h

    def all(self) -> dict:
        return {a: h.to_dict() for a, h in self.cache.items()}

    def start(self):
        def loop():
            while True:
                for area in list(self.controllers):
                    try:
                        self.check(area)
                    except Exception as e:
                        self._log(f"[monitor] {area} check failed: {e}")
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True).start()
