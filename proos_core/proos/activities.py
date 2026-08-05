"""
Watch-activity factory.

ROUTING MODEL (rewritten 2026-06-22, after live debugging on Dave's Family Room):

The display is routed by DISCRETE HDMI INPUT COMMANDS, not by waking the source
and hoping CEC follows. On this hardware the Samsung can't report its input, and
an already-awake Apple TV won't re-assert CEC -- so "wake the source" routes
reliably only from cold. Discrete codes (KEY_HDMI1 / KEY_HDMI3 / KEY_TV, fired at
the TV's `remote` entity) are stateless and idempotent: "go to this input" always
lands, regardless of source power or what was on before. That is the Control4
discrete model, and it dissolves the whole blue-screen class.

Consequences, baked into the targets below:
  * display  -> AUTHORITATIVE routing. Power TV on, then fire the source's
                discrete input code. Validation reports the input as COMMANDED
                (we own it), since the panel still can't read it back.
  * source   -> ADVISORY only. We wake the device so there's a picture, but we no
                longer block on, or trust, its (tvOS-26-flaky) power/idle state.
                A broken pyatv channel can no longer make routing fail or lie.
  * compete  -> best-effort power-down of other sources. Advisory: leaving a
                competitor awake no longer changes which input is on screen.

"Watch TV" (broadcast) is the same factory with broadcast=True: route to KEY_TV,
no external source to wake.
"""
from __future__ import annotations
from .model import Activity, Target, Command, Check
from .ha_client import Snapshot

# A source device that's doing something useful is in one of these states.
ALIVE = {"on", "playing", "paused", "idle", "standby"}
# A source that is genuinely not competing for the screen.
OFFISH = {"off", "standby", "idle", "unavailable", "unknown"}


def _st(snap: Snapshot, eid: str) -> str:
    return (snap.get(eid) or {}).get("state", "unavailable")


# States that count as "watching" for the binary verdict. Stricter than ALIVE
# (which the reconciler uses): 'idle'/'standby' = home screen / screensaver, not
# watching. Matches the live verdict sensors exactly.
WATCHING_STATES = {"playing", "paused", "on"}


def _art_on(snap: Snapshot, display_eid: str, art_switch: str | None = None) -> bool:
    """True if a Frame TV is resting in Art Mode. Reads the dedicated art switch
    when known (device-based, survives entity-id suffixes), else the media_player's
    frame_art_mode attribute. Best-effort: unknown -> False (falls back to power)."""
    if art_switch and _st(snap, art_switch) == "on":
        return True
    a = (snap.get(display_eid) or {}).get("attributes") or {}
    # VERIFIED on the live Frame: modern samsungtv exposes `art_mode_status`
    # ('on'/'off'); `frame_art_mode` kept for other/older builds.
    if str(a.get("art_mode_status", "")).lower() in ("on", "true"):
        return True
    return bool(a.get("frame_art_mode"))


def _audio_confirmed(snap: Snapshot, witness: dict | None) -> bool:
    """True if the room's audio is actually routed where it should be -- the audio
    device (Sonos/AVR) is on and reporting the expected source/input. The
    independent confirmation that audio, not just picture, is up. No witness (TV
    speakers) -> always True."""
    if not witness or not witness.get("entity"):
        return True
    rec = snap.get(witness["entity"])
    if rec is None:
        # The plan names a device this house does not have -- a stale id, a typo,
        # a renamed entity. That is a BROKEN PLAN, not evidence that audio is
        # unrouted, and it must never be readable as 'off': one bad id would
        # otherwise pin a lit room dark forever with nothing on screen to explain
        # it. Live house, 30 Jul 2026: this cost the Living Room every Live TV
        # verdict it should have had, for as long as the entry existed.
        return True
    if rec.get("state") in ("off", "unavailable", "unknown", None):
        return False
    return (rec.get("attributes", {}) or {}).get("source") == witness.get("source")


def make_watch_activity(*, area: str, key: str, source_label: str,
                        display_eid: str, tv_remote: str | None = None,
                        hdmi_code: str | None = None,
                        source_eid: str | None = None,
                        wake_remote: str | None = None,
                        wake: dict | None = None,
                        reachability_sensor: str | None = None,
                        audio_witness: dict | None = None,
                        competing_eids: list[str] | None = None,
                        broadcast: bool = False,
                        provisional: bool = False,
                        off_state: str = "full",
                        art_switch: str | None = None,
                        c4_select: dict | None = None) -> Activity:
    """
    Build one 'Watch <source_label>' activity.

    display_eid  - the TV media_player (power + on/off readback)
    tv_remote    - the TV's `remote.` entity (carries the discrete input codes)
    hdmi_code    - discrete input command for this source, e.g. 'KEY_HDMI1'/'KEY_TV'
    source_eid   - the source device's media_player (for state/advisory only)
    wake_remote  - the source's `remote.` entity, used to WAKE it. We wake via
                   the remote, not media_player.turn_on, because on tvOS 26 the
                   Apple TV's media_player power path is broken by the pyatv
                   Companion bug, while remote.turn_on goes over a path that
                   still works (proven live: it returned verified_state=on where
                   media_player.turn_on was a silent no-op).
    competing_eids - other sources to power down (advisory)
    broadcast    - True for the tuner ('Watch TV'); no source device
    """
    competing_eids = competing_eids or []

    # ---- display: power only. Routing is a ONE-SHOT action fired by the
    #      controller on every activation (see RoomController._route), not a
    #      convergence target -- re-issuing an input is a discrete action like a
    #      remote keypress, not a state to converge toward, and the reconciler
    #      stops driving a target the moment it validates (TV simply 'on').
    def drive_display(snap: Snapshot) -> list[Command]:
        if _st(snap, display_eid) != "on":
            return [Command("media_player", "turn_on", display_eid)]
        return []

    def validate_display(snap: Snapshot) -> Check:
        s = _st(snap, display_eid)
        if s == "on":
            how = f"routed to {hdmi_code}" if hdmi_code else "CEC wake-only"
            return Check(True, f"TV on ({how}, discrete not readback-confirmable)")
        if s in ("unavailable", "unknown"):
            # Samsung drops its connection and reports unavailable while on -- a
            # connection blip, not a real off. Tolerate it so a flapping TV
            # doesn't fail the activity; the drive only re-fires on a real off.
            return Check(True, f"TV power unconfirmable ('{s}'); Samsung connection blip")
        return Check(False, f"TV power is '{s}', expected on")

    targets = [
        Target("display", display_eid, "TV on",
               drive_display, validate_display),
    ]

    # ---- source: ADVISORY, report-only. The WAKE is fired once at activation
    #      by the controller (unconditional remote.turn_on), because the source's
    #      claimed state can't be trusted here. So drive_source does nothing --
    #      it must not re-wake on the phantom 'paused', and must not fight the
    #      activation wake. It exists purely so the source's state is reported.
    if source_eid and not broadcast:
        def drive_source(snap: Snapshot) -> list[Command]:
            return []  # wake is owned by RoomController activation, not the loop

        def validate_source(snap: Snapshot) -> Check:
            s = _st(snap, source_eid)
            if s in ALIVE:
                return Check(True, f"{source_label} is '{s}'")
            # Advisory: tvOS-26 flakiness here must not fail the activity.
            return Check(False, f"{source_label} power is '{s}' (advisory; routing is independent)")

        targets.append(Target("source", source_eid, f"{source_label} awake",
                              drive_source, validate_source,
                              after=["display"], advisory=True))

    # ---- competing sources: advisory power-down (no longer routing-critical).
    def make_compete(eid: str):
        def drive(snap: Snapshot) -> list[Command]:
            if _st(snap, eid) in OFFISH:
                return []
            return [Command("media_player", "turn_off", eid)]

        def validate(snap: Snapshot) -> Check:
            s = _st(snap, eid)
            if s in OFFISH:
                return Check(True, f"not competing ('{s}')")
            return Check(False, f"competing source is '{s}' (advisory)")
        return drive, validate

    for i, eid in enumerate(competing_eids):
        d, v = make_compete(eid)
        targets.append(Target(f"compete_{i}", eid, "source powered down",
                              d, v, advisory=True))

    # ---- summary: state-based verdict. For a source activity, the room is
    #      authoritatively "watching X" only when BOTH independent signals agree:
    #      the source's own state is alive AND its reachability witness is online.
    #      This is the core of state-based vs control-based -- we never report
    #      green on a claim alone. When no reachability witness is commissioned,
    #      we fall back to source-state only and flag the verdict unverified.
    #      A provisional (auto-discovered, uncommissioned) activity is always
    #      flagged so the installer can see it needs setup.
    def summary(snap: Snapshot) -> Check:
        # Verdict is binary: WATCHING or OFF. TV power stays ADVISORY (the Samsung
        # mis-reports it), so it never flips the verdict. Reachability is only a
        # guard -- a source that's genuinely off the network can't be "watching".
        # The rich detail (app, title, audio witness) lives on the area summary,
        # not here.
        disp = _st(snap, display_eid)
        if broadcast:
            # No source device -- the tuner IS the source, so TV power is the only
            # signal. Treat 'unavailable' as unknown; only off/standby is "off".
            # Audio must also be on the right device/input if a witness is set.
            # Audio is ADVISORY here, deliberately NOT a gate. For a SOURCE
            # activity the audio witness disambiguates WHICH source is on air, and
            # it stays decisive there. For broadcast there is no source device, so
            # the panel's own power and tuner input are the direct evidence of
            # what is on screen. Gating on audio meant a receiver left off -- or
            # merely named wrongly in the plan -- could deny a plainly-lit room
            # its verdict.
            if disp in ("off", "standby") or (off_state == "art" and _art_on(snap, display_eid, art_switch)):
                return Check(False, f"{area} is off")
            return Check(True, f"{area} is watching TV")
        src_live = source_eid is not None and _st(snap, source_eid) in WATCHING_STATES
        reach = _st(snap, reachability_sensor) if reachability_sensor else "home"
        if src_live and reach not in ("not_home", "off") and _audio_confirmed(snap, audio_witness):
            return Check(True, f"{area} is watching {source_label}")
        return Check(False, f"{area} is off")

    act = Activity(key=key, room=area,
                   label=("Watch TV" if broadcast else f"Watch {source_label}"),
                   targets=targets, summary=summary)
    # The route this activity wants on screen. The controller fires it once per
    # activation (power-independent, idempotent). Not a Target because it isn't a
    # convergence loop -- it's a deterministic action.
    #   c4_select form: {"c4_entity": media_player, "c4_source": "Apple TV"}
    #     -> fired via media_player.select_source. Control4 owns the discrete
    #        switch (it speaks Samsung's IP command set reliably); ProOS uses it
    #        purely as the command executor. C4's own 'source' echo is NOT
    #        trusted -- verification stays on the independent witnesses below.
    #   discrete form: {"tv_remote": remote, "hdmi_code": "KEY_HDMI1"}
    #     -> fired via remote.send_command (the original direct-to-TV path).
    if c4_select:
        act.route = {"c4_entity": c4_select["c4_entity"],
                     "c4_source": c4_select["c4_source"]}
    elif tv_remote and hdmi_code:
        act.route = {"tv_remote": tv_remote, "hdmi_code": hdmi_code}
    else:
        # No explicit route -> pure CEC: the wake pulls the TV to this source's
        # input. Brand-agnostic default; works with no discrete codes at all.
        act.route = None
    # The wake fired once per activation alongside the route. Fired
    # unconditionally because the source's own 'awake' state is exactly what we
    # can't trust here (tvOS-26 phantom 'paused'); re-asserting an already-awake
    # device is harmless. The wake carries its own domain/service/entity because
    # the path that actually wakes each device differs -- e.g. the Apple TV only
    # wakes via media_player.turn_on on its media_player, NOT remote.turn_on.
    #   explicit `wake` dict wins: {"domain","service","entity"}
    #   else fall back to the legacy remote.turn_on on wake_remote.
    if wake and (source_eid and not broadcast):
        act.wake = wake
    elif source_eid and not broadcast:
        # Default wake = the source's OWN media_player turn_on. Power -- on/off,
        # wake/sleep -- is ALWAYS media_player, never the `remote` entity, for
        # every device including the TV. Proven on the Apple TV: remote.turn_on
        # is a no-op there, media_player.turn_on works.
        act.wake = {"domain": "media_player", "service": "turn_on", "entity": source_eid}
    else:
        act.wake = None
    act.wake_remote = None  # power never goes through the remote entity
    act.source_eid = source_eid if not broadcast else None
    act.reachability_sensor = reachability_sensor if (source_eid and not broadcast) else None
    act.audio_witness = audio_witness
    act.provisional = provisional
    return act


def make_off_activity(*, area: str, display_eid: str,
                      source_eids: list[str] | None = None,
                      provisional: bool = False,
                      off_state: str = "full",
                      art_switch: str | None = None) -> Activity:
    """Build the room's 'TV Off' activity.

    Turns the display off (and sleeps the sources) and confirms it -- unlike
    routing, TV power IS readback-confirmable, so this is a fully verified action
    out of the box (not provisional). Power is media_player.turn_off everywhere,
    never the remote entity. Sources are slept advisory: a source that ignores
    turn_off doesn't fail the activity (the TV being off is what matters).
    """
    source_eids = source_eids or []

    def _off_ok(snap: Snapshot) -> bool:
        # 'off' is reached by real power-off, OR (art mode) by resting in Art Mode.
        return _st(snap, display_eid) in OFFISH or (
            off_state == "art" and _art_on(snap, display_eid, art_switch))

    def drive_display(snap: Snapshot) -> list[Command]:
        if _off_ok(snap):
            return []
        if off_state == "art" and art_switch:
            return [Command("switch", "turn_on", art_switch)]
        return [Command("media_player", "turn_off", display_eid)]

    def validate_display(snap: Snapshot) -> Check:
        if _off_ok(snap):
            in_art = off_state == "art" and _art_on(snap, display_eid, art_switch)
            return Check(True, "TV is in Art Mode" if in_art else "TV is off")
        want = "Art Mode" if off_state == "art" else "off"
        return Check(False, f"TV power is '{_st(snap, display_eid)}', expected {want}")

    targets = [Target("display", display_eid, "TV off",
                      drive_display, validate_display)]

    # Sleep each source via its OWN media_player.turn_off (advisory).
    def make_sleep(eid: str):
        def drive(snap: Snapshot) -> list[Command]:
            if _st(snap, eid) in OFFISH:
                return []
            return [Command("media_player", "turn_off", eid)]
        def validate(snap: Snapshot) -> Check:
            s = _st(snap, eid)
            if s in OFFISH:
                return Check(True, f"source asleep ('{s}')")
            return Check(False, f"source is '{s}' (advisory)")
        return drive, validate

    for i, eid in enumerate(source_eids):
        d, v = make_sleep(eid)
        targets.append(Target(f"sleep_{i}", eid, "source asleep",
                              d, v, advisory=True))

    def summary(snap: Snapshot) -> Check:
        if _off_ok(snap):
            return Check(True, f"{area} TV is off")
        return Check(False, f"{area} TV is on")

    act = Activity(key="tv_off", room=area, label="TV Off",
                   targets=targets, summary=summary)
    act.provisional = provisional
    return act
# Default wake path per source integration. CEC routing depends on the source
# actually waking; the path that wakes it differs by platform. media_player.
# turn_on is the broadly-correct default (Apple TV needs it specifically;
# remote.turn_on is a no-op there). Commissioning can override per source.
DEFAULT_WAKE = {
    "apple_tv":         ("media_player", "turn_on"),
    "androidtv_remote": ("media_player", "turn_on"),
    "androidtv":        ("media_player", "turn_on"),
}


def _default_wake_for(integration: str, entity: str) -> dict:
    domain, service = DEFAULT_WAKE.get(integration, ("media_player", "turn_on"))
    return {"domain": domain, "service": service, "entity": entity}


def _sensor_of(spec) -> str | None:
    """Pull the reachability binary_sensor out of a reachability spec, if any."""
    if isinstance(spec, dict):
        return spec.get("sensor")
    return None


def _witness_for(audio, *, source_eid=None, broadcast=False):
    """Per-activity audio witness {entity, source} from the room's audio plan, or
    None (TV speakers / no plan). Mirrors the generator's audio routing so the
    verdict checks exactly what the activity set."""
    if not audio:
        return None
    mode = audio.get("mode")
    ent = audio.get("entity")
    if mode == "tv" or not ent:
        return None
    if mode == "sonos":
        return {"entity": ent, "source": audio.get("source", "TV")}
    if mode == "avr":
        src = audio.get("broadcast") if broadcast else (audio.get("inputs") or {}).get(source_eid)
        return {"entity": ent, "source": src} if src else None
    return None


def build_watch_activities(cluster, reachability: dict | None = None,
                           audio: dict | None = None,
                           off_state: str = "full",
                           art_switch: str | None = None) -> list[Activity]:
    """
    Generate Watch activities from a discovered cluster -- brand-agnostic.

    Every discovered source gets a PROVISIONAL, CEC wake-only activity: tapping
    it wakes the source (via its integration's default wake) and lets HDMI-CEC
    one-touch-play pull the TV to that input. No discrete codes, no per-brand
    routing table -- works on any room with a display + sources, with nothing
    commissioned. Commissioning later overlays an explicit route / confirmed
    wake / reachability + audio witness and clears the provisional flag.

    audio: the resolved audio plan (generator._audio_config). Each activity gets
    the matching audio witness so "watching" confirms audio is routed, not just
    the picture.
    """
    if cluster.display is None:
        return []
    reachability = reachability or {}
    out: list[Activity] = []
    for src in cluster.sources:
        if getattr(cluster, "avswitch", None) and src.entity == cluster.avswitch:
            continue   # the room's AV switch is not a watchable source (brand-agnostic)
        label = cluster.label_for(src)
        others = [s.entity for s in cluster.sources if s.entity != src.entity]
        slug = src.entity.split(".", 1)[-1]
        out.append(make_watch_activity(
            area=cluster.area, key=f"watch_{slug}", source_label=label,
            display_eid=cluster.display.entity, source_eid=src.entity,
            wake=_default_wake_for(src.integration, src.entity),
            reachability_sensor=_sensor_of(reachability.get(src.entity)),
            audio_witness=_witness_for(audio, source_eid=src.entity),
            competing_eids=others, provisional=True,
        ))
    # Broadcast TV: the display's own tuner. Sends select_source "TV" on the
    # display by default -- the "TV" source is standard across most TV
    # integrations (and present on this Samsung's source_list), so broadcast
    # works out of the box without a per-brand code. Where a TV doesn't expose
    # "TV", the call fails harmlessly and the display still powers on;
    # commissioning can override with the right tuner command.
    all_sources = [s.entity for s in cluster.sources]
    tv_act = make_watch_activity(
        area=cluster.area, key="watch_tv", source_label="TV",
        display_eid=cluster.display.entity, source_eid=None,
        audio_witness=_witness_for(audio, broadcast=True),
        competing_eids=all_sources, broadcast=True, provisional=True,
        off_state=off_state, art_switch=art_switch,
    )
    tv_act.route = {"select_entity": cluster.display.entity, "select_source": "TV"}
    out.append(tv_act)
    # TV Off: every room with a display gets it. Turns the display off AND
    # sleeps the sources (all via media_player.turn_off). Fully verified (TV
    # power is readback-confirmable), so NOT provisional.
    out.append(make_off_activity(
        area=cluster.area, display_eid=cluster.display.entity,
        source_eids=[s.entity for s in cluster.sources],
        off_state=off_state, art_switch=art_switch))
    return out

# ---- Hardcoded Family Room map (confirmed live on Dave's HA 2026-06-22) ------
# Discrete input map verified by firing codes and watching the panel:
#   Apple TV -> KEY_HDMI1 (panel HDMI 2)
#   Shield   -> KEY_HDMI3 (panel HDMI 4)
#   Broadcast-> KEY_TV
# Fired at the Samsung's own remote entity, independent of the Apple TV channel.
TV         = "media_player.family_room_family_room_tv"
TV_REMOTE  = "remote.family_room_family_room_tv"
APPLE      = "media_player.family_room_apple_tv"
APPLE_REMOTE  = "remote.family_room_apple_tv"
SHIELD     = "media_player.family_room_shield_tv"
SHIELD_REMOTE = "remote.family_room_shield_tv"
SONOS      = "media_player.family_room"

family_watch_appletv = make_watch_activity(
    area="Family Room", key="family_watch_appletv", source_label="Apple TV",
    display_eid=TV, tv_remote=TV_REMOTE, hdmi_code="KEY_HDMI1",
    source_eid=APPLE, wake_remote=APPLE_REMOTE, competing_eids=[SHIELD],
)
family_watch_shield = make_watch_activity(
    area="Family Room", key="family_watch_shield", source_label="Shield",
    display_eid=TV, tv_remote=TV_REMOTE, hdmi_code="KEY_HDMI3",
    source_eid=SHIELD, wake_remote=SHIELD_REMOTE, competing_eids=[APPLE],
)
family_watch_tv = make_watch_activity(
    area="Family Room", key="family_watch_tv", source_label="TV",
    display_eid=TV, tv_remote=TV_REMOTE, hdmi_code="KEY_TV",
    source_eid=None, competing_eids=[APPLE, SHIELD], broadcast=True,
)
