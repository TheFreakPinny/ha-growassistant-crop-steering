"""Isolated HA fixture with an injected fixed fake actuator and memory stores."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import ServiceRegistry
from pytest_homeassistant_custom_component.common import async_test_home_assistant

from .harness import NativeHarness, SimulationClock


@pytest.fixture
async def simulation(hass, monkeypatch, enable_custom_integrations):
    forbidden_dispatch = AsyncMock(
        side_effect=AssertionError("Real HA service dispatch forbidden")
    )
    monkeypatch.setattr(ServiceRegistry, "async_call", forbidden_dispatch)
    clock = SimulationClock(monkeypatch)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.15
    )
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.15
    )
    harness = NativeHarness(hass, monkeypatch, clock)
    await harness.initialize()
    yield harness
    await harness.close()
    forbidden_dispatch.assert_not_awaited()


@pytest.fixture
async def restarted_hass():
    """A new HA runtime with fresh state, owners, listeners and config entries."""
    async with async_test_home_assistant() as hass:
        await hass.config.async_set_time_zone("UTC")
        yield hass
        await hass.async_stop(force=True)
