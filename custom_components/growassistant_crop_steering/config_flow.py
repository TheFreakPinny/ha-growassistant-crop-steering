"""Config flow for the GrowAssistant Crop Steering integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_LED_DAY_SENSOR,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)


class GrowAssistantCropSteeringConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GrowAssistant Crop Steering."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            name = user_input.get(CONF_NAME, DEFAULT_NAME)
            return self.async_create_entry(
                title=name,
                data={
                    CONF_NAME: name,
                    CONF_PUMP_SWITCH: user_input[CONF_PUMP_SWITCH],
                    CONF_LED_DAY_SENSOR: user_input[CONF_LED_DAY_SENSOR],
                    CONF_VWC_SENSOR: user_input[CONF_VWC_SENSOR],
                    CONF_DRAIN_SENSOR: user_input.get(CONF_DRAIN_SENSOR),
                    CONF_DRAIN_TRAY_SENSOR: user_input.get(CONF_DRAIN_TRAY_SENSOR),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_PUMP_SWITCH): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="switch")
                    ),
                    vol.Required(CONF_LED_DAY_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="binary_sensor")
                    ),
                    vol.Required(CONF_VWC_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(CONF_DRAIN_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="binary_sensor")
                    ),
                    vol.Optional(CONF_DRAIN_TRAY_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="binary_sensor")
                    ),
                }
            ),
        )