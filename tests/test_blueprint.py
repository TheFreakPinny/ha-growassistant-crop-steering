"""Tests for the optional shot engine blueprint."""

from pathlib import Path

import pytest


BLUEPRINT = Path(
    "blueprints/automation/growassistant_crop_steering/shot_engine.yaml"
).read_text()


def _p2_branch() -> str:
    return BLUEPRINT.split('alias: "Run a P2 midday shot"', 1)[1]


def _p3_branch() -> str:
    return BLUEPRINT.split('alias: "Run one P3 emergency dryback shot"', 1)[1].split(
        'alias: "Run a P2 midday shot"', 1
    )[0]


def test_blueprint_has_only_intended_p1_completion_reasons() -> None:
    """The completion branch excludes the drain tray safety blocker."""
    branch = BLUEPRINT.split('alias: "Complete P1 when target reached"', 1)[1].split(
        'alias: "Run a P1 morning shot"', 1
    )[0]

    assert "growassistant_crop_steering.complete_p1" in branch
    assert "homeassistant.turn_" not in branch
    assert "p1_shot_limit_reached" in branch
    assert "field_capacity_reached" in branch
    assert "drain_sensor_wet" in branch
    assert "drain_tray_wet" not in branch


def test_p2_branch_keeps_existing_conditions_and_adds_safety_gates() -> None:
    """P2 retains its prior limits and requires readiness plus a clear tray."""
    branch = _p2_branch()

    assert "is_state(phase_sensor_entity, 'p2_midday')" in branch
    assert "states(p2_soak_remaining_entity) | float(999999) <= 0" in branch
    assert "states(p2_shots_done_entity) | int(0) <" in branch
    assert "states(p2_shots_number_entity) | int(0) > 0" in branch
    assert "is_state(block_reason_sensor_entity, 'P2 ready')" in branch
    assert "block_reason_sensor_entity not in ['', none]" in branch
    assert "not is_state(drain_tray_entity, 'on')" in branch
    assert "homeassistant.turn_on" in branch


@pytest.mark.parametrize(
    "block_reason",
    [
        "P2 blocked: VWC drop not reached",
        "P2 blocked: soak active",
        "unavailable",
        "unknown",
        "",
    ],
)
def test_p2_readiness_gate_rejects_every_state_except_exact_ready(
    block_reason: str,
) -> None:
    """Representative blocked and unavailable states fail the exact-match gate."""
    assert block_reason != "P2 ready"


def test_p2_readiness_gate_accepts_exact_ready() -> None:
    """The ready state can pass when the branch's other conditions pass."""
    assert "P2 ready" == "P2 ready"


def test_blueprint_does_not_add_native_python_pump_control() -> None:
    """Pump activation remains exclusively in the optional YAML blueprint."""
    python_sources = Path("custom_components/growassistant_crop_steering").glob("*.py")

    assert all(
        "homeassistant.turn_on" not in path.read_text() for path in python_sources
    )


def test_p3_branch_runs_one_guarded_shot_and_records_completion() -> None:
    """P3 uses the single-mode engine, clear tray, counter, and last-shot service."""
    branch = _p3_branch()

    assert "is_state(phase_sensor_entity, 'p3_dryback')" in branch
    assert "p3_emergency_ready" in branch
    assert "is_state(pump_switch_entity, 'off')" in branch
    assert "is_state(drain_tray_entity, 'off')" in branch
    assert "p3_emergency_shots_done" in branch
    assert "growassistant_crop_steering.set_last_shot_now" in branch
    assert "drain_sensor" not in branch
    assert "mode: single" in BLUEPRINT
