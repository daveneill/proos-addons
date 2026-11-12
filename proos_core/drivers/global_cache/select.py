"""Global Caché — the settings a port needs, written to the unit and shown as the unit answers (register 1323).

  - IR port use (set_IR): emitter, blaster (port 3 only, Unified §4), sensor input, receiver — per family. Changing it
    reloads the unit's entities (a port becomes an emitter, a sensor or a receiver); a GC-100 resets itself after a
    successful set_IR (§4), so it is given time to come back.
  - Serial speed / parity / flow (set_SERIAL): a network serial port cannot set these from the far end, so the RS-232
    integration that uses the port (Denon, Marantz, LG …) relies on these being right on the unit.
  - Global Connect HDMI switch: which input is routed (setstate,<m>:<in>,<out>, §8).
"""
from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .entity import GlobalCacheEntity
from .protocol import SERIAL_BAUDS, SERIAL_FLOW, SERIAL_PARITY, GlobalCacheError

MODE_WORDS = {"IR": "IR emitter", "IR_BLASTER": "IR blaster", "SENSOR": "Sensor input",
              "SENSOR_NOTIFY": "Sensor input (notify)", "RECEIVER": "IR receiver", "IR_NOCARRIER": "IR, no carrier",
              "IRTRIPORT": "Tri-port emitters", "IRTRIPORT_BLASTER": "Tri-port with blaster", "SERIAL": "Serial",
              "BL2_BLASTER": "BL2 blaster", "LED_LIGHTING": "LED lighting"}
# what each family offers on an IR port (Unified §4; the unit has the last word — a refusal is shown as its reason)
FAMILY_MODES = {
    "itach": ("IR", "IR_BLASTER", "SENSOR", "SENSOR_NOTIFY"),
    "globalconnect": ("IR", "IR_BLASTER", "SENSOR", "SENSOR_NOTIFY", "RECEIVER"),
    "gc100": ("IR", "IR_NOCARRIER", "SENSOR", "SENSOR_NOTIFY"),
    "flex": ("IR", "IR_BLASTER", "IRTRIPORT", "IRTRIPORT_BLASTER", "SENSOR", "SENSOR_NOTIFY", "RECEIVER", "SERIAL"),
}
FLOW_WORDS = {"FLOW_NONE": "None", "FLOW_HARDWARE": "Hardware (RTS/CTS)"}
PARITY_WORDS = {"PARITY_NO": "None", "PARITY_ODD": "Odd", "PARITY_EVEN": "Even"}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = entry.runtime_data
    ents = []
    for p in hub.ports:
        if p.kind == "ir" and p.address in hub.ir_modes:
            ents.append(IRPortUse(hub, entry, p))
    for p in hub.serial_ports():
        if p.address in hub.serial_settings:
            ents += [SerialSetting(hub, entry, p, "baud"), SerialSetting(hub, entry, p, "parity"),
                     SerialSetting(hub, entry, p, "flow")]
    for mod in sorted({p.module for p in hub.hdmi_ports()}):
        ents.append(HdmiInput(hub, entry, [p for p in hub.hdmi_ports() if p.module == mod]))
    async_add_entities(ents)


class IRPortUse(GlobalCacheEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry, port) -> None:
        super().__init__(hub, entry, port, "-use")
        self._entry = entry
        self._attr_name = "Port %s use" % port.address
        modes = [m for m in FAMILY_MODES.get(hub.family, FAMILY_MODES["itach"])
                 if m not in ("IR_BLASTER", "IRTRIPORT_BLASTER") or port.port == 3 or hub.family == "flex"]
        cur = hub.ir_modes.get(port.address)
        if cur and cur not in modes:
            modes.append(cur)                      # whatever the unit says it is, it can show
        self._modes = modes
        self._attr_options = [MODE_WORDS.get(m, m) for m in modes]

    @property
    def current_option(self) -> str | None:
        m = self.coordinator.ir_modes.get(self.port.address)
        return MODE_WORDS.get(m, m) if m else None

    async def async_select_option(self, option: str) -> None:
        mode = self._modes[self._attr_options.index(option)]
        try:
            got = await self.coordinator.client.set_ir_mode(self.port.address, mode)
        except GlobalCacheError as e:
            raise HomeAssistantError(str(e)) from e
        self.coordinator.ir_modes[self.port.address] = got
        hass = self.hass
        if self.coordinator.family == "gc100":
            await asyncio.sleep(10)                # plumbing: the GC-100 resets after set_IR (§4)
        hass.config_entries.async_schedule_reload(self._entry.entry_id)


class SerialSetting(GlobalCacheEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry, port, what: str) -> None:
        super().__init__(hub, entry, port, "-" + what)
        self.what = what
        self._attr_name = "Serial %s %s" % (port.address, {"baud": "speed", "parity": "parity", "flow": "flow control"}[what])
        if what == "baud":
            self._vals = [str(b) for b in SERIAL_BAUDS]
            self._attr_options = list(self._vals)
        elif what == "parity":
            self._vals = list(SERIAL_PARITY)
            self._attr_options = [PARITY_WORDS[v] for v in self._vals]
        else:
            self._vals = list(SERIAL_FLOW)
            self._attr_options = [FLOW_WORDS[v] for v in self._vals]

    @property
    def current_option(self) -> str | None:
        s = self.coordinator.serial_settings.get(self.port.address) or {}
        v = s.get(self.what)
        if v is None or str(v) not in self._vals:
            return None
        return self._attr_options[self._vals.index(str(v))]

    async def async_select_option(self, option: str) -> None:
        s = dict(self.coordinator.serial_settings.get(self.port.address) or {})
        v = self._vals[self._attr_options.index(option)]
        s[self.what] = int(v) if self.what == "baud" else v
        try:
            got = await self.coordinator.client.set_serial(self.port.address, int(s.get("baud") or 9600),
                                                           s.get("flow") or "FLOW_NONE", s.get("parity") or "PARITY_NO",
                                                           s.get("stopbits"))
        except GlobalCacheError as e:
            raise HomeAssistantError(str(e)) from e
        self.coordinator.serial_settings[self.port.address] = got      # the unit's own answer
        self.async_write_ha_state()


class HdmiInput(GlobalCacheEntity, SelectEntity):
    """The Global Connect HDMI switch (HM, 3:1): which input goes to the output. NOT YET SEEN on a unit."""

    def __init__(self, hub, entry, ports) -> None:
        super().__init__(hub, entry, ports[0], "-hdmi")
        self._attr_name = "HDMI %d input" % ports[0].module
        n_in = max(1, len(ports) - 1)              # "SWITCH_HDMI_3:1": the ports are 3 inputs and 1 output
        self._attr_options = ["Input %d" % i for i in range(1, n_in + 1)]
        self._current: str | None = None

    @property
    def current_option(self) -> str | None:
        return self._current

    async def async_select_option(self, option: str) -> None:
        i = self._attr_options.index(option) + 1
        try:
            await self.coordinator.client.request("setstate,%d:%d,1" % (self.port.module, i),
                                                  lambda l: l.startswith("state,%d:%d," % (self.port.module, i)))
        except GlobalCacheError as e:
            raise HomeAssistantError(str(e)) from e
        self._current = option
        self.async_write_ha_state()
