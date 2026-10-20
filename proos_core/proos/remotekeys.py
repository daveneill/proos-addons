"""ONE BUTTON MAP FOR EVERY SCREEN AND EVERY REMOTE (20 Oct 2026, register 969j).

Dave, 15:05: "using the remote hard buttons as a full remote setup … this will also link with … redesign our media
player / TV control pages". Until now the only key tables lived inside dashboard.html (three hand-written tables:
Samsung, LG, Android TV). The ProOS Remote (Astrion HA100A) and the coming TV pages need the same answer to "what
does Up mean for THIS device", so it lives here, once, in Core.

THE PLATFORM'S OWN COMMANDS, NEVER INVENTED ONES. Each table is the vocabulary the integration itself accepts:
`remote.send_command` for the integrations whose `remote.` entity takes named commands (Apple TV, Samsung, Android TV,
Roku), `webostv.button` for LG (the LG integration has no remote entity; its button service is the documented door).
Media keys fall back to the platform's own `media_player` services when the player says it supports them.

A KEY THE DEVICE CAN'T DO IS NOT GUESSED. `plan()` returns None and the screen greys that key out (Rule Zero: say
"I don't know" rather than invent).
"""
from __future__ import annotations

# The logical keys every screen and remote speaks.
KEYS = ("up", "down", "left", "right", "ok", "back", "home", "menu", "play_pause", "rewind", "ffwd", "stop",
        "ch_up", "ch_down", "red", "green", "blue", "yellow", "info", "guide")

# platform -> (how to send, {logical key: the platform's own command})
TABLES = {
    # pyatv's remote-control names, as HA's apple_tv remote.send_command takes them. Apple's Back is Menu; Apple's
    # TV/Home button is Home; the context menu is Top Menu.
    "apple_tv": ("remote", {
        "up": "up", "down": "down", "left": "left", "right": "right", "ok": "select",
        "back": "menu", "home": "home", "menu": "top_menu",
        "play_pause": "play_pause", "rewind": "skip_backward", "ffwd": "skip_forward", "stop": "stop",
        "ch_up": "channel_up", "ch_down": "channel_down"}),
    # Samsung's own KEY_ codes (custom samsungtv_smart, and the platform's samsungtv — same key names).
    "samsungtv_smart": ("remote", {
        "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "ok": "KEY_ENTER",
        "back": "KEY_RETURN", "home": "KEY_HOME", "menu": "KEY_MENU",
        "play_pause": "KEY_PLAY", "rewind": "KEY_REWIND", "ffwd": "KEY_FF", "stop": "KEY_STOP",
        "ch_up": "KEY_CHUP", "ch_down": "KEY_CHDOWN",
        "red": "KEY_RED", "green": "KEY_GREEN", "blue": "KEY_CYAN", "yellow": "KEY_YELLOW",
        "info": "KEY_INFO", "guide": "KEY_GUIDE"}),
    # Android TV / Google TV / Shield — Android key names as androidtv_remote takes them.
    "androidtv_remote": ("remote", {
        "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT", "ok": "DPAD_CENTER",
        "back": "BACK", "home": "HOME", "menu": "MENU",
        "play_pause": "MEDIA_PLAY_PAUSE", "rewind": "MEDIA_REWIND", "ffwd": "MEDIA_FAST_FORWARD",
        "stop": "MEDIA_STOP", "ch_up": "CHANNEL_UP", "ch_down": "CHANNEL_DOWN",
        "red": "PROG_RED", "green": "PROG_GREEN", "blue": "PROG_BLUE", "yellow": "PROG_YELLOW",
        "info": "INFO", "guide": "GUIDE"}),
    "roku": ("remote", {
        "up": "up", "down": "down", "left": "left", "right": "right", "ok": "select",
        "back": "back", "home": "home", "menu": "info",
        "play_pause": "play", "rewind": "reverse", "ffwd": "forward",
        "ch_up": "channel_up", "ch_down": "channel_down"}),
    # LG webOS: the integration's own button service on the media_player.
    "webostv": ("webostv", {
        "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "ok": "ENTER",
        "back": "BACK", "home": "HOME", "menu": "MENU",
        "play_pause": "PLAY", "rewind": "REWIND", "ffwd": "FASTFORWARD", "stop": "STOP",
        "ch_up": "CHANNELUP", "ch_down": "CHANNELDOWN",
        "red": "RED", "green": "GREEN", "blue": "BLUE", "yellow": "YELLOW",
        "info": "INFO", "guide": "GUIDE"}),
}
TABLES["samsungtv"] = TABLES["samsungtv_smart"]

# media_player supported_features bits (HA MediaPlayerEntityFeature)
F_PAUSE, F_PREV, F_NEXT, F_STOP, F_PLAY = 1, 16, 32, 4096, 16384
_MEDIA_FALLBACK = {
    "play_pause": ("media_play_pause", F_PAUSE | F_PLAY),
    "rewind": ("media_previous_track", F_PREV),
    "ffwd": ("media_next_track", F_NEXT),
    "stop": ("media_stop", F_STOP),
}


def platform_of(entity_id: str, entity_registry: list) -> str | None:
    for e in entity_registry or []:
        if e.get("entity_id") == entity_id:
            return e.get("platform")
    return None


def remote_for(entity_id: str, entity_registry: list) -> str | None:
    """The `remote.` entity on the same device as a media_player — the door its keys go through."""
    dev = None
    for e in entity_registry or []:
        if e.get("entity_id") == entity_id:
            dev = e.get("device_id")
            break
    if not dev:
        return None
    for e in entity_registry or []:
        if e.get("device_id") == dev and str(e.get("entity_id", "")).startswith("remote.") \
                and not e.get("disabled_by"):
            return e["entity_id"]
    return None


def plan(key: str, entity_id: str, entity_registry: list, features: int = 0) -> dict | None:
    """What pressing `key` means for the device `entity_id` (its media_player), as ONE platform service call:
    {domain, service, data}. None when the device can't do that key — the screen greys it out."""
    if key not in KEYS or not entity_id:
        return None
    plat = platform_of(entity_id, entity_registry)
    how, table = TABLES.get(plat or "", (None, {}))
    cmd = table.get(key)
    if cmd and how == "remote":
        r = remote_for(entity_id, entity_registry)
        if r:
            return {"domain": "remote", "service": "send_command", "data": {"entity_id": r, "command": cmd}}
    if cmd and how == "webostv":
        return {"domain": "webostv", "service": "button", "data": {"entity_id": entity_id, "button": cmd}}
    fb = _MEDIA_FALLBACK.get(key)
    if fb and entity_id.startswith("media_player.") and (int(features or 0) & fb[1]):
        return {"domain": "media_player", "service": fb[0], "data": {"entity_id": entity_id}}
    return None


def capabilities(entity_id: str, entity_registry: list, features: int = 0) -> dict:
    """Which keys this device can do — {key: True/False} — so a legend never shows a key that does nothing."""
    return {k: plan(k, entity_id, entity_registry, features) is not None for k in KEYS}
