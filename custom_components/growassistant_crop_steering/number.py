"""Number platform for GrowAssistant Crop Steering numeric settings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_NAME,
    DOMAIN,
    NUMERIC_SETTING_DESCRIPTIONS,
    NumericSettingDescription,
    VERSION,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GrowAssistant Crop Steering number entities for a config entry."""
    async_add_entities(
        GrowAssistantSettingNumber(hass, entry, description)
        for description in NUMERIC_SETTING_DESCRIPTIONS
    )


class GrowAssistantSettingNumber(NumberEntity):
    """Integration-managed numeric crop steering setting."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        setting_description: NumericSettingDescription,
    ) -> None:
        """Initialize a numeric crop steering setting."""
        self.hass = hass
        self._entry = entry
        self._setting_description = setting_description
        self.entity_description = NumberEntityDescription(
            key=setting_description.key,
            translation_key=setting_description.key,
            name=setting_description.name,
            native_unit_of_measurement=setting_description.native_unit_of_measurement,
            native_min_value=setting_description.native_min_value,
            native_max_value=setting_description.native_max_value,
            native_step=setting_description.native_step,
        )
        self._attr_unique_id = f"{entry.entry_id}_{setting_description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the persisted managed value, legacy helper value, or default."""
        managed_value = self._entry.options.get(self._setting_description.key)
        if managed_value is not None:
            return _coerce_number(
                managed_value, self._setting_description.default_value
            )

        legacy_entity_id = self._entry.data.get(self._setting_description.key)
        if isinstance(legacy_entity_id, str) and legacy_entity_id:
            state = self.hass.states.get(legacy_entity_id)
            if state is not None:
                return _coerce_number(
                    state.state, self._setting_description.default_value
                )

        return self._setting_description.default_value

    async def async_set_native_value(self, value: float) -> None:
        """Persist an updated numeric setting value in config entry options."""
        options = dict(self._entry.options)
        options[self._setting_description.key] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()


def _coerce_number(value: Any, default: float) -> float:
    """Return value as a float, falling back to default if invalid."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Return shared GrowAssistant device info."""
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": entry.data.get(CONF_NAME, DEFAULT_NAME),
        "manufacturer": "GrowAssistant",
        "model": "Crop Steering Scaffold",
        "sw_version": VERSION,
    }
