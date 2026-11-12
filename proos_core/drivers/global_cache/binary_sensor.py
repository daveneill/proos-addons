"""Global Caché — sensor inputs (a port in SENSOR mode, a Flex FLC-RS input) as binary sensors (register 1323)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import GlobalCacheEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = entry.runtime_data
    async_add_entities([GlobalCacheInput(hub, entry, p) for p in hub.sensor_ports()])


class GlobalCacheInput(GlobalCacheEntity, BinarySensorEntity):
    def __init__(self, hub, entry, port) -> None:
        super().__init__(hub, entry, port, "-in")
        self._attr_name = "Input %s" % port.address

    @property
    def is_on(self) -> bool | None:
        v = (self.coordinator.data or {}).get(self.port.address)
        return None if v is None else v == 1          # 1 = closed (§7)
