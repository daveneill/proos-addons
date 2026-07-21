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
    # Auto-detect: prefer a Sonos/soundbar that exposes a 'TV' source; otherwise
    # fall back to an AVR and name-match each source to an AVR input.
    for dev in cluster.audio:
        sl = _source_list(client, dev.entity)
        if "TV" in sl:
            return {"mode": "sonos", "entity": dev.entity, "source": "TV"}
    avr = cluster.audio[0]
    sl = _source_list(client, avr.entity)
    inputs = {}
    for src in cluster.sources:
        match = _match_input(cluster.label_for(src), sl)
        if match:
            inputs[src.entity] = match
    broadcast = _match_input("TV Audio", sl) or _match_input("TV", sl)
    return {"mode": "avr", "entity": avr.entity, "inputs": inputs,
            "broadcast": broadcast, "power": True}


def _source_list(client, entity):
    raw = client.render_template(
        "{{ state_attr('%s','source_list') | to_json }}" % entity
    )
    try:
        import json as _j
        return _j.loads(raw) or []
    except Exception:
        return []


def _match_input(label, source_list):
    """Find the AVR input whose name matches a source label (case-insensitive,
    exact-or-contains). Returns the input name as the AVR spells it, or None."""
    if not label or not source_list:
        return None
    low = label.lower()
    for s in source_list:
        if s.lower() == low:
            return s
    for s in source_list:
        if low in s.lower() or s.lower() in low:
            return s
    return None


def _audio_steps(audio, *, source_eid=None, broadcast=False):
    """Return (fires, confirms) for the room's audio routing -- fires join the control
    burst, confirms the verify pass."""
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
        if audio.get("power", True):
            f, c = _control_confirm("Power on AV receiver", "media_player.turn_on", ent, "is_state('%s','on')" % ent)
            fires.append(f)
            if c:
                confirms.append(c)
        inp = audio.get("broadcast") if broadcast else (audio.get("inputs") or {}).get(source_eid)
        if inp:
            f, c = _control_confirm("Select AV receiver input (%s)" % inp, "media_player.select_source", ent,
                                    "state_attr('%s','source') == '%s'" % (ent, inp), data={"source": inp})
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
    """Return {object_id: script_config} for a room. Read-only against HA except
    for the small server-side renders used to detect the audio + tuner sources.
    Each returned config carries a stamped variables.proos_hash (edit-detection)."""
    commissioning = commissioning or {}
    scripts: dict = {}
    if cluster.display is None:
        return scripts  # no screen -> no activities (empty room stays empty)

    # Script object_ids are built from the IMMUTABLE area_id — never the room name. Renaming
    # a room in HA must never orphan its generated scripts. Falls back to slugging the name
    # only if a caller hasn't supplied area_id (legacy path), which for an un-renamed room is
    # identical to the area_id anyway.
    area_slug = cluster.area_id or _slug(cluster.area)
    disp = cluster.display.entity
    audio = _audio_config(client, cluster, commissioning.get("audio"))
    routes = commissioning.get("routes") or {}
    # Capability gate (brand-agnostic, per the degradation contract): verify-and-
    # retry + incident are emitted ONLY for a CERTIFIED display that provides
    # reliable_state (readback) -- because only then can we confirm the input
    # landed. A compatible/unsupported display fires the input best-effort with no
    # verify (it never claimed readback, so it's not "broken" and raises no
    # incident). Certifying a new brand flips this on with no code change.
    disp_verifies = discovery.has_capability(cluster.display.integration, "reliable_state")

    # one 'Watch <source>' per discovered source
    for src in cluster.sources:
        label = cluster.label_for(src)
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_{_slug(label)}"
        # --- CONTROL BURST: fire everything at Control4 speed. ---
        # ORDER IS DELIBERATE and load-bearing for perceived speed + safety:
        #   1. capture start-state (instant, invisible)
        #   2. THE INPUT SELECT -- FIRST visible action, so the picture starts switching the
        #      instant the activity is pressed. When the TV is already on (the common case:
        #      switching Apple TV -> TV, etc.) this is all that visibly happens, with no wake
        #      or art-off command spending a round-trip ahead of it.
        #   3. THEN the display wake (power-on if off, art-off to guarantee a live input) --
        #      these do real work only when the TV was NOT already on a live input; when it
        #      was, they are cheap no-ops that run *behind* the already-issued input switch.
        #   4. THEN the source wake (Apple TV reconnect, etc.), behind the picture change.
        # Verified: a select_source issued in Art Mode is accepted and the panel wakes onto
        # that input; from full-off it is re-asserted after the wake (cold-wake path).
        verify = []
        burst = [_cold_wake_capture(disp)]
        route = routes.get(src.entity) or {}
        disc = _discrete_inputs(client, disp)
        input_name = route.get("input")
        if not input_name and route.get("hdmi") is not None:
            input_name = "HDMI %s" % route["hdmi"]
        route_confirm = []
        # TRUST THE COMMITTED INPUT. `disc` is read from the display's LIVE source_list at
        # generation time -- if the TV happens to be off/unreachable when the installer
        # re-commits, that list comes back empty and the old `input_name in disc` test
        # SILENTLY DROPPED the input step, producing a Watch activity that never switches
        # the input (confirmed live). The installer's committed mapping is the authority;
        # only skip it if we CAN read the list and the input genuinely isn't on it.
        if input_name and (not disc or input_name in disc):
            fire, conf = _input_parts(disp, input_name, cluster.area)
            burst.append(fire)                 # INPUT FIRST -- immediate on-screen response
            # CONTROL-ONLY (Control4 model): the input is switched over IP control and TRUSTED.
            # We do NOT read the display's `source` back to "confirm" it on every run -- one
            # trusted command is the Control4 behaviour, and a blind re-assert double-popped the
            # Samsung source menu on every activity. The ONE exception is the cold-wake path:
            # from full-off the burst's select was fired at a panel that could not accept it, so
            # after the panel reaches a live input we re-assert the input ONCE -- gated on the
            # start-state captured above, so it never fires (and never double-pops) in the
            # common already-on / Art-Mode path.
            route_confirm = [_cold_wake_input_reassert(disp, input_name)]
        elif route.get("remote") and route.get("command"):
            burst.append({
                "alias": "Explicit HDMI route (commissioned)",
                "action": "remote.send_command",
                "target": {"entity_id": route["remote"]},
                "data": {"command": route["command"]},
            })
        # Display wake AFTER the input select (see ordering note above).
        burst += _wake_display_steps(client, disp)
        # Source wake. VERIFIED LIVE on this Apple TV, and CRITICAL for reliability:
        #   * media_player.turn_on ALONE does nothing when asleep (state stays 'off') --
        #     on tvOS the integration's connection goes stale in standby so the command
        #     never reaches the device.
        #   * homeassistant.reload_config_entry would reconnect, BUT it is an ADMIN-ONLY
        #     service: when the dashboard fires the script as its non-admin ProOS user, HA
        #     raises "Unauthorized" and ABORTS THE WHOLE ACTIVITY (verified in the error
        #     log + trace) -- so the wake, the HDMI route and the audio never run. That is
        #     the root of the "works, then stops, then delayed" unreliability. It must not
        #     appear in a script the dashboard runs.
        #   * The reliable NON-ADMIN wake (verified live: off -> idle): remote.turn_on
        #     FIRST -- on tvOS this re-establishes the stale connection (the remote goes
        #     'on' and the source list repopulates) -- then, after a short settle,
        #     media_player.turn_on lands and powers the screen on.
        # Both are entity services every user can call, so the activity never aborts.
        if src.integration in _REMOTE_SLEEP:
            remote_eid = _paired_remote(client, src.entity)
            asleep = "is_state('%s','off') or is_state('%s','unavailable')" % (src.entity, src.entity)
            wake_seq = [
                {"action": "remote.turn_on", "target": {"entity_id": remote_eid}},
                {"wait_template": "{{ is_state('%s','on') }}" % remote_eid,
                 "timeout": {"seconds": 6}, "continue_on_timeout": True},
                {"delay": {"seconds": 3}},   # let pyatv finish reconnecting before power-on
                {"action": "media_player.turn_on", "target": {"entity_id": src.entity}},
            ]
            # Gated so an already-awake device is neither disturbed nor slowed.
            burst.append({
                "alias": "Wake %s (reconnect via remote, then power on)" % label,
                "if": [{"condition": "template", "value_template": "{{ %s }}" % asleep}],
                "then": wake_seq,
            })
            awake = "not is_state('%s','off') and not is_state('%s','unavailable')" % (src.entity, src.entity)
            refire = list(wake_seq)   # same non-admin reconnect+power-on on retry
            wake_note = "reconnect via remote + re-wake if not"
        else:
            burst.append({"alias": "Wake %s" % label, "action": "media_player.turn_on", "target": {"entity_id": src.entity}})
            awake = "not is_state('%s','off') and not is_state('%s','unavailable')" % (src.entity, src.entity)
            refire = [{"action": "media_player.turn_on", "target": {"entity_id": src.entity}}]
            wake_note = "re-wake if not"
        verify.append({
            "alias": "Confirm %s awake (%s)" % (label, wake_note),
            "repeat": {"sequence": [
                {"wait_template": "{{ %s }}" % awake, "timeout": {"seconds": 3}, "continue_on_timeout": True},
                {"if": [{"condition": "template", "value_template": "{{ not (%s) }}" % awake}], "then": refire},
            ], "until": [{"condition": "template", "value_template": "{{ %s or repeat.index >= 3 }}" % awake}]},
        })
        verify.append({
            "alias": "Incident if %s never woke" % label,
            "if": [{"condition": "template", "value_template": "{{ not (%s) }}" % awake}],
            "then": [{"action": "logbook.log", "data": {
                "name": "ProOS %s" % cluster.area,
                "message": "%s did not wake (integration may have dropped its connection) -- check the device / re-pair" % label,
                "entity_id": src.entity}}],
        })
        # Input confirm/re-fire runs in the verify pass (the input was already fired in
        # the burst above, BEFORE the wake, so the display switched immediately).
        verify += route_confirm
        af, ac = _audio_steps(audio, source_eid=src.entity)
        burst += af
        verify += ac
        verify.insert(0, _wake_to_live_steps(client, disp))
        verify.append(_outcome_advisory(disp, cluster.area, area_slug,
            _live_expr(client, disp), _WATCH_ADVISORY_MSG))
        seq = burst + verify
        scripts[oid] = {
            "alias": f"ProOS · {cluster.area} · Watch {label}",
            "icon": _ICON_BY_INTEGRATION.get(src.integration, "mdi:television-play"),
            "mode": "restart",
            "description": "ProOS-generated activity (provisional). Edit freely in HA.",
            "variables": _marker("watch_source", audio, cluster.area),
            "sequence": seq,
        }

    # Watch TV -- ONLY when the installer committed the DISPLAY itself as a source
    # ("Also a source"). A display-only TV gets NO 'Watch TV' activity; the room is driven
    # by its external sources (Apple TV, etc.). Merely having a 'TV' tuner in the source_list
    # is not enough -- that was the old gate and it generated a stray Watch TV on every TV.
    # The activity uses the TV's OWN committed input (e.g. 'TV' for the broadcast tuner, or
    # whatever input the installer chose for the TV's live-TV / built-in-apps source).
    tv_source_input = cluster.display_input or "TV"
    if cluster.display_is_source:
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_tv"
        # Same deliberate order as the source activities: capture start-state, THEN the
        # input FIRST for immediate on-screen response, THEN the display wake.
        verify = []
        burst = [_cold_wake_capture(disp)]
        fire, conf = _input_parts(disp, tv_source_input, cluster.area)
        burst.append(fire)                     # INPUT FIRST -- select the TV's own input immediately
        burst += _wake_display_steps(client, disp)
        # Control-only, same as the source inputs above: one trusted fire, plus a single
        # cold-wake re-assert (gated on the captured start-state) so a full-off wake still
        # lands on the TV's input without double-popping the source menu on the common path.
        verify += [_cold_wake_input_reassert(disp, tv_source_input)]
        af, ac = _audio_steps(audio, broadcast=True)
        burst += af
        verify += ac
        verify.insert(0, _wake_to_live_steps(client, disp))
        verify.append(_outcome_advisory(disp, cluster.area, area_slug,
            _live_expr(client, disp), _WATCH_ADVISORY_MSG))
        seq = burst + verify
        scripts[oid] = {
            "alias": f"ProOS · {cluster.area} · Watch TV",
            "icon": "mdi:television-classic",
            "mode": "restart",
            "description": "ProOS-generated activity (provisional). Broadcast tuner.",
            "variables": _marker("watch_tv", audio, cluster.area),
            "sequence": seq,
        }

    # TV Off -- sleep every source FIRST (while the display, and thus the HDMI-CEC
    # bus, is still on), then power the display off LAST. The ordering is
    # load-bearing, not cosmetic:
    #   * Apple TV / Android TV sleep only via their paired remote (see _REMOTE_SLEEP);
    #     media_player.turn_off does not sleep them.
    #   * remote.turn_off on the Shield only STICKS while the display is still on.
    #     Kill the screen first and the Shield's sleep bounces back ~10s later; an
    #     awake Shield then re-powers the TV over CEC. So: sources first, a 1s settle,
    #     display last. Verified live in the Family Room (display held off 2.5min+).
    oid = f"{PROOS_PREFIX}_{area_slug}_tv_off"
    seq = []
    for src in cluster.sources:
        if src.integration in _REMOTE_SLEEP:
            # Paired remote resolved by DEVICE (not by assuming remote.<same object_id>).
            seq.append({
                "alias": f"Sleep {cluster.label_for(src)}",
                "action": "remote.turn_off",
                "target": {"entity_id": _paired_remote(client, src.entity)},
            })
        else:
            seq.append({
                "alias": f"Sleep {cluster.label_for(src)}",
                "action": "media_player.turn_off",
                "target": {"entity_id": src.entity},
            })
    if audio and audio.get("mode") == "avr" and audio.get("power", True):
        seq.append({
            "alias": "Power off AV receiver",
            "action": "media_player.turn_off",
            "target": {"entity_id": audio["entity"]},
        })
    seq.append({"alias": "Settle", "delay": {"seconds": 1}})
    # Off-state: 'art' rests a Frame TV in Art Mode (panel stays on showing art --
    # the intended resting/off state for a Frame); otherwise a real power off.
    _art_sw = _art_mode_switch(client, disp) if commissioning.get("off_state") == "art" else None
    if _art_sw:
        seq.append({
            "alias": "Rest display in Art Mode",
            "action": "switch.turn_on",
            "target": {"entity_id": _art_sw},
        })
    else:
        seq.append({
            "alias": "Turn off display",
            "action": "media_player.turn_off",
            "target": {"entity_id": disp},
        })
    scripts[oid] = {
        "alias": f"ProOS · {cluster.area} · TV Off",
        "icon": "mdi:television-off",
        "mode": "restart",
        "description": "ProOS-generated activity. Turns off the screen and sleeps sources.",
        "variables": _marker("tv_off", None, cluster.area),
        "sequence": seq,
    }

    # Stamp the edit-detection hash into every script now that its sequence is final.
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
            "object_ids": list(scripts)}
