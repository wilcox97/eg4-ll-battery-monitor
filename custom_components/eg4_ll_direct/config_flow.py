"""Config flow for the EG4 LL Direct integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ADDRESS,
    CONF_CHARGE_TARGET_PCT,
    CONF_LOW_SOC_WARN_PCT,
    DEFAULT_CHARGE_TARGET_PCT,
    DEFAULT_LOW_SOC_WARN_PCT,
    DEFAULT_NAME,
    DOMAIN,
)

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class EG4LLDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EG4 LL Direct."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not MAC_RE.match(address):
                errors["base"] = "invalid_mac"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{DEFAULT_NAME} ({address})",
                    data={CONF_ADDRESS: address},
                    options={
                        CONF_CHARGE_TARGET_PCT: DEFAULT_CHARGE_TARGET_PCT,
                        CONF_LOW_SOC_WARN_PCT: DEFAULT_LOW_SOC_WARN_PCT,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default="80:6F:B0:15:5E:71"): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EG4LLOptionsFlow":
        return EG4LLOptionsFlow(config_entry)


class EG4LLOptionsFlow(config_entries.OptionsFlow):
    """Options flow — lets you change charge target and low SOC warning
    from Settings -> Integrations -> EG4 LL Battery -> Configure."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHARGE_TARGET_PCT,
                    default=current.get(CONF_CHARGE_TARGET_PCT, DEFAULT_CHARGE_TARGET_PCT),
                ): vol.All(int, vol.Range(min=50, max=100)),
                vol.Required(
                    CONF_LOW_SOC_WARN_PCT,
                    default=current.get(CONF_LOW_SOC_WARN_PCT, DEFAULT_LOW_SOC_WARN_PCT),
                ): vol.All(int, vol.Range(min=5, max=50)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "charge_target_desc": "Stop-charge target (50-100%). 80% for longevity, 100% for outage prep.",
                "low_soc_desc": "Low battery warning threshold (5-50%). Creates a low_battery binary sensor.",
            },
        )
