"""Sensor platform for GrowAssistant Crop Steering."""

from __future__ import annotations

from datetime import datetime, timezone
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONFIG_ENTRY_KEYS,
    CONF_LAST_SHOT,
    CONF_LED_DAY_SENSOR,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P0_TRANSPIRATION_MIN,
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_FIELD_CAPACITY_VWC,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_DURATION_MIN,
    CONF_P1_INTERVAL_MIN,
    CONF_P1_MODE,
    CONF_P1_SOAK_MIN,
    CONF_P1_START_VWC,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_END_OFFSET_MIN,
    CONF_P2_INTERVAL_MIN,
    CONF_P2_MODE,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS,
    CONF_P2_SOAK_MIN,
    CONF_P2_SHOTS_DONE,
    CONF_P2_VWC_DROP,
    CONF_VWC_CAP,
    CONF_VWC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
    NUMERIC_SETTING_DEFAULTS,
    NUMERIC_SETTING_KEYS,
    MODE_MANUAL,
    MODE_OPTIONS,
    MODE_SENSOR,
    VERSION,
)

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
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_SHOTS_DONE,
    CONF_LAST_SHOT,
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

_BLOCK_REASON_SENSOR = SensorEntityDescription(
    key="block_reason",
    translation_key="block_reason",
    icon="mdi:information-outline",
)

_UNAVAILABLE_STATES = {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}


def _normalize_vwc_sensors(config_value: Any) -> list[str]:
    """Return configured VWC sensors as a list of entity IDs."""
    if isinstance(config_value, str):
        return [config_value] if config_value else []

    if isinstance(config_value, list):
        return [entity_id for entity_id in config_value if isinstance(entity_id, str)]

    return []


def _calculate_vwc_state(
    hass: HomeAssistant,
    config_value: Any,
) -> dict[str, Any]:
    """Return averaged VWC state and diagnostics for configured sensors."""
    vwc_sensors = _normalize_vwc_sensors(config_value)
    vwc_values: dict[str, float] = {}

    for entity_id in vwc_sensors:
        state = hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            continue

        try:
            vwc_values[entity_id] = float(state.state)
        except (TypeError, ValueError):
            continue

    vwc_valid_count = len(vwc_values)
    if vwc_valid_count == 1:
        vwc = next(iter(vwc_values.values()))
    elif vwc_valid_count > 1:
        vwc = sum(vwc_values.values()) / vwc_valid_count
    else:
        vwc = None

    return {
        "vwc": vwc,
        "vwc_sensors": vwc_sensors,
        "vwc_values": vwc_values,
        "vwc_valid_count": vwc_valid_count,
        "vwc_average": vwc,
    }


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
            GrowAssistantBlockReasonSensor(hass, entry),
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
        return {key: self._entry.data.get(key) for key in CONFIG_ENTRY_KEYS}


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

    @property
    def native_value(self) -> str:
        """Return a short reason explaining irrigation availability."""
        return _calculate_block_reason(self.hass, self._entry)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return block reason diagnostic attributes."""
        return _calculate_block_reason(self.hass, self._entry)[1]


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

    p2_done = _get_float_state(
        hass,
        entry.data.get(CONF_P2_SHOTS_DONE),
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

    p1_active = _get_bool_state(
        hass,
        entry.data.get(CONF_P1_ACTIVE),
        missing_entities,
    )

    p1_done = _get_bool_state(
        hass,
        entry.data.get(CONF_P1_DONE),
        missing_entities,
    )

    # Optional interval helpers are read only for diagnostics / future use.
    _get_numeric_state(hass, entry, CONF_P2_INTERVAL_MIN, [])
    _get_numeric_state(hass, entry, CONF_P1_INTERVAL_MIN, [])

    timing = _light_timing(
        hass,
        entry.data.get(CONF_LED_SUNRISE),
        entry.data.get(CONF_LED_SUNSET),
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

    debug_attributes = {
        "led_day": led_day,
        "since_on_s": since_on_s,
        "until_off_s": until_off_s,
        "p0_s": p0_s,
        "p1_s": p1_s,
        "p2_target": p2_target_value,
        "p2_done": p2_done_value,
        "p2_shots_left": p2_shots_left,
        "p2_time_ok": p2_time_ok,
        "missing_entities": missing_entities,
        "p1_mode": p1_mode,
        "p2_mode": p2_mode,
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

    if p1_mode_value == MODE_MANUAL:
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


def _calculate_soak_remaining(
    hass: HomeAssistant,
    entry: ConfigEntry,
    soak_config_key: str,
    active_phase: str,
) -> dict[str, Any]:
    """Calculate soak countdown state and attributes."""
    phase = _calculate_phase(hass, entry)[0]
    soak_s = _get_soak_seconds(hass, entry, soak_config_key)
    last_shot = _get_datetime_state(hass, entry.data.get(CONF_LAST_SHOT))
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
        "soak_s": soak_s,
        "elapsed_s": elapsed_s,
        "active": active,
        "remaining_s": remaining_s,
    }


def _calculate_block_reason(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, dict[str, Any]]:
    """Calculate the read-only irrigation block reason and diagnostics."""
    phase, phase_attributes = _calculate_phase(hass, entry)
    missing_entities = list(phase_attributes.get("missing_entities", []))
    _collect_missing_required_entities(hass, entry, missing_entities)

    vwc_state = _calculate_vwc_state(hass, entry.data.get(CONF_VWC_SENSOR))
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
    p2_done_raw = _get_float_state(
        hass,
        entry.data.get(CONF_P2_SHOTS_DONE),
        missing_entities,
    )
    _get_numeric_state(
        hass,
        entry,
        CONF_P2_END_OFFSET_MIN,
        missing_entities,
    )

    drain = _get_optional_bool_state(hass, entry.data.get(CONF_DRAIN_SENSOR))
    drain_tray_wet = _get_optional_bool_state(
        hass,
        entry.data.get(CONF_DRAIN_TRAY_SENSOR),
    )
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
        "p2_target": p2_target,
        "p2_done": p2_done,
        "p1_soak_remaining_s": p1_soak_remaining_s,
        "p2_soak_remaining_s": p2_soak_remaining_s,
        "drain": drain,
        "drain_tray_wet": drain_tray_wet,
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
        entity_id = entry.data.get(key)
        if key == CONF_VWC_SENSOR:
            vwc_sensors = _normalize_vwc_sensors(entity_id)
            if not vwc_sensors:
                missing_entities.append(key)
                continue

            vwc_state = _calculate_vwc_state(hass, entity_id)
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
        value = entry.data.get(key)
        if key == CONF_VWC_SENSOR:
            configured_required.update(_normalize_vwc_sensors(value))
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


def _get_optional_bool_state(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """Return an optional boolean state without marking it missing."""
    if entity_id is None:
        return None

    return _get_bool_state(hass, entity_id, [])


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


def _seconds_since_midnight(value: datetime) -> int:
    """Return seconds since local midnight for a datetime."""
    return value.hour * 3600 + value.minute * 60 + value.second
