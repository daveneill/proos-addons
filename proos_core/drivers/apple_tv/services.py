"""Define services for the Apple TV integration."""

from pyatv.const import KeyboardFocusState
from pyatv.exceptions import NotSupportedError, ProtocolError
from pyatv.interface import AppleTV as AppleTVInterface
import voluptuous as vol

from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, service

from .const import ATTR_TEXT, DOMAIN

SERVICE_SET_KEYBOARD_TEXT = "set_keyboard_text"
SERVICE_SET_KEYBOARD_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TEXT): cv.string,
    }
)

SERVICE_APPEND_KEYBOARD_TEXT = "append_keyboard_text"
SERVICE_APPEND_KEYBOARD_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TEXT): cv.string,
    }
)

SERVICE_CLEAR_KEYBOARD_TEXT = "clear_keyboard_text"
SERVICE_CLEAR_KEYBOARD_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def _get_atv(call: ServiceCall) -> AppleTVInterface:
    """Get the AppleTVInterface for a service call."""
    entry = service.async_get_config_entry(
        call.hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID]
    )
    atv: AppleTVInterface | None = entry.runtime_data.atv
    if atv is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="not_connected",
        )
    return atv


def _check_keyboard_focus(atv: AppleTVInterface) -> None:
    """Check that keyboard is focused on the device."""
    try:
        focus_state = atv.keyboard.text_focus_state
    except NotSupportedError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="keyboard_not_available",
        ) from err
    if focus_state is not KeyboardFocusState.Focused:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="keyboard_not_focused",
        )


async def _async_set_keyboard_text(call: ServiceCall) -> None:
    """Set text in the keyboard input field on an Apple TV."""
    atv = _get_atv(call)
    _check_keyboard_focus(atv)
    try:
        await atv.keyboard.text_set(call.data[ATTR_TEXT])
    except ProtocolError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="keyboard_error",
        ) from err


async def _async_append_keyboard_text(call: ServiceCall) -> None:
    """Append text to the keyboard input field on an Apple TV."""
    atv = _get_atv(call)
    _check_keyboard_focus(atv)
    try:
        await atv.keyboard.text_append(call.data[ATTR_TEXT])
    except ProtocolError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="keyboard_error",
        ) from err


async def _async_clear_keyboard_text(call: ServiceCall) -> None:
    """Clear text in the keyboard input field on an Apple TV."""
    atv = _get_atv(call)
    _check_keyboard_focus(atv)
    try:
        await atv.keyboard.text_clear()
    except ProtocolError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="keyboard_error",
        ) from err


# ── ProOS certified actions (register 1034) ────────────────────────────────────────────────────────────────────────
# Each answers with what the Apple TV said, or fails with the reason in words — never a guessed success.

def _proos(call: ServiceCall):
    entry = service.async_get_config_entry(call.hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID])
    manager = entry.runtime_data
    if manager.atv is None or getattr(manager, "proos", None) is None:
        raise HomeAssistantError("The Apple TV is not connected")
    return manager


async def _run(coro):
    from .proos_extras import NotOffered
    try:
        return await coro
    except NotOffered as err:
        raise HomeAssistantError(str(err)) from err
    except (ProtocolError, NotSupportedError) as err:
        raise HomeAssistantError("The Apple TV refused: %s" % err) from err


async def _proos_state(call: ServiceCall) -> ServiceResponse:
    m = _proos(call)
    px = m.proos
    want = set(call.data.get("include") or ["display", "keyboard"])
    out: dict = {"display_state": px.display_state}
    if "keyboard" in want:
        kb = {"focused": None, "text": None}
        try:
            kb["focused"] = m.atv.keyboard.text_focus_state is KeyboardFocusState.Focused
            if kb["focused"]:
                kb["text"] = await m.atv.keyboard.text_get()
        except Exception:  # noqa: BLE001
            pass
        out["keyboard"] = kb
    if "accounts" in want:
        try:
            out["accounts"] = await _run(px.accounts())
        except HomeAssistantError as err:
            out["accounts_error"] = str(err)
    if "now_playing" in want:
        try:
            out["now_playing"] = await _run(px.now_playing())
        except HomeAssistantError as err:
            out["now_playing_error"] = str(err)
    if "captions" in want:
        try:
            out["captions"] = await _run(px.captions())
        except HomeAssistantError as err:
            out["captions_error"] = str(err)
    return out


async def _proos_key(call: ServiceCall) -> None:
    await _run(_proos(call).proos.key(call.data["key"], call.data.get("action", "tap")))


async def _proos_touch(call: ServiceCall) -> None:
    d = call.data
    await _run(_proos(call).proos.touch(d["kind"], d.get("x", 500), d.get("y", 500), d.get("x2", 500),
                                        d.get("y2", 500), d.get("duration_ms", 200), d.get("action", "tap")))


async def _proos_media(call: ServiceCall) -> None:
    await _run(_proos(call).proos.media(call.data["command"], float(call.data.get("seconds", 0))))


async def _proos_switch_account(call: ServiceCall) -> None:
    await _run(_proos(call).proos.switch_account(call.data["account_id"]))


async def _proos_captions(call: ServiceCall) -> ServiceResponse:
    return await _run(_proos(call).proos.captions(call.data.get("settings")))


async def _proos_play_queue_item(call: ServiceCall) -> None:
    await _run(_proos(call).proos.play_queue_item(call.data["item_id"]))


async def _proos_language_option(call: ServiceCall) -> None:
    await _run(_proos(call).proos.language_option(dict(call.data["option"]), bool(call.data.get("enable", True))))


_ENTRY = {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
PROOS_SERVICES = {
    "proos_state": (_proos_state, vol.Schema({**_ENTRY, vol.Optional("include"): [cv.string]}), SupportsResponse.ONLY),
    "proos_key": (_proos_key, vol.Schema({**_ENTRY, vol.Required("key"): cv.string,
                                          vol.Optional("action", default="tap"): vol.In(["tap", "double", "hold", "down", "up"])}), SupportsResponse.NONE),
    "proos_touch": (_proos_touch, vol.Schema({**_ENTRY, vol.Required("kind"): vol.In(["swipe", "press", "hold", "release", "click"]),
                                              vol.Optional("x"): vol.Coerce(int), vol.Optional("y"): vol.Coerce(int),
                                              vol.Optional("x2"): vol.Coerce(int), vol.Optional("y2"): vol.Coerce(int),
                                              vol.Optional("duration_ms"): vol.Coerce(int),
                                              vol.Optional("action"): vol.In(["tap", "double", "hold"])}), SupportsResponse.NONE),
    "proos_media": (_proos_media, vol.Schema({**_ENTRY, vol.Required("command"): cv.string,
                                              vol.Optional("seconds"): vol.Coerce(float)}), SupportsResponse.NONE),
    "proos_switch_account": (_proos_switch_account, vol.Schema({**_ENTRY, vol.Required("account_id"): cv.string}), SupportsResponse.NONE),
    "proos_captions": (_proos_captions, vol.Schema({**_ENTRY, vol.Optional("settings"): dict}), SupportsResponse.ONLY),
    "proos_play_queue_item": (_proos_play_queue_item, vol.Schema({**_ENTRY, vol.Required("item_id"): cv.string}), SupportsResponse.NONE),
    "proos_language_option": (_proos_language_option, vol.Schema({**_ENTRY, vol.Required("option"): dict,
                                                                   vol.Optional("enable", default=True): cv.boolean}), SupportsResponse.NONE),
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the Apple TV integration."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_KEYBOARD_TEXT,
        _async_set_keyboard_text,
        schema=SERVICE_SET_KEYBOARD_TEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPEND_KEYBOARD_TEXT,
        _async_append_keyboard_text,
        schema=SERVICE_APPEND_KEYBOARD_TEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_KEYBOARD_TEXT,
        _async_clear_keyboard_text,
        schema=SERVICE_CLEAR_KEYBOARD_TEXT_SCHEMA,
    )
    for name, (handler, schema, resp) in PROOS_SERVICES.items():
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=resp)
