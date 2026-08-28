"""Sensor platform for GrowAssistant Crop Steering."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .config import configured_entity_value
from .const import (
    BOOLEAN_STATE_DEFAULTS,
    CONFIG_ENTRY_KEYS,
    CONF_LAST_SHOT,
    CONF_LAST_SHOT_TYPE,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P0_TRANSPIRATION_MIN,
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_FIELD_CAPACITY_VWC,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P1_DURATION_MIN,
    CONF_P1_INTERVAL_MIN,
    CONF_P1_MODE,
    CONF_P1_SHOTS,
    CONF_P1_SHOTS_DONE,
    CONF_P1_SOAK_MIN,
    CONF_P1_START_VWC,
    CONF_P2_END_OFFSET_MIN,
    CONF_P2_INTERVAL_MIN,
    CONF_P2_MODE,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS,
    CONF_P2_SOAK_MIN,
    CONF_P2_SHOTS_DONE,
    CONF_P2_VWC_DROP,
    CONF_P3_EMERGENCY_ENABLED,
    CONF_P3_EMERGENCY_MAX_SHOTS,
    CONF_P3_EMERGENCY_SHOT_DURATION_S,
    CONF_P3_EMERGENCY_SHOTS_DONE,
    CONF_P3_EMERGENCY_SOAK_MIN,
    CONF_P3_EMERGENCY_THRESHOLD_VWC,
    CONF_PUMP_SWITCH,
    CONF_VWC_CAP,
    CONF_VWC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
    NUMERIC_SETTING_DEFAULTS,
    NUMERIC_SETTING_KEYS,
    MODE_MANUAL,
    MODE_OPTIONS,
    MODE_SENSOR,
    SIGNAL_LAST_SHOT_UPDATED,
    LAST_SHOT_TYPE_P1,
    LAST_SHOT_TYPE_P2,
    LAST_SHOT_TYPE_P3_EMERGENCY,
    LAST_SHOT_TYPES,
    VERSION,
)
from .vwc import calculate_vwc_state, normalize_vwc_sensors


_PHASE_OFF = "off"
_PHASE_PRE_ON = "pre_on"
_PHASE_P0_TRANSPIRATION = "p0_transpiration"
_PHASE_P1_MORNING = "p1_morning"
_PHASE_P2_MIDDAY = "p2_midday"
_PHASE_P3_DRYBACK = "p3_dryback"

_DEFAULT_SOAK_SECONDS = 5 * 60

_REQUIRED_BLOCK_REASON_KEYS = (
    CONF_VWC_SENSOR,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
)

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

_P1_SOAK_REMAINING_SENSOR = SensorEntityDescription(
    key="p1_soak_remaining",
    translation_key="p1_soak_remaining",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:timer-sand",
)

_P2_SOAK_REMAINING_SENSOR = SensorEntityDescription(
    key="p2_soak_remaining",
    translation_key="p2_soak_remaining",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:timer-sand",
)

_P3_EMERGENCY_SOAK_REMAINING_SENSOR = SensorEntityDescription(
    key="p3_emergency_soak_remaining",
    translation_key="p3_emergency_soak_remaining",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:timer-alert-outline",
)

_BLOCK_REASON_SENSOR = SensorEntityDescription(
    key="block_reason",
    translation_key="block_reason",
    icon="mdi:information-outline",
)

_P1_DEBUG_SENSOR = SensorEntityDescription(
    key="p1_debug",
    translation_key="p1_debug",
    icon="mdi:bug-check-outline",
)

_DEBUG_SENSOR = SensorEntityDescription(
    key="debug",
    translation_key="debug",
    icon="mdi:bug-check-outline",
)

_LAST_SHOT_SENSOR = SensorEntityDescription(
    key=CONF_LAST_SHOT,
    translation_key="last_shot",
    device_class=SensorDeviceClass.TIMESTAMP,
    icon="mdi:clock-outline",
)

_UNAVAILABLE_STATES = {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}
_OPTIONAL_BINARY_WET_STATES = {STATE_ON, "wet", "open", "problem"}
_OPTIONAL_BINARY_CLEAR_STATES = {"off", "dry", "closed", "clear"}


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
            GrowAssistantLastShotSensor(hass, entry),
            GrowAssistantSoakRemainingSensor(
                hass,
                entry,
                _P1_SOAK_REMAINING_SENSOR,
                CONF_P1_SOAK_MIN,
                _PHASE_P1_MORNING,
            ),
            GrowAssistantSoakRemainingSensor(
                hass,
                entry,
                _P2_SOAK_REMAINING_SENSOR,
                CONF_P2_SOAK_MIN,
                _PHASE_P2_MIDDAY,
            ),
            GrowAssistantSoakRemainingSensor(
                hass,
                entry,
                _P3_EMERGENCY_SOAK_REMAINING_SENSOR,
                CONF_P3_EMERGENCY_SOAK_MIN,
                _PHASE_P3_DRYBACK,
            ),
            GrowAssistantBlockReasonSensor(hass, entry),
            GrowAssistantP1DebugSensor(hass, entry),
            GrowAssistantDebugSensor(hass, entry),
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
        """Return selected entities and configured setup options for diagnostics."""
        return {
            key: configured_entity_value(self._entry, key) for key in CONFIG_ENTRY_KEYS
        }


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
        return _calculate_phase(self.hass, self._entry)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return phase calculation debug attributes."""
        return _calculate_phase(self.hass, self._entry)[1]


class GrowAssistantLastShotSensor(SensorEntity):
    """Expose the integration-managed last-shot timestamp."""

    entity_description = _LAST_SHOT_SENSOR
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the last shot sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_shot"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to managed last-shot updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_SHOT_UPDATED}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the managed or legacy last-shot timestamp."""
        return _get_last_shot_datetime(self.hass, self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return last-shot source diagnostics."""
        last_shot, source = _get_last_shot_datetime_with_source(self.hass, self._entry)
        return {
            "source": source,
            "last_shot": last_shot.isoformat() if last_shot is not None else None,
            "last_shot_type": _get_last_shot_type(self._entry),
        }


class GrowAssistantSoakRemainingSensor(SensorEntity):
    """Count down soak time remaining for an active crop steering phase."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        entity_description: SensorEntityDescription,
        soak_config_key: str,
        active_phase: str,
    ) -> None:
        """Initialize the soak countdown sensor."""
        self.hass = hass
        self._entry = entry
        self.entity_description = entity_description
        self._soak_config_key = soak_config_key
        self._active_phase = active_phase
        self._attr_unique_id = f"{entry.entry_id}_{entity_description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to managed last-shot updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_SHOT_UPDATED}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> int:
        """Return the remaining soak time in seconds."""
        return self._soak_state()["remaining_s"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return soak countdown debug attributes."""
        soak_state = self._soak_state()
        return {
            "phase": soak_state["phase"],
            "last_shot": soak_state["last_shot"],
            "last_shot_type": soak_state["last_shot_type"],
            "effective_soak_type": soak_state["effective_soak_type"],
            "soak_s": soak_state["soak_s"],
            "elapsed_s": soak_state["elapsed_s"],
            "active": soak_state["active"],
        }

    def _soak_state(self) -> dict[str, Any]:
        """Calculate soak countdown state and attributes."""
        return _calculate_soak_remaining(
            self.hass,
            self._entry,
            self._soak_config_key,
            self._active_phase,
        )


class GrowAssistantBlockReasonSensor(SensorEntity):
    """Explain the current crop steering state without controlling irrigation."""

    entity_description = _BLOCK_REASON_SENSOR
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the block reason sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_block_reason"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to managed last-shot updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_SHOT_UPDATED}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str:
        """Return a short reason explaining irrigation availability."""
        return _calculate_block_reason(self.hass, self._entry)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return block reason diagnostic attributes."""
        return _calculate_block_reason(self.hass, self._entry)[1]


class GrowAssistantP1DebugSensor(SensorEntity):
    """Expose detailed P1 readiness diagnostics without changing control logic."""

    entity_description = _P1_DEBUG_SENSOR
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the P1 debug sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_p1_debug"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to managed last-shot updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_SHOT_UPDATED}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str:
        """Return the current P1 readiness state."""
        return _calculate_p1_debug(self.hass, self._entry)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed P1 readiness diagnostics."""
        return _calculate_p1_debug(self.hass, self._entry)[1]


class GrowAssistantDebugSensor(SensorEntity):
    """Expose phase-independent, read-only crop steering diagnostics."""

    entity_description = _DEBUG_SENSOR
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the general debug sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_debug"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to managed last-shot updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_SHOT_UPDATED}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str:
        """Return the current crop steering phase."""
        return _calculate_phase(self.hass, self._entry)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the calculations already used by diagnostic sensors."""
        return _calculate_debug(self.hass, self._entry)


def _configured_mode(entry: ConfigEntry, config_key: str, default: str) -> str:
    """Return a configured P1/P2 mode from options, data, or a safe default."""
    for source in (entry.options, entry.data):
        mode = source.get(config_key)
        if isinstance(mode, str) and mode.lower() in MODE_OPTIONS:
            return mode.lower()

    return default


def _calculate_phase(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, dict[str, Any]]:
    """Calculate the current crop steering phase and debug attributes."""
    missing_entities: list[str] = []

    p0_s = _minutes_to_seconds(
        _get_numeric_state(
            hass,
            entry,
            CONF_P0_TRANSPIRATION_MIN,
            missing_entities,
        )
    )

    p1_s = _minutes_to_seconds(
        _get_numeric_state(
            hass,
            entry,
            CONF_P1_DURATION_MIN,
            missing_entities,
        )
    )

    p2_target = _get_numeric_state(
        hass,
        entry,
        CONF_P2_SHOTS,
        missing_entities,
    )

    p2_done = _get_numeric_state(
        hass,
        entry,
        CONF_P2_SHOTS_DONE,
        missing_entities,
    )

    p2_end_offset_s = _minutes_to_seconds(
        _get_numeric_state(
            hass,
            entry,
            CONF_P2_END_OFFSET_MIN,
            missing_entities,
        )
    )

    p1_mode = _configured_mode(entry, CONF_P1_MODE, MODE_SENSOR)
    p2_mode = _configured_mode(entry, CONF_P2_MODE, MODE_SENSOR)

    p1_active = _get_boolean_state(hass, entry, CONF_P1_ACTIVE, missing_entities)

    p1_done = _get_boolean_state(hass, entry, CONF_P1_DONE, missing_entities)
    p1_window_opened_today = _get_boolean_state(
        hass, entry, CONF_P1_WINDOW_OPENED_TODAY, missing_entities
    )

    # Optional interval helpers are read only for diagnostics / future use.
    _get_numeric_state(hass, entry, CONF_P2_INTERVAL_MIN, [])
    _get_numeric_state(hass, entry, CONF_P1_INTERVAL_MIN, [])

    timing = _light_timing(
        hass,
        configured_entity_value(entry, CONF_LED_SUNRISE),
        configured_entity_value(entry, CONF_LED_SUNSET),
        missing_entities,
    )

    led_day = None if timing is None else timing[0]
    since_on_s = None if timing is None else timing[1]
    until_off_s = None if timing is None else timing[2]

    p2_target_value = max(0, int(p2_target or 0))
    p2_done_value = max(0, int(p2_done or 0))
    p2_shots_left = max(0, p2_target_value - p2_done_value)

    p2_time_ok = (
        until_off_s is not None
        and p2_end_offset_s is not None
        and until_off_s > p2_end_offset_s
    )

    p1_window_active = (
        led_day is True
        and since_on_s is not None
        and p0_s is not None
        and p1_s is not None
        and p0_s <= since_on_s < p0_s + p1_s
    )

    debug_attributes = {
        "led_day": led_day,
        "since_on_s": since_on_s,
        "until_off_s": until_off_s,
        "p0_s": p0_s,
        "p1_s": p1_s,
        "p2_target": p2_target_value,
        "p2_done": p2_done_value,
        "p2_shots_left": p2_shots_left,
        "p2_end_offset_s": p2_end_offset_s,
        "p2_time_ok": p2_time_ok,
        "missing_entities": missing_entities,
        "p1_mode": p1_mode,
        "p2_mode": p2_mode,
        "p1_active": p1_active,
        "p1_done": p1_done,
        "p1_window_opened_today": p1_window_opened_today,
        "p1_window_active": p1_window_active,
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

    if p1_mode_value == MODE_MANUAL:
        if since_on_s < p0_s + p1_s:
            return _PHASE_P1_MORNING, debug_attributes

        if p2_available:
            return _PHASE_P2_MIDDAY, debug_attributes

        return _PHASE_P3_DRYBACK, debug_attributes

    if p1_active:
        return _PHASE_P1_MORNING, debug_attributes

    if not p1_done and p1_window_active:
        return _PHASE_P1_MORNING, debug_attributes

    if p1_done and p2_available:
        return _PHASE_P2_MIDDAY, debug_attributes

    return _PHASE_P3_DRYBACK, debug_attributes


def _calculate_soak_remaining(
    hass: HomeAssistant,
    entry: ConfigEntry,
    soak_config_key: str,
    active_phase: str,
) -> dict[str, Any]:
    """Calculate soak countdown state and attributes."""
    phase = _calculate_phase(hass, entry)[0]
    last_shot = _get_last_shot_datetime(hass, entry)
    last_shot_type = _get_last_shot_type(entry)
    soak_keys_by_type = {
        LAST_SHOT_TYPE_P1: CONF_P1_SOAK_MIN,
        LAST_SHOT_TYPE_P2: CONF_P2_SOAK_MIN,
        LAST_SHOT_TYPE_P3_EMERGENCY: CONF_P3_EMERGENCY_SOAK_MIN,
    }
    soak_types_by_key = {value: key for key, value in soak_keys_by_type.items()}
    effective_soak_type = last_shot_type or soak_types_by_key.get(soak_config_key)
    effective_soak_key = soak_keys_by_type.get(last_shot_type, soak_config_key)
    soak_s = _get_soak_seconds(hass, entry, effective_soak_key)
    active = phase == active_phase
    elapsed_s = None
    remaining_s = 0

    if last_shot is not None:
        elapsed_s = max(0, int((dt_util.now() - last_shot).total_seconds()))
        if active:
            remaining_s = max(0, soak_s - elapsed_s)

    return {
        "phase": phase,
        "last_shot": last_shot.isoformat() if last_shot is not None else None,
        "last_shot_type": last_shot_type,
        "effective_soak_type": effective_soak_type,
        "soak_s": soak_s,
        "elapsed_s": elapsed_s,
        "active": active,
        "remaining_s": remaining_s,
    }


def _calculate_p1_debug(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, dict[str, Any]]:
    """Calculate detailed P1 readiness diagnostics."""
    phase, phase_attributes = _calculate_phase(hass, entry)
    missing_entities = list(phase_attributes.get("missing_entities", []))
    _collect_missing_required_entities(hass, entry, missing_entities)

    p0_s = _minutes_to_seconds(
        _get_numeric_state(hass, entry, CONF_P0_TRANSPIRATION_MIN, missing_entities)
    )
    p1_s = _minutes_to_seconds(
        _get_numeric_state(hass, entry, CONF_P1_DURATION_MIN, missing_entities)
    )
    p1_start_vwc = _get_numeric_state(hass, entry, CONF_P1_START_VWC, missing_entities)
    field_capacity_vwc = _get_numeric_state(
        hass, entry, CONF_FIELD_CAPACITY_VWC, missing_entities
    )
    p1_shots_target_raw = _get_numeric_state(
        hass, entry, CONF_P1_SHOTS, missing_entities
    )
    p1_shots_done_raw = _get_numeric_state(
        hass, entry, CONF_P1_SHOTS_DONE, missing_entities
    )

    p1_mode = _configured_mode(entry, CONF_P1_MODE, MODE_SENSOR)
    p1_active = _get_boolean_state(hass, entry, CONF_P1_ACTIVE, missing_entities)
    p1_done = _get_boolean_state(hass, entry, CONF_P1_DONE, missing_entities)
    p1_window_opened_today = _get_boolean_state(
        hass, entry, CONF_P1_WINDOW_OPENED_TODAY, missing_entities
    )

    light_start_s = _get_time_seconds(
        hass, configured_entity_value(entry, CONF_LED_SUNRISE), []
    )
    light_end_s = _get_time_seconds(
        hass, configured_entity_value(entry, CONF_LED_SUNSET), []
    )
    now = dt_util.now()

    led_day = phase_attributes.get("led_day")
    since_on_s = phase_attributes.get("since_on_s")
    p1_window_start_s = p0_s
    p1_window_end_s = p0_s + p1_s if p0_s is not None and p1_s is not None else None
    p1_window_active = bool(
        phase_attributes.get("p1_window_active")
        or (
            led_day is True
            and since_on_s is not None
            and p1_window_start_s is not None
            and p1_window_end_s is not None
            and p1_window_start_s <= since_on_s < p1_window_end_s
        )
    )

    vwc_state = calculate_vwc_state(
        hass, configured_entity_value(entry, CONF_VWC_SENSOR)
    )
    vwc = vwc_state["vwc"]
    vwc_valid = vwc is not None and vwc_state["vwc_valid_count"] > 0
    vwc_below_start = (
        vwc is not None and p1_start_vwc is not None and vwc <= p1_start_vwc
    )
    vwc_below_field_capacity = (
        vwc is not None and field_capacity_vwc is not None and vwc < field_capacity_vwc
    )

    p1_soak_state = _calculate_soak_remaining(
        hass, entry, CONF_P1_SOAK_MIN, _PHASE_P1_MORNING
    )
    p1_soak_remaining_s = p1_soak_state["remaining_s"]
    soak_ok = p1_soak_remaining_s == 0

    p1_shots_done = max(0, int(p1_shots_done_raw or 0))
    p1_shots_target = max(0, int(p1_shots_target_raw or 0))
    p1_shots_left = max(0, p1_shots_target - p1_shots_done)

    drain_sensor = _get_optional_binary_sensor_state(hass, entry, CONF_DRAIN_SENSOR)
    drain_tray_sensor = _get_optional_binary_sensor_state(
        hass, entry, CONF_DRAIN_TRAY_SENSOR
    )
    drain_wet = drain_sensor["wet"]
    drain_tray_wet = drain_tray_sensor["wet"]

    missing_entities = _deduplicate_missing_entities(missing_entities, entry)
    blocking_reasons: list[str] = []
    passed_conditions: list[str] = []

    if missing_entities:
        blocking_reasons.append("missing_required_entities")

    if p1_done:
        blocking_reasons.append("p1_already_done")

    if p1_active:
        passed_conditions.append("p1_already_active")
    else:
        if led_day is False:
            blocking_reasons.append("led_day_false")
        if not p1_window_active:
            blocking_reasons.append("p1_window_not_active")
        if p1_mode == MODE_MANUAL:
            blocking_reasons.append("p1_mode_manual_waiting")
        if p1_window_opened_today:
            blocking_reasons.append("p1_window_already_opened_today")

    if vwc_valid:
        passed_conditions.append("vwc_valid")
    else:
        blocking_reasons.append("vwc_invalid")

    if p1_start_vwc is None:
        blocking_reasons.append("p1_start_vwc_missing")
    elif vwc_below_start:
        passed_conditions.append("vwc_below_start_threshold")
    elif vwc is not None:
        blocking_reasons.append("vwc_above_start_threshold")

    if field_capacity_vwc is None:
        blocking_reasons.append("field_capacity_missing")
    elif vwc_below_field_capacity:
        passed_conditions.append("vwc_below_field_capacity")
    elif vwc is not None:
        blocking_reasons.append("field_capacity_reached")

    if soak_ok:
        passed_conditions.append("soak_finished")
    else:
        blocking_reasons.append("soak_not_finished")

    if p1_shots_left > 0:
        passed_conditions.append("p1_shots_available")
    else:
        blocking_reasons.append("p1_shot_limit_reached")

    if drain_sensor["configured"] and not drain_sensor["available"]:
        blocking_reasons.append("drain_sensor_unavailable")
    elif drain_wet:
        blocking_reasons.append("drain_sensor_wet")
    else:
        passed_conditions.append("drain_sensor_clear_or_ignored")

    if drain_tray_sensor["configured"] and not drain_tray_sensor["available"]:
        blocking_reasons.append("drain_tray_unavailable")
    elif drain_tray_wet:
        blocking_reasons.append("drain_tray_wet")
    else:
        passed_conditions.append("drain_tray_clear_or_ignored")

    if missing_entities:
        state = "missing_required"
    elif p1_active:
        state = "active"
        blocking_reasons = [
            reason
            for reason in blocking_reasons
            if reason
            in {
                "vwc_invalid",
                "p1_start_vwc_missing",
                "vwc_above_start_threshold",
                "field_capacity_missing",
                "field_capacity_reached",
                "soak_not_finished",
                "p1_shot_limit_reached",
                "drain_sensor_wet",
                "drain_sensor_unavailable",
                "drain_tray_wet",
                "drain_tray_unavailable",
            }
        ]
    elif p1_done:
        state = "complete"
    elif not p1_window_active:
        state = "inactive_window"
    elif blocking_reasons:
        state = "blocked"
    else:
        state = "ready"

    attributes = {
        "phase": phase,
        "led_day": led_day,
        "now": now.isoformat(),
        "light_start": _format_time_seconds(light_start_s),
        "light_end": _format_time_seconds(light_end_s),
        "since_on_s": since_on_s,
        "p0_s": p0_s,
        "p1_s": p1_s,
        "p1_window_start_s": p1_window_start_s,
        "p1_window_end_s": p1_window_end_s,
        "p1_window_active": p1_window_active,
        "p1_mode": p1_mode,
        "p1_active": p1_active,
        "p1_done": p1_done,
        "p1_window_opened_today": p1_window_opened_today,
        "vwc": vwc,
        "vwc_valid": vwc_valid,
        "vwc_sensors": vwc_state["vwc_sensors"],
        "vwc_values": vwc_state["vwc_values"],
        "p1_start_vwc": p1_start_vwc,
        "field_capacity_vwc": field_capacity_vwc,
        "vwc_below_start": vwc_below_start,
        "vwc_below_field_capacity": vwc_below_field_capacity,
        "last_shot": p1_soak_state["last_shot"],
        "p1_soak_remaining_s": p1_soak_remaining_s,
        "soak_ok": soak_ok,
        "p1_shots_done": p1_shots_done,
        "p1_shots_target": p1_shots_target,
        "p1_shots_left": p1_shots_left,
        "drain_sensor_configured": drain_sensor["configured"],
        "drain_sensor_ignored": not drain_sensor["configured"],
        "drain_sensor_available": drain_sensor["available"],
        "drain_sensor_state": drain_sensor["state"],
        "drain_wet": drain_wet,
        "drain_tray_sensor_configured": drain_tray_sensor["configured"],
        "drain_tray_sensor_ignored": not drain_tray_sensor["configured"],
        "drain_tray_sensor_available": drain_tray_sensor["available"],
        "drain_tray_sensor_state": drain_tray_sensor["state"],
        "drain_tray_wet": drain_tray_wet,
        "missing_entities": missing_entities,
        "blocking_reasons": blocking_reasons,
        "passed_conditions": passed_conditions,
    }

    return state, attributes


def _calculate_p3_emergency(
    hass: HomeAssistant, entry: ConfigEntry, phase: str | None = None
) -> dict[str, Any]:
    """Calculate fail-closed P3 emergency-shot readiness without pump control."""
    phase = phase or _calculate_phase(hass, entry)[0]
    enabled = _get_boolean_state(hass, entry, CONF_P3_EMERGENCY_ENABLED, [])
    threshold = _get_numeric_state(hass, entry, CONF_P3_EMERGENCY_THRESHOLD_VWC, [])
    duration = _get_numeric_state(hass, entry, CONF_P3_EMERGENCY_SHOT_DURATION_S, [])
    soak_min = _get_numeric_state(hass, entry, CONF_P3_EMERGENCY_SOAK_MIN, [])
    done_raw = _get_numeric_state(hass, entry, CONF_P3_EMERGENCY_SHOTS_DONE, [])
    max_raw = _get_numeric_state(hass, entry, CONF_P3_EMERGENCY_MAX_SHOTS, [])
    done = max(0, int(done_raw or 0))
    maximum = max(0, int(max_raw or 0))
    left = max(0, maximum - done)
    soak_remaining = _calculate_soak_remaining(
        hass, entry, CONF_P3_EMERGENCY_SOAK_MIN, _PHASE_P3_DRYBACK
    )["remaining_s"]
    vwc_state = calculate_vwc_state(
        hass, configured_entity_value(entry, CONF_VWC_SENSOR)
    )
    vwc = vwc_state["vwc"]
    vwc_valid = vwc is not None and vwc_state["vwc_valid_count"] > 0
    tray = _get_optional_binary_sensor_state(hass, entry, CONF_DRAIN_TRAY_SENSOR)
    pump_entity = configured_entity_value(entry, CONF_PUMP_SWITCH)
    pump_state = hass.states.get(pump_entity) if pump_entity else None
    pump_off = pump_state is not None and pump_state.state == "off"

    if phase != _PHASE_P3_DRYBACK:
        status = "p3_emergency_phase_inactive"
    elif not enabled:
        status = "p3_emergency_disabled"
    elif not vwc_valid or threshold is None:
        status = "p3_emergency_vwc_invalid"
    elif vwc > threshold:
        status = "p3_emergency_vwc_above_threshold"
    elif soak_remaining > 0:
        status = "p3_emergency_soak_active"
    elif done >= maximum:
        status = "p3_emergency_shot_limit_reached"
    elif duration is None or not math.isfinite(duration) or duration <= 0:
        status = "p3_emergency_shot_duration_invalid"
    elif not tray["configured"] or not tray["available"]:
        status = "p3_emergency_drain_tray_unavailable"
    elif tray["wet"]:
        status = "p3_emergency_drain_tray_wet"
    elif not pump_off:
        status = "p3_emergency_pump_not_off"
    else:
        status = "p3_emergency_ready"

    return {
        "p3_emergency_enabled": enabled,
        "p3_emergency_threshold_vwc": threshold,
        "p3_emergency_shot_duration_s": duration,
        "p3_emergency_soak_min": soak_min,
        "p3_emergency_soak_remaining_s": soak_remaining,
        "p3_emergency_shots_done": done,
        "p3_emergency_max_shots": maximum,
        "p3_emergency_shots_left": left,
        "p3_emergency_ready": status == "p3_emergency_ready",
        "p3_emergency_status": status,
        "p3_emergency_vwc": vwc,
        "p3_emergency_vwc_valid": vwc_valid,
        "p3_emergency_pump_off": pump_off,
        "p3_emergency_drain_tray_configured": tray["configured"],
        "p3_emergency_drain_tray_available": tray["available"],
        "p3_emergency_drain_tray_wet": tray["wet"],
    }


def _calculate_debug(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Combine existing calculations with diagnostics for the current phase."""
    phase, phase_attributes = _calculate_phase(hass, entry)
    _p1_state, p1_attributes = _calculate_p1_debug(hass, entry)
    block_reason, block_attributes = _calculate_block_reason(hass, entry)
    last_shot, last_shot_source = _get_last_shot_datetime_with_source(hass, entry)
    p3_emergency = _calculate_p3_emergency(hass, entry, phase)

    p2_shots_done = block_attributes["p2_done"]
    p2_shots_target = block_attributes["p2_target"]
    phase_diagnostics = _calculate_phase_diagnostics(
        phase,
        phase_attributes,
        p1_attributes,
        {
            **block_attributes,
            "p2_time_ok": phase_attributes.get("p2_time_ok"),
        },
    )
    attributes = {
        **p1_attributes,
        **phase_diagnostics,
        **p3_emergency,
        "phase": phase,
        "block_reason": block_reason,
        "until_off_s": phase_attributes.get("until_off_s"),
        "vwc_valid_count": block_attributes["vwc_valid_count"],
        "vwc_average": block_attributes["vwc_average"],
        "vwc_cap_active": block_attributes["vwc_cap_active"],
        "p2_mode": block_attributes["p2_mode"],
        "p2_ref_vwc": block_attributes["p2_ref_vwc"],
        "p2_vwc_drop": block_attributes["p2_vwc_drop"],
        "p2_drop_threshold": block_attributes["p2_drop_threshold"],
        "p2_shots_done": p2_shots_done,
        "p2_shots_target": p2_shots_target,
        "p2_shots_left": max(0, p2_shots_target - p2_shots_done),
        "p2_soak_remaining_s": block_attributes["p2_soak_remaining_s"],
        "p2_end_offset_s": phase_attributes.get("p2_end_offset_s"),
        "p2_time_ok": phase_attributes.get("p2_time_ok"),
        "last_shot": last_shot.isoformat() if last_shot is not None else None,
        "last_shot_source": last_shot_source,
        "drain_sensor_configured": block_attributes["drain_sensor_configured"],
        "drain_sensor_entity_id": block_attributes["drain_sensor_entity_id"],
        "drain_sensor_available": block_attributes["drain_sensor_available"],
        "drain_sensor_state": block_attributes["drain_sensor_state"],
        "drain_sensor_ignored": block_attributes["drain_sensor_ignored"],
        "drain_wet": block_attributes["drain_wet"],
        "drain_tray_sensor_configured": block_attributes[
            "drain_tray_sensor_configured"
        ],
        "drain_tray_sensor_entity_id": block_attributes["drain_tray_sensor_entity_id"],
        "drain_tray_sensor_available": block_attributes["drain_tray_sensor_available"],
        "drain_tray_sensor_state": block_attributes["drain_tray_sensor_state"],
        "drain_tray_sensor_ignored": block_attributes["drain_tray_sensor_ignored"],
        "drain_tray_wet": block_attributes["drain_tray_wet"],
        "optional_unavailable_entities": block_attributes[
            "optional_unavailable_entities"
        ],
    }

    return attributes


def _calculate_phase_diagnostics(
    phase: str,
    phase_attributes: dict[str, Any],
    p1_attributes: dict[str, Any],
    block_attributes: dict[str, Any],
) -> dict[str, Any]:
    """Describe only the conditions relevant to the current phase.

    This deliberately derives its answers from the existing read-only phase, P1,
    and block-reason calculations.  It does not participate in phase selection or
    irrigation control.
    """
    blocking: list[str] = []
    passed: list[str] = []
    missing = block_attributes.get("missing_entities", [])
    led_day = phase_attributes.get("led_day")

    if missing:
        blocking.append("required_entity_unavailable")

    if phase in {_PHASE_OFF, _PHASE_PRE_ON, _PHASE_P0_TRANSPIRATION}:
        if led_day is True:
            passed.append("led_day_true")
        else:
            blocking.append("led_day_false")
        if phase == _PHASE_P0_TRANSPIRATION:
            blocking.append("p0_transpiration_active")
            phase_reason = "p0_transpiration_active"
        elif missing:
            phase_reason = "required_entity_unavailable"
        elif led_day is False:
            phase_reason = "light_cycle_ended"
        else:
            phase_reason = "pre_on"
        return {
            "phase_reason": phase_reason,
            "blocking_reasons": blocking,
            "passed_conditions": passed,
        }

    if phase == _PHASE_P1_MORNING:
        if p1_attributes.get("p1_active"):
            passed.append("p1_already_active")
        else:
            start_checks = (
                (
                    p1_attributes.get("p1_mode") == MODE_SENSOR,
                    "p1_mode_sensor",
                    "p1_mode_manual",
                ),
                (led_day is True, "led_day_true", "led_day_false"),
                (
                    p1_attributes.get("p1_window_active") is True,
                    "p1_window_active",
                    "p1_window_not_active",
                ),
                (
                    not p1_attributes.get("p1_done"),
                    "p1_not_done",
                    "p1_already_done",
                ),
                (
                    not p1_attributes.get("p1_window_opened_today"),
                    "p1_window_available",
                    "p1_window_already_opened_today",
                ),
            )
            for condition, passed_name, blocked_name in start_checks:
                (passed if condition else blocking).append(
                    passed_name if condition else blocked_name
                )

        shot_checks = (
            (p1_attributes.get("vwc_valid") is True, "vwc_valid", "vwc_invalid"),
            (
                p1_attributes.get("vwc_below_start") is True,
                "vwc_below_start_threshold",
                "vwc_above_start_threshold",
            ),
            (
                p1_attributes.get("vwc_below_field_capacity") is True,
                "vwc_below_field_capacity",
                "field_capacity_reached",
            ),
            (
                p1_attributes.get("soak_ok") is True,
                "soak_finished",
                "soak_not_finished",
            ),
            (
                p1_attributes.get("p1_shots_left", 0) > 0,
                "shot_limit_available",
                "p1_shot_limit_reached",
            ),
        )
        for condition, passed_name, blocked_name in shot_checks:
            (passed if condition else blocking).append(
                passed_name if condition else blocked_name
            )
        _append_drain_diagnostics(block_attributes, blocking, passed)
        return {
            "phase_reason": "p1_morning_active",
            "blocking_reasons": blocking,
            "passed_conditions": passed,
        }

    p2_blocking, p2_passed = _calculate_p2_diagnostics(
        led_day, p1_attributes, block_attributes
    )
    blocking.extend(p2_blocking)

    if phase == _PHASE_P2_MIDDAY:
        passed.extend(p2_passed)
        phase_reason = blocking[0] if blocking else "p2_midday_active"
    else:
        passed.append("p3_dryback_active")
        if led_day is False:
            passed.append("led_day_false")
            # Once the light cycle has ended, P2 readiness is no longer relevant.
            blocking = [
                reason for reason in blocking if reason == "required_entity_unavailable"
            ]
            phase_reason = "light_cycle_ended"
        else:
            passed.extend(p2_passed)
            phase_reason = _calculate_daytime_p3_phase_reason(
                phase_attributes, p1_attributes, block_attributes
            )

    return {
        "phase_reason": phase_reason,
        "blocking_reasons": blocking,
        "passed_conditions": passed,
    }


def _calculate_daytime_p3_phase_reason(
    phase_attributes: dict[str, Any],
    p1_attributes: dict[str, Any],
    block_attributes: dict[str, Any],
) -> str:
    """Explain daytime P3 using only conditions that select the phase."""
    p1_mode = p1_attributes.get("p1_mode")

    # In sensor mode, an incomplete P1 selects P3 after its eligibility window
    # closes. P2 shot-readiness conditions do not participate in that decision.
    if p1_mode != MODE_MANUAL and not p1_attributes.get("p1_done"):
        if not p1_attributes.get("p1_window_active"):
            return "p1_window_ended_without_completion"
        return "p1_not_done"

    # Both manual progression and a completed sensor-mode P1 use these same two
    # P2 availability gates in _calculate_phase(). Keep their ordering stable if
    # both are false; neither operational shot-readiness nor P2 mode selects P3.
    p2_target = block_attributes.get("p2_target", 0)
    p2_done = block_attributes.get("p2_done", 0)
    if p2_target <= 0 or p2_done >= p2_target:
        return "p2_shot_limit_reached"
    if phase_attributes.get("p2_time_ok") is False:
        return "p2_end_offset_reached"

    return "p3_dryback_active"


def _calculate_p2_diagnostics(
    led_day: bool | None,
    p1_attributes: dict[str, Any],
    block_attributes: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return P2 readiness conditions without adding interval-based gating."""
    blocking: list[str] = []
    passed: list[str] = []
    p2_mode = block_attributes.get("p2_mode")
    vwc_valid = block_attributes.get("vwc_valid_count", 0) > 0
    reference_available = (block_attributes.get("p2_ref_vwc") or 0) > 0
    drop_threshold = block_attributes.get("p2_drop_threshold")
    vwc = block_attributes.get("vwc")
    drop_reached = (
        vwc is not None and drop_threshold is not None and vwc <= drop_threshold
    )

    checks = (
        (p2_mode == MODE_SENSOR, "p2_mode_sensor", "p2_mode_manual"),
        (led_day is True, "led_day_true", "led_day_false"),
        (p1_attributes.get("p1_done") is True, "p1_done", "p1_not_done"),
        (reference_available, "p2_reference_available", "p2_reference_missing"),
        (
            block_attributes.get("p2_done", 0) < block_attributes.get("p2_target", 0),
            "p2_shot_limit_available",
            "p2_shot_limit_reached",
        ),
        (
            block_attributes.get("p2_time_ok") is True,
            "p2_time_window_open",
            "p2_end_offset_reached",
        ),
        (vwc_valid, "vwc_valid", "vwc_invalid"),
        (drop_reached, "vwc_drop_reached", "p2_vwc_drop_not_reached"),
        (not block_attributes.get("vwc_cap_active"), "vwc_cap_clear", "vwc_cap_active"),
        (
            block_attributes.get("p2_soak_remaining_s", 0) == 0,
            "soak_finished",
            "p2_soak_active",
        ),
    )
    for condition, passed_name, blocked_name in checks:
        (passed if condition else blocking).append(
            passed_name if condition else blocked_name
        )
    _append_drain_diagnostics(
        block_attributes, blocking, passed, check_normal_sensor=False
    )
    return blocking, passed


def _append_drain_diagnostics(
    attributes: dict[str, Any],
    blocking: list[str],
    passed: list[str],
    *,
    check_normal_sensor: bool = True,
) -> None:
    """Append phase-aware drain diagnostics, keeping the tray fail-closed."""
    if check_normal_sensor:
        sensors = (
            ("drain_sensor", "drain_sensor", "drain_wet"),
            ("drain_tray_sensor", "drain_tray", "drain_tray_wet"),
        )
    else:
        sensors = (("drain_tray_sensor", "drain_tray", "drain_tray_wet"),)
        if attributes.get("drain_sensor_configured") and not attributes.get(
            "drain_sensor_available"
        ):
            passed.append("drain_sensor_unavailable_ignored_in_p2")
        elif attributes.get("drain_wet"):
            passed.append("drain_sensor_wet_ignored_in_p2")
        else:
            passed.append("drain_sensor_clear_or_ignored")

    for prefix, condition_name, wet_key in sensors:
        if attributes.get(f"{prefix}_configured") and not attributes.get(
            f"{prefix}_available"
        ):
            blocking.append(f"{condition_name}_unavailable")
        elif attributes.get(wet_key):
            blocking.append(f"{condition_name}_wet")
        else:
            passed.append(f"{condition_name}_clear_or_ignored")


def _calculate_block_reason(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, dict[str, Any]]:
    """Calculate the read-only irrigation block reason and diagnostics."""
    phase, phase_attributes = _calculate_phase(hass, entry)
    missing_entities = list(phase_attributes.get("missing_entities", []))
    _collect_missing_required_entities(hass, entry, missing_entities)

    vwc_state = calculate_vwc_state(
        hass, configured_entity_value(entry, CONF_VWC_SENSOR)
    )
    vwc = vwc_state["vwc"]
    p1_mode = _configured_mode(entry, CONF_P1_MODE, MODE_SENSOR)
    p2_mode = _configured_mode(entry, CONF_P2_MODE, MODE_SENSOR)
    p1_start_vwc = _get_numeric_state(
        hass,
        entry,
        CONF_P1_START_VWC,
        missing_entities,
    )
    field_capacity_vwc = _get_numeric_state(
        hass,
        entry,
        CONF_FIELD_CAPACITY_VWC,
        missing_entities,
    )
    p2_ref_vwc = _get_numeric_state(
        hass,
        entry,
        CONF_P2_REF_VWC,
        missing_entities,
    )
    p2_vwc_drop = _get_numeric_state(
        hass,
        entry,
        CONF_P2_VWC_DROP,
        missing_entities,
    )
    p2_target_raw = _get_numeric_state(
        hass,
        entry,
        CONF_P2_SHOTS,
        missing_entities,
    )
    p1_done_raw = _get_numeric_state(
        hass,
        entry,
        CONF_P1_SHOTS_DONE,
        missing_entities,
    )
    p2_done_raw = _get_numeric_state(
        hass,
        entry,
        CONF_P2_SHOTS_DONE,
        missing_entities,
    )
    _get_numeric_state(
        hass,
        entry,
        CONF_P2_END_OFFSET_MIN,
        missing_entities,
    )

    drain_sensor = _get_optional_binary_sensor_state(hass, entry, CONF_DRAIN_SENSOR)
    drain_tray_sensor = _get_optional_binary_sensor_state(
        hass,
        entry,
        CONF_DRAIN_TRAY_SENSOR,
    )
    drain_wet = drain_sensor["wet"]
    drain_tray_wet = drain_tray_sensor["wet"]
    vwc_cap = _get_optional_numeric_state(hass, entry, CONF_VWC_CAP)
    vwc_cap_active = vwc is not None and vwc_cap is not None and vwc >= vwc_cap

    p1_soak_remaining_s = _calculate_soak_remaining(
        hass,
        entry,
        CONF_P1_SOAK_MIN,
        _PHASE_P1_MORNING,
    )["remaining_s"]
    p2_soak_remaining_s = _calculate_soak_remaining(
        hass,
        entry,
        CONF_P2_SOAK_MIN,
        _PHASE_P2_MIDDAY,
    )["remaining_s"]

    p1_done_count = max(0, int(p1_done_raw or 0))
    p2_target = max(0, int(p2_target_raw or 0))
    p2_done = max(0, int(p2_done_raw or 0))
    p2_drop_threshold = (
        p2_ref_vwc - p2_vwc_drop
        if p2_ref_vwc is not None and p2_vwc_drop is not None
        else None
    )
    p2_time_ok = phase_attributes.get("p2_time_ok")

    missing_entities = _deduplicate_missing_entities(missing_entities, entry)

    attributes = {
        "phase": phase,
        "vwc": vwc,
        "vwc_sensors": vwc_state["vwc_sensors"],
        "vwc_values": vwc_state["vwc_values"],
        "vwc_valid_count": vwc_state["vwc_valid_count"],
        "vwc_average": vwc_state["vwc_average"],
        "p1_mode": p1_mode,
        "p2_mode": p2_mode,
        "p1_start_vwc": p1_start_vwc,
        "field_capacity_vwc": field_capacity_vwc,
        "p2_ref_vwc": p2_ref_vwc,
        "p2_vwc_drop": p2_vwc_drop,
        "p2_drop_threshold": p2_drop_threshold,
        "p1_done_count": p1_done_count,
        "p2_target": p2_target,
        "p2_done": p2_done,
        "p1_soak_remaining_s": p1_soak_remaining_s,
        "p2_soak_remaining_s": p2_soak_remaining_s,
        "drain": drain_wet,
        "drain_wet": drain_wet,
        "drain_sensor_configured": drain_sensor["configured"],
        "drain_sensor_entity_id": drain_sensor["entity_id"],
        "drain_sensor_state": drain_sensor["state"],
        "drain_sensor_available": drain_sensor["available"],
        "drain_sensor_ignored": not drain_sensor["configured"],
        "drain_tray_wet": drain_tray_wet,
        "drain_tray_sensor_configured": drain_tray_sensor["configured"],
        "drain_tray_sensor_entity_id": drain_tray_sensor["entity_id"],
        "drain_tray_sensor_state": drain_tray_sensor["state"],
        "drain_tray_sensor_available": drain_tray_sensor["available"],
        "drain_tray_sensor_ignored": not drain_tray_sensor["configured"],
        "optional_unavailable_entities": [
            sensor_state["entity_id"]
            for sensor_state in (drain_sensor, drain_tray_sensor)
            if sensor_state["configured"] and not sensor_state["available"]
        ],
        "vwc_cap_active": vwc_cap_active,
        "missing_entities": missing_entities,
    }

    if missing_entities:
        return "missing required entity", attributes

    p1_mode_value = (p1_mode or "").lower()
    p2_mode_value = (p2_mode or "").lower()

    if phase == _PHASE_OFF:
        return "off", attributes

    if phase == _PHASE_PRE_ON:
        return "off", attributes

    if phase == _PHASE_P3_DRYBACK:
        if phase_attributes.get("led_day"):
            if p2_mode_value == MODE_MANUAL:
                return "P2 blocked: mode is manual", attributes

            if p2_ref_vwc is None or p2_ref_vwc <= 0:
                return "P2 blocked: no reference VWC", attributes

            if p2_done >= p2_target:
                return "P2 blocked: shot limit reached", attributes

            if p2_time_ok is False:
                return "P2 blocked: end offset reached", attributes

            if vwc_cap_active:
                return "P2 blocked: VWC cap active", attributes

            if p2_soak_remaining_s > 0:
                return "P2 blocked: soak active", attributes

            if drain_tray_sensor["configured"] and not drain_tray_sensor["available"]:
                return "P2 blocked: drain tray unavailable", attributes

            if drain_tray_wet:
                return "P2 blocked: drain tray wet", attributes

            if (
                vwc is not None
                and p2_drop_threshold is not None
                and vwc > p2_drop_threshold
            ):
                return "P2 blocked: VWC drop not reached", attributes

        return "P3 dryback active", attributes

    if phase == _PHASE_P0_TRANSPIRATION:
        return "P0 transpiration active", attributes

    if phase == _PHASE_P1_MORNING:
        if p1_mode_value == MODE_MANUAL:
            return "P1 blocked: mode is manual", attributes

        if p1_soak_remaining_s > 0:
            return "P1 blocked: soak active", attributes

        if drain_sensor["configured"] and not drain_sensor["available"]:
            return "P1 blocked: drain sensor unavailable", attributes

        if drain_wet:
            return "P1 blocked: drain sensor wet", attributes

        if drain_tray_sensor["configured"] and not drain_tray_sensor["available"]:
            return "P1 blocked: drain tray unavailable", attributes

        if drain_tray_wet:
            return "P1 blocked: drain tray wet", attributes

        if (
            vwc is not None
            and field_capacity_vwc is not None
            and vwc >= field_capacity_vwc
        ):
            return "P1 complete: field capacity reached", attributes

        if vwc is not None and p1_start_vwc is not None and vwc > p1_start_vwc:
            return "P1 blocked: VWC above start threshold", attributes

        return "P1 ready", attributes

    if phase == _PHASE_P2_MIDDAY:
        if p2_mode_value == MODE_MANUAL:
            return "P2 blocked: mode is manual", attributes

        if p2_ref_vwc is None or p2_ref_vwc <= 0:
            return "P2 blocked: no reference VWC", attributes

        if p2_done >= p2_target:
            return "P2 blocked: shot limit reached", attributes

        if p2_time_ok is False:
            return "P2 blocked: end offset reached", attributes

        if vwc_cap_active:
            return "P2 blocked: VWC cap active", attributes

        if p2_soak_remaining_s > 0:
            return "P2 blocked: soak active", attributes

        if drain_tray_sensor["configured"] and not drain_tray_sensor["available"]:
            return "P2 blocked: drain tray unavailable", attributes

        if drain_tray_wet:
            return "P2 blocked: drain tray wet", attributes

        if (
            vwc is not None
            and p2_drop_threshold is not None
            and vwc > p2_drop_threshold
        ):
            return "P2 blocked: VWC drop not reached", attributes

        return "P2 ready", attributes

    return "off", attributes


def _collect_missing_required_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    missing_entities: list[str],
) -> None:
    """Append missing required block reason entities before detailed reads."""
    for key in _REQUIRED_BLOCK_REASON_KEYS:
        entity_id = configured_entity_value(entry, key)
        if key == CONF_VWC_SENSOR:
            vwc_sensors = normalize_vwc_sensors(entity_id)
            if not vwc_sensors:
                missing_entities.append(key)
                continue

            vwc_state = calculate_vwc_state(hass, entity_id)
            if vwc_state["vwc_valid_count"] == 0:
                missing_entities.extend(vwc_sensors)
            continue

        if entity_id is None:
            missing_entities.append(key)
            continue

        state = hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            missing_entities.append(entity_id)


def _configured_required_entities(entry: ConfigEntry) -> set[str]:
    """Return configured required entity identifiers, flattening list values."""
    configured_required = set(_REQUIRED_BLOCK_REASON_KEYS)

    for key in (*_REQUIRED_BLOCK_REASON_KEYS, *NUMERIC_SETTING_KEYS):
        value = configured_entity_value(entry, key)
        if key == CONF_VWC_SENSOR:
            configured_required.update(normalize_vwc_sensors(value))
        elif isinstance(value, str) and value:
            configured_required.add(value)
        elif value is None:
            configured_required.add(key)

    return configured_required


def _deduplicate_missing_entities(
    missing_entities: list[str],
    entry: ConfigEntry,
) -> list[str]:
    """Return unique missing required entity identifiers."""
    configured_required = _configured_required_entities(entry)
    deduplicated: list[str] = []

    for entity_id in missing_entities:
        if entity_id in configured_required and entity_id not in deduplicated:
            deduplicated.append(entity_id)

    return deduplicated


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


def _get_boolean_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_key: str,
    missing_entities: list[str],
) -> bool | None:
    """Return a managed boolean state, falling back to a legacy helper."""
    managed_value = entry.options.get(config_key)
    if managed_value is not None:
        if isinstance(managed_value, bool):
            return managed_value

        if isinstance(managed_value, str):
            normalized = managed_value.lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False

        if isinstance(managed_value, int):
            return bool(managed_value)

        missing_entities.append(config_key)
        return None

    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id:
        return _get_bool_state(hass, entity_id, missing_entities)

    return BOOLEAN_STATE_DEFAULTS.get(config_key, False)


def _configured_optional_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a configured optional entity ID from options or entry data."""
    entity_id = configured_entity_value(entry, config_key)
    if isinstance(entity_id, str) and entity_id:
        return entity_id

    return None


def _get_optional_binary_sensor_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_key: str,
) -> dict[str, Any]:
    """Return optional binary sensor diagnostics without marking it required."""
    entity_id = _configured_optional_entity_id(entry, config_key)
    if entity_id is None:
        return {
            "configured": False,
            "entity_id": None,
            "state": None,
            "available": True,
            "wet": False,
        }

    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return {
            "configured": True,
            "entity_id": entity_id,
            "state": None if state is None else state.state,
            "available": False,
            "wet": False,
        }

    normalized_state = state.state.lower()
    wet = normalized_state in _OPTIONAL_BINARY_WET_STATES
    clear = normalized_state in _OPTIONAL_BINARY_CLEAR_STATES

    return {
        "configured": True,
        "entity_id": entity_id,
        "state": state.state,
        "available": clear or wet,
        "wet": wet,
    }


def _get_numeric_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_key: str,
    missing_entities: list[str],
) -> float | None:
    """Return a managed numeric setting, falling back to a legacy helper."""
    managed_value = entry.options.get(config_key)
    if managed_value is not None:
        try:
            return float(managed_value)
        except (TypeError, ValueError):
            missing_entities.append(config_key)
            return None

    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id:
        return _get_float_state(hass, entity_id, missing_entities)

    return NUMERIC_SETTING_DEFAULTS.get(config_key)


def _get_optional_numeric_state(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str
) -> float | None:
    """Return an optional managed numeric state without marking it missing."""
    return _get_numeric_state(hass, entry, config_key, [])


def _minutes_to_seconds(value: float | None) -> int | None:
    """Convert minutes to seconds."""
    if value is None:
        return None

    return max(0, int(value * 60))


def _get_soak_seconds(hass: HomeAssistant, entry: ConfigEntry, config_key: str) -> int:
    """Return configured soak seconds, defaulting to five minutes if invalid."""
    soak_min = _get_numeric_state(hass, entry, config_key, [])
    soak_s = _minutes_to_seconds(soak_min)
    return _DEFAULT_SOAK_SECONDS if soak_s is None else soak_s


def _get_last_shot_datetime(hass: HomeAssistant, entry: ConfigEntry) -> datetime | None:
    """Return managed last-shot timestamp, falling back to a legacy helper."""
    last_shot, _source = _get_last_shot_datetime_with_source(hass, entry)
    return last_shot


def _get_last_shot_type(entry: ConfigEntry) -> str | None:
    """Return a valid managed last-shot type without inferring historical state."""
    value = entry.options.get(CONF_LAST_SHOT_TYPE)
    return value if value in LAST_SHOT_TYPES else None


def _get_last_shot_datetime_with_source(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[datetime | None, str | None]:
    """Return last-shot timestamp and whether it came from managed or legacy storage."""
    if CONF_LAST_SHOT in entry.options:
        return _parse_managed_datetime(entry.options.get(CONF_LAST_SHOT)), "managed"

    entity_id = entry.data.get(CONF_LAST_SHOT)
    if isinstance(entity_id, str) and entity_id:
        return _get_datetime_state(hass, entity_id), "legacy"

    return None, None


def _parse_managed_datetime(value: Any) -> datetime | None:
    """Parse a managed ISO datetime from config entry options."""
    if not isinstance(value, str) or not value:
        return None

    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    return parsed


def _get_datetime_state(hass: HomeAssistant, entity_id: str | None) -> datetime | None:
    """Return an input_datetime state as an aware datetime if available."""
    if entity_id is None:
        return None

    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None

    timestamp = state.attributes.get("timestamp")
    if timestamp is not None:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    parsed = dt_util.parse_datetime(state.state)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(state.state)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    return parsed


def _light_timing(
    hass: HomeAssistant,
    sunrise_entity_id: str | None,
    sunset_entity_id: str | None,
    missing_entities: list[str],
) -> tuple[bool, int, int] | None:
    """Return calculated light state plus seconds since on and until off."""
    sunrise_s = _get_time_seconds(hass, sunrise_entity_id, missing_entities)
    sunset_s = _get_time_seconds(hass, sunset_entity_id, missing_entities)

    if sunrise_s is None or sunset_s is None:
        return None

    now = dt_util.now()
    now_s = _seconds_since_midnight(now)

    if sunrise_s == sunset_s:
        return True, 0, 24 * 60 * 60

    if sunset_s > sunrise_s:
        led_day = sunrise_s <= now_s < sunset_s
        return led_day, now_s - sunrise_s, sunset_s - now_s

    led_day = now_s >= sunrise_s or now_s < sunset_s
    if now_s >= sunrise_s:
        return led_day, now_s - sunrise_s, sunset_s + 24 * 60 * 60 - now_s

    return led_day, now_s + 24 * 60 * 60 - sunrise_s, sunset_s - now_s


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


def _format_time_seconds(value: int | None) -> str | None:
    """Return a HH:MM:SS string for seconds since midnight."""
    if value is None:
        return None

    normalized = value % (24 * 60 * 60)
    hour = normalized // 3600
    minute = (normalized % 3600) // 60
    second = normalized % 60
    return f"{hour:02}:{minute:02}:{second:02}"


def _seconds_since_midnight(value: datetime) -> int:
    """Return seconds since local midnight for a datetime."""
    return value.hour * 3600 + value.minute * 60 + value.second
