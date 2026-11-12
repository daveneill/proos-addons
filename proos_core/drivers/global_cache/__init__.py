"""Global Caché — every iTach, iTach Flex, GC-100 and Global Connect, the platform's way (register 1323, 12 Nov 2026).

Dave, 12 Nov: "It needs to support the way the platform is supporting the other rack one … and support all the
models". So this driver adds NO IR library, no serial library and no brand list of its own. It hands the unit's ports
to the platform:
  - IR ports → the platform's infrared emitters (and receivers): LG, Samsung, Marantz, Gree, Philips … — every
    platform infrared integration, today's and tomorrow's — send through them
  - serial ports → the platform's serial port list (usb.async_register_serial_port_scanner, as ESPHome's serial proxy
    does), as socket://<unit>:<4999…>: Denon, Marantz, LG TV, Samsung ExLink, NAD, Monoprice … pick them as a port
  - relays → switches; sensor inputs → binary sensors; the Global Connect HDMI switch → a select
Written to the maker's Unified TCP API v1.1 (protocol.py, benched against every family).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.typing import ConfigType

from .const import CONF_MAC, CONF_MODEL, DOMAIN, MANUFACTURER, PLATFORMS
from .hub import GlobalCacheHub
from .protocol import BEACON_GROUP, BEACON_PORT, GlobalCacheError, parse_beacon

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if "usb" in hass.config.components:
        try:
            from homeassistant.components import usb
            usb.async_register_serial_port_scanner(hass, _scan_serial_ports)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Global Caché: the platform's serial port list did not take our ports — %s", e)
    await _start_beacon_listener(hass)
    return True


@callback
def _scan_serial_ports(hass: HomeAssistant):
    """Every loaded unit's serial ports, as the platform's port picker shows them (socket://<host>:<port>)."""
    from homeassistant.components.usb import SerialDevice
    out = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        hub: GlobalCacheHub = entry.runtime_data
        for n, p in enumerate(hub.serial_ports(), 1):
            s = hub.serial_settings.get(p.address) or {}
            speed = " · %s baud" % s["baud"] if s.get("baud") else ""
            out.append(SerialDevice(
                device="socket://%s:%d" % (hub.host, p.tcp_port),
                serial_number="%s-serial-%d" % ((hub.mac or hub.host).replace(":", ""), n),
                manufacturer=MANUFACTURER,
                description="%s serial %d (%s)%s" % (entry.title, n, p.address, speed),
            ))
    return out


async def _start_beacon_listener(hass: HomeAssistant) -> None:
    """Listen for the units' own beacons and offer any not yet set up (the platform's discovery flow decides)."""
    loop = asyncio.get_running_loop()
    offered: set[str] = set()

    class _Beacon(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            b = parse_beacon(data)
            if not b:
                return
            b["host"] = b.get("host") or addr[0]
            key = b.get("mac") or b["host"]
            if key in offered:
                return
            offered.add(key)
            discovery_flow.async_create_flow(hass, DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=b)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", BEACON_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton(BEACON_GROUP), socket.inet_aton("0.0.0.0")))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(_Beacon, sock=sock)
    except OSError as e:
        _LOGGER.info("Global Caché: not listening for beacons (%s); DHCP and typed addresses still work", e)
        return

    @callback
    def _stop(_event=None):
        transport.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = GlobalCacheHub(hass, entry, entry.data[CONF_HOST], entry.data.get(CONF_MAC, ""), entry.data.get(CONF_MODEL, ""))
    try:
        await hub.async_read_unit()
        await hub.async_config_entry_first_refresh()
    except GlobalCacheError as e:
        await hub.async_close()
        raise ConfigEntryNotReady(str(e)) from e
    entry.runtime_data = hub
    conns = {(dr.CONNECTION_NETWORK_MAC, hub.mac)} if hub.mac else set()
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.unique_id or entry.entry_id)}, connections=conns,
        manufacturer=MANUFACTURER, model=hub.model_words, name=entry.title, sw_version=hub.version or None,
        configuration_url="http://%s" % hub.host)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _ports_changed(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_close()
        _ports_changed(hass)
    return ok


@callback
def _ports_changed(hass: HomeAssistant) -> None:
    try:
        from homeassistant.components import usb
        usb.async_notify_serial_ports_changed(hass)
    except Exception:  # noqa: BLE001
        pass
