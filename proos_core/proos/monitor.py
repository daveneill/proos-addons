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

# STAGE 2 BUILD 2 (16 Aug 2026): `unknown` is NOT a measurement of absence.
# A fresh entity after an HA restart sits in `unknown` and used to be
# announced "offline · not responding" (census finding M1). Only HA's own
# `unavailable` — its native absence fact — reports.
DEAD = ("unavailable",)
# States where HA actively believes the device is present/in-use. If it's one of
# these but an independent probe says unreachable, that's a contradiction.
BELIEVES_PRESENT = ("on", "playing", "paused", "idle")

# Integration-specific guidance -- turns "offline" into "here's what to do".
# Injected by the server: entity -> physical switch-port reading, or None.
# The room-level escalation reads it instead of counting lost witnesses (step 4
# of the rule inventory). None everywhere means no controller, and the original
# all-devices-lost condition is used unchanged.
PORTS_FN = None

# ── ONE HEALTH SYSTEM (Stage 2 build 2, 16 Aug 2026) ────────────────────────
# Injected by the server: () -> the watcher's live report. For any device the
# watcher watches, THIS module does not judge — it translates the watcher's
# verdict (which carries every ruling Dave made this week: A-6 silence, A-8
# resting precondition, D-1 per-device gear, switch-wins) into the room view,
# guidance words included, so Pro and the dashboard speak identical truth.
# Two systems answering "is this device okay" with different rules is Shape 1,
# the defect class this codebase produces most — this closes it at the layer
# boundary. None -> no watcher available; own judgement survives for devices
# the watcher does not cover. Carries no claim: PLUMBING.
WATCHERS = None

SUGGESTED = {
    "sonos": "Power-cycle the speaker or check its Wi-Fi; Sonos drops offline when it loses network.",
    "apple_tv": "Check the Apple TV's power and network; it may have lost its pairing.",
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

    # ── the watcher's verdicts, translated — never re-judged ───────────────
    wmap = {}
    _wfn = globals().get("WATCHERS")
    if _wfn:
        try:
            wmap = {i.get("entity"): i
                    for i in ((_wfn() or {}).get("items") or [])
                    if i.get("entity")}
        except Exception:                                        # noqa: BLE001
            wmap = {}

    # --- device faults ---
    for d in devices:
        it = wmap.get(d.entity)
        if it is not None:
            # ONE VOICE: the watcher owns this device. Its verdict and its
            # guidance are relayed verbatim; nothing here forms an opinion.
            if it.get("status") == "fault":
                v = it.get("verdict")
                if v == "offline":
                    kind, headline = "device_fault", f"{d.name} is offline"
                elif v == "no_path":
                    kind = "unreachable"
                    headline = f"{d.name} has no network path"
                else:
                    kind = "unreachable"
                    headline = f"{d.name} is not responding through its integration"
                issues.append(Issue(
                    kind=kind, headline=headline,
                    detail=(it.get("guidance")
                            or "The awareness layer has confirmed this fault "
                               "— open Health in Pro for the full reading."),
                    entity=d.entity,
                    auto_recoverable=False,
                ))
            continue
        # No watch exists: report only HA's NATIVE absence fact.
        rec = snap.get(d.entity, {})
        st = rec.get("state", "unavailable")
        if st in DEAD:
            age = _age_minutes(rec.get("last_changed"), now)
            issues.append(Issue(
                kind="device_fault",
                headline=f"{d.name} is offline",
                detail=f"Last seen {_fmt_age(age)} ago; not responding. Device or network fault.",
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
        # ONE WITNESS SYSTEM (Stage 2 build 2): for a watched device the
        # watcher already consulted the witness under the corrected rules —
        # its answer is reused here for the room fallback and never re-judged.
        # A mute witness (reachable None) counts as NOTHING, per A-6.
        it = wmap.get(d.entity)
        if it is not None:
            if it.get("has_signal"):
                witnessed.append(d)
                if it.get("reachable") is False:
                    lost.append(d)
            continue
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
                    detail=(f"Its integration still reports '{st}', but {d.name} did not answer an "
                            f"independent network probe. Likely a power or network drop "
                            f"the integration hasn't noticed yet."),
                    entity=d.entity,
                    suggested_action=SUGGESTED.get(d.integration, "Check the device's power and network."),
                    auto_recoverable=False,
                ))
            elif (cluster.display is not None
                  and d.entity == cluster.display.entity
                  and st in ("off", "standby")):
                # CLASS FACT (Dave, 9 Aug — brand-agnostic, known from the
                # integration the moment the device is added): PANELS leave the
                # network when powered down — WoL/MAC is what wakes them. A
                # display that is off + witness-gone ALONE is resting, never a
                # fault. It still counted into `lost` above, so a whole room
                # going dark (the switch test) escalates WITH the panel in it.
                pass
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
    # STEP 4 OF THE RULE INVENTORY (Dave, 16 Aug 2026: read and follow your own
    # documentation). The plan named this one: *"Topology replaces #6. Delete
    # the all-devices-lost condition."* — and it is the rule behind the
    # complaint that started the day.
    #
    # THE OLD CONDITION required EVERY witnessed device in the room to be lost.
    # Dave pulled the switch feeding the Bedroom's TV, Shield and Apple TV; two
    # of the three trackers were still stale at `home`, so `len(lost)` was 1 of
    # 3 and the room said nothing. **His switch test failed twice on exactly
    # this line**, and both times the answer was "the room-level escalation
    # covers it" — it did not.
    #
    # THE MEASUREMENT: if a device in this room sits on a switch the controller
    # says is OFFLINE, the room has no network path. That is one fact from the
    # thing the cables plug into, and it does not care how many client trackers
    # have caught up yet.
    _cut = []
    _fn = globals().get("PORTS_FN")
    if _fn:
        for d in devices:
            try:
                p = _fn(d.entity)
            except Exception:                                    # noqa: BLE001
                p = None
            if p and p.get("gear_online") is False:
                _cut.append((d, p.get("gear")))
    if _cut:
        _gear = sorted({g for _d, g in _cut if g})
        _who = _gear[0] if len(_gear) == 1 else "%d pieces of network gear" % len(_gear)
        issues.insert(0, Issue(
            kind="room_offline",
            headline=f"{getattr(ctrl, 'area', 'This room')} has no network path — "
                     f"{_who} is offline",
            detail=(f"{_who} is not reporting to the network controller, and "
                    f"{_cut[0][0].name} is on it. The devices in this room have "
                    f"not been shown to be faulty — the path to them is down. "
                    f"This is read from the switch itself, so it does not wait "
                    f"for every device to be noticed missing."),
            suggested_action="Check that switch or access point: power, its PoE "
                             "port, and its uplink cable.",
            auto_recoverable=False,
        ))
    # THE FALLBACK, where no port can be read at all: the original condition,
    # unchanged. A home with no controller behaves exactly as it did — this is
    # the same honest split as `OFFNET_WHEN_OFF`, and it is the reason the rule
    # count does not drop to zero by pretending.
    elif len(witnessed) >= 2 and len(lost) == len(witnessed):
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
