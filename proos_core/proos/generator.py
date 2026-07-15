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
    """Build the audio routing step(s) for one activity given the room's plan."""
    if not audio:
        return []
    mode = audio.get("mode")
    ent = audio.get("entity")
    if mode == "tv" or not ent:
        return []
    if mode == "sonos":
        return [{
            "alias": "Route TV audio to Sonos",
            "action": "media_player.select_source",
            "target": {"entity_id": ent},
            "data": {"source": audio.get("source", "TV")},
        }]
    if mode == "avr":
        steps = []
        if audio.get("power", True):
            steps.append({
                "alias": "Power on AV receiver",
                "action": "media_player.turn_on",
                "target": {"entity_id": ent},
            })
        inp = audio.get("broadcast") if broadcast else (audio.get("inputs") or {}).get(source_eid)
        if inp:
            steps.append({
                "alias": f"Select AV receiver input ({inp})",
                "action": "media_player.select_source",
                "target": {"entity_id": ent},
                "data": {"source": inp},
            })
        # No matched input -> power the AVR but leave input to commissioning
        # (don't guess a wrong input). Honest: audio device on, route unconfirmed.
        return steps
    return []


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
    (see build_room_scripts), honouring the degradation contract. This fires
    select_source, waits up to 3s for `source` to become the requested input, and
    repeats up to `tries` times if it hasn't.

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
        {
            "alias": "Route to %s (verify + retry)" % input_name,
            "repeat": {
                "sequence": [
                    {
                        "action": "media_player.select_source",
                        "target": {"entity_id": disp},
                        "data": {"source": input_name},
                    },
                    {
                        "wait_template": "{{ %s }}" % landed,
                        "timeout": {"seconds": 3},
                        "continue_on_timeout": True,
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


def build_room_scripts(client, cluster, commissioning=None) -> dict:
    """Return {object_id: script_config} for a room. Read-only against HA except
    for the small server-side renders used to detect the audio + tuner sources.
    Each returned config carries a stamped variables.proos_hash (edit-detection)."""
    commissioning = commissioning or {}
    scripts: dict = {}
    if cluster.display is None:
        return scripts  # no screen -> no activities (empty room stays empty)

    area_slug = _slug(cluster.area)
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
        seq = [{
            # Power the display explicitly first. Idempotent (turning on an
            # already-on TV is a no-op) and makes AVR rooms robust where CEC
            # one-touch-play from the source may not chain-power the screen.
            "alias": "Turn on display",
            "action": "media_player.turn_on",
            "target": {"entity_id": disp},
        }, _wait_display_on(disp), {
            "alias": "Wake source (CEC one-touch-play)",
            "action": "media_player.turn_on",
            "target": {"entity_id": src.entity},
        }]
        route = routes.get(src.entity) or {}
        # Deterministic input routing. CEC one-touch-play (above) wakes the source
        # but only *requests* the display follow; on a ProOS Samsung display we can
        # additionally drive the exact input over licensed IP control, so the screen
        # lands on the right HDMI even when CEC is flaky or disabled. Commission the
        # mapping per source as routes[src.entity] = {"input": "HDMI 2"} (or
        # {"hdmi": 2}); an IR/remote route {"remote":..., "command":...} still works
        # as a legacy fallback. With nothing commissioned we stay CEC-only (today's
        # behaviour) rather than guessing a wrong input -- Verify, Don't Assume.
        disc = _discrete_inputs(client, disp)
        input_name = route.get("input")
        if not input_name and route.get("hdmi") is not None:
            input_name = "HDMI %s" % route["hdmi"]
        if input_name and input_name in disc:
            if disp_verifies:
                # Certified display with readback: confirm the input landed, re-fire
                # if not, raise an incident if it never takes. Bounded to activation.
                seq += _route_and_verify(disp, input_name, cluster.area)
            else:
                # Compatible display: send the input best-effort, no verify/incident.
                seq.append({
                    "alias": "Select %s (best-effort — display not readback-verified)" % input_name,
                    "action": "media_player.select_source",
                    "target": {"entity_id": disp},
                    "data": {"source": input_name},
                })
        elif route.get("remote") and route.get("command"):
            seq.append({
                "alias": "Explicit HDMI route (commissioned)",
                "action": "remote.send_command",
                "target": {"entity_id": route["remote"]},
                "data": {"command": route["command"]},
            })
        seq += _audio_steps(audio, source_eid=src.entity)
        scripts[oid] = {
            "alias": f"ProOS · {cluster.area} · Watch {label}",
            "icon": _ICON_BY_INTEGRATION.get(src.integration, "mdi:television-play"),
            "mode": "restart",
            "description": "ProOS-generated activity (provisional). Edit freely in HA.",
            "variables": _marker("watch_source", audio, cluster.area),
            "sequence": seq,
        }

    # Watch TV (broadcast tuner) -- only if the display exposes a 'TV' source
    if _display_has_tv_source(client, disp):
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_tv"
        seq = [{
            "alias": "Turn on display",
            "action": "media_player.turn_on",
            "target": {"entity_id": disp},
        }, _wait_display_on(disp)] + (
            _route_and_verify(disp, "TV", cluster.area) if disp_verifies else [{
                "alias": "Tune display to broadcast TV (best-effort)",
                "action": "media_player.select_source",
                "target": {"entity_id": disp},
                "data": {"source": "TV"},
            }]
        ) + _audio_steps(audio, broadcast=True)
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
            # Paired remote shares the media_player's object_id.
            seq.append({
                "alias": f"Sleep {cluster.label_for(src)}",
                "action": "remote.turn_off",
                "target": {"entity_id": "remote." + src.entity.split(".", 1)[1]},
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
