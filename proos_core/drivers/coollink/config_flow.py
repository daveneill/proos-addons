"""Config flow for the CoolLink (CoolAutomation) integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .coolapi import CoolLinkNet
from .const import (
    CONF_HALF_STEPS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_SEND_WAKEUP_PROMPT,
    CONF_SUPPORTED_MODES,
    DEFAULT_HOST,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_PORT,
    DOMAIN,
)

# Toggleable HVAC modes (OFF is always available and not listed here).
AVAILABLE_MODES = [
    HVACMode.COOL.value,
    HVACMode.HEAT.value,
    HVACMode.HEAT_COOL.value,
    HVACMode.DRY.value,
    HVACMode.FAN_ONLY.value,
]

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        **{vol.Required(mode, default=True): bool for mode in AVAILABLE_MODES},
        vol.Required(CONF_MIN_TEMP, default=DEFAULT_MIN_TEMP): int,
        vol.Required(CONF_MAX_TEMP, default=DEFAULT_MAX_TEMP): int,
        vol.Required(CONF_HALF_STEPS, default=False): bool,
        vol.Required(CONF_SEND_WAKEUP_PROMPT, default=False): bool,
    }
)


class CoolLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a CoolLink config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

        errors: dict[str, str] = {}
        host = user_input[CONF_HOST]
        port = user_input[CONF_PORT]
        wakeup = user_input[CONF_SEND_WAKEUP_PROMPT]

        client = CoolLinkNet(host, port, send_wakeup=wakeup)
        try:
            units = await client.status()
        except OSError:
            errors["base"] = "cannot_connect"
        else:
            if not units:
                errors["base"] = "no_units"

        if errors:
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors=errors
            )

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()

        supported_modes = [m for m in AVAILABLE_MODES if user_input.get(m)]
        return self.async_create_entry(
            title=host,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_SUPPORTED_MODES: supported_modes,
                CONF_MIN_TEMP: user_input[CONF_MIN_TEMP],
                CONF_MAX_TEMP: user_input[CONF_MAX_TEMP],
                CONF_HALF_STEPS: user_input[CONF_HALF_STEPS],
                CONF_SEND_WAKEUP_PROMPT: wakeup,
            },
        )
