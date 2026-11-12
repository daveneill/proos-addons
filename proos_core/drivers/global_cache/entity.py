"""Global Caché — the shared base: every entity belongs to its unit's device (register 1323)."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .hub import GlobalCacheHub, KIND_WORDS
from .protocol import IOPort


class GlobalCacheEntity(CoordinatorEntity[GlobalCacheHub]):
    _attr_has_entity_name = True

    def __init__(self, hub: GlobalCacheHub, entry, port: IOPort, suffix: str = "") -> None:
        super().__init__(hub)
        self.port = port
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = "%s-%s-%s%s" % (base, port.kind, port.address, suffix)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})
        self._attr_name = "%s %s" % (KIND_WORDS[port.kind], port.address)
