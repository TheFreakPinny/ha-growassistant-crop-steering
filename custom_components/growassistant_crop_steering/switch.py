"""Switch platform for GrowAssistant Crop Steering state flags."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BOOLEAN_STATE_DEFAULTS,
    BOOLEAN_STATE_DESCRIPTIONS,
    BooleanStateDescription,
    DEFAULT_NAME,
    DOMAIN,
    VERSION,
)

PARALLEL_UPDATES = 0
SIGNAL_SWITCH_STATE_UPDATED = f"{DOMAIN}_switch_state_updated"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GrowAssistant Crop Steering switch entities for a config entry."""
    async_add_entities(
        GrowAssistantStateSwitch(hass, entry, description)
        for description in BOOLEAN_STATE_DESCRIPTIONS
    )


class GrowAssistantStateSwitch(SwitchEntity):
    """Integration-managed editable boolean crop steering state flag."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state_description: BooleanStateDescription,
    ) -> None:
        """Initialize a boolean crop steering state switch."""
        self.hass = hass
        self._entry = entry
        self._state_description = state_description
        self.entity_description = SwitchEntityDescription(
            key=state_description.key,
            translation_key=state_description.key,
            name=state_description.name,
            icon=state_description.icon,
        )
        self._attr_unique_id = f"{entry.entry_id}_{state_description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Register for service-driven state updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SWITCH_STATE_UPDATED}_{self._entry.entry_id}",
                self._handle_state_updated,
            )
        )

    @callback
    def _handle_state_updated(self) -> None:
        """Write the current persisted state after an external update."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return the persisted managed state, legacy helper state, or default."""
        managed_value = self._entry.options.get(self._state_description.key)
        if managed_value is not None:
            return _coerce_bool(managed_value, self._state_description.default_value)

        legacy_entity_id = self._entry.data.get(self._state_description.key)
        if isinstance(legacy_entity_id, str) and legacy_entity_id:
            state = self.hass.states.get(legacy_entity_id)
            if state is not None:
                return state.state == STATE_ON

        return self._state_description.default_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Persist the state flag as on."""
        self._set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Persist the state flag as off."""
        self._set_state(False)

    def _set_state(self, value: bool) -> None:
        """Persist an updated state flag value in config entry options."""
        options = dict(self._entry.options)
        options[self._state_description.key] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()


def _coerce_bool(value: Any, default: bool) -> bool:
    """Return value as a bool, falling back to default if unset or invalid."""
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"1", "true", "on", "yes"}:
            return True
        if normalized in {"0", "false", "off", "no"}:
            return False

    if isinstance(value, int):
        return bool(value)

    return default


def managed_boolean_state(entry: ConfigEntry, config_key: str) -> bool:
    """Return a persisted managed boolean state value or its default."""
    return _coerce_bool(
        entry.options.get(config_key),
        BOOLEAN_STATE_DEFAULTS.get(config_key, False),
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
