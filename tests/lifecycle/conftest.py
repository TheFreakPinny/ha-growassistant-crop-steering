"""Real Home Assistant fixtures with synthetic helpers and no network access."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growassistant_crop_steering.const import (
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def lifecycle_environment(enable_custom_integrations, freezer):
    """Enable local platforms, forbid hardware connections, and fix cycle time."""
    freezer.move_to("2026-10-12 10:00:00+00:00")


@pytest.fixture
async def entry(hass):
    """Create an actual HA config entry and external helper states."""
    await hass.config.async_set_time_zone("UTC")
    hass.states.async_set("input_datetime.sunrise", "08:00:00")
    hass.states.async_set("input_datetime.sunset", "20:00:00")
    hass.states.async_set("sensor.vwc", "45")
    hass.states.async_set("switch.pump", "off")
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Lifecycle Grow",
        unique_id=DOMAIN,
        data={
            "name": "Lifecycle Grow",
            CONF_LED_SUNRISE: "input_datetime.sunrise",
            CONF_LED_SUNSET: "input_datetime.sunset",
            CONF_VWC_SENSOR: ["sensor.vwc"],
            CONF_PUMP_SWITCH: "switch.pump",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def pump(hass):
    """Replace external pump service with a recording mock."""
    mock = AsyncMock()
    hass.services.async_register("homeassistant", "turn_off", mock)
    return mock
