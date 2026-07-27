"""
ProOS Core -- activity generator.

Turns a discovered AVCluster into real Home Assistant SCRIPTS, one per activity,
under the  proos_<area>_<key>  namespace. The scripts ARE the activities' editable
command path: an installer (or the ProOS installer UI, or an MCP client) can open
and modify them in HA without touching Core. Core owns discovery + generation +
verdicts; HA owns the artifacts.

Why scripts (not in-memory objects, not automations):
  - Visible and editable in HA (Settings -> Scripts) and via the config REST API,
    so they can be inspected and changed without redeploying Core.
  - Invoked on demand when an activity is chosen -- that's a script, not a
    trigger-based automation.

Phase-1 behaviour:
  - CEC-default: 'watch <source>' wakes the source (media_player.turn_on); HDMI-CEC
    one-touch-play pulls the display to that input. A per-room commissioning
    override can append an explicit HDMI route step.
  - Sonos as TV audio (default ON wherever the room's audio device exposes a 'TV'
    source): each watch/tune script routes audio with select_source(sonos, 'TV').
    An installer can flip a room to the TV's own speakers via the audio override.
  - create-if-absent + self-heal: a script the installer HASN'T edited is refreshed
    to match the current room (so a removed source's step disappears and a new
    source's step appears) on ordinary discovery; a script the installer HAS edited
    is never touched. Edit detection is a content hash stamped into the script's
    variables (proos_hash) -- see _content_hash / generate(). An explicit
    regenerate (overwrite=True) force-replaces every generated script regardless.

Wake is media_player.turn_on. Sleep uses each source's paired remote.<object_id>
where media_player.turn_off won't actually sleep the device (Apple TV / Android TV).
TV power is never a gate -- these scripts only issue commands; the verdict lives
elsewhere and treats the Samsung's reported power as advisory.
"""
from __future__ import annotations
import hashlib
import json
import re

from . import discovery

PROOS_PREFIX = "proos"

_ICON_BY_INTEGRATION = {
    "apple_tv": "mdi:apple",
    "androidtv_remote": "mdi:android",
    "firetv": "mdi:amazon",
    "roku": "mdi:roku",
    "cast": "mdi:cast",
    "kodi": "mdi:kodi",
}

# Source integrations whose devices do NOT sleep on media_player.turn_off and must
# be put to sleep via their paired remote.<object_id> entity instead. Apple TV
# (tvOS ignores media_player.turn_off) and Android TV / Shield (reports back 'on'
# within ~0.5s) are both verified. The paired remote shares the media_player's
# object_id, so remote.<oid> is derived directly.
_REMOTE_SLEEP = {"apple_tv", "androidtv_remote"}

# ── Certified wake profiles ──────────────────────────────────────────────────
# Part of each source integration's CERTIFICATION: how this class of device is
# woken, and what the last-resort "press the button" retry is. The wake
# STRUCTURE (asleep-guard -> wake -> confirm -> retry) is identical for every
# source and lives in _wake_source_steps; only these two facts vary. A new
# certified source class gets a row here — or inherits _default
# (media_player.turn_on, retried once), which suits devices that don't sleep
# deeply. No device class ever gets an unconfirmed fire-and-hope wake.
_WAKE_PROFILES = {
    # apple_tv: NO keypress retry — HA's apple_tv remote REJECTS send_command
    # while the box is disconnected (proven live: 400 on every command when
    # asleep), so a command retry physically cannot work. The retry is a
    # re-issue of the wake pair. If that still fails, the integration's
    # Companion channel is down (stale pairing) or CEC is off — a
    # commissioning fault the watcher must surface, not something a script
    # can push through.
    "apple_tv":         {"method": "remote_reconnect", "retry": "reissue"},
    "androidtv_remote": {"method": "remote_reconnect", "retry": "command",
                         "wake_cmd": "WAKEUP"},
    "_default":         {"method": "media_player", "retry": "reissue"},
}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "x"


def _paired_remote(client, mp_entity: str) -> str:
    """The remote.* entity on the SAME DEVICE as this media_player — resolved by device, not
    by assuming remote.<same object_id>. HA de-dupes entity_ids per domain, so a Shield whose
    media_player is 'bedroom_tv_2' can have its remote as 'bedroom_tv'; the object-id guess
    then targets a non-existent entity and the sleep/wake silently no-ops (the Shield-won't-
    sleep bug). Falls back to the guess only if the device exposes no remote."""
    tmpl = ("{% set ns = namespace(r=[]) %}"
            "{% for e in device_entities(device_id(" + json.dumps(mp_entity) + ")) %}"
            "{% if e.startswith('remote.') %}{% set ns.r = ns.r + [e] %}{% endif %}"
            "{% endfor %}{{ ns.r | to_json }}")
    try:
        rl = json.loads(client.render_template(tmpl) or "[]")
        if isinstance(rl, list) and rl:
            return rl[0]
    except Exception:
        pass
    return "remote." + mp_entity.split(".", 1)[1]


# ── Edit detection ──────────────────────────────────────────────────────────
# A stable hash over ONLY the behaviour-bearing fields of a script (never over the
# variables block, which carries our own markers + the hash itself). Stamped into
# variables.proos_hash at generation. On a later sync we compare a script's current
# content hash to its stored hash: equal => the installer never touched it (safe to
# refresh); different => the installer edited it (leave it alone). sort_keys makes
# the hash independent of key ordering when HA round-trips the config.
_HASH_FIELDS = ("alias", "icon", "mode", "sequence")


def _content_hash(cfg: dict) -> str:
    basis = {k: (cfg or {}).get(k) for k in _HASH_FIELDS}
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _as_cfg(existing):
    """Coerce whatever client.get_script() returns into the script-config dict, or
    None if it isn't one. Defensive: if the client hands back a shape we don't
    recognise, callers fall back to leave-it-alone (create-if-absent) rather than
    risk clobbering an installer's script."""
    if isinstance(existing, dict):
        if "sequence" in existing:
            return existing
        for k in ("config", "result", "script"):
            inner = existing.get(k)
            if isinstance(inner, dict) and "sequence" in inner:
                return inner
    return None


def _audio_config(client, cluster, override):
    """Resolve the room's TV-audio plan, or None for the display's own speakers.

    Returns a dict describing how to route TV audio:
      {'mode':'sonos','entity':<sonos>,'source':'TV'}
        -> one select_source for every activity (soundbar / Sonos with a TV input)
      {'mode':'avr','entity':<avr>,'inputs':{<source_eid>:<input>},
                    'broadcast':<input>, 'power':True}
        -> power the AVR on + select the input matching what's being watched
         (Denon/Marantz etc.; each source can land on a different AVR input)

    override (from COMMISSIONING[area]['audio']):
      None                          -> auto-detect (Sonos-with-TV-source preferred,
                                       then an AVR audio device)
      {'mode':'tv'}                 -> None (use the display's own speakers)
      {'mode':'sonos',...} / {'mode':'avr',...} -> explicit (installer override)
    """
    if override:
        if override.get("mode") == "tv":
            return None
        if override.get("mode") in ("sonos", "avr") and override.get("entity"):
            return override
    if not cluster.audio:
        return None
    # Auto-detect covers ONLY the soundbar case: a speaker exposing a literal
    # 'TV' source (Sonos and soundbars across brands present exactly this as
    # their TV-audio capability). For an AVR with no explicit plan, activities
    # power it on/off and nothing more — the old behaviour of GUESSING its
    # inputs by name-matching source labels against the source_list was removed
    # deliberately: it was brand/naming-dependent and silent (a rename broke
    # routing with no warning), which violates the identity standard. Explicit
    # per-source AVR inputs now come from the installer's committed AV-switch
    # plan (rec['avswitch'] -> the override above), chosen from the device's
    # OWN source_list in Pro — any brand, no guessing.
    for dev in cluster.audio:
        sl = _source_list(client, dev.entity)
        if "TV" in sl:
            return {"mode": "sonos", "entity": dev.entity, "source": "TV"}
    avr = cluster.audio[0]
    return {"mode": "avr", "entity": avr.entity, "inputs": {},
            "broadcast": None, "power": True}


def _source_list(client, entity):
    raw = client.render_template(
        "{{ state_attr('%s','source_list') | to_json }}" % entity
    )
    try:
        import json as _j
        return _j.loads(raw) or []
    except Exception:
        return []


def _supports_power(client, entity):
    """True when the media_player implements TURN_ON and TURN_OFF
    (supported_features bits 128/256). Any-brand safety gate: the streaming
    twin of a receiver (e.g. the HEOS player of a Denon/Marantz — same box,
    different integration, identical name) exposes source selection but NOT
    power. Emitting turn_on/turn_off at such an entity makes HA raise, which
    ABORTS the script mid-sequence — observed live: TV Off died at 'Power off
    AV receiver' and never reached 'Turn off the TV'. Capability, not brand."""
    if client is None:
        return True
    try:
        raw = client.render_template(
            "{{ state_attr('%s','supported_features') | int(0) }}" % entity)
        feats = int(str(raw).strip() or 0)
        return bool(feats & 128) and bool(feats & 256)
    except Exception:
        return True


def _audio_steps(audio, *, source_eid=None, broadcast=False, client=None):
    """Return (fires, confirms) for the room's audio routing -- fires join the control
    burst, confirms the verify pass. AVR steps carry continue_on_error so a
    misbehaving audio device can degrade the room to video-without-audio-routing
    but can never abort the activity (report-not-drive: Core's watcher surfaces
    the failure; the script keeps going)."""
    if not audio:
        return [], []
    mode = audio.get("mode")
    ent = audio.get("entity")
    if mode == "tv" or not ent:
        return [], []
    if mode == "sonos":
        src = audio.get("source", "TV")
        f, c = _control_confirm("Route TV audio to Sonos", "media_player.select_source", ent,
                                "state_attr('%s','source') == '%s'" % (ent, src), data={"source": src})
        return [f], ([c] if c else [])
    if mode == "avr":
        fires, confirms = [], []
        if audio.get("power", True) and _supports_power(client, ent):
            f, c = _control_confirm("Power on AV receiver", "media_player.turn_on", ent, "is_state('%s','on')" % ent)
            f["continue_on_error"] = True
            fires.append(f)
            if c:
                confirms.append(c)
        inp = audio.get("broadcast") if broadcast else (audio.get("inputs") or {}).get(source_eid)
        if inp:
            f, c = _control_confirm("Select AV receiver input (%s)" % inp, "media_player.select_source", ent,
                                    "state_attr('%s','source') == '%s'" % (ent, inp), data={"source": inp})
            f["continue_on_error"] = True
            fires.append(f)
            if c:
                confirms.append(c)
        return fires, confirms
    return [], []


def _discrete_inputs(client, display_eid):
    """Return the display's selectable discrete HDMI inputs, e.g. ['HDMI 1','HDMI 2'].

    The ProOS Samsung integration (samsungtv_smart) exposes these as source_list
    entries and routes them over licensed IP control -- deterministic, unlike CEC.
    A core/CEC-only display returns []. Used to decide whether an activity can
    route the display to a named input with select_source instead of trusting
    HDMI-CEC one-touch-play.
    """
    return [s for s in _source_list(client, display_eid)
            if isinstance(s, str) and s.strip().lower().startswith("hdmi")]


def _marker(kind, audio, area=None):
    # Travels inside the script so ProOS-owned objects are identifiable and the
    # active audio mode is self-describing. proos_area lets the installer UI group
    # / filter activities by room without re-deriving it. proos_provisional is kept
    # for backward-compat; the authoritative edit signal is now the content hash
    # (proos_hash), stamped in generate()/build_room_scripts.
    m = {
        "proos_managed": True,
        "proos_provisional": True,
        "proos_kind": kind,
        "proos_audio": (audio.get("mode") if audio else "tv"),
    }
    if area:
        m["proos_area"] = area
    return m


def _display_has_tv_source(client, display_eid) -> bool:
    out = (client.render_template(
        "{{ 'TV' in (state_attr('%s','source_list') or []) }}" % display_eid
    ) or "").strip().lower()
    return out == "true"


def _wait_display_on(disp, timeout=15):
    """Gate the rest of an activity on the display actually being on.

    A TV takes time to power up before it will accept source / input commands,
    so firing the source-select or HDMI-route the instant after turn_on races
    the screen and lands on a black/blank input. This step blocks until the
    display reports 'on', then lets the rest of the sequence run.

    wait_template (not a state trigger) is deliberate: it short-circuits the
    moment the display is already on -- switching sources on an already-watching
    TV adds zero delay -- and otherwise waits out the power-up. A state trigger
    would never fire when the TV is already on and would always burn the full
    timeout. continue_on_timeout keeps the macro moving if a display reports its
    power state slowly or not at all, so a flaky TV degrades to today's
    behaviour (proceed anyway) rather than hanging.
    """
    return {
        "alias": "Wait for the display to power up",
        "wait_template": "{{ is_state('%s','on') }}" % disp,
        "timeout": {"seconds": timeout},
        "continue_on_timeout": True,
    }


def _route_and_verify(disp, input_name, area, tries=3):
    """Discrete input routing that VERIFIES it landed and re-fires if it didn't --
    the answer to fire-and-forget input switching (the Samsung that stays on Live
    TV even though the activity 'selected HDMI 2').

    A certified display (any brand) reports its current input in the `source`
    attribute -- that's the `reliable_state` capability the certification bar
    requires -- so the switch IS readback-confirmable (the old assumption that it
    wasn't is why nothing noticed). This is only emitted for a certified display
    with readback; a compatible display fires the input best-effort with no verify
    (see build_room_scripts), honouring the degradation contract. It fires the input ONCE immediately as a plain control command (Control4-style --
    works even if state read-back is down), then confirms it landed and re-fires up to
    `tries` times ONLY if it didn't.

    This is a BOUNDED, activation-scoped retry -- NOT a Core reconcile loop. It
    runs only while this activity is being activated and stops the instant the
    input is correct or the tries are spent, so it can never re-assert an input
    after the room is turned off (the exact behaviour Core's retired drive loop
    was removed to prevent). Execution stays where it belongs: the HA script.

    If the display still isn't on the input after every try, it writes a logbook
    incident naming the room + input, so the failure surfaces to awareness /
    the installer instead of silently leaving the wrong picture on screen. On a
    Samsung the usual cause is IP Control not paired (or lost after a TV reset)."""
    cur = "state_attr('%s','source')" % disp
    landed = "%s == '%s'" % (cur, input_name)
    # "readable" = the display actually reports a current input. If it doesn't
    # (e.g. a Samsung whose source read-back comes from SmartThings and SmartThings
    # isn't linked), we can SEND the input over IP Control but can't CONFIRM it --
    # so we must not raise a false "didn't switch" incident on an unconfirmable
    # display. The incident fires ONLY when source is readable AND wrong.
    readable = "%s not in [none, 'unknown', 'unavailable', '']" % cur
    return [
        # CONTROL (Control4-style baseline): command the input immediately and
        # unconditionally -- the screen is told to switch even if state read-back is
        # unavailable. No pre-wait; this is the speed + safety floor a control-only
        # system provides, and it fires every activation.
        {
            "alias": "Select %s" % input_name,
            "action": "media_player.select_source",
            "target": {"entity_id": disp},
            "data": {"source": input_name},
        },
        # CONFIRM (ProOS state layer on top): wait for it to land; re-fire ONLY if it
        # didn't, up to `tries` times, then stop. No delay on the happy path (already
        # on the input -> passes at once). Bounded, activation-scoped -- never a loop.
        {
            "alias": "Confirm %s landed (re-fire if not)" % input_name,
            "repeat": {
                "sequence": [
                    {
                        "wait_template": "{{ %s }}" % landed,
                        "timeout": {"seconds": 3},
                        "continue_on_timeout": True,
                    },
                    {
                        "if": [{"condition": "template", "value_template": "{{ not (%s) }}" % landed}],
                        "then": [{
                            "action": "media_player.select_source",
                            "target": {"entity_id": disp},
                            "data": {"source": input_name},
                        }],
                    },
                ],
                "until": [{
                    "condition": "template",
                    "value_template": "{{ %s or repeat.index >= %d }}" % (landed, tries),
                }],
            },
        },
        {
            "alias": "Incident if the display read back a wrong input",
            "if": [{"condition": "template", "value_template": "{{ %s and not (%s) }}" % (readable, landed)}],
            "then": [{
                "action": "logbook.log",
                "data": {
                    "name": "ProOS %s" % area,
                    "message": ("display did not switch to %s after %d tries "
                                "-- check the certified display's input control "
                                "(e.g. re-pair after a TV reset)" % (input_name, tries)),
                    "entity_id": disp,
                },
            }],
        },
    ]


def _art_mode_switch(client, disp):
    """The Frame TV's Art Mode switch on the SAME device as the display (resolved
    by device, so the display's entity-id suffix like `_2` doesn't matter), or
    None. Used when a room's off-state is Art Mode: 'TV Off' rests the panel in
    Art Mode instead of powering it down."""
    try:
        tmpl = ("{% set d = device_id('" + disp + "') %}"
                "{{ (device_entities(d) | select('match', 'switch\\..*art_mode') "
                "| list | first) if d else '' }}")
        r = (client.render_template(tmpl) or "").strip()
        return r or None
    except Exception:
        return None


def _control_confirm(alias, action, entity, confirm, data=None, tries=2):
    """Split a command into its (control FIRE, state CONFIRM) parts, so the caller can
    fire ALL controls first at Control4 speed and run the confirms as a backing pass.
    Returns (fire_step, confirm_step_or_None). The confirm waits for `confirm`, re-firing
    ONLY if it didn't take (bounded, activation-scoped). None confirm = fire-only."""
    fire = {"alias": alias, "action": action, "target": {"entity_id": entity}}
    if data:
        fire["data"] = data
    conf = None
    if confirm:
        refire = {"action": action, "target": {"entity_id": entity}}
        if data:
            refire["data"] = data
        conf = {
            "alias": "Confirm: %s" % alias,
            "repeat": {
                "sequence": [
                    {"wait_template": "{{ %s }}" % confirm, "timeout": {"seconds": 3}, "continue_on_timeout": True},
                    {"if": [{"condition": "template", "value_template": "{{ not (%s) }}" % confirm}], "then": [refire]},
                ],
                "until": [{"condition": "template", "value_template": "{{ %s or repeat.index >= %d }}" % (confirm, tries)}],
            },
        }
    return fire, conf


def _source_wake(src):
    """Correct wake command for a source. apple_tv / androidtv_remote wake via their
    paired `remote.turn_on` -- `media_player.turn_on` is a silent no-op on tvOS 26 /
    recent Android TV (the cause of "the Apple TV doesn't turn on every time"). Others
    wake via media_player.turn_on. Returns (action, entity)."""
    if src.integration in _REMOTE_SLEEP:
        return "remote.turn_on", "remote." + src.entity.split(".", 1)[1]
    return "media_player.turn_on", src.entity


def _input_parts(disp, input_name, area, tries=3):
    """(fire, [confirm, incident]) for a discrete input select: control fire, then a
    confirm that re-fires only if it didn't land, then an incident if the display is
    readable and still wrong."""
    cur = "state_attr('%s','source')" % disp
    landed = "%s == '%s'" % (cur, input_name)
    readable = "%s not in [none, 'unknown', 'unavailable', '']" % cur
    fire = {"alias": "Select %s" % input_name, "action": "media_player.select_source",
            "target": {"entity_id": disp}, "data": {"source": input_name}, "continue_on_error": True}
    confirm = {
        "alias": "Confirm %s landed (re-fire if not)" % input_name,
        "repeat": {
            "sequence": [
                {"wait_template": "{{ %s }}" % landed, "timeout": {"seconds": 3}, "continue_on_timeout": True},
                {"if": [{"condition": "template", "value_template": "{{ not (%s) }}" % landed}],
                 "then": [{"action": "media_player.select_source", "target": {"entity_id": disp}, "data": {"source": input_name}}]},
            ],
            "until": [{"condition": "template", "value_template": "{{ %s or repeat.index >= %d }}" % (landed, tries)}],
        },
    }
    incident = {
        "alias": "Incident if the display read back a wrong input",
        "if": [{"condition": "template", "value_template": "{{ %s and not (%s) }}" % (readable, landed)}],
        "then": [{"action": "logbook.log", "data": {
            "name": "ProOS %s" % area,
            "message": "display did not switch to %s -- check the certified display's input control (e.g. re-pair after a TV reset)" % input_name,
            "entity_id": disp}}],
    }
    return fire, [confirm, incident]


def _wake_display_steps(client, disp, timeout=15):
    """Control-only wake burst that covers BOTH ways a client can leave the panel:
    resting in Art Mode, OR fully powered off -- INCLUDING the awkward case where the
    room's off-state is Art Mode but the client manually powered the TV all the way off.
    No blocking waits, so the picture comes up as fast as the panel physically can
    (Control4 behaviour); a cold panel that wakes a few seconds later is caught by the
    heal in the verify pass (see _wake_to_live_steps).

    CONFIRMED on the Frame:
      * art switch OFF  -> DETERMINISTIC 'go to a live input' (artModeControl artModeOff)
      * art switch ON   -> Art Mode (the rest state)
      * full power-on   -> Wake-on-LAN, via media_player.turn_on

    Why the power-on is GATED on the panel being off: in Art Mode the media_player reports
    'on', and firing media_player.turn_on there sends KEY_POWER -- a TOGGLE that can bounce a
    live panel back into art. Gating on `is_state(disp,'off')` fires WOL only from a genuine
    full-off and leaves the deterministic art-off to handle the Art-Mode case. media_player
    is never used as the live signal (it reads 'on' while showing art)."""
    art = _art_mode_switch(client, disp)
    if art:
        return [
            {"alias": "Power on if the panel is fully off (WOL)",
             "if": [{"condition": "state", "entity_id": disp, "state": "off"}],
             "then": [{"action": "media_player.turn_on", "target": {"entity_id": disp},
                       "continue_on_error": True}]},
            {"alias": "Take the TV to a live input (Art Mode off)", "action": "switch.turn_off",
             "target": {"entity_id": art}, "continue_on_error": True}]
    return [{"alias": "Turn on display", "action": "media_player.turn_on",
             "target": {"entity_id": disp}, "continue_on_error": True}]


def _cold_wake_capture(disp):
    """Capture the START state (was the panel FULLY off?) as `proos_cold_wake`, BEFORE the
    activity touches anything. Must be the very first step in the sequence. Lets the verify
    pass re-assert the input only on the cold-wake path (from full-off the input select is
    fired at a panel that cannot accept it yet), without any readback."""
    return {"variables": {"proos_cold_wake": "{{ is_state('%s','off') }}" % disp}}


def _wake_to_live_steps(client, disp):
    """Verify-pass heal: WAIT for the panel to reach a live input; if it hasn't, RE-ASSERT
    the deterministic 'go to live' command until it does (bounded to ride the ~30s
    post-power-off WOL dead zone), then let the advisory fire if it still hasn't.

    This is what makes an 'on' activity cover ALL start states, including the awkward one:
    the room's off-state is Art Mode but the client manually powered the TV fully off. From
    full-off the burst's art-off could not land (IP control is unreachable while the panel is
    off) and a Frame wakes back INTO Art Mode -- so we re-assert art-off here, once the panel
    is reachable, until it reports a live input. In the common (already-on / Art-Mode) path
    the very first wait returns immediately, so no time is added."""
    live = _live_expr(client, disp)
    art = _art_mode_switch(client, disp)
    reassert = ([{"action": "switch.turn_off", "target": {"entity_id": art}, "continue_on_error": True}]
                if art else
                [{"action": "media_player.turn_on", "target": {"entity_id": disp}, "continue_on_error": True}])
    return {
        "alias": "Bring the panel to a live input (covers Art Mode and full-off)",
        "repeat": {"sequence": [
            {"wait_template": "{{ %s }}" % live, "timeout": {"seconds": 6}, "continue_on_timeout": True},
            {"if": [{"condition": "template", "value_template": "{{ not (%s) }}" % live}], "then": reassert},
        ], "until": [{"condition": "template", "value_template": "{{ %s or repeat.index >= 8 }}" % live}]},
    }


def _cold_wake_input_reassert(disp, input_name):
    """Re-assert the input ONCE, but ONLY after a cold (full-off) wake -- gated on the
    start-state captured in the wake burst. In the common already-on / Art-Mode path this
    never fires, which is what previously double-popped the source menu on every activity.
    From full-off it re-sends the input after the panel has reached a live input, so the
    activity lands on the right HDMI even though the burst's first select was lost."""
    return {
        "alias": "Re-assert %s after a cold wake" % input_name,
        "if": [{"condition": "template", "value_template": "{{ proos_cold_wake | default(false) }}"}],
        "then": [{"action": "media_player.select_source", "target": {"entity_id": disp},
                  "data": {"source": input_name}, "continue_on_error": True}],
    }


_WATCH_ADVISORY_MSG = ("The TV did not turn on or switch input for this activity. It may "
    "have lost its connection to Home Assistant (a Samsung can drop its authorization). On "
    "the TV, accept the 'Allow this device' prompt (Settings > General > External Device "
    "Manager > Device Connection Manager), or re-pair the TV in Home Assistant, then try the "
    "activity again.")


def _live_expr(client, disp):
    """Template that is TRUE only when the display is genuinely on a LIVE input.
    On a Frame that means the art switch is OFF (media_player reads 'on' in Art Mode too)."""
    art = _art_mode_switch(client, disp)
    if art:
        return "is_state('%s','off')" % art
    return "is_state('%s','on')" % disp


def _outcome_advisory(disp, area, area_slug, target_expr, message):
    """Final, self-describing step: if the room did NOT reach its target state, post a
    VISIBLE advisory (a persistent notification) naming what happened and how to recover, and
    log it; if it DID, clear any stale advisory. Turns a silent control failure -- e.g. a TV
    that dropped its HA authorization and refuses commands -- into a clear prompt. The product
    must recover or advise, never do nothing."""
    nid = "proos_%s_room" % area_slug
    return {
        "alias": "Outcome check -- advise if the room did not respond",
        "if": [{"condition": "template", "value_template": "{{ %s }}" % target_expr}],
        "then": [{"action": "persistent_notification.dismiss", "data": {"notification_id": nid}}],
        "else": [
            {"action": "persistent_notification.create", "data": {
                "notification_id": nid, "title": "%s did not respond" % area, "message": message}},
            {"action": "logbook.log", "data": {
                "name": "ProOS %s" % area, "message": message, "entity_id": disp}},
        ],
    }


def build_room_scripts(client, cluster, commissioning=None) -> dict:
    """Return {object_id: script_config} for a room, using ONLY standard HA actions an
    installer finds in the script builder -- media_player / remote turn_on|turn_off|
    select_source, Wait-for-trigger, If, Delay, Run-script. No repeat/retry loops, no
    incident logging, no ProOS-internal steps: a generated activity opens in the HA editor
    as plain, editable commands. Confirm / re-fire / recovery live in Core (report-not-
    drive), not in the script.

    Model (Control4-style): a reusable 'Display On' script powers the screen ONLY if it is
    off and waits for it to actually come up; each Watch activity runs it synchronously
    (action: script.<display_on>) then continues into the input select. When the TV is
    already on, Display On is a no-op and source selection / waking is instant.
    """
    commissioning = commissioning or {}
    scripts: dict = {}
    if cluster.display is None:
        return scripts

    area_slug = cluster.area_id or _slug(cluster.area)
    disp = cluster.display.entity
    audio = _audio_config(client, cluster, commissioning.get("audio"))
    routes = commissioning.get("routes") or {}
    # A certified display reports power reliably -> wait on its real state (fast, exact, and
    # the same signal the dashboard shows). A native-only display can't be trusted to report
    # power, so fall back to a fixed warm-up delay.
    disp_reliable = discovery.has_capability(cluster.display.integration, "reliable_state")

    # --- reusable 'Display On' building block (this is the Control4 'TV On') ---
    display_on_oid = f"{PROOS_PREFIX}_{area_slug}_display_on"
    if disp_reliable:
        wait_step = {
            "alias": "Wait until the TV is on",
            "wait_for_trigger": [{"trigger": "state", "entity_id": disp, "to": "on"}],
            "timeout": {"seconds": 20}, "continue_on_timeout": True,
        }
    else:
        wait_step = {"alias": "Wait for the TV to warm up", "delay": {"seconds": 10}}
    scripts[display_on_oid] = {
        "alias": f"ProOS · {cluster.area} · Display On",
        "icon": "mdi:television",
        "mode": "restart",
        "description": "Turns the display on only if it is off, and waits until it is on. "
                       "Watch activities run this first.",
        "variables": _marker("display_on", None, cluster.area),
        "sequence": [{
            "alias": "If the display is off, turn it on and wait until it is on",
            "if": [{"condition": "state", "entity_id": disp, "state": "off"}],
            "then": [
                {"alias": "Turn on the TV", "action": "media_player.turn_on",
                 "target": {"entity_id": disp}},
                wait_step,
            ],
        }],
    }
    display_on_step = {"alias": "Turn the TV on (if it isn't already)",
                       "action": "script." + display_on_oid}

    # ── THE WAKE STANDARD ────────────────────────────────────────────────────
    # Every certified source wakes the same way, differing only in the two
    # facts its certification supplies: HOW to wake it, and what the final
    # "press the button" retry is. The structure is universal:
    #   asleep? (off/standby) -> wake -> CONFIRM it left standby -> retry once
    # Fire-and-hope turn_on is not certifiable: a device that can sleep must
    # have a confirmed wake, or "watch X" silently leaves a dark box — the
    # Apple TV bug, and it would have been the next integration's bug too.
    # Adding a certified source class = adding one profile row here.
    #   remote_reconnect: wake via the paired remote entity, retry with an
    #                     explicit wake keypress (deep-sleep boxes can accept
    #                     the reconnect yet keep video output down).
    #   media_player:     wake via media_player.turn_on, retry the same call.
    def _wake_source_steps(src):
        label = cluster.label_for(src)
        profile = _WAKE_PROFILES.get(src.integration, _WAKE_PROFILES["_default"])
        asleep = ["off", "standby"]
        still = ("states('%s') in ['off','standby','unavailable']" % src.entity)
        if profile["method"] == "remote_reconnect":
            remote_eid = _paired_remote(client, src.entity)
            wake = [
                {"alias": f"Reconnect {label}", "action": "remote.turn_on",
                 "target": {"entity_id": remote_eid}},
                {"alias": "Wait a moment for it to reconnect", "delay": {"seconds": 3}},
                {"alias": f"Turn on {label}", "action": "media_player.turn_on",
                 "target": {"entity_id": src.entity}, "continue_on_error": True},
            ]
            if profile.get("retry") == "command":
                retry = [{"action": "remote.send_command",
                          "target": {"entity_id": remote_eid},
                          "data": {"command": profile["wake_cmd"]},
                          "continue_on_error": True}]
            else:
                # Commands are rejected while disconnected — the only honest
                # retry is re-issuing the wake pair itself.
                retry = [{"action": "remote.turn_on",
                          "target": {"entity_id": remote_eid},
                          "continue_on_error": True},
                         {"action": "media_player.turn_on",
                          "target": {"entity_id": src.entity},
                          "continue_on_error": True}]
        else:
            wake = [{"alias": f"Turn on {label}", "action": "media_player.turn_on",
                     "target": {"entity_id": src.entity}, "continue_on_error": True}]
            retry = [{"action": "media_player.turn_on",
                      "target": {"entity_id": src.entity},
                      "continue_on_error": True}]
        return [{
            "alias": f"Wake {label} if it is asleep",
            "if": [{"condition": "state", "entity_id": src.entity, "state": asleep}],
            "then": wake + [
                {"alias": f"Confirm {label} is awake", "wait_template":
                 "{{ not (%s) }}" % still, "timeout": {"seconds": 6},
                 "continue_on_timeout": True},
                {"alias": "Retry wake if it still hasn't come up",
                 "if": [{"condition": "template", "value_template": "{{ %s }}" % still}],
                 "then": retry},
            ],
        }]

    def _audio_fire_steps(*, source_eid=None, broadcast=False):
        fires, _ = _audio_steps(audio, source_eid=source_eid, broadcast=broadcast,
                                client=client)
        return fires

    def _input_step(display_input):
        return {"alias": f"Select {display_input}", "action": "media_player.select_source",
                "target": {"entity_id": disp}, "data": {"source": display_input}}

    # one 'Watch <source>' per discovered source
    for src in cluster.sources:
        label = cluster.label_for(src)
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_{_slug(label)}"
        seq = [display_on_step]
        route = routes.get(src.entity) or {}
        input_name = route.get("input")
        if not input_name and route.get("hdmi") is not None:
            input_name = "HDMI %s" % route["hdmi"]
        disc = _discrete_inputs(client, disp)
        if input_name and (not disc or input_name in disc):
            seq.append(_input_step(input_name))
        elif route.get("remote") and route.get("command"):
            seq.append({
                "alias": "Select input (commissioned HDMI route)",
                "action": "remote.send_command",
                "target": {"entity_id": route["remote"]},
                "data": {"command": route["command"]},
            })
        # Audio routing (AVR power + input select) fires BEFORE the source wake:
        # the display route above plus the amp switching are the steps the user
        # SEES and HEARS, so they must land instantly on press — otherwise the
        # screen sits unchanged for the wake's reconnect/delay and the user
        # presses the activity again. The source wake is the slow, invisible
        # tail (reconnect + settle), so it goes last. Same Control4 principle
        # as Display On first: perceptible feedback front-loaded, waiting
        # back-loaded. (AVR steps are continue_on_error, so this order can
        # never stop the wake from running.)
        seq += _audio_fire_steps(source_eid=src.entity)
        seq += _wake_source_steps(src)
        scripts[oid] = {
            "alias": f"ProOS · {cluster.area} · Watch {label}",
            "icon": _ICON_BY_INTEGRATION.get(src.integration, "mdi:television-play"),
            "mode": "restart",
            "description": "Turns on the room and switches to this source.",
            "variables": {**_marker("watch_source", audio, cluster.area), "proos_source": src.entity},
            "sequence": seq,
        }

    # Watch TV -- only when the display itself is committed as a source.
    if cluster.display_is_source:
        tv_source_input = cluster.display_input or "TV"
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_tv"
        seq = [display_on_step, _input_step(tv_source_input)]
        seq += _audio_fire_steps(broadcast=True)
        scripts[oid] = {
            "alias": f"ProOS · {cluster.area} · Watch TV",
            "icon": "mdi:television-classic",
            "mode": "restart",
            "description": "Turns on the room and switches to the TV's own input.",
            "variables": _marker("watch_tv", audio, cluster.area),
            "sequence": seq,
        }

    # TV Off -- sleep sources FIRST (while the display / CEC bus is still up), settle,
    # then power the display off LAST. Ordering is load-bearing.
    oid = f"{PROOS_PREFIX}_{area_slug}_tv_off"
    seq = []
    for src in cluster.sources:
        if src.integration in _REMOTE_SLEEP:
            seq.append({"alias": f"Sleep {cluster.label_for(src)}", "action": "remote.turn_off",
                        "target": {"entity_id": _paired_remote(client, src.entity)}})
        else:
            seq.append({"alias": f"Sleep {cluster.label_for(src)}", "action": "media_player.turn_off",
                        "target": {"entity_id": src.entity}})
    if (audio and audio.get("mode") == "avr" and audio.get("power", True)
            and _supports_power(client, audio["entity"])):
        # continue_on_error: a receiver that fails to answer must never stop the
        # display from being turned off (the step below is the one that matters).
        seq.append({"alias": "Power off AV receiver", "action": "media_player.turn_off",
                    "target": {"entity_id": audio["entity"]}, "continue_on_error": True})
    seq.append({"alias": "Settle", "delay": {"seconds": 1}})
    _art_sw = _art_mode_switch(client, disp) if commissioning.get("off_state") == "art" else None
    if _art_sw:
        seq.append({"alias": "Rest display in Art Mode", "action": "switch.turn_on",
                    "target": {"entity_id": _art_sw}})
    else:
        seq.append({"alias": "Turn off the TV", "action": "media_player.turn_off",
                    "target": {"entity_id": disp}})
    scripts[oid] = {
        "alias": f"ProOS · {cluster.area} · TV Off",
        "icon": "mdi:television-off",
        "mode": "restart",
        "description": "Sleeps the sources and turns the screen off.",
        "variables": _marker("tv_off", None, cluster.area),
        "sequence": seq,
    }

    # Stamp the edit-detection hash now that every sequence is final.
    for _oid, _cfg in scripts.items():
        _cfg["variables"]["proos_hash"] = _content_hash(_cfg)
    return scripts


def generate(client, cluster, commissioning=None, overwrite=False) -> dict:
    """Create-if-absent + self-heal (default), or force-regenerate (overwrite=True).

    For each activity the room should have:
      - absent            -> create it.
      - overwrite=True    -> replace it (explicit 'regenerate').
      - present, UNEDITED -> refresh it to the current room ONLY if the generated
                             content changed (e.g. a source was removed/added), so
                             stale steps disappear on their own. Unedited is decided
                             by the content hash: the script's current content still
                             matches the proos_hash we stamped when we wrote it.
      - present, EDITED   -> leave it exactly as the installer left it (never clobber).
      - present, LEGACY (no stored hash) -> migrate only if it is byte-identical to
                             fresh generator output (provably unedited); otherwise
                             leave it (can't prove it's unedited).

    Returns {created:[...], kept:[...], refreshed:[...], object_ids:[...]}.
    """
    scripts = build_room_scripts(client, cluster, commissioning)
    created, kept, refreshed = [], [], []
    # ── Identity mapping: a watch activity IS its source, not its label ─────
    # Object_ids embed the label slug, and labels are modifiable — a re-paired
    # device with its name not yet populated generated 'watch_media_player_
    # bedroom_bedroom_apple_tv' NEXT TO the existing 'watch_apple_tv' for the
    # SAME box. So before writing, map each planned watch script to any
    # existing script whose stamped proos_source (immutable identity) matches:
    # the existing object_id is REUSED (alias/content still refresh under the
    # usual hash rules), and provably-unedited duplicates are removed.
    area_slug = cluster.area_id or _slug(cluster.area)
    prefix = "script.%s_%s_" % (PROOS_PREFIX, area_slug)
    tmpl = ("{% set ns = namespace(x=[]) %}"
            "{% for s in states.script %}"
            "{% if s.entity_id.startswith(" + json.dumps(prefix) + ") %}"
            "{% set ns.x = ns.x + [s.entity_id] %}{% endif %}"
            "{% endfor %}{{ ns.x | to_json }}")
    by_identity = {}          # (kind, source_eid) -> [(oid, cfg), ...]
    try:
        for seid in json.loads(client.render_template(tmpl) or "[]"):
            eoid = seid.split(".", 1)[1]
            ecfg0 = _as_cfg(client.get_script(eoid))
            if not isinstance(ecfg0, dict):
                continue
            v0 = ecfg0.get("variables") or {}
            if v0.get("proos_kind") == "watch_source" and v0.get("proos_source"):
                by_identity.setdefault(("watch_source", v0["proos_source"]),
                                       []).append((eoid, ecfg0))
    except Exception:
        by_identity = {}
    remapped, deduped = {}, []
    for oid in list(scripts):
        cfg = scripts[oid]
        v = (cfg.get("variables") or {})
        if v.get("proos_kind") != "watch_source" or not v.get("proos_source"):
            continue
        matches = by_identity.get(("watch_source", v["proos_source"])) or []
        keep_oid = oid
        if matches and all(m[0] != oid for m in matches):
            # The source already has a script under another (older) id — that
            # id is the activity's identity now. Write there, don't mint a twin.
            keep_oid = matches[0][0]
            remapped[oid] = keep_oid
            scripts[keep_oid] = cfg
            del scripts[oid]
        # Remove surplus twins for this source — only when provably unedited.
        for eoid, ecfg0 in matches:
            if eoid == keep_oid:
                continue
            stored0 = (ecfg0.get("variables") or {}).get("proos_hash")
            if stored0 and stored0 == _content_hash(ecfg0):
                try:
                    if client.delete_script(eoid):
                        deduped.append(eoid)
                except Exception:
                    pass
    for oid, cfg in scripts.items():
        new_hash = (cfg.get("variables") or {}).get("proos_hash")
        try:
            existing = client.get_script(oid)
        except Exception:
            existing = None
        if existing is None:
            client.upsert_script(oid, cfg)
            created.append(oid)
            continue
        if overwrite:
            client.upsert_script(oid, cfg)
            refreshed.append(oid)
            continue
        ecfg = _as_cfg(existing)
        if ecfg is None:
            kept.append(oid)          # present but unreadable shape -> don't touch
            continue
        stored = (ecfg.get("variables") or {}).get("proos_hash")
        cur = _content_hash(ecfg)
        if stored is None:
            # Legacy script (written before hashing): migrate ONLY if it is exactly
            # what the generator produces now -- proof it was never edited. Adds the
            # hash so future syncs can self-heal it. Otherwise leave it untouched.
            if cur == new_hash:
                client.upsert_script(oid, cfg)
                refreshed.append(oid)
            else:
                kept.append(oid)
            continue
        if cur == stored:
            # Unedited. Refresh only if the room changed what it should contain.
            if new_hash != stored:
                client.upsert_script(oid, cfg)
                refreshed.append(oid)
            else:
                kept.append(oid)
        else:
            kept.append(oid)          # installer edited it -> protect
    return {"created": created, "kept": kept, "refreshed": refreshed,
            "remapped": remapped, "deduped": deduped,
            "object_ids": list(scripts)}
