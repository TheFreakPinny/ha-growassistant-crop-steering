"""Synthetic pump states, real HA dispatch and in-memory HA Store only."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growassistant_crop_steering.const import CONF_PUMP_SWITCH, DOMAIN
from custom_components.growassistant_crop_steering.native_safety import (
    OwnershipEvidence,
)
from custom_components.growassistant_crop_steering.pump import PumpController


@pytest.fixture
async def controller(hass, monkeypatch):
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.03
    )
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.03
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PUMP_SWITCH: "switch.pump"})
    entry.add_to_hass(hass)
    hass.states.async_set("switch.pump", "off")

    async def service(call):
        hass.states.async_set(
            call.data["entity_id"],
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )

    hass.services.async_register("switch", "turn_on", service)
    hass.services.async_register("switch", "turn_off", service)
    pump = PumpController(hass, entry)
    pump.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    await pump.async_initialize()
    yield pump
    await pump.async_close()
