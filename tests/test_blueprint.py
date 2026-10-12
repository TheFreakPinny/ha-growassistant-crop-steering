"""Tests for the optional shot engine blueprint."""

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

BLUEPRINT = Path(
    "blueprints/automation/growassistant_crop_steering/shot_engine.yaml"
).read_text()


def _p2_branch() -> str:
    return BLUEPRINT.split('alias: "Run a P2 midday shot"', 1)[1]


def _p1_branch() -> str:
    return BLUEPRINT.split('alias: "Run a P1 morning shot"', 1)[1].split(
        'alias: "Run one P3 emergency dryback shot"', 1
    )[0]


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


class _BlueprintLoader(yaml.SafeLoader):
    """Preserve input names without changing PyYAML's global loader."""


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: loader.construct_scalar(node)
)


@pytest_asyncio.fixture
async def blueprint_hass(tmp_path):
    """Use real Home Assistant state and template helpers without starting services."""
    hass = HomeAssistant(str(tmp_path))
    yield hass
    await hass.async_stop()


def _p2_templates():
    document = yaml.load(BLUEPRINT, Loader=_BlueprintLoader)
    branch = next(
        branch
        for branch in document["action"][0]["choose"]
        if branch["alias"] == "Run a P2 midday shot"
    )
    return [
        item["value_template"] for item in document["condition"] + branch["conditions"]
    ]


def _p2_ready(hass, overrides=None, inputs=None):
    """Render every actual P2 and top-level condition using HA's Jinja environment."""
    values = {
        "pump_switch_entity": "off",
        "phase_sensor_entity": "p2_midday",
        "p2_soak_remaining_entity": "0",
        "p2_shots_done_entity": "0",
        "p2_shots_number_entity": "3",
        "block_reason_sensor_entity": "P2 ready",
        "drain_tray_entity": "off",
    }
    values.update(overrides or {})
    variables = {name: "sensor." + name for name in values}
    variables.update(inputs or {})
    for name, state in values.items():
        if state is not None and variables[name]:
            hass.states.async_set(variables[name], state)
    results = [
        Template(source, hass).async_render(variables) for source in _p2_templates()
    ]
    assert all(isinstance(result, bool) for result in results)
    return all(results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block_reason",
    [
        "P2 blocked: VWC drop not reached",
        "P2 blocked: soak active",
        "unavailable",
        "unknown",
        "",
        None,
        "p2 ready",
        "P2 ready ",
    ],
)
async def test_p2_readiness_gate_rejects_every_state_except_exact_ready(
    blueprint_hass, block_reason
):
    """Blocked, absent and non-exact readiness states prevent the actual branch."""
    assert not _p2_ready(blueprint_hass, {"block_reason_sensor_entity": block_reason})


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["", None])
async def test_p2_readiness_requires_configured_block_reason(blueprint_hass, entity):
    assert not _p2_ready(blueprint_hass, inputs={"block_reason_sensor_entity": entity})


@pytest.mark.asyncio
async def test_p2_readiness_gate_accepts_exact_ready(blueprint_hass):
    """The real templates permit the branch when every condition passes."""
    assert _p2_ready(blueprint_hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "state"),
    [
        ("pump_switch_entity", "on"),
        ("pump_switch_entity", "unavailable"),
        ("phase_sensor_entity", "p1_morning"),
        ("p2_soak_remaining_entity", "1"),
        ("p2_soak_remaining_entity", "unknown"),
        ("p2_shots_done_entity", "3"),
        ("p2_shots_done_entity", "4"),
        ("p2_shots_number_entity", "0"),
        ("p2_shots_number_entity", "unknown"),
        ("drain_tray_entity", "on"),
    ],
)
async def test_p2_other_conditions_block_ready_reason(blueprint_hass, name, state):
    """A ready diagnostic cannot override pump, phase, soak, limit or tray gates."""
    assert not _p2_ready(blueprint_hass, {name: state})


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["", None])
async def test_p2_preserves_optional_drain_tray_behavior(blueprint_hass, entity):
    """The existing Blueprint permits an omitted tray; preserve that behavior."""
    assert _p2_ready(blueprint_hass, inputs={"drain_tray_entity": entity})


def test_blueprint_does_not_add_native_python_pump_control() -> None:
    """Pump activation remains exclusively in the optional YAML blueprint."""
    python_sources = Path("custom_components/growassistant_crop_steering").glob("*.py")

    assert all(
        "homeassistant.turn_on" not in path.read_text() for path in python_sources
    )


def test_blueprint_records_exact_type_for_each_completed_shot() -> None:
    """Each existing post-shot service call labels its own shot type."""
    assert "shot_type: p1" in _p1_branch()
    assert "shot_type: p2" in _p2_branch()
    assert "shot_type: p3_emergency" in _p3_branch()


def test_p3_branch_runs_one_guarded_shot_and_records_completion() -> None:
    """P3 uses the single-mode engine, clear tray, counter, and last-shot service."""
    branch = _p3_branch()

    assert "is_state(phase_sensor_entity, 'p3_dryback')" in branch
    assert "p3_emergency_ready" in branch
    assert "is_state(pump_switch_entity, 'off')" in branch
    assert "is_state(drain_tray_entity, 'off')" in branch
    assert "p3_emergency_shots_done" in branch
    assert "states(p3_emergency_shot_duration_entity) | float(0) > 0" in branch
    assert "growassistant_crop_steering.set_last_shot_now" in branch
    assert "drain_sensor" not in branch
    assert "mode: single" in BLUEPRINT
