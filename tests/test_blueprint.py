"""Tests for the optional shot engine blueprint."""

from pathlib import Path


def test_blueprint_has_only_intended_p1_completion_reasons() -> None:
    """The completion branch excludes the drain tray safety blocker."""
    blueprint = Path(
        "blueprints/automation/growassistant_crop_steering/shot_engine.yaml"
    ).read_text()
    branch = blueprint.split('alias: "Complete P1 when target reached"', 1)[1].split(
        'alias: "Run a P1 morning shot"', 1
    )[0]

    assert "growassistant_crop_steering.complete_p1" in branch
    assert "homeassistant.turn_" not in branch
    assert "p1_shot_limit_reached" in branch
    assert "field_capacity_reached" in branch
    assert "drain_sensor_wet" in branch
    assert "drain_tray_wet" not in branch
