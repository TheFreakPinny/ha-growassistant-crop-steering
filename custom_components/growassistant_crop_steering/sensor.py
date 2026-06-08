"""Sensor platform for GrowAssistant Crop Steering."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_LED_DAY_SENSOR,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
    VERSION,
)

_STATUS_SENSOR = SensorEntityDescription(
    key="status",
    translation_key="status",
    icon="mdi:sprout",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GrowAssistant Crop Steering sensors for a config entry."""
    async_add_entities([GrowAssistantStatusSensor(entry)])


class GrowAssistantStatusSensor(SensorEntity):
    """Status sensor for the GrowAssistant Crop Steering scaffold."""

    entity_description = _STATUS_SENSOR
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the status sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.data.get(CONF_NAME, DEFAULT_NAME),
            "manufacturer": "GrowAssistant",
            "model": "Crop Steering Scaffold",
            "sw_version": VERSION,
        }

    @property
    def native_value(self) -> str:
        """Return the scaffold status."""
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return selected Home Assistant entities for the scaffold."""
        return {
            CONF_PUMP_SWITCH: self._entry.data.get(CONF_PUMP_SWITCH),
            CONF_LED_DAY_SENSOR: self._entry.data.get(CONF_LED_DAY_SENSOR),
            CONF_VWC_SENSOR: self._entry.data.get(CONF_VWC_SENSOR),
            CONF_DRAIN_SENSOR: self._entry.data.get(CONF_DRAIN_SENSOR),
            CONF_DRAIN_TRAY_SENSOR: self._entry.data.get(CONF_DRAIN_TRAY_SENSOR),
        }