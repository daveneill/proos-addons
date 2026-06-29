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
  - create-if-absent: an existing script is NEVER overwritten by routine discovery,
    so installer edits are never clobbered. An explicit regenerate (overwrite=True)
    is the only thing that replaces them. (provisional/committed hashing is a later
    refinement; create-if-absent is the safe Phase-1 stand-in.)

Wake is media_player.turn_on. Sleep uses each source's paired remote.<object_id>
where media_player.turn_off won't actually sleep the device (Apple TV / Android TV).
TV power is never a gate -- these scripts only issue commands; the verdict lives
elsewhere and treats the Samsung's reported power as advisory.
"""
from __future__ import annotations
import json
import re

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
    # / filter activities by room without re-deriving it. (Future: a config hash
    # here lets us distinguish installer-edited from untouched for regeneration.)
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


def build_room_scripts(client, cluster, commissioning=None) -> dict:
    """Return {object_id: script_config} for a room. Read-only against HA except
    for the small server-side renders used to detect the audio + tuner sources."""
    commissioning = commissioning or {}
    scripts: dict = {}
    if cluster.display is None:
        return scripts  # no screen -> no activities (empty room stays empty)

    area_slug = _slug(cluster.area)
    disp = cluster.display.entity
    audio = _audio_config(client, cluster, commissioning.get("audio"))
    routes = commissioning.get("routes") or {}

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
            seq.append({
                "alias": "Select %s (discrete IP input)" % input_name,
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
            "alias": f"ProOS \u00b7 {cluster.area} \u00b7 Watch {label}",
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
        }, _wait_display_on(disp), {
            "alias": "Tune display to broadcast TV",
            "action": "media_player.select_source",
            "target": {"entity_id": disp},
            "data": {"source": "TV"},
        }] + _audio_steps(audio, broadcast=True)
        scripts[oid] = {
            "alias": f"ProOS \u00b7 {cluster.area} \u00b7 Watch TV",
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
    seq.append({
        "alias": "Turn off display",
        "action": "media_player.turn_off",
        "target": {"entity_id": disp},
    })
    scripts[oid] = {
        "alias": f"ProOS \u00b7 {cluster.area} \u00b7 TV Off",
        "icon": "mdi:television-off",
        "mode": "restart",
        "description": "ProOS-generated activity. Turns off the screen and sleeps sources.",
        "variables": _marker("tv_off", None, cluster.area),
        "sequence": seq,
    }
    return scripts


def generate(client, cluster, commissioning=None, overwrite=False) -> dict:
    """Create-if-absent (default) the room's activity scripts in HA.

    overwrite=False (routine discovery): an existing script is left untouched, so
                    installer edits survive. Only missing scripts are created.
    overwrite=True  (explicit 'regenerate'): force-replace every generated script.

    Returns {created:[...], kept:[...], object_ids:[...]}.
    """
    scripts = build_room_scripts(client, cluster, commissioning)
    created, kept = [], []
    for oid, cfg in scripts.items():
        if not overwrite:
            try:
                if client.get_script(oid) is not None:
                    kept.append(oid)
                    continue
            except Exception:
                pass  # treat a lookup failure as 'absent' and (re)create
        client.upsert_script(oid, cfg)
        created.append(oid)
    return {"created": created, "kept": kept, "object_ids": list(scripts)}
