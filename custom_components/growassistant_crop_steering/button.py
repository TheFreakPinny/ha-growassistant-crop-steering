"""Button platform for GrowAssistant Crop Steering service actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_NAME,
    DOMAIN,
    SERVICE_CLEAR_LAST_SHOT,
    SERVICE_RESET_CYCLE,
    SERVICE_SET_LAST_SHOT_NOW,
    SERVICE_START_P1,
    SERVICE_STOP_PUMP,
    VERSION,
)

PARALLEL_UPDATES = 0
ATTR_CONFIG_ENTRY_ID = "config_entry_id"


@dataclass(frozen=True)
class GrowAssistantServiceButtonDescription:
    """Description of a GrowAssistant service button."""

    key: str
    name: str
    icon: str
    service: str


SERVICE_BUTTON_DESCRIPTIONS: tuple[GrowAssistantServiceButtonDescription, ...] = (
    GrowAssistantServiceButtonDescription(
        SERVICE_RESET_CYCLE,
        "Reset Cycle",
        "mdi:restart",
        SERVICE_RESET_CYCLE,
    ),
    GrowAssistantServiceButtonDescription(
        SERVICE_START_P1,
        "Start P1",
        "mdi:play",
        SERVICE_START_P1,
    ),
    GrowAssistantServiceButtonDescription(
        SERVICE_STOP_PUMP,
        "Stop Pump",
        "mdi:water-pump-off",
        SERVICE_STOP_PUMP,
    ),
    GrowAssistantServiceButtonDescription(
        SERVICE_SET_LAST_SHOT_NOW,
        "Set Last Shot Now",
        "mdi:clock-plus-outline",
        SERVICE_SET_LAST_SHOT_NOW,
    ),
    GrowAssistantServiceButtonDescription(
        SERVICE_CLEAR_LAST_SHOT,
        "Clear Last Shot",
        "mdi:clock-remove-outline",
        SERVICE_CLEAR_LAST_SHOT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GrowAssistant Crop Steering button entities for a config entry."""
    async_add_entities(
        GrowAssistantServiceButton(hass, entry, description)
        for description in SERVICE_BUTTON_DESCRIPTIONS
    )


class GrowAssistantServiceButton(ButtonEntity):
    """Button that calls a GrowAssistant Crop Steering service for one entry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        service_description: GrowAssistantServiceButtonDescription,
    ) -> None:
        """Initialize a GrowAssistant service button."""
        self.hass = hass
        self._entry = entry
        self._service_description = service_description
        self.entity_description = ButtonEntityDescription(
            key=service_description.key,
            translation_key=service_description.key,
            name=service_description.name,
            icon=service_description.icon,
        )
        self._attr_unique_id = f"{entry.entry_id}_{service_description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Call the configured GrowAssistant service for this config entry."""
        await self.hass.services.async_call(
            DOMAIN,
            self._service_description.service,
            {ATTR_CONFIG_ENTRY_ID: self._entry.entry_id},
            blocking=True,
        )


def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Return shared GrowAssistant device info."""
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": entry.data.get(CONF_NAME, DEFAULT_NAME),
        "manufacturer": "GrowAssistant",
        "model": "Crop Steering Scaffold",
        "sw_version": VERSION,
    }
