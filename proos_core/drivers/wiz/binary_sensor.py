"""WiZ integration binary sensor platform."""

from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OCCUPANCY_SOURCES, SIGNAL_WIZ_PIR
from .coordinator import WizConfigEntry, WizCoordinator, WizData
from .entity import WizEntity

OCCUPANCY_UNIQUE_ID = "{}_occupancy"

# ProOS: unique-id suffix for the connection-health sensor (see WizProOSConnectionEntity).
PROOS_CONNECTION_UNIQUE_ID = "{}_proos_connection"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WiZ binary sensor platform."""
    mac = entry.runtime_data.bulb.mac

    # ProOS: always add the connection-health sensor. It is added unconditionally
    # (not gated on any device capability) and is ALWAYS available, so it gives the
    # ProOS awareness / self-heal layer a stable liveness signal instead of inferring
    # health from the light/occupancy entities, which go unavailable when the bulb
    # drops off the network.
    async_add_entities([WizProOSConnectionEntity(entry.runtime_data, entry.title)])

    if er.async_get(hass).async_get_entity_id(
        Platform.BINARY_SENSOR, DOMAIN, OCCUPANCY_UNIQUE_ID.format(mac)
    ):
        async_add_entities([WizOccupancyEntity(entry.runtime_data, entry.title)])
        return

    cancel_dispatcher: Callable[[], None] | None = None

    @callback
    def _async_add_occupancy_sensor() -> None:
        nonlocal cancel_dispatcher
        assert cancel_dispatcher is not None
        cancel_dispatcher()
        cancel_dispatcher = None
        async_add_entities([WizOccupancyEntity(entry.runtime_data, entry.title)])

    cancel_dispatcher = async_dispatcher_connect(
        hass, SIGNAL_WIZ_PIR.format(mac), _async_add_occupancy_sensor
    )

    @callback
    def _async_cancel_dispatcher() -> None:
        nonlocal cancel_dispatcher
        if cancel_dispatcher is not None:
            cancel_dispatcher()
            cancel_dispatcher = None

    entry.async_on_unload(_async_cancel_dispatcher)


class WizOccupancyEntity(WizEntity, BinarySensorEntity):
    """Representation of WiZ Occupancy sensor."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, wiz_data: WizData, name: str) -> None:
        """Initialize an WiZ device."""
        super().__init__(wiz_data, name)
        self._attr_unique_id = OCCUPANCY_UNIQUE_ID.format(self._device.mac)
        self._async_update_attrs()

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        if self._device.state.get_source() in OCCUPANCY_SOURCES:
            self._attr_is_on = self._device.status


class WizProOSConnectionEntity(CoordinatorEntity[WizCoordinator], BinarySensorEntity):
    """ProOS connection-health sensor.

    Reports whether the WiZ integration currently has a live connection to the bulb
    (``on`` = the coordinator's last update succeeded, ``off`` = it did not). Unlike
    the light/occupancy entities, this sensor is ALWAYS available, so it reads ``off``
    even when the device is offline at startup — a stable, debounce-friendly signal
    the ProOS awareness/self-heal layer consumes instead of watching a flapping light
    entity or scraping logs. Diagnostic; no pywizlight changes; keyed off the existing
    coordinator so it costs nothing extra.
    """

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, wiz_data: WizData, name: str) -> None:
        """Initialize the ProOS connection sensor."""
        super().__init__(wiz_data.coordinator)
        device = wiz_data.bulb
        self._attr_unique_id = PROOS_CONNECTION_UNIQUE_ID.format(device.mac)
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, device.mac)}
        )
        self._attr_is_on = self.coordinator.last_update_success

    @property
    def available(self) -> bool:
        """Always available — the whole point is to report health when the device is not."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reflect the coordinator's connection state."""
        self._attr_is_on = self.coordinator.last_update_success
        super()._handle_coordinator_update()
