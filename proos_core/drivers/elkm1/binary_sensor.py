"""Support for control of ElkM1 binary sensors."""

from typing import Any

from elkm1_lib.const import ZoneLogicalStatus, ZoneType
from elkm1_lib.elements import Element
from elkm1_lib.zones import Zone

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElkM1ConfigEntry
from .const import DOMAIN
from .entity import ElkAttachedEntity, ElkEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ElkM1ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the Elk-M1 sensor platform."""
    elk_data = config_entry.runtime_data
    elk = elk_data.elk
    auto_configure = elk_data.auto_configure

    entities: list[ElkEntity] = []
    for element in elk.zones:
        # Don't create binary sensors for zones that are analog
        if element.definition in {ZoneType.TEMPERATURE, ZoneType.ANALOG_ZONE}:  # type: ignore[attr-defined]
            continue

        if auto_configure:
            if not element.configured:
                continue
        elif not elk_data.config["zone"]["included"][element.index]:
            continue

        entities.append(ElkBinarySensor(element, elk, elk_data))

    async_add_entities(entities)

    # ProOS: panel-level connection-health sensor for the awareness layer.
    async_add_entities([ElkProOSConnectionEntity(elk_data)])


class ElkBinarySensor(ElkAttachedEntity, BinarySensorEntity):
    """Representation of ElkM1 binary sensor."""

    _element: Zone
    _attr_entity_registry_enabled_default = False

    def _element_changed(self, element: Element, changeset: dict[str, Any]) -> None:
        # Zone in NORMAL state is OFF; any other state is ON
        self._attr_is_on = bool(
            self._element.logical_status is not ZoneLogicalStatus.NORMAL
        )


class ElkProOSConnectionEntity(BinarySensorEntity):
    """ProOS connection-health sensor for the ElkM1 panel.

    Reports whether ProOS currently holds a live connection to the panel
    (``elk.is_connected()``). Security is NOT power-aware, so a lost panel is a real,
    high-priority signal for the ProOS awareness/self-heal layer. Always available
    (reads ``off`` when the panel is unreachable) and polled so it never misses a
    change. Diagnostic; lives on the panel's system device.
    """

    _attr_name = "Connection"
    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, elk_data: "object") -> None:
        """Initialize the ProOS connection sensor."""
        self._elk = elk_data.elk
        prefix = elk_data.prefix
        self._attr_unique_id = (
            f"{prefix}_proos_connection" if prefix else "elkm1_proos_connection"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{prefix}_system")}
        )
        self._attr_is_on = self._elk.is_connected()

    @property
    def available(self) -> bool:
        """Always available — the point is to report health when the panel is not."""
        return True

    async def async_update(self) -> None:
        """Reflect the panel connection state."""
        self._attr_is_on = self._elk.is_connected()
