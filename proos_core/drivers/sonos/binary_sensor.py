"""Entity representing a Sonos power sensor."""

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    SONOS_CREATE_BATTERY,
    SONOS_CREATE_MEDIA_PLAYER,
    SONOS_CREATE_MIC_SENSOR,
)
from .entity import SonosEntity
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

ATTR_BATTERY_POWER_SOURCE = "power_source"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SonosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sonos from a config entry."""

    @callback
    def _async_create_battery_entity(speaker: SonosSpeaker) -> None:
        _LOGGER.debug("Creating battery binary_sensor on %s", speaker.zone_name)
        entity = SonosPowerEntity(speaker, config_entry)
        async_add_entities([entity])

    @callback
    def _async_create_mic_entity(speaker: SonosSpeaker) -> None:
        _LOGGER.debug("Creating microphone binary_sensor on %s", speaker.zone_name)
        async_add_entities([SonosMicrophoneSensorEntity(speaker, config_entry)])

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, SONOS_CREATE_BATTERY, _async_create_battery_entity
        )
    )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, SONOS_CREATE_MIC_SENSOR, _async_create_mic_entity
        )
    )

    @callback
    def _async_create_proos_connection_entity(speaker: SonosSpeaker) -> None:
        _LOGGER.debug("Creating ProOS connection sensor on %s", speaker.zone_name)
        async_add_entities([SonosProOSConnectionSensor(speaker, config_entry)])

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, SONOS_CREATE_MEDIA_PLAYER, _async_create_proos_connection_entity
        )
    )


class SonosPowerEntity(SonosEntity, BinarySensorEntity):
    """Representation of a Sonos power entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the power entity binary sensor."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-power"

    async def _async_fallback_poll(self) -> None:
        """Poll the device for the current state."""
        await self.speaker.async_poll_battery()

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        return self.speaker.charging

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        return {
            ATTR_BATTERY_POWER_SOURCE: self.speaker.power_source,
        }

    @property
    def available(self) -> bool:
        """Return whether this device is available."""
        return self.speaker.available and self.speaker.charging is not None


class SonosMicrophoneSensorEntity(SonosEntity, BinarySensorEntity):
    """Representation of a Sonos microphone sensor entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "microphone"

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the microphone binary sensor entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-microphone"

    async def _async_fallback_poll(self) -> None:
        """Handle polling when subscription fails."""
        await self.hass.async_add_executor_job(self.poll_state)

    @soco_error()
    def poll_state(self) -> None:
        """Poll the current state of the microphone."""
        self.speaker.mic_enabled = self.soco.mic_enabled

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        return self.speaker.mic_enabled


class SonosProOSConnectionSensor(SonosEntity, BinarySensorEntity):
    """ProOS connection-health sensor for a Sonos speaker.

    Reports whether ProOS currently holds a live connection to the speaker
    (``speaker.available`` — the same signal every Sonos entity uses), but stays ALWAYS
    available itself, so it reads ``off`` when the speaker drops rather than going
    unavailable. Availability changes are pushed via the SONOS_STATE_UPDATED dispatcher
    that SonosEntity already subscribes to. A stable liveness signal for the ProOS
    awareness/self-heal layer. Diagnostic.
    """

    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the ProOS connection sensor."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}_proos_connection"

    async def _async_fallback_poll(self) -> None:
        """No-op: availability is pushed via the speaker activity dispatcher."""

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the speaker is not."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the speaker connection is live."""
        return self.speaker.available
