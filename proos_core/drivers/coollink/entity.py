"""Base entity for the CoolLink integration."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CoolLinkDataUpdateCoordinator


class CoolLinkEntity(CoordinatorEntity[CoolLinkDataUpdateCoordinator]):
    """Common base for CoolLink entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoolLinkDataUpdateCoordinator,
        unit_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._unit_id = unit_id
        self._unit = coordinator.data[unit_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit_id)},
            manufacturer="CoolAutomation",
            model="CoolLink / CoolMasterNet",
            name=unit_id,
            sw_version=coordinator.info.get("version"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        # The unit may drop off a poll transiently; keep the last snapshot.
        if self._unit_id in self.coordinator.data:
            self._unit = self.coordinator.data[self._unit_id]
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return super().available and self._unit_id in self.coordinator.data
