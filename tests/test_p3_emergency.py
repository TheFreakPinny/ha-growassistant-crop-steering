"""Regression tests for read-only P3 emergency readiness."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.growassistant_crop_steering import sensor
from custom_components.growassistant_crop_steering.const import (
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_P3_EMERGENCY_ENABLED,
    CONF_P3_EMERGENCY_MAX_SHOTS,
    CONF_P3_EMERGENCY_SHOT_DURATION_S,
    CONF_P3_EMERGENCY_SHOTS_DONE,
    CONF_P3_EMERGENCY_SOAK_MIN,
    CONF_P3_EMERGENCY_THRESHOLD_VWC,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
)


class _States:
    def __init__(self, values):
        self.values = values

    def get(self, entity_id):
        value = self.values.get(entity_id)
        return None if value is None else SimpleNamespace(state=value)


def _result(
    *, vwc=25, soak=0, done=0, maximum=2, enabled=True, tray="off", drain="off"
):
    entry = SimpleNamespace(
        options={
            CONF_P3_EMERGENCY_ENABLED: enabled,
            CONF_P3_EMERGENCY_THRESHOLD_VWC: 30,
            CONF_P3_EMERGENCY_SHOT_DURATION_S: 20,
            CONF_P3_EMERGENCY_SOAK_MIN: 15,
            CONF_P3_EMERGENCY_SHOTS_DONE: done,
            CONF_P3_EMERGENCY_MAX_SHOTS: maximum,
        },
        data={
            CONF_VWC_SENSOR: "sensor.vwc",
            CONF_PUMP_SWITCH: "switch.pump",
            CONF_DRAIN_TRAY_SENSOR: "binary_sensor.tray",
            CONF_DRAIN_SENSOR: "binary_sensor.drain",
        },
    )
    values = {"switch.pump": "off", "binary_sensor.drain": drain}
    if tray is not None:
        values["binary_sensor.tray"] = tray
    hass = SimpleNamespace(states=_States(values), services=Mock())
    vwc_state = {
        "vwc": vwc,
        "vwc_valid_count": int(vwc is not None),
        "vwc_sensors": ["sensor.vwc"],
        "vwc_values": [] if vwc is None else [vwc],
    }
    with (
        patch.object(sensor, "calculate_vwc_state", return_value=vwc_state),
        patch.object(
            sensor,
            "_calculate_soak_remaining",
            return_value={"remaining_s": soak},
        ),
    ):
        result = sensor._calculate_p3_emergency(hass, entry, "p3_dryback")
    hass.services.assert_not_called()
    return result


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"enabled": False}, "p3_emergency_disabled"),
        ({"vwc": 31}, "p3_emergency_vwc_above_threshold"),
        ({"vwc": None}, "p3_emergency_vwc_invalid"),
        ({"soak": 1}, "p3_emergency_soak_active"),
        ({"done": 2}, "p3_emergency_shot_limit_reached"),
        ({"tray": "on"}, "p3_emergency_drain_tray_wet"),
        ({"tray": None}, "p3_emergency_drain_tray_unavailable"),
    ],
)
def test_p3_emergency_fails_closed(kwargs, status) -> None:
    result = _result(**kwargs)
    assert result["p3_emergency_ready"] is False
    assert result["p3_emergency_status"] == status


@pytest.mark.parametrize("drain", ["on", "unavailable"])
def test_normal_drain_does_not_block_ready_p3_emergency(drain) -> None:
    result = _result(drain=drain)
    assert result["p3_emergency_ready"] is True
    assert result["p3_emergency_status"] == "p3_emergency_ready"
