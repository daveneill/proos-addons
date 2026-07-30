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

    def _committed_cluster(self):
        """The AVCluster from this room's COMMITTED project record -- the SAME source the
        generator builds the scripts from and that pro.html shows. Using it here makes the
        dashboard's activity list + verdicts mirror EXACTLY what was committed and
        generated, so 'pro is great but the dashboard doesn't match' can't happen: both
        read one source of truth. Live discovery (below) roles purely by integration and
        ignores the installer's committed roles/inputs, which is why the dashboard drifted
        from pro. None if the room isn't committed yet -> caller falls back to discovery
        (the un-commissioned, zero-config provisional path)."""
        try:
            from . import project  # lazy: avoids controller<->project import cycle
            rec = (project.load().get("areas") or {}).get(self.area)
            if rec and rec.get("committed") and rec.get("display"):
                return project._cluster_from_record(self.client, self.area, rec)
        except Exception:
            pass
        return None

    def refresh(self):
        # Committed rooms build from the DECLARED record (identical to the generator and
        # to pro.html) so the dashboard mirrors pro exactly. Un-committed rooms fall back
        # to brand-agnostic live discovery: every discovered source becomes a PROVISIONAL
        # CEC wake-only activity (zero-config). Empty HA -> no display -> no activities.
        cluster = self._committed_cluster() or discover_av(self.client, self.area)
        self.cluster = cluster
        off_state, art_switch = self._off_config(cluster)
        acts = build_watch_activities(cluster, reachability=self.reachability,
                                      audio=self._audio_plan(cluster),
                                      off_state=off_state, art_switch=art_switch)
        # Overlay per-room commissioning on top: an explicit route (C4 / discrete),
        # a confirmed wake, a reachability witness -- and clear the provisional
        # flag. The overlay never creates activities, only enhances discovered
        # ones, so it can't produce phantoms: if a source isn't discovered, its
        # binding simply doesn't apply. The overlay lives in a config-shaped dict
        # today (COMMISSIONING, below) and will move verbatim to the add-on config
        # / installer UI without changing this engine.
        acts = apply_commissioning(self.area, acts)
        # The RECORD's commissioning overlays the runtime activities too. The
        # generator has always consumed _commissioning_from_record(); the live
        # engine only got the legacy static dict above, so committed routes
        # never reached the verdict ladder or the converger, and committed
        # rooms wrongly stayed 'provisional'. Single source of truth, applied
        # to BOTH consumers.
        try:
            from . import project as _prj
            try:
                _rec = _prj._resolve_rec(_prj.load(), self.area)
            except Exception:
                _rec = (_prj.load().get("areas") or {}).get(self.area)
            if _rec and _rec.get("committed"):
                _routes = _prj._routes_for(_rec)
                # AVR / matrix rooms: the display's input is CONSTANT (it
                # always looks at the switch), so per-source truth lives in the
                # record's avswitch plan -- the receiver's committed input name
                # per source becomes each activity's audio witness, which is
                # exactly the testimony the verdict ladder disambiguates with.
                _sw = _rec.get("avswitch") or {}
                _sw_ent, _sw_in = _sw.get("entity"), (_sw.get("inputs") or {})
                _cleared = []
                for a in acts:
                    src = getattr(a, "source_eid", None)
                    r = _routes.get(src) if src else None
                    if r and r.get("input"):
                        a.route = dict(a.route or {}, display_input=r["input"])
                        a.provisional = False
                        _cleared.append(a.key)
                    if src and _sw_ent and src in _sw_in:
                        a.audio_witness = {"entity": _sw_ent,
                                           "source": _sw_in[src]}
                        a.provisional = False
                        if a.key not in _cleared:
                            _cleared.append(a.key + "(avr)")
                    if src and src == _rec.get("display"):
                        # TV-as-its-own-source: its route IS the tuner --
                        # no input map applies, commissioning is inherent
                        a.provisional = False
                        if a.key not in _cleared:
                            _cleared.append(a.key + "(self)")
                    if not src:
                        a.provisional = False  # broadcast/off in a committed room
                print("  [controller] %s record-overlay routes=%s avswitch=%s "
                      "cleared=%s" % (self.area, _routes,
                                      {"entity": _sw_ent, "inputs": _sw_in}
                                      if _sw_ent else None, _cleared),
                      flush=True)
        except Exception as e:                               # noqa: BLE001
            print("  [controller] %s record-overlay FAILED: %s" % (self.area, e),
                  flush=True)
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

    def _off_config(self, cluster):
        """Per-display power-off behaviour from the committed project store:
        ('full'|'art', art_switch_or_None). 'full' (real power off) unless the
        installer chose Art Mode for a Frame TV, in which case we also resolve the
        art switch so the verdict treats 'in Art Mode' as off."""
        off_state, art_switch = "full", None
        try:
            from . import project, generator  # lazy: avoids import cycle
            rec = (project.load().get("areas") or {}).get(self.area)
            if rec and rec.get("off_state") == "art":
                off_state = "art"
                if cluster and cluster.display:
                    art_switch = generator._art_mode_switch(self.client, cluster.display.entity)
        except Exception:
            pass
        return off_state, art_switch

    def generate_scripts(self, overwrite: bool = False) -> dict:
        """Materialise this room's activities as editable HA scripts under the
        proos_<area>_<key> namespace. Create-if-absent by default (installer edits
        survive); overwrite=True force-regenerates. Called once at startup and on an
        explicit installer 'regenerate'. Generation is separate from refresh() so
        discovery stays a pure read."""
        # The committed AV config (project record) is the ONLY generator of activities.
        # Generate from the SAME record cluster + committed routes/off-state the commit uses, so
        # startup, commit and the whole-home self-heal are byte-identical. Un-committed -> nothing.
        from . import project
        try:
            rec = project._resolve_rec(project.load(), self.area)
        except Exception:
            rec = None
        if not (rec and rec.get("committed") and rec.get("display")):
            return {"created": [], "kept": [], "refreshed": [], "object_ids": []}
        cluster = project._cluster_from_record(self.client, self.area, rec)
        return generator.generate(self.client, cluster,
                                  project._commissioning_from_record(rec), overwrite=overwrite)

    def _script_entity_for(self, a) -> str | None:
        """The REAL generated HA script entity_id for an activity, computed with the
        generator's own naming rule so callers fire EXACTLY what generate() produced --
        no reconstruction, no drift between the dashboard and the generated scripts.

        Core keys a watch-source by entity id (watch_<object_id>) but the generator
        names the script by the source's label-slug (watch_<slug(label)>); the two never
        matched, which forced the dashboard to guess. Here we resolve the source by its
        entity (a.source_eid) and slug its label exactly as the generator does, so the id
        is authoritative. Broadcast + TV-off are fixed keys. Best-effort: any failure
        yields None and the caller keeps its own fallback."""
        try:
            area_slug = generator._slug(self.area)
            src_eid = getattr(a, "source_eid", None)
            if src_eid:
                for src in (getattr(self.cluster, "sources", None) or []):
                    if src.entity == src_eid:
                        suffix = "watch_" + generator._slug(self.cluster.label_for(src))
                        return f"script.{generator.PROOS_PREFIX}_{area_slug}_{suffix}"
                return None
            if a.key == "tv_off":
                return f"script.{generator.PROOS_PREFIX}_{area_slug}_tv_off"
            if a.key == "watch_tv":
                return f"script.{generator.PROOS_PREFIX}_{area_slug}_watch_tv"
            return None
        except Exception:
            return None

    def list_activities(self) -> list[dict]:
        snap = self.client.snapshot(
            list({e for a in self.activities.values() for e in a.entity_ids()})
        ) if self.activities else {}
        return [
            {"key": a.key, "label": a.label, "verdict": a.summary(snap).detail,
             "provisional": getattr(a, "provisional", False),
             # Authoritative execution target: the exact generated script entity_id.
             # The dashboard fires THIS (navbar + activities widget) instead of
             # reconstructing a name, so dashboard == pro == the generated activity.
             "script": self._script_entity_for(a),
             "source_eid": getattr(a, "source_eid", None)}
            for a in self.activities.values()
        ]

    def fire_plan(self, target: str) -> list:
        """Canonical ordered call_service steps to run ONE activity in this room. This is
        the SINGLE SOURCE OF TRUTH for firing: every client (the dashboard and Pro's
        test-fire) fetches this and sends it verbatim, so the two can never diverge. Rule:
        stop every OTHER activity script in the room, then turn on the target. The target's
        own concurrency is its HA run mode (single/restart/queued/parallel). Cancels are
        unconditional (an idempotent turn_off on an idle script is harmless) so two
        activities can't race regardless of any one client's stale view."""
        steps = []
        seen = set()
        for a in self.activities.values():
            eid = self._script_entity_for(a)
            if eid and eid != target and eid not in seen:
                seen.add(eid)
                steps.append({"domain": "script", "service": "turn_off", "entity_id": eid})
        steps.append({"domain": "script", "service": "turn_on", "entity_id": target})
        return steps

    # ---- Execution retired --------------------------------------------------
    # start() / heal() / recover() and the Reconciler are gone. ProOS Core no
    # longer drives devices: execution lives entirely in the HA scripts
    # (script.proos_<area>_<key>), fired from the dashboard. Core is awareness
    # only -- discovery, script generation, verdicts, health. Removing this is
    # what stops a stale desired-state from re-asserting after "TV Off".

    def status(self) -> dict:
        return self.run.to_dict() if self.run else {"status": "idle"}
