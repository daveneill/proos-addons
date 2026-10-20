"""ProOS connection-health binary sensor for Denon AVR."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ProOS connection-health sensor."""
    async_add_entities([DenonProOSConnectionEntity(entry)])


class DenonProOSConnectionEntity(BinarySensorEntity):
    """ProOS connection-health sensor.

    The denonavr integration has no coordinator — connection health lives on the
    media_player entity (``available`` flips as the receiver responds or times out).
    This sensor reflects that media_player's availability but stays ALWAYS available
    itself, so it reads ``off`` when the receiver is unreachable — a stable liveness
    signal the ProOS awareness/self-heal layer consumes. Polled so it can't miss a
    change and is race-free at startup. Diagnostic.
    """

    _attr_name = "Connection"
    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: DenonavrConfigEntry) -> None:
        """Initialize the ProOS connection sensor."""
        self._entry = entry
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_proos_connection"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})
        self._mp_entity_id: str | None = None
        # Setup only completes after a successful connect, so start connected.
        self._attr_is_on = True

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the device is not."""
        return True

    def _resolve_media_player(self) -> str | None:
        """Find the media_player entity for this config entry."""
        registry = er.async_get(self.hass)
        for reg_entry in er.async_entries_for_config_entry(
            registry, self._entry.entry_id
        ):
            if reg_entry.domain == "media_player":
                return reg_entry.entity_id
        return None

    async def async_update(self) -> None:
        """Reflect the media_player's availability as the connection state."""
        if self._mp_entity_id is None:
            self._mp_entity_id = self._resolve_media_player()
        if self._mp_entity_id is None:
            return
        state = self.hass.states.get(self._mp_entity_id)
        if state is not None:
            self._attr_is_on = state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
