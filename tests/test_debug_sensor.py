"""Tests for the phase-independent diagnostic sensor."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers.entity import EntityCategory

from custom_components.growassistant_crop_steering import sensor


def _entry():
    return SimpleNamespace(entry_id="entry", data={}, options={})


def test_debug_sensor_identity_and_phase_state() -> None:
    """The debug entity is diagnostic, stable, and follows the phase helper."""
    entity = sensor.GrowAssistantDebugSensor(SimpleNamespace(), _entry())

    with patch.object(sensor, "_calculate_phase", return_value=("p2_midday", {})):
        assert entity.native_value == "p2_midday"

    assert entity.unique_id == "entry_debug"
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.GrowAssistantP1DebugSensor is not None


def test_general_debug_combines_existing_calculations_without_side_effects() -> None:
    """P1/P2, VWC, last-shot, and unavailable safety data are combined read-only."""
    hass = SimpleNamespace(services=Mock())
    p1 = {
        "phase": "p2_midday",
        "led_day": True,
        "now": "2026-08-22T12:00:00+00:00",
        "light_start": "06:00:00",
        "light_end": "18:00:00",
        "since_on_s": 21600,
        "p1_mode": "sensor",
        "p1_active": False,
        "p1_done": True,
        "p1_window_opened_today": True,
        "p1_window_active": False,
        "p0_s": 3600,
        "p1_s": 7200,
        "p1_window_start_s": 3600,
        "p1_window_end_s": 10800,
        "p1_start_vwc": 48,
        "field_capacity_vwc": 55,
        "p1_shots_done": 3,
        "p1_shots_target": 3,
        "p1_shots_left": 0,
        "p1_soak_remaining_s": 0,
        "vwc": 44,
        "vwc_valid": True,
        "vwc_sensors": ["sensor.vwc"],
        "vwc_values": [44],
        "missing_entities": [],
        "blocking_reasons": [],
        "passed_conditions": ["vwc_valid"],
    }
    block = {
        "vwc_valid_count": 1,
        "vwc_average": 44,
        "vwc_cap_active": False,
        "p2_mode": "sensor",
        "p2_ref_vwc": 50,
        "p2_vwc_drop": 5,
        "p2_drop_threshold": 45,
        "p2_done": 1,
        "p2_target": 4,
        "p2_soak_remaining_s": 120,
        "drain_sensor_configured": True,
        "drain_sensor_entity_id": "binary_sensor.drain",
        "drain_sensor_available": False,
        "drain_sensor_state": "unavailable",
        "drain_sensor_ignored": False,
        "drain_wet": False,
        "drain_tray_sensor_configured": False,
        "drain_tray_sensor_entity_id": None,
        "drain_tray_sensor_available": True,
        "drain_tray_sensor_state": None,
        "drain_tray_sensor_ignored": True,
        "drain_tray_wet": False,
        "optional_unavailable_entities": ["binary_sensor.drain"],
    }
    phase = {
        "until_off_s": 21600,
        "p2_end_offset_s": 1800,
        "p2_time_ok": True,
    }

    with (
        patch.object(sensor, "_calculate_phase", return_value=("p2_midday", phase)),
        patch.object(sensor, "_calculate_p1_debug", return_value=("complete", p1)),
        patch.object(
            sensor,
            "_calculate_block_reason",
            return_value=("P2 blocked: soak active", block),
        ),
        patch.object(
            sensor,
            "_get_last_shot_datetime_with_source",
            return_value=(None, "managed"),
        ),
    ):
        attributes = sensor._calculate_debug(hass, _entry())

    assert attributes["p1_shots_target"] == 3
    assert attributes["p2_ref_vwc"] == 50
    assert attributes["p2_drop_threshold"] == 45
    assert attributes["p2_shots_done"] == 1
    assert attributes["p2_shots_target"] == 4
    assert attributes["p2_shots_left"] == 3
    assert attributes["p2_soak_remaining_s"] == 120
    assert attributes["drain_sensor_available"] is False
    assert attributes["optional_unavailable_entities"] == ["binary_sensor.drain"]
    hass.services.assert_not_called()
