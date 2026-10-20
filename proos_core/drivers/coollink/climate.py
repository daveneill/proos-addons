"""Climate platform for CoolLink (CoolAutomation) indoor units."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_HALF_STEPS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_SUPPORTED_MODES,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
)
from .coordinator import CoolLinkConfigEntry, CoolLinkDataUpdateCoordinator
from .entity import CoolLinkEntity

_LOGGER = logging.getLogger(__name__)

# Hub mode string  <->  Home Assistant HVAC mode.
CM_TO_HA_STATE = {
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "auto": HVACMode.HEAT_COOL,
    "dry": HVACMode.DRY,
    "fan": HVACMode.FAN_ONLY,
}
HA_STATE_TO_CM = {value: key for key, value in CM_TO_HA_STATE.items()}

# Hub fan-speed string  <->  Home Assistant fan mode label.
FAN_VERY_LOW = "very low"
FAN_TOP = "top"
CM_TO_HA_FAN = {
    "vlow": FAN_VERY_LOW,
    "low": FAN_LOW,
    "med": FAN_MEDIUM,
    "high": FAN_HIGH,
    "top": FAN_TOP,
    "auto": FAN_AUTO,
}
HA_FAN_TO_CM = {value: key for key, value in CM_TO_HA_FAN.items()}
FAN_MODES = [FAN_AUTO, FAN_VERY_LOW, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_TOP]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CoolLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up CoolLink climate entities from a config entry."""
    coordinator = config_entry.runtime_data
    data = config_entry.data

    supported = data.get(
        CONF_SUPPORTED_MODES,
        [m.value for m in CM_TO_HA_STATE.values()],
    )
    hvac_modes = [HVACMode.OFF] + [
        HVACMode(m) for m in supported if HVACMode(m) != HVACMode.OFF
    ]

    async_add_entities(
        CoolLinkClimate(
            coordinator,
            unit_id,
            hvac_modes,
            min_temp=data.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP),
            max_temp=data.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP),
            half_steps=data.get(CONF_HALF_STEPS, False),
        )
        for unit_id in coordinator.data
    )


class CoolLinkClimate(CoolLinkEntity, ClimateEntity):
    """A single indoor unit exposed as a HA climate entity."""

    _attr_name = None
    _attr_fan_modes = FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: CoolLinkDataUpdateCoordinator,
        unit_id: str,
        hvac_modes: list[HVACMode],
        min_temp: float,
        max_temp: float,
        half_steps: bool,
    ) -> None:
        super().__init__(coordinator, unit_id)
        self._attr_unique_id = unit_id
        self._attr_hvac_modes = hvac_modes
        self._attr_min_temp = min_temp
        self._attr_max_temp = max_temp
        self._attr_target_temperature_step = 0.5 if half_steps else 1.0

    @property
    def temperature_unit(self) -> str:
        if self._unit.temperature_unit == "celsius":
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    @property
    def current_temperature(self) -> float | None:
        return self._unit.temperature

    @property
    def target_temperature(self) -> float | None:
        return self._unit.thermostat

    @property
    def hvac_mode(self) -> HVACMode:
        if not self._unit.is_on:
            return HVACMode.OFF
        return CM_TO_HA_STATE.get(self._unit.mode, HVACMode.COOL)

    @property
    def fan_mode(self) -> str:
        # Fall back to Auto when the hub reports no/unknown fan speed (e.g. "----"),
        # so fan_mode is always a valid member of fan_modes.
        return CM_TO_HA_FAN.get(self._unit.fan_speed, FAN_AUTO)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._unit = await self._unit.set_thermostat(temp)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        cm_speed = HA_FAN_TO_CM.get(fan_mode, fan_mode)
        self._unit = await self._unit.set_fan_speed(cm_speed)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        self._unit = await self._unit.set_mode(HA_STATE_TO_CM[hvac_mode])
        self._unit = await self._unit.turn_on()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        self._unit = await self._unit.turn_on()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        self._unit = await self._unit.turn_off()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
