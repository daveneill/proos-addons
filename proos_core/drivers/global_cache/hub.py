"""Global Caché — one unit, its ports and their state (register 1323).

ONE command connection per unit (a GC-100 takes no more — Unified §1). What each IR-capable port IS comes from the unit
(get_IR: IR, IR_BLASTER, SENSOR, RECEIVER …), never from a guess: an IR-mode port becomes the platform's infrared
emitter, a RECEIVER port its infrared receiver, a SENSOR port a binary sensor. Relay and sensor states are read from
the unit; a Global Connect pushes sensor changes on the open connection ("getstate … notify"), a GC-100 pushes
"statechange" when set to auto-notify, the rest are read every POLL_SECONDS.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, IR_SENSOR_MODES, IR_SEND_MODES, POLL_SECONDS
from .protocol import GlobalCacheClient, GlobalCacheError, IOPort, family_of, io_ports, parse_sendir

_LOGGER = logging.getLogger(__name__)

FAMILY_WORDS = {"itach": "iTach", "flex": "iTach Flex", "gc100": "GC-100", "globalconnect": "Global Connect"}
KIND_WORDS = {"ir": "IR", "serial": "Serial", "relay": "Relay", "sensor": "Sensor", "hdmi": "HDMI"}


class GlobalCacheHub(DataUpdateCoordinator[dict]):
    """A Global Caché unit (a Global Connect MODULE is its own unit, with its own address)."""

    def __init__(self, hass: HomeAssistant, entry, host: str, mac: str = "", model: str = "") -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="%s %s" % (DOMAIN, host),
                         update_interval=timedelta(seconds=POLL_SECONDS))
        self.host, self.mac, self.beacon_model = host, mac, model
        self.client = GlobalCacheClient(host, on_line=self._on_line)
        self.modules: list = []
        self.family = ""
        self.version = ""
        self.ports: list[IOPort] = []
        self.ir_modes: dict[str, str] = {}
        self.serial_settings: dict[str, dict] = {}
        self._receivers: list = []          # infrared receiver entities, in port order
        self._pushed: dict[str, int] = {}

    # — what the unit is —
    async def async_read_unit(self) -> None:
        self.modules = await self.client.get_devices()
        self.family = family_of(self.modules, self.beacon_model)
        try:
            self.version = await self.client.get_version()
        except GlobalCacheError:
            self.version = ""
        self.ports = io_ports(self.modules)
        for p in self.ports:
            if p.kind == "ir" and p.address not in self.ir_modes:
                try:
                    self.ir_modes[p.address] = await self.client.get_ir_mode(p.address)
                except GlobalCacheError as e:
                    # a Flex reports its cable mode the same way (§4); a port that refuses get_IR is left out, said in the log
                    _LOGGER.warning("Global Caché %s: port %s did not report its mode — %s", self.host, p.address, e)
            elif p.kind == "serial":
                try:
                    self.serial_settings[p.address] = await self.client.get_serial(p.address)
                except GlobalCacheError as e:
                    _LOGGER.warning("Global Caché %s: serial %s did not report its settings — %s", self.host, p.address, e)
        if self.family == "globalconnect":
            for p in self.sensor_ports():
                try:   # the Global Connect pushes this port's changes on THIS connection from now on
                    await self.client.request("getstate,%s,notify" % p.address,
                                              lambda l, a=p.address: l.startswith("state," + a + ","))
                except GlobalCacheError:
                    pass
        for p in self.receiver_ports():
            try:
                await self.client.receive_ir(p.address, True)
            except GlobalCacheError as e:
                _LOGGER.warning("Global Caché %s: receiver %s did not start — %s", self.host, p.address, e)

    @property
    def model_words(self) -> str:
        kinds = []
        for p in self.ports:
            w = KIND_WORDS[p.kind]
            if w not in kinds:
                kinds.append(w)
        return "%s (%s)" % (FAMILY_WORDS.get(self.family, "Global Caché"), " · ".join(kinds) or "no ports")

    def emitter_ports(self) -> list[IOPort]:
        return [p for p in self.ports if p.kind == "ir" and self.ir_modes.get(p.address) in IR_SEND_MODES]

    def receiver_ports(self) -> list[IOPort]:
        return [p for p in self.ports if p.kind == "ir" and self.ir_modes.get(p.address) == "RECEIVER"]

    def sensor_ports(self) -> list[IOPort]:
        ir_addrs = {p.address for p in self.ports if p.kind == "ir"}
        out = [p for p in self.ports if p.kind == "ir" and self.ir_modes.get(p.address) in IR_SENSOR_MODES]
        # a sensor-only module (a Flex's FLC-RS, module 2) — not the Global Connect's twin of its IR ports
        out += [p for p in self.ports if p.kind == "sensor" and p.address not in ir_addrs]
        return out

    def relay_ports(self) -> list[IOPort]:
        return [p for p in self.ports if p.kind == "relay"]

    def serial_ports(self) -> list[IOPort]:
        return [p for p in self.ports if p.kind == "serial"]

    def hdmi_ports(self) -> list[IOPort]:
        return [p for p in self.ports if p.kind == "hdmi"]

    # — state —
    async def _async_update_data(self) -> dict:
        try:
            states = {}
            for p in self.relay_ports():
                states[p.address] = await self.client.get_state(p.address)
            for p in self.sensor_ports():
                if p.address in self._pushed and self.family == "globalconnect":
                    states[p.address] = self._pushed[p.address]
                else:
                    states[p.address] = await self.client.get_state(p.address)
            if not states:
                await self.client.get_version()       # an IR-only or serial-only unit: is it answering?
            return states
        except GlobalCacheError as e:
            raise UpdateFailed(str(e)) from e

    @callback
    def _on_line(self, line: str) -> None:
        """Lines the unit sends unasked: a sensor change, a received IR code."""
        if line.startswith("statechange,") or line.startswith("state,"):
            try:
                addr, val = line.split(",")[1], int(line.rsplit(",", 1)[1])
            except (IndexError, ValueError):
                return
            self._pushed[addr] = val
            data = dict(self.data or {})
            data[addr] = val
            self.async_set_updated_data(data)
        elif line.startswith("sendir,") and self._receivers:
            got = parse_sendir(line)
            if got:
                # the stream always says port 1 (Unified §4, a known issue): the first receiver hears it
                self._receivers[0].handle_code(*got)

    def register_receiver(self, entity) -> None:
        self._receivers.append(entity)

    async def async_close(self) -> None:
        await self.client.close()
