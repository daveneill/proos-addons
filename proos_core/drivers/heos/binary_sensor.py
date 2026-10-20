"""ProOS connection-health binary sensors for HEOS."""

from __future__ import annotations

from collections.abc import Sequence

from pyheos import HeosPlayer

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HeosConfigEntry, HeosCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ProOS connection-health sensors, one per HEOS player."""
    coordinator = entry.runtime_data

    def add_entities_callback(players: Sequence[HeosPlayer]) -> None:
        async_add_entities(
            HeosProOSConnectionEntity(coordinator, player) for player in players
        )

    coordinator.async_add_platform_callback(add_entities_callback)
    add_entities_callback(list(coordinator.heos.players.values()))


class HeosProOSConnectionEntity(CoordinatorEntity[HeosCoordinator], BinarySensorEntity):
    """ProOS connection-health sensor for a HEOS player.

    Reports whether ProOS currently holds a live connection to this player
    (``player.available`` — the same signal the media_player uses), but stays ALWAYS
    available itself, so it reads ``off`` when the player drops rather than going
    unavailable. A stable liveness signal for the ProOS awareness/self-heal layer.
    Updated via the coordinator, which fires on the HEOS controller's connect/disconnect
    events. Diagnostic.
    """

    _attr_name = "Connection"
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HeosCoordinator, player: HeosPlayer) -> None:
        """Initialize the ProOS connection sensor."""
        super().__init__(coordinator, context=player.player_id)
        self._player = player
        self._attr_unique_id = f"{player.player_id}_proos_connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(player.player_id))}
        )
        self._attr_is_on = player.available

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the device is not."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reflect the player's connection state."""
        self._attr_is_on = self._player.available
        super()._handle_coordinator_update()
