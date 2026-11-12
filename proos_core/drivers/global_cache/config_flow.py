"""Global Caché — setup (register 1323).

Three ways in, all the platform's own: DHCP (the platform sees a Global Caché MAC, 00:0C:1E, join the network — the
manifest's matcher), the unit's own discovery beacon (once one unit is set up, every other unit and every Global
Connect module that announces itself is offered), and a typed address. Each is the same check: the unit answers
getdevices on port 4998. One entry per unit — a Global Connect chassis is one entry per module, as the maker built it
(each module its own address, Global Connect TCP API).
"""
from __future__ import annotations

from typing import Any

import probatio  # the platform's own forms library since 2026.11 (voluptuous is banned in its code)

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import CONF_MAC, CONF_MODEL, DOMAIN
from .hub import FAMILY_WORDS, KIND_WORDS
from .protocol import GlobalCacheClient, GlobalCacheError, family_of, io_ports


async def _describe(host: str, model: str = "") -> str:
    """What answers at this address, in words — or raises GlobalCacheError."""
    client = GlobalCacheClient(host)
    try:
        mods = await client.get_devices()
    finally:
        await client.close()
    ports = io_ports(mods)
    if not ports:
        raise GlobalCacheError("the unit at %s reports no IR, serial, relay or HDMI ports" % host)
    counts = {}
    for p in ports:
        counts[p.kind] = counts.get(p.kind, 0) + 1
    fam = FAMILY_WORDS.get(family_of(mods, model), "Global Caché")
    return "%s — %s" % (fam, ", ".join("%d %s" % (n, KIND_WORDS[k]) for k, n in counts.items()))


class GlobalCacheConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Global Caché unit."""

    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._mac = ""
        self._model = ""
        self._what = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"reason": ""}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            self._async_abort_entries_match({CONF_HOST: host})
            try:
                what = await _describe(host)
            except (GlobalCacheError, OSError) as e:
                errors["base"] = "cannot_connect"
                placeholders["reason"] = str(e)
            else:
                return self.async_create_entry(title="%s at %s" % (what.split(" — ")[0], host),
                                               data={CONF_HOST: host, CONF_MAC: "", CONF_MODEL: ""})
        return self.async_show_form(step_id="user", data_schema=probatio.Schema({probatio.Required(CONF_HOST): str}),
                                    errors=errors, description_placeholders=placeholders)

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        self._host, self._mac = discovery_info.ip, format_mac(discovery_info.macaddress)
        return await self._found()

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """The unit's own beacon (AMXB on 239.255.250.250:9131), heard by a unit already set up."""
        self._host = discovery_info.get("host") or ""
        self._mac = format_mac(discovery_info.get("mac") or "") if discovery_info.get("mac") else ""
        self._model = discovery_info.get("model") or ""
        if not self._host:
            return self.async_abort(reason="cannot_connect")
        return await self._found()

    async def _found(self) -> ConfigFlowResult:
        if self._mac:
            await self.async_set_unique_id(self._mac)
            self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})
        self._async_abort_entries_match({CONF_HOST: self._host})
        try:
            self._what = await _describe(self._host, self._model)
        except (GlobalCacheError, OSError):
            return self.async_abort(reason="cannot_connect")
        self.context["title_placeholders"] = {"name": "%s (%s)" % (self._what.split(" — ")[0], self._host)}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="%s at %s" % (self._what.split(" — ")[0], self._host),
                                           data={CONF_HOST: self._host, CONF_MAC: self._mac, CONF_MODEL: self._model})
        return self.async_show_form(step_id="confirm", description_placeholders={"what": self._what, "host": self._host})
