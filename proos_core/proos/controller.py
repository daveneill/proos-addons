"""
Room controller -- the per-room awareness brain.

Holds the discovered cluster + generated activities and exposes the read path:
discovery (refresh), script materialisation (generate_scripts), and per-activity
verdicts (list_activities). Core no longer drives devices -- execution lives in
the generated HA scripts (script.proos_<area>_<key>), fired from the dashboard --
so there is no reconcile loop, no background run, and nothing here that can
re-assert a room's state.
"""
from __future__ import annotations

from .discovery import discover_av
from .activities import build_watch_activities
from . import generator


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
#
# Reachability witness = the source's router device_tracker (UniFi, src_type
# 'router'), state 'home'/'not_home'. Resolved at commissioning by IP (the
# device's actual network address), NOT by MAC: Apple TVs present randomized
# private Wi-Fi MACs and Shields present a different interface MAC than the
# router sees, so a source-side MAC never matches the tracker. IP is the stable
# join; the resolved pairing is stored here so runtime never re-guesses.
COMMISSIONING: dict = {
    "Family Room": {
        "media_player.family_room_apple_tv": {"reachability_sensor": "device_tracker.apple_tv_family_room"},
        "media_player.family_room_shield_tv": {"reachability_sensor": "device_tracker.shield_family_room"},
    },
    "Bedroom": {
        "media_player.bedroom_apple_tv": {"reachability_sensor": "device_tracker.apple_tv_bedroom"},
        "media_player.bedroom_shield_tv": {"reachability_sensor": "device_tracker.shield_bedroom"},
    },
    "Living Room": {
        # TV audio = Marantz/Denon AV receiver (not a Sonos). Each video source
        # lands on its own AVR input; broadcast TV uses the ARC return. Apple TV
        # and TV Audio name-match automatically; the Shield's physical AVR input
        # is install-specific and must be commissioned (left unset until then so
        # we never select a wrong input).
        "audio": {
            "mode": "avr",
            "entity": "media_player.living_room_av_receiver",
            "inputs": {
                "media_player.living_room_apple_tv": "Apple TV",
                "media_player.living_room_shield_tv": "Blu-ray",
            },
            "broadcast": "TV Audio",
            "power": True,
        },
        "media_player.living_room_apple_tv": {"reachability_sensor": "device_tracker.apple_tv_living_room"},
        "media_player.living_room_shield_tv": {"reachability_sensor": "device_tracker.shield_living_room"},
    },
}


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


class RoomController:
    def __init__(self, client, area: str, reachability: dict | None = None):
        self.client = client
        self.area = area
        self.reachability = reachability or {}
        # Inert sentinels read by the monitor's awareness checks. `run` stays None
        # (execution retired); `asserted` tracks the room's apparent desired state
        # for drift reporting (set by the monitor, never used to drive anything).
        self.run = None
        self.asserted = None
        self.activities: dict = {}
        self.refresh()
        # On first sight of a room, materialise its activities as editable HA
        # scripts (create-if-absent, so installer edits survive). Best-effort: a
        # generation failure must never break discovery or the read path.
        try:
            self.generate_scripts()
        except Exception as e:
            print(f"[generator] {self.area}: script generation skipped ({e})")

    def refresh(self):
        cluster = discover_av(self.client, self.area)
        self.cluster = cluster
        # Brand-agnostic discovery: every discovered source becomes a PROVISIONAL
        # CEC wake-only activity. Works with zero commissioning -- no Control4, no
        # discrete codes -- on any room with a display + sources. Empty HA -> no
        # display -> no activities (empty->empty holds by construction).
        acts = build_watch_activities(cluster, reachability=self.reachability,
                                      audio=self._audio_plan(cluster))
        # Overlay per-room commissioning on top: an explicit route (C4 / discrete),
        # a confirmed wake, a reachability witness -- and clear the provisional
        # flag. The overlay never creates activities, only enhances discovered
        # ones, so it can't produce phantoms: if a source isn't discovered, its
        # binding simply doesn't apply. The overlay lives in a config-shaped dict
        # today (COMMISSIONING, below) and will move verbatim to the add-on config
        # / installer UI without changing this engine.
        acts = apply_commissioning(self.area, acts)
        self.activities = {a.key: a for a in acts}

    def _audio_plan(self, cluster) -> dict | None:
        """Resolve this room's audio plan (Sonos/AVR/TV) via the generator's
        resolver, so the verdict's audio witness matches exactly what the
        generated scripts route. Best-effort: any failure -> no witness."""
        try:
            override = (COMMISSIONING.get(self.area) or {}).get("audio")
            return generator._audio_config(self.client, cluster, override)
        except Exception:
            return None

    def generate_scripts(self, overwrite: bool = False) -> dict:
        """Materialise this room's activities as editable HA scripts under the
        proos_<area>_<key> namespace. Create-if-absent by default (installer edits
        survive); overwrite=True force-regenerates. Called once at startup and on an
        explicit installer 'regenerate'. Generation is separate from refresh() so
        discovery stays a pure read."""
        commissioning = COMMISSIONING.get(self.area)
        return generator.generate(self.client, self.cluster, commissioning, overwrite=overwrite)

    def list_activities(self) -> list[dict]:
        snap = self.client.snapshot(
            list({e for a in self.activities.values() for e in a.entity_ids()})
        ) if self.activities else {}
        return [
            {"key": a.key, "label": a.label, "verdict": a.summary(snap).detail,
             "provisional": getattr(a, "provisional", False)}
            for a in self.activities.values()
        ]

    # ---- Execution retired --------------------------------------------------
    # start() / heal() / recover() and the Reconciler are gone. ProOS Core no
    # longer drives devices: execution lives entirely in the HA scripts
    # (script.proos_<area>_<key>), fired from the dashboard. Core is awareness
    # only -- discovery, script generation, verdicts, health. Removing this is
    # what stops a stale desired-state from re-asserting after "TV Off".

    def status(self) -> dict:
        return self.run.to_dict() if self.run else {"status": "idle"}
