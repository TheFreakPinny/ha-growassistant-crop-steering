"""Real HA dispatch, config entries and Store; only synthetic pump hardware."""

from datetime import datetime

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.pump import PumpController
from custom_components.growassistant_crop_steering.shot_engine import _ShotCoordinator


class ShotClock:
    """Patch HA's civil clock while retaining real event-loop monotonic timers."""

    def __init__(self):
        self.move_to("2026-10-13 08:00:00+00:00")

    def move_to(self, value):
        self.at = datetime.fromisoformat(value)


@pytest.fixture
def shot_clock(monkeypatch):
    clock = ShotClock()
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock.at)
    monkeypatch.setattr(
        dt_util, "now", lambda: clock.at.astimezone(dt_util.DEFAULT_TIME_ZONE)
    )
    return clock


@pytest.fixture
async def engine(hass, shot_clock, monkeypatch, enable_custom_integrations):
    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.05
    )
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.05
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "pump_switch": "switch.pump",
            "vwc_sensor": ["sensor.vwc"],
            "led_sunrise": "input_datetime.sunrise",
            "led_sunset": "input_datetime.sunset",
            "drain_sensor": "binary_sensor.drain",
            "drain_tray_sensor": "binary_sensor.tray",
        },
        options={
            "p1_active": True,
            "p1_done": False,
            "p1_shot_duration_s": 1,
            "p2_shot_duration_s": 1,
            "p3_emergency_shot_duration_s": 1,
            "p2_ref_vwc": 55,
            "vwc_cap": 90,
            "p3_emergency_enabled": True,
            "p3_emergency_max_shots": 3,
        },
    )
    entry.add_to_hass(hass)
    for entity, state in {
        "switch.pump": "off",
        "sensor.vwc": "40",
        "input_datetime.sunrise": "06:00:00",
        "input_datetime.sunset": "18:00:00",
        "binary_sensor.drain": "off",
        "binary_sensor.tray": "off",
    }.items():
        hass.states.async_set(entity, state)
    calls = []

    async def service(call):
        calls.append((call.service, call.data["entity_id"]))
        hass.states.async_set(
            call.data["entity_id"],
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )

    for action in ("turn_on", "turn_off"):
        hass.services.async_register("switch", action, service)
    pump = PumpController(hass, entry)
    await pump.async_initialize()
    coordinator = _ShotCoordinator(hass, entry, pump)
    await coordinator.async_initialize()
    coordinator.calls = calls
    coordinator.simulated_service = service
    yield coordinator
    # Tests repair mocked off handlers before fixture teardown.
    hass.services.async_register("switch", "turn_off", service)
    await coordinator.async_close()
