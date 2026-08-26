"""Regression tests for P2 drain-tray block-reason safety."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.growassistant_crop_steering import sensor
from custom_components.growassistant_crop_steering.const import (
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_P1_MODE,
    CONF_P2_MODE,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS,
    CONF_P2_SHOTS_DONE,
    CONF_P2_VWC_DROP,
    MODE_SENSOR,
)


def _block_reason_for_drains(
    *,
    tray_available: bool = True,
    tray_wet: bool = False,
    drain_available: bool = True,
    drain_wet: bool = False,
) -> str:
    entry = SimpleNamespace(
        entry_id="test",
        data={},
        options={
            CONF_P1_MODE: MODE_SENSOR,
            CONF_P2_MODE: MODE_SENSOR,
        },
    )
    numeric = {
        CONF_P2_REF_VWC: 50,
        CONF_P2_VWC_DROP: 5,
        CONF_P2_SHOTS: 3,
        CONF_P2_SHOTS_DONE: 0,
    }

    def optional_binary(_hass, _entry, key):
        if key == CONF_DRAIN_TRAY_SENSOR:
            return {
                "configured": True,
                "available": tray_available,
                "wet": tray_wet,
                "state": (
                    "on" if tray_wet else ("off" if tray_available else "unavailable")
                ),
                "entity_id": "binary_sensor.drain_tray",
            }
        assert key == CONF_DRAIN_SENSOR
        return {
            "configured": True,
            "available": drain_available,
            "wet": drain_wet,
            "state": (
                "on" if drain_wet else ("off" if drain_available else "unavailable")
            ),
            "entity_id": "binary_sensor.drain",
        }

    with (
        patch.object(
            sensor, "_calculate_phase", return_value=("p2_midday", {"p2_time_ok": True})
        ),
        patch.object(sensor, "_collect_missing_required_entities"),
        patch.object(
            sensor,
            "calculate_vwc_state",
            return_value={
                "vwc": 40,
                "vwc_sensors": [],
                "vwc_values": [],
                "vwc_valid_count": 1,
                "vwc_average": 40,
            },
        ),
        patch.object(
            sensor,
            "_get_numeric_state",
            side_effect=lambda _h, _e, key, _m: numeric.get(key),
        ),
        patch.object(
            sensor, "_get_optional_binary_sensor_state", side_effect=optional_binary
        ),
        patch.object(sensor, "_get_optional_numeric_state", return_value=None),
        patch.object(
            sensor, "_calculate_soak_remaining", return_value={"remaining_s": 0}
        ),
        patch.object(
            sensor,
            "_deduplicate_missing_entities",
            side_effect=lambda values, _entry: values,
        ),
    ):
        return sensor._calculate_block_reason(SimpleNamespace(), entry)[0]


@pytest.mark.parametrize(
    ("available", "wet", "expected"),
    [
        (False, False, "P2 blocked: drain tray unavailable"),
        (True, True, "P2 blocked: drain tray wet"),
        (True, False, "P2 ready"),
    ],
)
def test_p2_drain_tray_is_a_fail_safe_gate(
    available: bool, wet: bool, expected: str
) -> None:
    """An unavailable or wet configured tray prevents P2 readiness."""
    assert _block_reason_for_drains(tray_available=available, tray_wet=wet) == expected


@pytest.mark.parametrize(
    ("available", "wet"),
    [(True, True), (False, False)],
)
def test_normal_drain_sensor_does_not_block_p2(available: bool, wet: bool) -> None:
    """P2 readiness treats the normal drain sensor as diagnostic-only."""
    assert (
        _block_reason_for_drains(drain_available=available, drain_wet=wet) == "P2 ready"
    )


def test_drain_tray_is_not_a_completion_reason() -> None:
    """The tray state blocks irrigation rather than completing a phase."""
    reason = _block_reason_for_drains(tray_wet=True)

    assert reason == "P2 blocked: drain tray wet"
    assert "complete" not in reason.lower()
