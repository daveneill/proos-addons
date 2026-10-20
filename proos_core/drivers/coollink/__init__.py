"""The CoolLink (CoolAutomation) integration.

A drop-in Home Assistant integration for CoolAutomation CoolMasterNet /
CoolLinkBridge / CoolLinkHub controllers, using a tolerant status parser so it
works with the CoolLink family that the official ``coolmaster`` integration
rejects. Self-contained: no external Python packages required.
"""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .coolapi import CoolLinkNet
from .const import CONF_SEND_WAKEUP_PROMPT, DEFAULT_PORT, DOMAIN
from .coordinator import CoolLinkConfigEntry, CoolLinkDataUpdateCoordinator

PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: CoolLinkConfigEntry) -> bool:
    """Set up CoolLink from a config entry."""
    client = CoolLinkNet(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        send_wakeup=entry.data.get(CONF_SEND_WAKEUP_PROMPT, False),
    )

    try:
        info = await client.info()
        status = await client.status()
    except OSError as error:
        raise ConfigEntryNotReady(f"Cannot reach CoolLink hub: {error}") from error

    if not status:
        raise ConfigEntryNotReady("CoolLink hub returned no units yet")

    coordinator = CoolLinkDataUpdateCoordinator(hass, entry, client, info)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CoolLinkConfigEntry) -> bool:
    """Unload a CoolLink config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: CoolLinkConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a unit's device from the UI."""
    return not device_entry.identifiers.intersection(
        (DOMAIN, unit_id) for unit_id in config_entry.runtime_data.data
    )
