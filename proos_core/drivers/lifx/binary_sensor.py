"""Binary sensor entities for LIFX integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import HEV_CYCLE_STATE
from .coordinator import LIFXConfigEntry, LIFXUpdateCoordinator
from .entity import LIFXEntity
from .util import lifx_features

HEV_CYCLE_STATE_SENSOR = BinarySensorEntityDescription(
    key=HEV_CYCLE_STATE,
    translation_key="clean_cycle",
    entity_category=EntityCategory.DIAGNOSTIC,
    device_class=BinarySensorDeviceClass.RUNNING,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LIFXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up LIFX from a config entry."""
    coordinator = entry.runtime_data

    # ProOS: always add the connection-health sensor (stable liveness for awareness).
    async_add_entities([LIFXProOSConnectionEntity(coordinator)])

    if lifx_features(coordinator.device)["hev"]:
        async_add_entities(
            [LIFXHevCycleBinarySensorEntity(coordinator, HEV_CYCLE_STATE_SENSOR)]
        )


class LIFXHevCycleBinarySensorEntity(LIFXEntity, BinarySensorEntity):
    """LIFX HEV cycle state binary sensor."""

    def __init__(
        self,
        coordinator: LIFXUpdateCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.serial_number}_{description.key}"
        self._async_update_attrs()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._async_update_attrs()
        super()._handle_coordinator_update()

    @callback
    def _async_update_attrs(self) -> None:
        """Handle coordinator updates."""
        self._attr_is_on = self.coordinator.async_get_hev_cycle_state()


class LIFXProOSConnectionEntity(LIFXEntity, BinarySensorEntity):
    """ProOS connection-health sensor.

    Reports whether the LIFX integration currently has a live connection to the bulb
    (``on`` = the coordinator's last update succeeded, ``off`` = it did not). Unlike the
    light entities, it is ALWAYS available, so it reads ``off`` even when the bulb is
    offline at startup — a stable, debounce-friendly signal the ProOS awareness/self-heal
    layer consumes instead of watching a flapping light entity. Diagnostic; keyed off the
    existing per-device coordinator, so it costs nothing extra.
    """

    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LIFXUpdateCoordinator) -> None:
        """Initialise the ProOS connection sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_proos_connection"
        self._attr_is_on = coordinator.last_update_success

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the device is not."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reflect the coordinator's connection state."""
        self._attr_is_on = self.coordinator.last_update_success
        super()._handle_coordinator_update()
