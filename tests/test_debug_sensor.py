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
    assert attributes["block_reason"] == "P2 blocked: soak active"
    assert "p2_soak_active" in attributes["blocking_reasons"]
    assert "p2_mode_sensor" in attributes["passed_conditions"]
    assert "p2_interval_available" not in attributes["blocking_reasons"]
    hass.services.assert_not_called()


def _phase_diagnostic_inputs():
    """Return complete, ready sensor-mode diagnostic inputs."""
    phase = {"led_day": True, "p2_time_ok": True}
    p1 = {
        "p1_mode": "sensor",
        "p1_active": False,
        "p1_done": True,
        "p1_window_active": True,
        "p1_window_opened_today": False,
        "vwc_valid": True,
        "vwc_below_start": True,
        "vwc_below_field_capacity": True,
        "soak_ok": True,
        "p1_shots_left": 1,
    }
    block = {
        "missing_entities": [],
        "p2_mode": "sensor",
        "p2_ref_vwc": 50,
        "p2_drop_threshold": 45,
        "vwc": 44,
        "vwc_valid_count": 1,
        "p2_done": 1,
        "p2_target": 4,
        "p2_time_ok": True,
        "vwc_cap_active": False,
        "p2_soak_remaining_s": 0,
        "drain_sensor_configured": False,
        "drain_sensor_available": True,
        "drain_wet": False,
        "drain_tray_sensor_configured": False,
        "drain_tray_sensor_available": True,
        "drain_tray_wet": False,
    }
    return phase, p1, block


def test_p1_waiting_to_start_evaluates_start_conditions() -> None:
    """A waiting P1 reports its mode, light, window, and daily availability."""
    phase, p1, block = _phase_diagnostic_inputs()
    p1.update(
        {
            "p1_done": False,
            "p1_window_active": False,
            "p1_window_opened_today": True,
        }
    )

    result = sensor._calculate_phase_diagnostics("p1_morning", phase, p1, block)

    assert "p1_mode_sensor" in result["passed_conditions"]
    assert "led_day_true" in result["passed_conditions"]
    assert "p1_window_not_active" in result["blocking_reasons"]
    assert "p1_window_already_opened_today" in result["blocking_reasons"]
    assert not any(reason.startswith("p2_") for reason in result["blocking_reasons"])


def test_active_p1_ignores_start_only_conditions() -> None:
    """An active P1 continues independently of its original start window."""
    phase, p1, block = _phase_diagnostic_inputs()
    phase["led_day"] = False
    p1.update(
        {
            "p1_active": True,
            "p1_mode": "manual",
            "p1_window_active": False,
            "p1_window_opened_today": True,
        }
    )

    result = sensor._calculate_phase_diagnostics("p1_morning", phase, p1, block)

    assert "p1_already_active" in result["passed_conditions"]
    assert "p1_window_not_active" not in result["blocking_reasons"]
    assert "p1_window_already_opened_today" not in result["blocking_reasons"]
    assert "led_day_false" not in result["blocking_reasons"]
    assert "p1_mode_manual" not in result["blocking_reasons"]


@pytest.mark.parametrize(
    ("p1_updates", "block_updates", "reason"),
    [
        ({"soak_ok": False}, {}, "soak_not_finished"),
        ({"p1_shots_left": 0}, {}, "p1_shot_limit_reached"),
        (
            {},
            {"drain_sensor_configured": True, "drain_sensor_available": False},
            "drain_sensor_unavailable",
        ),
        ({}, {"drain_tray_wet": True}, "drain_tray_wet"),
    ],
)
def test_active_p1_keeps_shot_safety_diagnostics(
    p1_updates, block_updates, reason
) -> None:
    """Active P1 still reports soak, limit, and drain shot blockers."""
    phase, p1, block = _phase_diagnostic_inputs()
    p1.update({"p1_active": True, **p1_updates})
    block.update(block_updates)

    result = sensor._calculate_phase_diagnostics("p1_morning", phase, p1, block)

    assert reason in result["blocking_reasons"]
    assert "p1_already_active" in result["passed_conditions"]


def test_legacy_p1_debug_calculator_remains_unchanged() -> None:
    """The legacy debug entity continues to use its original calculator."""
    _phase, p1, _block = _phase_diagnostic_inputs()
    # The legacy entity still delegates exclusively to its original calculator.
    entity = sensor.GrowAssistantP1DebugSensor(SimpleNamespace(), _entry())
    with patch.object(
        sensor, "_calculate_p1_debug", return_value=("legacy", p1)
    ) as calc:
        assert entity.native_value == "legacy"
        assert entity.extra_state_attributes is p1
        assert calc.call_count == 2


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"p2_soak_remaining_s": 30}, "p2_soak_active"),
        (
            {
                "drain_sensor_configured": True,
                "drain_sensor_available": False,
            },
            "drain_sensor_unavailable",
        ),
        ({"p2_time_ok": False}, "p2_end_offset_reached"),
    ],
)
def test_p2_sensor_mode_reports_current_gate(updates, reason) -> None:
    """P2 reports VWC/drop and current safety gates, never an interval gate."""
    phase, p1, block = _phase_diagnostic_inputs()
    block.update(updates)

    result = sensor._calculate_phase_diagnostics("p2_midday", phase, p1, block)

    assert reason in result["blocking_reasons"]
    assert "p2_mode_sensor" in result["passed_conditions"]
    assert "vwc_drop_reached" in result["passed_conditions"]
    assert not any("interval" in item for item in result["blocking_reasons"])


def test_p3_after_light_off_discards_obsolete_readiness_failures() -> None:
    """Light-off P3 explains dryback without stale P1 or P2 failures."""
    phase, p1, block = _phase_diagnostic_inputs()
    phase["led_day"] = False
    p1.update({"p1_done": False, "p1_shots_left": 0})
    block.update({"p2_time_ok": False, "p2_done": 4})

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result == {
        "phase_reason": "light_cycle_ended",
        "blocking_reasons": [],
        "passed_conditions": ["p3_dryback_active", "led_day_false"],
    }


def test_p3_during_light_exposes_p2_end_offset() -> None:
    """Daytime P3 retains the actual P2 condition that selected dryback."""
    phase, p1, block = _phase_diagnostic_inputs()
    phase["p2_time_ok"] = False
    block.update(
        {
            "p2_time_ok": False,
            "p2_ref_vwc": None,
            "p2_soak_remaining_s": 30,
            "drain_wet": True,
        }
    )

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result["phase_reason"] == "p2_end_offset_reached"
    assert "p2_end_offset_reached" in result["blocking_reasons"]
    assert "p2_reference_missing" in result["blocking_reasons"]
    assert "p2_soak_active" in result["blocking_reasons"]
    assert "drain_sensor_wet" in result["blocking_reasons"]
    assert not any(reason.startswith("p1_") for reason in result["blocking_reasons"])


def test_daytime_p3_shot_limit_is_phase_reason_despite_shot_blockers() -> None:
    """Selection gates outrank operational P2 blockers as the P3 reason."""
    phase, p1, block = _phase_diagnostic_inputs()
    block.update(
        {
            "p2_done": 4,
            "p2_ref_vwc": None,
            "vwc": 48,
            "p2_soak_remaining_s": 30,
            "drain_sensor_configured": True,
            "drain_sensor_available": False,
            "drain_tray_wet": True,
        }
    )

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result["phase_reason"] == "p2_shot_limit_reached"
    assert {
        "p2_shot_limit_reached",
        "p2_reference_missing",
        "p2_vwc_drop_not_reached",
        "p2_soak_active",
        "drain_sensor_unavailable",
        "drain_tray_wet",
    } <= set(result["blocking_reasons"])


@pytest.mark.parametrize(
    "block_updates",
    [
        {"p2_ref_vwc": None},
        {"p2_soak_remaining_s": 30},
        {"vwc": 48},
        {"vwc_cap_active": True},
        {"drain_wet": True},
        {"drain_tray_sensor_configured": True, "drain_tray_sensor_available": False},
    ],
)
def test_operational_p2_blocker_does_not_select_daytime_p3(block_updates) -> None:
    """Shot-readiness failures never masquerade as phase-selection reasons."""
    phase, p1, block = _phase_diagnostic_inputs()
    block.update(block_updates)

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result["phase_reason"] == "p3_dryback_active"
    assert result["blocking_reasons"]


def test_incomplete_p1_selects_daytime_p3_before_p2_readiness() -> None:
    """An elapsed incomplete P1 is the daytime P3 progression reason."""
    phase, p1, block = _phase_diagnostic_inputs()
    p1.update({"p1_done": False, "p1_window_active": False})
    block.update({"p2_ref_vwc": None, "p2_soak_remaining_s": 30})

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result["phase_reason"] == "p1_window_ended_without_completion"
    assert "p2_reference_missing" in result["blocking_reasons"]
    assert "p2_soak_active" in result["blocking_reasons"]


def test_manual_progression_uses_phase_availability_not_p2_mode() -> None:
    """Manual P1 progression still selects P3 from shot/time availability."""
    phase, p1, block = _phase_diagnostic_inputs()
    p1.update({"p1_mode": "manual", "p1_done": False})
    block.update({"p2_mode": "manual", "p2_done": 4})

    result = sensor._calculate_phase_diagnostics("p3_dryback", phase, p1, block)

    assert result["phase_reason"] == "p2_shot_limit_reached"
    assert "p2_mode_manual" in result["blocking_reasons"]
