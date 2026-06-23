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

All power is media_player.turn_on/turn_off (never the remote). TV power is never a
gate -- these scripts only issue commands; the verdict lives elsewhere and treats
the Samsung's reported power as advisory.
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


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "x"


def _audio_target(client, cluster, override):
    """Return (entity_id, source) for TV audio, or None for the display's own speakers.

    override:
      None                                       -> auto: first audio device that
                                                    exposes a 'TV' source
      {'mode': 'tv'}                             -> None (use TV speakers)
      {'mode':'sonos','entity':..,'source':..}   -> explicit (installer override)
    """
    if override:
        if override.get("mode") == "tv":
            return None
        if override.get("mode") == "sonos" and override.get("entity"):
            return (override["entity"], override.get("source", "TV"))
    if not cluster.audio:
        return None
    # Find the first audio device that actually has a 'TV' input -- checked
    # server-side so we never emit a select_source for a source that doesn't exist.
    eids = [d.entity for d in cluster.audio]
    tmpl = (
        "{% set out = namespace(hit='') %}"
        "{% set eids = " + json.dumps(eids) + " %}"
        "{% for e in eids %}"
        "{% if not out.hit and 'TV' in (state_attr(e,'source_list') or []) %}"
        "{% set out.hit = e %}{% endif %}"
        "{% endfor %}{{ out.hit }}"
    )
    hit = (client.render_template(tmpl) or "").strip()
    return (hit, "TV") if hit and hit != "None" else None


def _audio_step(audio):
    if not audio:
        return []
    eid, source = audio
    return [{
        "alias": "Route TV audio to Sonos",
        "action": "media_player.select_source",
        "target": {"entity_id": eid},
        "data": {"source": source},
    }]


def _marker(kind, audio):
    # Travels inside the script so ProOS-owned objects are identifiable and the
    # active audio mode is self-describing. (Future: a config hash here lets us
    # distinguish installer-edited from untouched for selective regeneration.)
    return {
        "proos_managed": True,
        "proos_provisional": True,
        "proos_kind": kind,
        "proos_audio": "sonos" if audio else "tv",
    }


def _display_has_tv_source(client, display_eid) -> bool:
    out = (client.render_template(
        "{{ 'TV' in (state_attr('%s','source_list') or []) }}" % display_eid
    ) or "").strip().lower()
    return out == "true"


def build_room_scripts(client, cluster, commissioning=None) -> dict:
    """Return {object_id: script_config} for a room. Read-only against HA except
    for the small server-side renders used to detect the audio + tuner sources."""
    commissioning = commissioning or {}
    scripts: dict = {}
    if cluster.display is None:
        return scripts  # no screen -> no activities (empty room stays empty)

    area_slug = _slug(cluster.area)
    disp = cluster.display.entity
    audio = _audio_target(client, cluster, commissioning.get("audio"))
    routes = commissioning.get("routes") or {}

    # one 'Watch <source>' per discovered source
    for src in cluster.sources:
        label = cluster.label_for(src)
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_{_slug(label)}"
        seq = [{
            "alias": "Wake source (CEC one-touch-play)",
            "action": "media_player.turn_on",
            "target": {"entity_id": src.entity},
        }]
        route = routes.get(src.entity)
        if route:
            seq.append({
                "alias": "Explicit HDMI route (commissioned)",
                "action": "remote.send_command",
                "target": {"entity_id": route["remote"]},
                "data": {"command": route["command"]},
            })
        seq += _audio_step(audio)
        scripts[oid] = {
            "alias": f"ProOS \u00b7 {cluster.area} \u00b7 Watch {label}",
            "icon": _ICON_BY_INTEGRATION.get(src.integration, "mdi:television-play"),
            "mode": "restart",
            "description": "ProOS-generated activity (provisional). Edit freely in HA.",
            "variables": _marker("watch_source", audio),
            "sequence": seq,
        }

    # Watch TV (broadcast tuner) -- only if the display exposes a 'TV' source
    if _display_has_tv_source(client, disp):
        oid = f"{PROOS_PREFIX}_{area_slug}_watch_tv"
        seq = [{
            "alias": "Tune display to broadcast TV",
            "action": "media_player.select_source",
            "target": {"entity_id": disp},
            "data": {"source": "TV"},
        }] + _audio_step(audio)
        scripts[oid] = {
            "alias": f"ProOS \u00b7 {cluster.area} \u00b7 Watch TV",
            "icon": "mdi:television-classic",
            "mode": "restart",
            "description": "ProOS-generated activity (provisional). Broadcast tuner.",
            "variables": _marker("watch_tv", audio),
            "sequence": seq,
        }

    # TV Off -- turn off the screen and sleep every source
    oid = f"{PROOS_PREFIX}_{area_slug}_tv_off"
    seq = [{
        "alias": "Turn off display",
        "action": "media_player.turn_off",
        "target": {"entity_id": disp},
    }]
    for src in cluster.sources:
        seq.append({
            "alias": f"Sleep {cluster.label_for(src)}",
            "action": "media_player.turn_off",
            "target": {"entity_id": src.entity},
        })
    scripts[oid] = {
        "alias": f"ProOS \u00b7 {cluster.area} \u00b7 TV Off",
        "icon": "mdi:television-off",
        "mode": "restart",
        "description": "ProOS-generated activity. Turns off the screen and sleeps sources.",
        "variables": _marker("tv_off", None),
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
