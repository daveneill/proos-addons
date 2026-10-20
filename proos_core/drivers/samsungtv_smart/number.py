"""Samsung TV Smart - Number entities.

Provides interactive Number entities (sliders in HA UI) for:
- IP Control picture backlight (0-50)
- Art Mode brightness (0-100)
- Art Mode color temperature (-5 to +5)

Art Mode numbers wrap the existing art_api methods and only appear on Frame TVs.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_ID, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.art import SamsungTVAsyncArt
from .api.ipcontrol import (
    DEFAULT_IP_CONTROL_PORT,
    SamsungIPControl,
    SamsungIPControlAuthError,
    SamsungIPControlError,
)
from .const import (
    CONF_ENABLE_IP_CONTROL,
    CONF_IP_CONTROL_PORT,
    CONF_IP_CONTROL_TOKEN,
    CONF_IS_FRAME_TV,
    CONF_WS_NAME,
    DATA_ART_API,
    DATA_CFG,
    DEFAULT_PORT,
    DOMAIN,
    WS_PREFIX,
)
from .token_notify import METHOD_IP_CONTROL, clear_token_problem, notify_token_problem

_LOGGER = logging.getLogger(__name__)


def _ip_control_active(entry: ConfigEntry) -> bool:
    """True when IP Control is paired AND enabled in the options."""
    return bool(entry.data.get(CONF_IP_CONTROL_TOKEN)) and entry.options.get(
        CONF_ENABLE_IP_CONTROL, True
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung TV number entities from config entry."""
    config = hass.data[DOMAIN][entry.entry_id][DATA_CFG]
    host = config[CONF_HOST]
    port = config.get(CONF_PORT, DEFAULT_PORT)
    token = config.get(CONF_TOKEN)
    ws_name = config.get(CONF_WS_NAME, "HomeAssistant")
    device_unique_id = config.get(CONF_ID, entry.entry_id)
    device_name = config.get(CONF_NAME) or entry.title or host

    if _ip_control_active(entry):
        async_add_entities(
            [
                SamsungTVIPControlBacklightNumber(
                    hass, entry, host, device_name, device_unique_id
                )
            ],
            True,
        )
        _LOGGER.debug("IP Control backlight number entity created for %s", device_name)

    session = async_get_clientsession(hass)

    # Reuse shared art_api if already created by sensor.py / select.py,
    # otherwise create one (same pattern as select.py).
    art_api = hass.data[DOMAIN][entry.entry_id].get(DATA_ART_API)
    if not art_api:
        art_api = SamsungTVAsyncArt(
            host=host,
            port=port,
            token=token,
            session=session,
            timeout=5,
            name=f"{WS_PREFIX} {ws_name} Art Number",
        )

    # Use persisted flag if available, otherwise probe live
    is_frame_tv_cached = entry.data.get(CONF_IS_FRAME_TV, False)
    if is_frame_tv_cached:
        is_frame_supported = True
    else:
        try:
            async with asyncio.timeout(5):
                is_frame_supported = await art_api.supported()
        except Exception:
            is_frame_supported = False

    if not is_frame_supported:
        _LOGGER.debug(
            "Not a Frame TV, skipping art number entities for %s", device_name
        )
        return

    entities: list[RestoreNumber] = [
        SamsungTVArtBrightnessNumber(
            hass, entry, art_api, device_name, device_unique_id
        ),
        SamsungTVArtColorTemperatureNumber(
            hass, entry, art_api, device_name, device_unique_id
        ),
    ]
    async_add_entities(entities)
    _LOGGER.debug(
        "Frame Art number entities (brightness, color temperature) created for %s",
        device_name,
    )


# ══════════════════════════════════════════════════════════════════════════
# IP Control Backlight (0-50, native TV scale)
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVIPControlBacklightNumber(NumberEntity):
    """Number entity for the IP Control picture backlight value."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:brightness-6"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 1

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        device_name: str,
        device_unique_id: str,
    ) -> None:
        """Initialize the IP Control backlight number."""
        self.hass = hass
        self._entry_id = entry.entry_id
        self._host = host
        self._device_name = device_name
        self._device_unique_id = device_unique_id
        self._ip_control: SamsungIPControl | None = None
        self._ip_control_token: str | None = None
        self._attr_unique_id = f"{device_unique_id}_ip_control_backlight"
        self._attr_name = "Backlight"
        self._attr_native_value: float | None = None
        self._attr_available = False

    @property
    def device_info(self) -> DeviceInfo:
        """Link this entity to the TV device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            name=self._device_name,
        )

    @property
    def available(self) -> bool:
        """Available only while IP Control is paired, enabled, and reachable."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return bool(entry and _ip_control_active(entry) and self._attr_available)

    def _device_title(self) -> str:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return entry.title if entry else (self._device_name or "this Samsung TV")

    def _get_ip_control(self) -> SamsungIPControl | None:
        """Return a live IP Control client if paired AND enabled."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or not _ip_control_active(entry):
            self._ip_control = None
            self._ip_control_token = None
            return None
        token = entry.data.get(CONF_IP_CONTROL_TOKEN)
        if self._ip_control is None or self._ip_control_token != token:
            self._ip_control = SamsungIPControl(
                self.hass,
                self._host,
                port=entry.data.get(CONF_IP_CONTROL_PORT, DEFAULT_IP_CONTROL_PORT),
                token=token,
            )
            self._ip_control_token = token
        return self._ip_control

    async def async_set_native_value(self, value: float) -> None:
        """Set picture backlight through IP Control."""
        backlight = int(value)
        if backlight < 0 or backlight > 50:
            raise HomeAssistantError("Backlight must be between 0 and 50.")

        client = self._get_ip_control()
        if client is None:
            raise HomeAssistantError(
                "IP Control is not paired or is disabled for this TV."
            )

        try:
            self._attr_native_value = await client.async_set_backlight(backlight)
            self._attr_available = True
        except SamsungIPControlAuthError as ex:
            self._mark_unavailable()
            notify_token_problem(
                self.hass, self._entry_id, METHOD_IP_CONTROL, self._device_title()
            )
            raise HomeAssistantError(
                f"IP Control token rejected while setting backlight: {ex}"
            ) from ex
        except SamsungIPControlError as ex:
            self._mark_unavailable()
            raise HomeAssistantError(
                f"Failed to set backlight via IP Control: {ex}"
            ) from ex

        clear_token_problem(self.hass, self._entry_id, METHOD_IP_CONTROL)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Read current picture backlight from IP Control."""
        client = self._get_ip_control()
        if client is None:
            self._mark_unavailable()
            return

        try:
            self._attr_native_value = await client.async_get_backlight()
            self._attr_available = True
        except SamsungIPControlAuthError as ex:
            _LOGGER.warning(
                "IP Control backlight read for %s: token rejected (%s) — "
                "re-pair via the integration options",
                self._host,
                ex,
            )
            self._mark_unavailable()
            notify_token_problem(
                self.hass, self._entry_id, METHOD_IP_CONTROL, self._device_title()
            )
        except SamsungIPControlError as ex:
            _LOGGER.debug(
                "Could not refresh IP Control backlight for %s: %s", self._host, ex
            )
            self._mark_unavailable()
        else:
            clear_token_problem(self.hass, self._entry_id, METHOD_IP_CONTROL)

    def _mark_unavailable(self) -> None:
        """Expose no slider value until a live IP Control read succeeds."""
        self._attr_available = False
        self._attr_native_value = None


# ══════════════════════════════════════════════════════════════════════════
# Base class
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVArtNumberBase(RestoreNumber):
    """Base class for Samsung Frame TV Art Mode number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        art_api: SamsungTVAsyncArt,
        device_name: str,
        device_unique_id: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._art_api = art_api
        self._device_name = device_name
        self._device_unique_id = device_unique_id
        self._attr_native_value: float | None = None
        self._media_player_entity_id: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known value after a restart.

        Art Mode brightness/color temperature can only be read while the TV is
        actually in Art Mode; in standby the read is skipped and the value would
        otherwise be "unknown" until the TV next enters Art Mode. Restoring the
        last stored value keeps the slider showing the most recent value across
        restarts; a live poll refreshes it once the TV is back in Art Mode.
        """
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is not None and last_data.native_value is not None:
            self._attr_native_value = last_data.native_value

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            name=self._device_name,
        )

    def _is_tv_in_art_mode(self) -> bool:
        """Return True only if the media_player is on and in Art Mode.

        Checks the HA state of the media_player entity for this config entry.
        This avoids hitting the WebSocket Art API when the TV is off/standby,
        which would cause async_update to block for the full timeout duration
        and trigger HA's '> 10 seconds' warning.
        """
        if self._media_player_entity_id is None:
            entity_registry = er.async_get(self.hass)
            for entity in entity_registry.entities.values():
                if (
                    entity.config_entry_id == self._entry.entry_id
                    and entity.domain == "media_player"
                ):
                    self._media_player_entity_id = entity.entity_id
                    break

        if not self._media_player_entity_id:
            return False

        state = self.hass.states.get(self._media_player_entity_id)
        if state is None:
            return False

        # Art Mode brightness/color temp are only meaningful (and readable)
        # when the TV is actually in Art Mode. In this integration a Frame TV
        # displaying art reports media_player state "off" with the
        # "art_mode_status" attribute set to "on" (HA convention) — so the
        # attribute is the authoritative signal, checked BEFORE the state.
        if state.attributes.get("art_mode_status") == "on":
            return True

        if state.state in ("off", "unavailable", "unknown"):
            return False

        # TV is on (normal viewing) — art values are not readable, but allow
        # polling when the attribute is absent entirely (e.g. older firmware
        # or attribute not yet populated) so we don't go permanently blind.
        return "art_mode_status" not in state.attributes


# ══════════════════════════════════════════════════════════════════════════
# Brightness (0-100, mapped to TV's 1-10 scale)
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVArtBrightnessNumber(SamsungTVArtNumberBase):
    """Number entity for Art Mode brightness (0-100, mapped to TV 1-10)."""

    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 10
    _attr_native_unit_of_measurement = "%"

    def __init__(self, hass, entry, art_api, device_name, device_unique_id):
        super().__init__(hass, entry, art_api, device_name, device_unique_id)
        self._attr_unique_id = f"{device_unique_id}_art_brightness"
        self._attr_name = "Art Mode Brightness"

    async def async_set_native_value(self, value: float) -> None:
        """Set brightness on the TV.

        Accepts 0-100 from UI, converts to TV's 1-10 scale.
        """
        ui_brightness = int(value)
        if ui_brightness == 0:
            tv_brightness = 0
        else:
            tv_brightness = max(1, min(10, round(ui_brightness / 10)))
        try:
            _LOGGER.debug(
                "Frame Art: Setting brightness UI=%d -> TV=%d",
                ui_brightness,
                tv_brightness,
            )
            await self._art_api.set_brightness(tv_brightness)
            self._attr_native_value = ui_brightness
            self.async_write_ha_state()
        except Exception as ex:
            _LOGGER.error("Error setting art mode brightness: %s", ex)

    async def async_update(self) -> None:
        """Read current brightness from TV (TV scale 1-10) and convert to 0-100."""
        if not self._is_tv_in_art_mode():
            return
        try:
            async with asyncio.timeout(5):
                result = await self._art_api.get_brightness()
            if result is not None:
                # API may return a dict {"value": N} or a raw int depending on path
                tv_value = result.get("value") if isinstance(result, dict) else result
                if tv_value is not None:
                    self._attr_native_value = int(tv_value) * 10
        except Exception as ex:
            _LOGGER.debug("Could not refresh art mode brightness: %s", ex)


# ══════════════════════════════════════════════════════════════════════════
# Color Temperature (-5 to +5, native TV scale)
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVArtColorTemperatureNumber(SamsungTVArtNumberBase):
    """Number entity for Art Mode color temperature (-5 = warm to +5 = cool)."""

    _attr_icon = "mdi:thermometer"
    _attr_native_min_value = -5
    _attr_native_max_value = 5
    _attr_native_step = 1

    def __init__(self, hass, entry, art_api, device_name, device_unique_id):
        super().__init__(hass, entry, art_api, device_name, device_unique_id)
        self._attr_unique_id = f"{device_unique_id}_art_color_temperature"
        self._attr_name = "Art Mode Color Temperature"

    async def async_set_native_value(self, value: float) -> None:
        """Set color temperature on the TV (-5 to +5)."""
        ct_value = int(value)
        try:
            _LOGGER.debug("Frame Art: Setting color temperature to %d", ct_value)
            await self._art_api.set_color_temperature(ct_value)
            self._attr_native_value = ct_value
            self.async_write_ha_state()
        except Exception as ex:
            _LOGGER.error("Error setting art mode color temperature: %s", ex)

    async def async_update(self) -> None:
        """Read current color temperature from TV."""
        if not self._is_tv_in_art_mode():
            return
        try:
            async with asyncio.timeout(5):
                result = await self._art_api.get_color_temperature()
            if result is not None:
                tv_value = result.get("value") if isinstance(result, dict) else result
                if tv_value is not None:
                    self._attr_native_value = int(tv_value)
        except Exception as ex:
            _LOGGER.debug("Could not refresh art mode color temperature: %s", ex)
