"""DataUpdateCoordinator for the CoolLink integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .coolapi import CoolLinkNet, CoolLinkUnit
from .const import BACKOFF_BASE_DELAY, DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_RETRIES

_LOGGER = logging.getLogger(__name__)


class CoolLinkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, CoolLinkUnit]]):
    """Manage polling every indoor unit behind one hub."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CoolLinkConfigEntry,
        client: CoolLinkNet,
        info: dict[str, str],
    ) -> None:
        self._client = client
        self.info = info
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, CoolLinkUnit]:
        retries_left = MAX_RETRIES
        while retries_left > 0:
            retries_left -= 1
            try:
                status = await self._client.status()
            except OSError as error:
                if retries_left == 0:
                    raise UpdateFailed(
                        f"Error communicating with CoolLink hub: {error}"
                    ) from error
                _LOGGER.debug(
                    "CoolLink communication error (%d retries left): %s",
                    retries_left,
                    error,
                )
            else:
                if status:
                    return status
                _LOGGER.debug(
                    "CoolLink returned an empty unit list (%d retries left)",
                    retries_left,
                )
                if retries_left == 0:
                    raise UpdateFailed("CoolLink hub returned no units")
            await asyncio.sleep(BACKOFF_BASE_DELAY ** (MAX_RETRIES - retries_left))

        raise UpdateFailed("CoolLink hub returned no units")


# Config-entry type carrying this coordinator as runtime_data. Written as a plain
# alias (not a 3.12 ``type`` statement) so it stays valid on any Python HA supports.
CoolLinkConfigEntry = ConfigEntry[CoolLinkDataUpdateCoordinator]
