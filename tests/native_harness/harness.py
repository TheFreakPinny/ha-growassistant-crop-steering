"""In-memory composition of the unchanged native controllers.

Only use with pytest's isolated HomeAssistant fixture. No real integration setup,
service registration, entity selection or hardware adapter is exposed here.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import ServiceRegistry
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growassistant_crop_steering.const import (
    BOOLEAN_STATE_DEFAULTS,
    DOMAIN,
    NUMERIC_SETTING_DESCRIPTIONS,
)
from custom_components.growassistant_crop_steering.cycle import _NativeCycleController
from custom_components.growassistant_crop_steering.native_safety import (
    OwnershipEvidence,
)
from custom_components.growassistant_crop_steering.pump import PumpController
from custom_components.growassistant_crop_steering.recovery_revision import (
    LEGACY_METADATA,
)
from custom_components.growassistant_crop_steering.scheduler import _NativeScheduler
from custom_components.growassistant_crop_steering.shot_engine import _ShotCoordinator

PUMP = "switch.native_harness_pump"
SENSORS = tuple(f"sensor.native_harness_vwc_{index}" for index in range(3))
_CONFIG = (
    {spec.key for spec in NUMERIC_SETTING_DESCRIPTIONS}
    | set(BOOLEAN_STATE_DEFAULTS)
    | {"p1_mode", "p2_mode"}
)


class MemoryStore:
    """Deep-copy persisted intent, including unresolved records, across restarts."""

    def __init__(self, records, key):
        self.records = records
        self.key = key

    async def async_load(self):
        self.loaded_revision = deepcopy(
            self.records.get("_participant_envelopes", {}).get(self.key)
            or (LEGACY_METADATA if self.key in self.records else None)
        )
        return deepcopy(self.records.get(self.key))

    async def async_save(self, data):
        self.records[self.key] = deepcopy(data)
        if hasattr(self, "_revision_candidate"):
            self.records.setdefault("_participant_envelopes", {})[self.key] = deepcopy(
                self._revision_candidate
            )
            self._revision_completed = True

    async def async_read_versioned(self):
        return (
            await self.async_load(),
            deepcopy(
                self.records.get("_participant_envelopes", {}).get(self.key)
                or (LEGACY_METADATA if self.key in self.records else None)
            ),
        )

    async def async_save_versioned(self, data, metadata):
        self._revision_candidate, self._revision_completed = deepcopy(metadata), False
        await self.async_save(data)
        if not self._revision_completed:
            raise RuntimeError("Memory journal write completion unknown")


@dataclass(frozen=True)
class PumpCommand:
    service: str
    monotonic: float
    at: datetime


class FakePump(ServiceRegistry):
    """Fixed virtual target; cannot forward any request to HA services.

    service_delay models communication latency. confirmation_delay independently
    delays an off report after service completion. None suppresses confirmation.
    Timed-out dispatch tasks are still handled by the real PumpController.
    """

    def __init__(self, hass):
        super().__init__(hass)
        self.hass = hass
        self.commands: list[PumpCommand] = []
        self.service_delay = {"turn_on": 0.0, "turn_off": 0.0}
        self.confirmation_delay = {"turn_on": 0.0, "turn_off": 0.0}
        self.fail_on = False
        self._reports: list[asyncio.TimerHandle] = []

    @staticmethod
    def has_service(domain, service):
        return domain == "switch" and service in {"turn_on", "turn_off"}

    def report(self, value):
        self.hass.states.async_set(PUMP, value, force_update=True)

    async def async_call(
        self, domain, service, service_data=None, *, blocking=False, **kwargs
    ):
        if not self.has_service(domain, service) or service_data != {"entity_id": PUMP}:
            raise ValueError("Only the fixed virtual pump is admissible")
        self.commands.append(
            PumpCommand(service, self.hass.loop.time(), dt_util.utcnow())
        )
        await asyncio.sleep(self.service_delay[service])
        if service == "turn_on" and self.fail_on:
            raise OSError("Simulated pump communication failure")
        delay = self.confirmation_delay[service]
        value = "on" if service == "turn_on" else "off"
        if delay == 0:
            self.report(value)
        elif delay is not None:
            self._reports.append(self.hass.loop.call_later(delay, self.report, value))

    def close(self):
        for report in self._reports:
            report.cancel()
        self._reports.clear()


class SimulationClock:
    """Controlled aware civil time; safety deadlines keep real monotonic time."""

    def __init__(self, monkeypatch):
        self.at = datetime.fromisoformat("2026-10-13T06:00:00+00:00")
        monkeypatch.setattr(dt_util, "utcnow", lambda: self.at)
        monkeypatch.setattr(
            dt_util, "now", lambda: self.at.astimezone(dt_util.DEFAULT_TIME_ZONE)
        )

    def move_to(self, at):
        value = datetime.fromisoformat(at)
        if value.tzinfo is None:
            raise ValueError("Simulation time must be timezone-aware")
        self.at = value


class NativeHarness:
    """Wire the real pump, shot, cycle and scheduler into pytest-only fake IO."""

    def __init__(self, hass, monkeypatch, clock, *, checkpoint=None, options=None):
        self.hass = hass
        self.clock = clock
        self.actor = FakePump(hass)
        self.records = deepcopy(checkpoint["records"]) if checkpoint else {}
        self.entry = MockConfigEntry(
            **(
                {"entry_id": checkpoint["entry_id"]}
                if checkpoint and "entry_id" in checkpoint
                else {}
            ),
            domain=DOMAIN,
            data={
                "pump_switch": PUMP,
                "vwc_sensor": list(SENSORS),
                "led_sunrise": "06:00:00",
                "led_sunset": "18:00:00",
                "drain_sensor": "binary_sensor.native_harness_drain",
                "drain_tray_sensor": "binary_sensor.native_harness_tray",
            },
            options=deepcopy(checkpoint["options"])
            if checkpoint
            else {
                "p1_active": False,
                "p1_done": False,
                "p1_window_opened_today": False,
                "p2_ref_vwc": 0,
                "p1_shots_done": 0,
                "p2_shots_done": 0,
                "p3_emergency_shots_done": 0,
                "p1_shot_duration_s": 1,
                "p2_shot_duration_s": 1,
                "p3_emergency_shot_duration_s": 1,
                "p1_start_vwc": 50,
                "field_capacity_vwc": 60,
                "p1_duration_min": 120,
                "p2_end_offset_min": 120,
                "p1_shots": 3,
                "p2_shots": 3,
                "p3_emergency_max_shots": 2,
                "p1_soak_min": 1,
                "p2_soak_min": 1,
                "p3_emergency_soak_min": 1,
                "p3_emergency_enabled": True,
                "vwc_cap": 90,
            },
        )
        self.entry.add_to_hass(hass)
        if options:
            self.configure(**options)
        # Replace the dispatcher itself: no registered HA switching service runs.
        monkeypatch.setattr(hass, "services", self.actor)
        self.actor.report(checkpoint["pump_state"] if checkpoint else "off")
        self.vwc(40, 40, 40)
        for entity in ("drain", "tray"):
            hass.states.async_set(f"binary_sensor.native_harness_{entity}", "off")
        self.pump = PumpController(hass, self.entry)
        self.pump.store = MemoryStore(self.records, "pump")
        self.shots = _ShotCoordinator(hass, self.entry, self.pump)
        self.shots.store = MemoryStore(self.records, "shots")
        self.cycle = _NativeCycleController(self.shots)
        self.cycle.store = MemoryStore(self.records, "cycle")
        self.scheduler = _NativeScheduler(hass, self.entry, self.shots, self.cycle)

    def configure(self, **values):
        if any(key not in _CONFIG for key in values) or any(
            not isinstance(value, (bool, int, float))
            and not (key in {"p1_mode", "p2_mode"} and value in {"sensor", "manual"})
            for key, value in values.items()
        ):
            raise ValueError("Only scalar steering configuration is admissible")
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, **values}
        )

    def vwc(self, *values):
        if len(values) != 3:
            raise ValueError("Supply exactly three independent sensor readings")
        for entity, value in zip(SENSORS, values, strict=True):
            if value is None:
                self.hass.states.async_remove(entity)
            else:
                self.hass.states.async_set(
                    entity,
                    str(value),
                    force_update=True,
                    timestamp=self.clock.at.timestamp(),
                )

    async def ramp_vwc(self, *values):
        """Emit plausible independent reports without manufacturing spread faults.

        Direct vwc() remains available for deliberately unsafe/transient reports.
        Each step changes a peer by at most five percentage points; production
        disagreement policy remains unmodified, including on intermediate events.
        """
        if len(values) != 3:
            raise ValueError("Supply exactly three independent sensor readings")
        current = [float(self.hass.states.get(entity).state) for entity in SENSORS]
        while current != list(values):
            current = [
                old + max(-5, min(5, new - old))
                for old, new in zip(current, values, strict=True)
            ]
            self.vwc(*current)
            await self.hass.async_block_till_done()

    def lighting(self, sunrise, sunset):
        """Accept local wall-clock times only, never helper entity assignments."""
        from datetime import time

        for value in (sunrise, sunset):
            if time.fromisoformat(value).tzinfo is not None:
                raise ValueError("Lighting requires local wall-clock times")
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, "led_sunrise": sunrise, "led_sunset": sunset},
        )

    def move_to(self, at, *, refresh=True):
        self.clock.move_to(at)
        if refresh:
            self.vwc(
                *(
                    None
                    if (state := self.hass.states.get(entity)) is None
                    else state.state
                    for entity in SENSORS
                )
            )

    async def initialize(self, *, fresh=True):
        await self.hass.config.async_set_time_zone("UTC")
        # Local evidence is for this fixed fake only; restore never grants it.
        if fresh:
            evidence = OwnershipEvidence(PUMP, True, True, True, True)
            self.pump.ownership = self.shots.ownership = evidence
        await self.pump.async_initialize()
        await self.shots.async_initialize()
        await self.cycle.async_initialize()
        await self.scheduler.async_initialize()

    async def observe(self):
        return await self.cycle.async_observe(self.clock.at)

    def checkpoint(self):
        """Capture crash-visible data, not the coordinator's mutable live record."""
        return deepcopy(
            {
                "entry_id": self.entry.entry_id,
                "records": self.records,
                "options": dict(self.entry.options),
                "pump_state": self.hass.states.get(PUMP).state,
            }
        )

    async def close(self):
        await self.scheduler.async_close()
        await self.cycle.async_close()
        self.actor.confirmation_delay["turn_off"] = 0
        self.actor.service_delay["turn_off"] = 0
        self.actor.close()
        await self.shots.async_close()


async def wait_for(predicate):
    """Bound event-driven assertions; no fixed long sleeps or unbounded polling."""
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)
