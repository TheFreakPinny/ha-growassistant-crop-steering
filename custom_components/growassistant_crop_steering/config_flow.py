"""Config flow for the GrowAssistant Crop Steering integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import DEFAULT_NAME, DOMAIN


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
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data={CONF_NAME: user_input.get(CONF_NAME, DEFAULT_NAME)},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_NAME, default=DEFAULT_NAME): str}),
        )
