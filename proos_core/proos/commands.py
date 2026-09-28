"""
ProOS Core -- device command catalog.

Powers the device-first activity/automation step builder: pick a device, and Core returns
the commands THAT device actually supports -- derived from its capabilities (media_player
supported_features + a paired remote for wake/sleep + its source list), named cleanly. The
composite commands (e.g. "Wake (reconnect + power on)") are the SAME steps the generator
produces, so what you add by hand matches what ProOS generates.

Two calls:
  device_commands(client, entity) -> the pick-list (labels, groups, params).
  build_command(client, entity, command, params) -> the exact HA step to insert.
"""
from __future__ import annotations
import json
from . import generator

# media_player supported_features bits
_PAUSE, _VOL_SET, _VOL_MUTE, _PREV, _NEXT = 1, 4, 8, 16, 32
_TURN_ON, _TURN_OFF, _VOL_STEP, _SELECT_SOURCE, _PLAY = 128, 256, 1024, 2048, 16384


def _probe(client, entity):
    """One render: supported_features, source_list, device_class, name, and any paired
    remote on the SAME device (resolved by device, per the identity standard)."""
    e = json.dumps(entity)
    tmpl = (
        "{% set d = device_id(" + e + ") %}"
        "{% set rem = (device_entities(d) | select('match','^remote\\\\.') | list) if d else [] %}"
        "{{ {'sf': state_attr(" + e + ",'supported_features') or 0,"
        "'src': state_attr(" + e + ",'source_list') or [],"
        "'dc': state_attr(" + e + ",'device_class'),"
        "'name': state_attr(" + e + ",'friendly_name') or " + e + ","
        "'remote': (rem[0] if rem else none)} | to_json }}"
    )
    try:
        return json.loads(client.render_template(tmpl) or "{}")
    except Exception:
        return {}


def _label_from(entity, info):
    return (info.get("name") or entity.split(".", 1)[-1].replace("_", " ").title())


def device_commands(client, entity: str) -> dict:
    """The commands a device supports, grouped, with parameter specs."""
    domain = entity.split(".", 1)[0]
    info = _probe(client, entity)
    label = _label_from(entity, info)
    sf = int(info.get("sf") or 0)
    src = [s for s in (info.get("src") or []) if isinstance(s, str)]
    remote = info.get("remote")
    cmds = []

    def add(cid, lbl, group, params=None):
        cmds.append({"id": cid, "label": lbl, "group": group, "params": params or []})

    if domain == "media_player":
        # Power / wake — a paired-remote device wakes+sleeps via the remote (Apple TV / Shield);
        # otherwise plain power. Only ONE of these pairs is offered.
        if remote:
            add("wake", "Wake (reconnect + power on)", "Power")
            add("sleep", "Sleep", "Power")
        elif sf & _TURN_ON or sf & _TURN_OFF:
            add("turn_on", "Turn on", "Power")
            add("turn_off", "Turn off", "Power")
        if sf & _SELECT_SOURCE and src:
            add("select_source", "Select input", "Video",
                [{"key": "source", "label": "Input", "type": "select", "options": src}])
            if "TV" in src:
                add("route_tv_audio", "Route TV audio (to this)", "Audio")
        if sf & _PLAY or sf & _PAUSE:
            add("play", "Play", "Playback")
            add("pause", "Pause", "Playback")
        if sf & _NEXT:
            add("next", "Next", "Playback")
        if sf & _PREV:
            add("previous", "Previous", "Playback")
        if sf & _VOL_SET:
            add("volume_set", "Set volume", "Audio",
                [{"key": "level", "label": "Volume %", "type": "number", "min": 0, "max": 100, "default": 30}])
        if sf & _VOL_MUTE:
            add("mute", "Mute", "Audio")
            add("unmute", "Unmute", "Audio")
    elif domain == "remote":
        add("remote_on", "Turn on", "Power")
        add("remote_off", "Turn off", "Power")
        add("send_command", "Send command", "Control",
            [{"key": "command", "label": "Command", "type": "text"}])
    elif domain == "switch":
        add("switch_on", "Turn on", "Power")
        add("switch_off", "Turn off", "Power")
    else:
        # Generic on/off for anything toggleable.
        add("hass_on", "Turn on", "Power")
        add("hass_off", "Turn off", "Power")

    return {"entity": entity, "label": label, "domain": domain, "commands": cmds}


def build_command(client, entity: str, command: str, params: dict | None = None) -> dict:
    """Return the exact HA step (a single action, or one composite if/then) for a command."""
    params = params or {}
    info = _probe(client, entity)
    label = _label_from(entity, info)

    def action(act, data=None, target_entity=None, alias=None):
        step = {"alias": alias or "", "action": act,
                "target": {"entity_id": target_entity or entity}}
        if data:
            step["data"] = data
        return step

    if command == "wake":
        remote = info.get("remote") or ("remote." + entity.split(".", 1)[1])
        return {"step": {
            "alias": f"Wake {label} if it is asleep",
            "if": [{"condition": "state", "entity_id": entity, "state": "off"}],
            "then": [
                {"alias": f"Reconnect {label}", "action": "remote.turn_on", "target": {"entity_id": remote}},
                {"alias": "Wait a moment for it to reconnect", "delay": {"seconds": 3}},
                {"alias": f"Turn on {label}", "action": "media_player.turn_on", "target": {"entity_id": entity}},
            ]}}
    if command == "sleep":
        remote = info.get("remote") or ("remote." + entity.split(".", 1)[1])
        return {"step": action("remote.turn_off", target_entity=remote, alias=f"Sleep {label}")}
    if command == "turn_on":
        return {"step": action("media_player.turn_on", alias=f"Turn on {label}")}
    if command == "turn_off":
        return {"step": action("media_player.turn_off", alias=f"Turn off {label}")}
    if command in ("select_source", "route_tv_audio"):
        source = "TV" if command == "route_tv_audio" else (params.get("source") or "")
        alias = ("Route TV audio to " + label) if command == "route_tv_audio" else ("Select " + source)
        return {"step": action("media_player.select_source", {"source": source}, alias=alias)}
    if command == "play":
        return {"step": action("media_player.media_play", alias=f"Play {label}")}
    if command == "pause":
        return {"step": action("media_player.media_pause", alias=f"Pause {label}")}
    if command == "next":
        return {"step": action("media_player.media_next_track", alias="Next")}
    if command == "previous":
        return {"step": action("media_player.media_previous_track", alias="Previous")}
    if command == "volume_set":
        try:
            lvl = max(0.0, min(1.0, float(params.get("level", 30)) / 100.0))
        except Exception:
            lvl = 0.3
        return {"step": action("media_player.volume_set", {"volume_level": round(lvl, 3)},
                               alias=f"Set {label} volume")}
    if command == "mute":
        return {"step": action("media_player.volume_mute", {"is_volume_muted": True}, alias=f"Mute {label}")}
    if command == "unmute":
        return {"step": action("media_player.volume_mute", {"is_volume_muted": False}, alias=f"Unmute {label}")}
    if command == "remote_on":
        return {"step": action("remote.turn_on", alias=f"Turn on {label}")}
    if command == "remote_off":
        return {"step": action("remote.turn_off", alias=f"Turn off {label}")}
    if command == "send_command":
        return {"step": action("remote.send_command", {"command": params.get("command", "")},
                               alias=f"{label}: {params.get('command','command')}")}
    if command == "switch_on":
        return {"step": action("switch.turn_on", alias=f"Turn on {label}")}
    if command == "switch_off":
        return {"step": action("switch.turn_off", alias=f"Turn off {label}")}
    if command == "hass_on":
        return {"step": action("homeassistant.turn_on", alias=f"Turn on {label}")}
    if command == "hass_off":
        return {"step": action("homeassistant.turn_off", alias=f"Turn off {label}")}
    return {"error": "unknown command"}
