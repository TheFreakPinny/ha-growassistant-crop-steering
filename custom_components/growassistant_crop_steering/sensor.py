"""Sensor platform for GrowAssistant Crop Steering."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONFIG_ENTITY_KEYS,
    CONF_LED_DAY_SENSOR,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P0_TRANSPIRATION_MIN,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_DURATION_MIN,
    CONF_P1_INTERVAL_MIN,
    CONF_P1_MODE,
    CONF_P2_END_OFFSET_MIN,
    CONF_P2_INTERVAL_MIN,
    CONF_P2_SHOTS,
    CONF_P2_SHOTS_DONE,
    DEFAULT_NAME,
    DOMAIN,
    VERSION,
)

_PHASE_OFF = "off"
_PHASE_PRE_ON = "pre_on"
_PHASE_P0_TRANSPIRATION = "p0_transpiration"
_PHASE_P1_MORNING = "p1_morning"
_PHASE_P2_MIDDAY = "p2_midday"
_PHASE_P3_DRYBACK = "p3_dryback"

_MODE_MANUAL = "manual"

_STATUS_SENSOR = SensorEntityDescription(
    key="status",
    translation_key="status",
    icon="mdi:sprout",
)

_PHASE_SENSOR = SensorEntityDescription(
    key="phase",
    translation_key="phase",
    icon="mdi:chart-timeline-variant",
)

_UNAVAILABLE_STATES = {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GrowAssistant Crop Steering sensors for a config entry."""
    async_add_entities(
        [
            GrowAssistantStatusSensor(entry),
            GrowAssistantPhaseSensor(hass, entry),
        ]
    )


class GrowAssistantStatusSensor(SensorEntity):
    """Status sensor for the GrowAssistant Crop Steering scaffold."""

    entity_description = _STATUS_SENSOR
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the status sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        """Return the scaffold status."""
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return selected Home Assistant entities for the scaffold."""
        return {key: self._entry.data.get(key) for key in CONFIG_ENTITY_KEYS}


class GrowAssistantPhaseSensor(SensorEntity):
    """Calculate the current crop steering phase from configured helpers."""

    entity_description = _PHASE_SENSOR
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the phase sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_phase"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        """Return the current crop steering phase."""
        return self._calculate_phase()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return phase calculation debug attributes."""
        return self._calculate_phase()[1]

    def _calculate_phase(self) -> tuple[str, dict[str, Any]]:
        """Calculate the current crop steering phase and debug attributes."""
        missing_entities: list[str] = []

        led_day = _get_bool_state(
            self.hass,
            self._entry.data.get(CONF_LED_DAY_SENSOR),
            missing_entities,
        )

        p0_s = _minutes_to_seconds(
            _get_float_state(
                self.hass,
                self._entry.data.get(CONF_P0_TRANSPIRATION_MIN),
                missing_entities,
            )
        )

        p1_s = _minutes_to_seconds(
            _get_float_state(
                self.hass,
                self._entry.data.get(CONF_P1_DURATION_MIN),
                missing_entities,
            )
        )

        p2_target = _get_float_state(
            self.hass,
            self._entry.data.get(CONF_P2_SHOTS),
            missing_entities,
        )

        p2_done = _get_float_state(
            self.hass,
            self._entry.data.get(CONF_P2_SHOTS_DONE),
            missing_entities,
        )

        p2_end_offset_s = _minutes_to_seconds(
            _get_float_state(
                self.hass,
                self._entry.data.get(CONF_P2_END_OFFSET_MIN),
                missing_entities,
            )
        )

        p1_mode = _get_text_state(
            self.hass,
            self._entry.data.get(CONF_P1_MODE),
            missing_entities,
        )

        p1_active = _get_bool_state(
            self.hass,
            self._entry.data.get(CONF_P1_ACTIVE),
            missing_entities,
        )

        p1_done = _get_bool_state(
            self.hass,
            self._entry.data.get(CONF_P1_DONE),
            missing_entities,
        )

        # Optional interval helpers are read only for diagnostics / future use.
        _get_float_state(self.hass, self._entry.data.get(CONF_P2_INTERVAL_MIN), [])
        _get_float_state(self.hass, self._entry.data.get(CONF_P1_INTERVAL_MIN), [])

        timing = _light_timing(
            self.hass,
            self._entry.data.get(CONF_LED_SUNRISE),
            self._entry.data.get(CONF_LED_SUNSET),
            missing_entities,
        )

        since_on_s = None if timing is None else timing[0]
        until_off_s = None if timing is None else timing[1]

        p2_target_value = max(0, int(p2_target or 0))
        p2_done_value = max(0, int(p2_done or 0))
        p2_shots_left = max(0, p2_target_value - p2_done_value)

        p2_time_ok = (
            until_off_s is not None
            and p2_end_offset_s is not None
            and until_off_s > p2_end_offset_s
        )

        debug_attributes = {
            "led_day": led_day,
            "since_on_s": since_on_s,
            "p0_s": p0_s,
            "p1_s": p1_s,
            "p2_target": p2_target_value,
            "p2_done": p2_done_value,
            "p2_shots_left": p2_shots_left,
            "p2_time_ok": p2_time_ok,
            "missing_entities": missing_entities,
            "p1_mode": p1_mode,
            "p1_active": p1_active,
            "p1_done": p1_done,
        }

        if led_day is None:
            return _PHASE_OFF, debug_attributes

        if not led_day:
            return _PHASE_P3_DRYBACK, debug_attributes

        if missing_entities:
            return _PHASE_OFF, debug_attributes

        if since_on_s is None:
            return _PHASE_PRE_ON, debug_attributes

        if p0_s is None or p1_s is None:
            return _PHASE_OFF, debug_attributes

        if since_on_s < 0:
            return _PHASE_PRE_ON, debug_attributes

        if since_on_s < p0_s:
            return _PHASE_P0_TRANSPIRATION, debug_attributes

        p2_available = p2_target_value > 0 and p2_shots_left > 0 and p2_time_ok
        p1_mode_value = (p1_mode or "").lower()

        if p1_mode_value == _MODE_MANUAL:
            if since_on_s < p0_s + p1_s:
                return _PHASE_P1_MORNING, debug_attributes

            if p2_available:
                return _PHASE_P2_MIDDAY, debug_attributes

            return _PHASE_P3_DRYBACK, debug_attributes

        if p1_active:
            return _PHASE_P1_MORNING, debug_attributes

        if p1_done and p2_available:
            return _PHASE_P2_MIDDAY, debug_attributes

        return _PHASE_P3_DRYBACK, debug_attributes


def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Return shared GrowAssistant device info."""
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": entry.data.get(CONF_NAME, DEFAULT_NAME),
        "manufacturer": "GrowAssistant",
        "model": "Crop Steering Scaffold",
        "sw_version": VERSION,
    }


def _get_text_state(
    hass: HomeAssistant,
    entity_id: str | None,
    missing_entities: list[str],
) -> str | None:
    """Return an entity state as text if available."""
    if entity_id is None:
        missing_entities.append("not_configured")
        return None

    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        missing_entities.append(entity_id)
        return None

    return state.state


def _get_bool_state(
    hass: HomeAssistant,
    entity_id: str | None,
    missing_entities: list[str],
) -> bool | None:
    """Return a boolean entity state if available."""
    state = _get_text_state(hass, entity_id, missing_entities)
    if state is None:
        return None

    return state == STATE_ON


def _get_float_state(
    hass: HomeAssistant,
    entity_id: str | None,
    missing_entities: list[str],
) -> float | None:
    """Return a numeric entity state if available."""
    state = _get_text_state(hass, entity_id, missing_entities)
    if state is None:
        return None

    try:
        return float(state)
    except ValueError:
        missing_entities.append(entity_id or "not_configured")
        return None


def _minutes_to_seconds(value: float | None) -> int | None:
    """Convert minutes to seconds."""
    if value is None:
        return None

    return max(0, int(value * 60))


def _light_timing(
    hass: HomeAssistant,
    sunrise_entity_id: str | None,
    sunset_entity_id: str | None,
    missing_entities: list[str],
) -> tuple[int, int] | None:
    """Return seconds since light-on and until light-off."""
    sunrise_s = _get_time_seconds(hass, sunrise_entity_id, missing_entities)
    sunset_s = _get_time_seconds(hass, sunset_entity_id, missing_entities)

    if sunrise_s is None or sunset_s is None:
        return None

    now = dt_util.now()
    now_s = _seconds_since_midnight(now)

    if sunrise_s == sunset_s:
        return 0, 24 * 60 * 60

    if sunset_s > sunrise_s:
        return now_s - sunrise_s, sunset_s - now_s

    if now_s >= sunrise_s:
        return now_s - sunrise_s, sunset_s + 24 * 60 * 60 - now_s

    return now_s + 24 * 60 * 60 - sunrise_s, sunset_s - now_s


def _get_time_seconds(
    hass: HomeAssistant,
    entity_id: str | None,
    missing_entities: list[str],
) -> int | None:
    """Return seconds since midnight for an input_datetime entity."""
    if entity_id is None:
        missing_entities.append("not_configured")
        return None

    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        missing_entities.append(entity_id)
        return None

    hour = state.attributes.get("hour")
    minute = state.attributes.get("minute")
    second = state.attributes.get("second", 0)

    if hour is not None and minute is not None:
        return int(hour) * 3600 + int(minute) * 60 + int(second or 0)

    state_value = state.state

    if "T" in state_value:
        state_value = state_value.split("T", 1)[1]

    if " " in state_value:
        state_value = state_value.rsplit(" ", 1)[1]

    parts = state_value.split(":")
    if len(parts) < 2:
        missing_entities.append(entity_id)
        return None

    try:
        parsed_hour = int(parts[0])
        parsed_minute = int(parts[1])
        parsed_second = int(float(parts[2])) if len(parts) > 2 else 0
    except ValueError:
        missing_entities.append(entity_id)
        return None

    return parsed_hour * 3600 + parsed_minute * 60 + parsed_second


def _seconds_since_midnight(value: datetime) -> int:
    """Return seconds since local midnight for a datetime."""
    return value.hour * 3600 + value.minute * 60 + value.second