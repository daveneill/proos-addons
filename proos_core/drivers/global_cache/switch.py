"""Global Caché — relays as switches (register 1323): screens, lifts, gates, a projector's trigger."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import GlobalCacheEntity
from .protocol import GlobalCacheError


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = entry.runtime_data
    async_add_entities([GlobalCacheRelay(hub, entry, p) for p in hub.relay_ports()])


class GlobalCacheRelay(GlobalCacheEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        v = (self.coordinator.data or {}).get(self.port.address)
        return None if v is None else v != 0          # SPST 1 closed; SPDT/DPDT 1 or 2 = a throw made (§6)

    async def _set(self, state: int) -> None:
        try:
            got = await self.coordinator.client.set_state(self.port.address, state)
        except GlobalCacheError as e:
            raise HomeAssistantError(str(e)) from e
        data = dict(self.coordinator.data or {})
        data[self.port.address] = got                  # the unit's own answer, not what was asked
        self.coordinator.async_set_updated_data(data)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(1)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(0)
