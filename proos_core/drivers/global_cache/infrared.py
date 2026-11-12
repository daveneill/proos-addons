"""Global Caché — IR ports as the platform's own infrared emitters and receivers (register 1323).

The platform turns every infrared integration's command — LG, Samsung, Marantz, Gree, Philips, and any added later —
into raw timings (µs, lit positive / dark negative) and a carrier (infrared_protocols.Command). This emitter plays
exactly that on its port: sendir counts = µs × carrier (protocol.timings_to_counts), and waits for the unit's
completeir. Same shape as the platform's own Broadlink and ESPHome emitters.
"""
from __future__ import annotations

from homeassistant.components.infrared import (
    InfraredCommand,
    InfraredEmitterEntity,
    InfraredReceivedSignal,
    InfraredReceiverEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_CARRIER
from .entity import GlobalCacheEntity
from .protocol import GlobalCacheError

PARALLEL_UPDATES = 1        # one code per port at a time (Unified §9: a second code is refused "busyir")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    ents = [GlobalCacheEmitter(hub, entry, p) for p in hub.emitter_ports()]
    ents += [GlobalCacheReceiver(hub, entry, p) for p in hub.receiver_ports()]
    async_add_entities(ents)


class GlobalCacheEmitter(GlobalCacheEntity, InfraredEmitterEntity):
    def __init__(self, hub, entry, port) -> None:
        super().__init__(hub, entry, port)
        mode = hub.ir_modes.get(port.address, "IR")
        self._attr_name = "IR %s%s" % (port.address, " blaster" if "BLASTER" in mode else "")

    async def async_send_command(self, command: InfraredCommand) -> None:
        try:
            await self.coordinator.client.send_ir(self.port.address, int(command.modulation or DEFAULT_CARRIER),
                                                  command.get_raw_timings())
        except GlobalCacheError as e:
            raise HomeAssistantError(str(e)) from e


class GlobalCacheReceiver(GlobalCacheEntity, InfraredReceiverEntity):
    """A port set to RECEIVER: codes from a remote pointed at it reach the platform (an old remote becomes an input)."""

    def __init__(self, hub, entry, port) -> None:
        super().__init__(hub, entry, port, "-rx")
        self._attr_name = "IR %s receiver" % port.address
        hub.register_receiver(self)

    @callback
    def handle_code(self, frequency: int, timings: list[int]) -> None:
        self._handle_received_signal(InfraredReceivedSignal(timings=timings, modulation=frequency))
