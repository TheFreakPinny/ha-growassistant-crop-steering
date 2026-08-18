"""Tests for the P1 completion transition."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.growassistant_crop_steering import _complete_p1_for_entry
from custom_components.growassistant_crop_steering.const import (
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_REF_VWC,
    CONF_VWC_SENSOR,
)


@dataclass
class _State:
    state: str


class _States:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, entity_id: str) -> _State | None:
        value = self._values.get(entity_id)
        return _State(value) if value is not None else None


class _ConfigEntries:
    def async_update_entry(self, entry: SimpleNamespace, *, options: dict) -> None:
        entry.options = options


def _make_hass(values: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        states=_States(values),
        config_entries=_ConfigEntries(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )


def _make_entry(sensors: str | list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="test-entry",
        data={CONF_VWC_SENSOR: sensors},
        options={
            CONF_P1_ACTIVE: True,
            CONF_P1_DONE: False,
            CONF_P1_WINDOW_OPENED_TODAY: True,
            CONF_P2_REF_VWC: 12.3,
        },
    )


@pytest.mark.asyncio
async def test_complete_p1_captures_single_sensor_without_pump_control() -> None:
    """A valid VWC completes P1 while preserving unrelated state."""
    hass = _make_hass({"sensor.vwc": "54.2"})
    entry = _make_entry("sensor.vwc")

    assert await _complete_p1_for_entry(hass, entry) is True

    assert entry.options[CONF_P2_REF_VWC] == 54.2
    assert entry.options[CONF_P1_DONE] is True
    assert entry.options[CONF_P1_ACTIVE] is False
    assert entry.options[CONF_P1_WINDOW_OPENED_TODAY] is True
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_p1_averages_all_valid_configured_sensors() -> None:
    """Completion uses the same multi-sensor average as P1 Debug."""
    hass = _make_hass(
        {"sensor.vwc_1": "40", "sensor.vwc_2": "50", "sensor.bad": "unknown"}
    )
    entry = _make_entry(["sensor.vwc_1", "sensor.vwc_2", "sensor.bad"])

    assert await _complete_p1_for_entry(hass, entry) is True
    assert entry.options[CONF_P2_REF_VWC] == 45


@pytest.mark.asyncio
async def test_complete_p1_with_no_valid_vwc_preserves_safe_state(caplog) -> None:
    """Missing readings must not create a reference or partially complete P1."""
    hass = _make_hass({"sensor.vwc": "unavailable"})
    entry = _make_entry("sensor.vwc")
    original_options = dict(entry.options)

    assert await _complete_p1_for_entry(hass, entry) is False

    assert entry.options == original_options
    assert "no valid VWC was available" in caplog.text
    hass.services.async_call.assert_not_awaited()
