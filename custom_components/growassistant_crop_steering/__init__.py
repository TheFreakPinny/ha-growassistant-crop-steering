"""The GrowAssistant Crop Steering integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    BOOLEAN_STATE_DEFAULTS,
    CONF_LAST_SHOT,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_SHOTS_DONE,
    CONF_P1_SOAK_MIN,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS_DONE,
    CONF_PUMP_SWITCH,
    DOMAIN,
    NUMERIC_SETTING_DEFAULTS,
    SERVICE_CLEAR_LAST_SHOT,
    SERVICE_RESET_CYCLE,
    SERVICE_SET_LAST_SHOT_NOW,
    SERVICE_START_P1,
    SERVICE_STOP_PUMP,
    SIGNAL_LAST_SHOT_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (Platform.NUMBER, Platform.SENSOR, Platform.SWITCH)

DOMAIN_COUNTER = "counter"
DOMAIN_INPUT_BOOLEAN = "input_boolean"
DOMAIN_INPUT_DATETIME = "input_datetime"
DOMAIN_INPUT_NUMBER = "input_number"
DOMAIN_HOMEASSISTANT = "homeassistant"

SERVICE_COUNTER_RESET = "reset"
SERVICE_SET_DATETIME = "set_datetime"
SERVICE_SET_VALUE = "set_value"
SERVICE_TURN_OFF = "turn_off"
SERVICE_TURN_ON = "turn_on"

SIGNAL_SWITCH_STATE_UPDATED = f"{DOMAIN}_switch_state_updated"
SIGNAL_NUMBER_STATE_UPDATED = f"{DOMAIN}_number_state_updated"

ATTR_DATETIME = "datetime"
ATTR_VALUE = "value"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up GrowAssistant Crop Steering services."""

    async def _handle_reset_cycle(call: ServiceCall) -> None:
        """Reset configured daily/cycle state helpers."""
        _LOGGER.info("GrowAssistant Crop Steering reset_cycle service requested")
        for entry in _entries_for_service(hass, SERVICE_RESET_CYCLE):
            await _reset_cycle_for_entry(hass, entry)

    async def _handle_start_p1(call: ServiceCall) -> None:
        """Prepare helpers so external automation can start P1 shots."""
        _LOGGER.info("GrowAssistant Crop Steering start_p1 service requested")
        for entry in _entries_for_service(hass, SERVICE_START_P1):
            await _start_p1_for_entry(hass, entry)

    async def _handle_set_last_shot_now(call: ServiceCall) -> None:
        """Set the managed last-shot timestamp to now."""
        _LOGGER.info("GrowAssistant Crop Steering set_last_shot_now service requested")
        for entry in _entries_for_service(hass, SERVICE_SET_LAST_SHOT_NOW):
            await _set_last_shot_for_entry(hass, entry, dt_util.now())

    async def _handle_clear_last_shot(call: ServiceCall) -> None:
        """Clear the managed last-shot timestamp."""
        _LOGGER.info("GrowAssistant Crop Steering clear_last_shot service requested")
        for entry in _entries_for_service(hass, SERVICE_CLEAR_LAST_SHOT):
            await _clear_last_shot_for_entry(hass, entry)

    async def _handle_stop_pump(call: ServiceCall) -> None:
        """Turn off the configured pump switch or input_boolean helper."""
        _LOGGER.info("GrowAssistant Crop Steering stop_pump service requested")
        for entry in _entries_for_service(hass, SERVICE_STOP_PUMP):
            await _stop_pump_for_entry(hass, entry)

    hass.services.async_register(DOMAIN, SERVICE_RESET_CYCLE, _handle_reset_cycle)
    hass.services.async_register(DOMAIN, SERVICE_START_P1, _handle_start_p1)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_LAST_SHOT_NOW, _handle_set_last_shot_now
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_LAST_SHOT, _handle_clear_last_shot
    )
    hass.services.async_register(DOMAIN, SERVICE_STOP_PUMP, _handle_stop_pump)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GrowAssistant Crop Steering from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a GrowAssistant Crop Steering config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _entries_for_service(hass: HomeAssistant, service_name: str) -> list[ConfigEntry]:
    """Return configured entries that should receive a service call."""
    entries = list(hass.config_entries.async_entries(DOMAIN))

    if not entries:
        _LOGGER.warning(
            "GrowAssistant Crop Steering service %s skipped because no config entries are configured",
            service_name,
        )
        return []

    if len(entries) > 1:
        _LOGGER.warning(
            "GrowAssistant Crop Steering service %s will be applied to %s config entries; only one entry is expected",
            service_name,
            len(entries),
        )

    return entries


async def _reset_cycle_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reset daily/cycle helper state for one config entry without touching the pump."""
    await _set_boolean_state(hass, entry, CONF_P1_ACTIVE, False)
    await _set_boolean_state(hass, entry, CONF_P1_DONE, False)
    await _set_boolean_state(hass, entry, CONF_P1_WINDOW_OPENED_TODAY, False)
    await _reset_shots_done_counter(hass, entry, CONF_P1_SHOTS_DONE)
    await _reset_shots_done_counter(hass, entry, CONF_P2_SHOTS_DONE)
    await _set_numeric_setting(hass, entry, CONF_P2_REF_VWC, 0)
    _LOGGER.info(
        "GrowAssistant Crop Steering reset_cycle completed for config entry %s",
        entry.entry_id,
    )


async def _reset_shots_done_counter(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str
) -> None:
    """Reset a managed shot counter and a legacy counter helper if present."""
    if config_key in NUMERIC_SETTING_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = 0
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering reset managed shot counter %s for config entry %s",
            config_key,
            entry.entry_id,
        )

    entity_id = _legacy_counter_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_COUNTER,
        SERVICE_COUNTER_RESET,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering reset legacy shot counter %s (%s) for config entry %s",
        config_key,
        entity_id,
        entry.entry_id,
    )


async def _start_p1_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set P1 state flags for one config entry without starting the pump."""
    await _set_boolean_state(hass, entry, CONF_P1_ACTIVE, True)
    await _set_boolean_state(hass, entry, CONF_P1_WINDOW_OPENED_TODAY, True)
    await _set_boolean_state(hass, entry, CONF_P1_DONE, False)
    await _set_numeric_setting(hass, entry, CONF_P2_REF_VWC, 0)
    await _set_last_shot_before_soak(hass, entry)
    _LOGGER.info(
        "GrowAssistant Crop Steering start_p1 completed for config entry %s",
        entry.entry_id,
    )


async def _set_boolean_state(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, value: bool
) -> None:
    """Persist a managed boolean state flag and mirror it to a legacy helper if present."""
    if config_key in BOOLEAN_STATE_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = value
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_SWITCH_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering set managed boolean state %s to %s for config entry %s",
            config_key,
            value,
            entry.entry_id,
        )

    entity_id = _legacy_input_boolean_entity_id(entry, config_key)
    if entity_id is None:
        return

    service = SERVICE_TURN_ON if value else SERVICE_TURN_OFF
    await hass.services.async_call(
        DOMAIN_INPUT_BOOLEAN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering set legacy %s (%s) to %s for config entry %s",
        config_key,
        entity_id,
        value,
        entry.entry_id,
    )


async def _stop_pump_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn off the configured pump switch or input_boolean for one config entry."""
    await _call_helper_service(
        hass, entry, CONF_PUMP_SWITCH, DOMAIN_HOMEASSISTANT, SERVICE_TURN_OFF
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering stop_pump completed for config entry %s",
        entry.entry_id,
    )


async def _set_numeric_setting(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, value: float | int
) -> None:
    """Persist a managed numeric setting and mirror it to a legacy helper if present."""
    if config_key in NUMERIC_SETTING_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = value
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering set managed numeric setting %s to %s for config entry %s",
            config_key,
            value,
            entry.entry_id,
        )

    entity_id = _legacy_numeric_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_INPUT_NUMBER,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering set legacy %s (%s) to %s for config entry %s",
        config_key,
        entity_id,
        value,
        entry.entry_id,
    )


async def _set_last_shot_before_soak(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Backdate last_shot so external P1 automation can allow the first shot."""
    p1_soak_min = _numeric_setting_value(hass, entry, CONF_P1_SOAK_MIN, 0)
    last_shot = dt_util.now() - timedelta(minutes=p1_soak_min, seconds=1)
    await _set_last_shot_for_entry(hass, entry, last_shot)


async def _set_last_shot_for_entry(
    hass: HomeAssistant, entry: ConfigEntry, last_shot: datetime
) -> None:
    """Persist a managed last-shot timestamp and mirror it to a legacy helper."""
    options = dict(entry.options)
    options[CONF_LAST_SHOT] = last_shot.isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    async_dispatcher_send(hass, f"{SIGNAL_LAST_SHOT_UPDATED}_{entry.entry_id}")
    _LOGGER.info(
        "GrowAssistant Crop Steering set managed %s to %s for config entry %s",
        CONF_LAST_SHOT,
        last_shot.isoformat(),
        entry.entry_id,
    )

    last_shot_entity_id = _legacy_input_datetime_entity_id(entry, CONF_LAST_SHOT)
    if last_shot_entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_INPUT_DATETIME,
        SERVICE_SET_DATETIME,
        {
            ATTR_ENTITY_ID: last_shot_entity_id,
            ATTR_DATETIME: last_shot.strftime("%Y-%m-%d %H:%M:%S"),
        },
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering mirrored %s to legacy helper %s for config entry %s",
        CONF_LAST_SHOT,
        last_shot_entity_id,
        entry.entry_id,
    )


async def _clear_last_shot_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear the managed last-shot timestamp without requiring legacy helpers."""
    options = dict(entry.options)
    options[CONF_LAST_SHOT] = None
    hass.config_entries.async_update_entry(entry, options=options)
    async_dispatcher_send(hass, f"{SIGNAL_LAST_SHOT_UPDATED}_{entry.entry_id}")
    _LOGGER.info(
        "GrowAssistant Crop Steering cleared managed %s for config entry %s",
        CONF_LAST_SHOT,
        entry.entry_id,
    )

    if _legacy_input_datetime_entity_id(entry, CONF_LAST_SHOT) is not None:
        _LOGGER.info(
            "GrowAssistant Crop Steering skipped clearing legacy %s because input_datetime helpers cannot be emptied reliably",
            CONF_LAST_SHOT,
        )


async def _call_helper_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_key: str,
    domain: str,
    service: str,
) -> None:
    """Call a service for a configured helper entity if it is present."""
    entity_id = _configured_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering called %s.%s for %s (%s) on config entry %s",
        domain,
        service,
        config_key,
        entity_id,
        entry.entry_id,
    )


def _legacy_counter_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy counter helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_input_boolean_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy input_boolean helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_input_datetime_entity_id(
    entry: ConfigEntry, config_key: str
) -> str | None:
    """Return a legacy input_datetime helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_numeric_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy input_number helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _numeric_setting_value(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, default: float
) -> float:
    """Read a managed numeric setting, falling back to a legacy helper or default."""
    fallback = NUMERIC_SETTING_DEFAULTS.get(config_key, default)
    managed_value = entry.options.get(config_key)
    if managed_value is not None:
        try:
            return float(managed_value)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "GrowAssistant Crop Steering could not parse managed %s value %r as a number; using fallback",
                config_key,
                managed_value,
            )

    entity_id = _legacy_numeric_entity_id(entry, config_key)
    if entity_id is None:
        return fallback

    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not read %s (%s); using %s",
            config_key,
            entity_id,
            fallback,
        )
        return fallback

    try:
        return float(state.state)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not parse %s (%s) state %r as a number; using %s",
            config_key,
            entity_id,
            state.state,
            fallback,
        )
        return fallback


def _configured_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a configured entity id, logging and skipping if it is missing."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    _LOGGER.warning(
        "GrowAssistant Crop Steering config entry %s has no configured entity for %s; skipping",
        entry.entry_id,
        config_key,
    )
    return None


def _state_as_float(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, default: float
) -> float:
    """Read a configured entity state as a float."""
    entity_id = _configured_entity_id(entry, config_key)
    if entity_id is None:
        return default

    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not read %s (%s); using %s",
            config_key,
            entity_id,
            default,
        )
        return default

    try:
        return float(state.state)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not parse %s (%s) state %r as a number; using %s",
            config_key,
            entity_id,
            state.state,
            default,
        )
        return default
