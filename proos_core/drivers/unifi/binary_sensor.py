"""ProOS connection-health binary sensor for UniFi Network."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UnifiConfigEntry
from .const import DOMAIN
from .hub import UnifiHub


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: UnifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ProOS connection-health sensor for the UniFi controller."""
    hub = config_entry.runtime_data
    async_add_entities([UnifiProOSConnectionEntity(hub)])


class UnifiProOSConnectionEntity(BinarySensorEntity):
    """ProOS connection-health sensor for the UniFi Network controller.

    Reports whether ProOS currently holds a live connection to the controller
    (``hub.available`` — the same websocket state the integration itself uses), but
    stays ALWAYS available so it reads ``off`` when the controller drops rather than
    going unavailable. A stable liveness signal for the ProOS awareness/self-heal
    layer, and the anchor for UniFi-driven recovery (PoE-port cycling / device
    restart). Updated on the hub's connection-change dispatcher signal. Diagnostic.
    """

    _attr_name = "Connection"
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hub: UnifiHub) -> None:
        """Initialize the ProOS connection sensor on the controller device."""
        self._hub = hub
        uid = hub.config.entry.unique_id
        self._attr_unique_id = f"{uid}_proos_connection"
        # Attach to the UniFi Network controller device (same identifiers the hub uses).
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uid)})
        self._attr_is_on = hub.available

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the controller is not."""
        return True

    async def async_added_to_hass(self) -> None:
        """Subscribe to the hub's connection-status change signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_reachable, self._handle_reachable
            )
        )

    @callback
    def _handle_reachable(self) -> None:
        """Reflect the controller's live connection state."""
        self._attr_is_on = self._hub.available
        self.async_write_ha_state()
