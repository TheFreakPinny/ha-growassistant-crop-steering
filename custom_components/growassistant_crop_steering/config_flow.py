"""Config flow for the GrowAssistant Crop Steering integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONFIG_ENTRY_KEYS,
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_LAST_SHOT,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_MODE,
    CONF_P1_SHOTS_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_MODE,
    CONF_P2_SHOTS_DONE,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
    MODE_OPTIONS,
    MODE_SENSOR,
)

_REQUIRED_ENTITY_FIELDS: tuple[tuple[str, str | list[str]], ...] = (
    (CONF_PUMP_SWITCH, ["switch", "input_boolean"]),
    (CONF_VWC_SENSOR, "sensor"),
    (CONF_LED_SUNRISE, "input_datetime"),
    (CONF_LED_SUNSET, "input_datetime"),
    (CONF_P1_ACTIVE, "input_boolean"),
    (CONF_P1_DONE, "input_boolean"),
    (CONF_P1_WINDOW_OPENED_TODAY, "input_boolean"),
    (CONF_P1_SHOTS_DONE, "counter"),
    (CONF_P2_SHOTS_DONE, "counter"),
    (CONF_LAST_SHOT, "input_datetime"),
)

_OPTIONAL_ENTITY_FIELDS: tuple[tuple[str, str | list[str]], ...] = (
    (CONF_DRAIN_SENSOR, "binary_sensor"),
    (CONF_DRAIN_TRAY_SENSOR, "binary_sensor"),
)


def _mode_selector() -> selector.SelectSelector:
    """Return a fixed P1/P2 mode selector."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(MODE_OPTIONS),
        )
    )


def _configured_mode(
    entry: config_entries.ConfigEntry,
    config_key: str,
    default: str = MODE_SENSOR,
) -> str:
    """Return a P1/P2 mode from options, then data, then a safe default."""
    for source in (entry.options, entry.data):
        mode = source.get(config_key)
        if isinstance(mode, str) and mode.lower() in MODE_OPTIONS:
            return mode.lower()

    return default


def _options_schema(entry: config_entries.ConfigEntry) -> vol.Schema:
    """Return the options form schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_P1_MODE,
                default=_configured_mode(entry, CONF_P1_MODE),
            ): _mode_selector(),
            vol.Required(
                CONF_P2_MODE,
                default=_configured_mode(entry, CONF_P2_MODE),
            ): _mode_selector(),
        }
    )


def _entity_selector(
    domain: str | list[str],
    *,
    multiple: bool = False,
) -> selector.EntitySelector:
    """Return an entity selector constrained to one or more Home Assistant domains."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _data_schema() -> vol.Schema:
    """Return the setup form schema."""
    schema: dict[Any, Any] = {vol.Optional(CONF_NAME, default=DEFAULT_NAME): str}

    for config_key, domain in _REQUIRED_ENTITY_FIELDS:
        schema[vol.Required(config_key)] = _entity_selector(
            domain,
            multiple=config_key == CONF_VWC_SENSOR,
        )

    schema[vol.Required(CONF_P1_MODE, default=MODE_SENSOR)] = _mode_selector()
    schema[vol.Required(CONF_P2_MODE, default=MODE_SENSOR)] = _mode_selector()

    for config_key, domain in _OPTIONAL_ENTITY_FIELDS:
        schema[vol.Optional(config_key)] = _entity_selector(domain)

    return vol.Schema(schema)


class GrowAssistantCropSteeringConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GrowAssistant Crop Steering."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)

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
                    **{key: user_input.get(key) for key in CONFIG_ENTRY_KEYS},
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_data_schema(),
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for GrowAssistant Crop Steering."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow handler."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage P1/P2 steering mode options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_P1_MODE: user_input[CONF_P1_MODE],
                    CONF_P2_MODE: user_input[CONF_P2_MODE],
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self._config_entry),
        )
