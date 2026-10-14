"""Inactive native phase ownership. See docs/native-cycle.md before harness use."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .irrigation import Decision, capture_inputs, evaluate
from .number import SIGNAL_NUMBER_STATE_UPDATED
from .shot_engine import JOURNAL_TIMEOUT, _ShotCoordinator
from .switch import SIGNAL_SWITCH_STATE_UPDATED

_FLAGS = ("p1_active", "p1_done", "p1_window_opened_today", "p2_ref_vwc")
_COUNTERS = ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")
_SCHEDULE = (
    "led_sunrise",
    "led_sunset",
    "p0_transpiration_min",
    "p1_duration_min",
    "p2_end_offset_min",
    "p1_mode",
    "p2_mode",
)
_CLEAN = {**dict.fromkeys(_FLAGS[:3], False), "p2_ref_vwc": 0}


class _NativeCycleController:
    """Explicit internal API; scheduler supplies all events and timing.

    No listeners, public activation or legacy helper migration. Restored identity
    is diagnostic only: restarting never grants permission to resume a cycle.
    """

    def __init__(self, coordinator: _ShotCoordinator) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.entry = coordinator.entry
        self.store = Store(self.hass, 1, f"{DOMAIN}.native_cycle.{self.entry.entry_id}")
        self.record: dict[str, Any] | None = None
        self.recovery_reason: str | None = None
        self._initialized = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._write: asyncio.Task[Any] | None = None

    @property
    def cycle_id(self) -> str | None:
        return None if self.record is None else self.record["cycle_id"]

    @property
    def phase(self) -> str | None:
        return None if self.record is None else self.record["phase"]

    def block(self, reason: str) -> None:
        self.recovery_reason = self.recovery_reason or reason
        self.coordinator._interrupt(reason)

    async def async_initialize(self) -> None:
        if self._initialized or self._closed or self.coordinator._cycle is not None:
            raise RuntimeError("Cycle controller already owned or closed")
        if not self.coordinator.initialized:
            raise RuntimeError("Initialize the shot coordinator first")
        if (
            self.coordinator.execution_active
            or self.coordinator._accounting_lock.locked()
        ):
            raise RuntimeError("Cannot attach cycle ownership during a shot or reset")
        self.coordinator._cycle = self
        try:
            async with asyncio.timeout(JOURNAL_TIMEOUT):
                stored = await self.store.async_load()
            if stored is not None:
                if (
                    not isinstance(stored, dict)
                    or not isinstance(stored.get("cycle_id"), str)
                    or not isinstance(stored.get("values"), dict)
                    or stored.get("phase")
                    not in {
                        "p0_transpiration",
                        "p1_morning",
                        "p2_midday",
                        "p3_dryback",
                        "ended",
                    }
                ):
                    raise ValueError("Invalid native cycle record")
                instant = datetime.fromisoformat(stored["cycle_id"])
                if instant.tzinfo is None:
                    raise ValueError("Naive cycle identity")
                self.record = stored
                mismatch = any(
                    self.entry.options.get(key) != stored["values"].get(key)
                    for key in _FLAGS
                )
                self.block(
                    "cycle_persistence_inconsistent"
                    if mismatch or stored.get("pending")
                    else "cycle_restart_requires_review"
                )
            self._initialized = True
        except BaseException:
            self.block("cycle_load_failed")
            raise

    def _decision(self, now: datetime) -> Decision:
        return evaluate(capture_inputs(self.hass, self.entry), now)

    def permits(self, cycle_id: str | None) -> bool:
        """Additional ownership gate, never a replacement for Phase 2A."""
        if (
            not self._initialized
            or self._closed
            or self.recovery_reason
            or self.record is None
            or self.record.get("pending")
            or self.record["phase"] == "ended"
            or cycle_id != self.cycle_id
        ):
            return False
        config = capture_inputs(self.hass, self.entry).config
        expected = self.coordinator._journal.get(
            "native_counters", self.record["counters"]
        )
        if any(
            self.entry.options.get(key) != self.record["values"].get(key)
            for key in _FLAGS
        ):
            self.block("cycle_options_changed")
        elif any(config[key] != self.record["schedule"][key] for key in _SCHEDULE):
            self.block("cycle_schedule_changed")
        elif any(self.entry.options.get(key) != expected.get(key) for key in _COUNTERS):
            self.block("cycle_counters_changed")
        elif (
            self.record["values"]["p1_done"]
            and self.record.get("reference_cycle") != cycle_id
        ):
            self.block("cycle_reference_stale")
        return self.recovery_reason is None

    async def _save(self) -> None:
        if self._write is not None and not self._write.done():
            raise RuntimeError("Native cycle write unresolved")
        self._write = self.hass.async_create_background_task(
            self.store.async_save(deepcopy(self.record)),
            "development cycle persistence",
        )
        done, _ = await asyncio.wait({self._write}, timeout=JOURNAL_TIMEOUT)
        if not done:
            self._write.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            raise RuntimeError("Native cycle write timed out")
        self._write.result()

    async def _commit(
        self,
        target: dict[str, Any],
        *,
        reset: bool = False,
        start: str | None = None,
        completing: bool = False,
    ) -> None:
        """Caller holds accounting lock; unresolved intent always blocks."""
        before = dict(self.entry.options)
        capturing = (
            target.get("reference_cycle")
            and before.get("p2_ref_vwc") != target["values"]["p2_ref_vwc"]
        )
        self.record = {**target, "pending": True}
        try:
            await self._save()
            if self._closed or self.recovery_reason:
                raise RuntimeError("Cycle transition interrupted")
            if dict(self.entry.options) != before:
                raise RuntimeError("Options changed during cycle persistence")
            current = self._decision(dt_util.now())
            if current.cycle_id != target["cycle_id"]:
                raise RuntimeError("Cycle changed during transition")
            config = capture_inputs(self.hass, self.entry).config
            if any(config[key] != target["schedule"][key] for key in _SCHEDULE):
                raise RuntimeError("Schedule changed during transition")
            if capturing and current.vwc_average != target["values"]["p2_ref_vwc"]:
                raise RuntimeError("VWC changed during reference capture")
            if start == "sensor" and not current.p1_start_eligible:
                raise RuntimeError("P1 readiness changed during transition")
            if start == "manual" and (
                current.phase != "p1_morning" or config["p1_mode"] != "manual"
            ):
                raise RuntimeError("Manual P1 window changed during transition")
            if completing and not self._completion_due(current):
                raise RuntimeError("P1 completion condition changed during transition")
            updates = dict(target["values"])
            if reset:
                updates.update(target["counters"])
            self.hass.config_entries.async_update_entry(
                self.entry, options={**before, **updates}
            )
            if reset:
                # Retain all operation/recovery records. Replace only the owned
                # accounting checkpoint, while no request can reserve this lock.
                self.coordinator._journal["native_counters"] = dict(target["counters"])
                self.coordinator._journal["last_accounting"] = {
                    "cycle_id": target["cycle_id"],
                    "values": updates,
                }
                await self.coordinator._save()
                self.coordinator._cycle_id = target["cycle_id"]
            self.record["pending"] = False
            await self._save()
            if self._closed or self.recovery_reason:
                raise RuntimeError("Cycle transition interrupted")
            async_dispatcher_send(
                self.hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{self.entry.entry_id}"
            )
            async_dispatcher_send(
                self.hass, f"{SIGNAL_SWITCH_STATE_UPDATED}_{self.entry.entry_id}"
            )
        except BaseException:
            self.block("cycle_transition_requires_review")
            raise

    async def async_sunrise(self, now: datetime | None = None) -> bool:
        """Live sunrise only. Duplicate events do nothing; no catch-up resets."""
        now = now or dt_util.now()
        async with self._lock:
            decision = self._decision(now)
            if decision.cycle_id is not None and decision.cycle_id == self.cycle_id:
                return False
            if not self._initialized or self._closed or self.recovery_reason:
                return False
            windows = decision.windows
            if windows is None:
                self.block("invalid_cycle_schedule")
                return False
            if (
                not 0
                <= (
                    now.astimezone(UTC) - windows.cycle_start.astimezone(UTC)
                ).total_seconds()
                <= 1
            ):
                self.block("missed_sunrise_requires_review")
                return False
            if self.record is not None:
                old = datetime.fromisoformat(self.cycle_id).astimezone(UTC)
                if windows.cycle_start.astimezone(UTC) <= old or not self.permits(
                    self.cycle_id
                ):
                    self.block("ambiguous_cycle_ownership")
                    return False
            else:
                if any(
                    self.entry.options.get(key) != value
                    for key, value in {**_CLEAN, **dict.fromkeys(_COUNTERS, 0)}.items()
                ):
                    self.block("explicit_clean_managed_state_required")
                    return False
                # Legacy ownership must be reviewed, even when options mask it.
                if any(
                    isinstance(self.entry.data.get(key), str)
                    and "." in self.entry.data[key]
                    for key in (*_FLAGS, *_COUNTERS)
                ):
                    self.block("legacy_cycle_migration_required")
                    return False
            active = self.coordinator.execution_active
            self.coordinator._generation += 1
            self.coordinator._interrupt("cycle_reset")
            if active:
                self.block("reset_during_shot_requires_review")
                return False
            if self.coordinator._blocked or self.coordinator._closing:
                self.block("shot_recovery_unresolved")
                return False
            async with self.coordinator._accounting_lock:
                previous_cycle = self.cycle_id
                if self.record is not None:
                    # Persist old cycle end before new-cycle reset intent.
                    self.record = {**self.record, "phase": "ended"}
                    try:
                        await self._save()
                    except BaseException:
                        self.block("cycle_end_requires_review")
                        raise
                config = capture_inputs(self.hass, self.entry).config
                await self._commit(
                    {
                        "cycle_id": decision.cycle_id,
                        "phase": "p0_transpiration",
                        "values": dict(_CLEAN),
                        "counters": dict.fromkeys(_COUNTERS, 0),
                        "reference_cycle": None,
                        "previous_cycle_end": previous_cycle,
                        "schedule": {key: config[key] for key in _SCHEDULE},
                    },
                    reset=True,
                )
            return True

    async def async_observe(self, now: datetime | None = None) -> Decision:
        """Called by the sole scheduler before authorization and after execution."""
        now = now or dt_util.now()
        decision = self._decision(now)
        if decision.cycle_id != self.cycle_id:
            await self.async_sunrise(now)
        async with self._lock:
            if not self.permits(decision.cycle_id):
                return self._decision(now)
            if self.coordinator.execution_active:
                return decision  # Capacity never interrupts an authorized shot.
            if self.coordinator._blocked or self.coordinator.state in {
                "interrupted",
                "fault",
            }:
                self.block("shot_result_requires_review")
                return decision
            async with self.coordinator._accounting_lock:
                decision = self._decision(now)
                if decision.p1_start_eligible:
                    await self._start(decision)
                elif self._completion_due(decision):
                    await self._complete(decision, automatic=True)
                decision = self._decision(now)
                if decision.phase != self.phase and not self.recovery_reason:
                    await self._commit({**self.record, "phase": decision.phase})
                return self._decision(now)

    async def _start(self, decision: Decision) -> None:
        await self._commit(
            {
                **self.record,
                "phase": "p1_morning",
                "values": {
                    **self.record["values"],
                    "p1_active": True,
                    "p1_done": False,
                    "p1_window_opened_today": True,
                    "p2_ref_vwc": 0,
                },
                "reference_cycle": None,
            },
            start=self.record["schedule"]["p1_mode"],
        )

    def _completion_due(self, decision: Decision) -> bool:
        return bool(
            self.entry.options.get("p1_active") is True
            and decision.phase == "p1_morning"
            and set(decision.blocking_reasons)
            & {"field_capacity_reached", "shot_limit_reached", "drain_sensor_wet"}
        )

    async def _complete(self, decision: Decision, *, automatic: bool = False) -> bool:
        if decision.vwc_average is None or any(
            reason.startswith("invalid_") for reason in decision.blocking_reasons
        ):
            return False
        await self._commit(
            {
                **self.record,
                "values": {
                    **self.record["values"],
                    "p1_active": False,
                    "p1_done": True,
                    "p2_ref_vwc": decision.vwc_average,
                },
                "reference_cycle": decision.cycle_id,
            },
            completing=automatic,
        )
        return True

    async def async_manual_start(self) -> bool:
        async with self._lock, self.coordinator._accounting_lock:
            now = dt_util.now()
            decision = self._decision(now)
            if not self.permits(decision.cycle_id) or self.coordinator.execution_active:
                return False
            windows = decision.windows
            if (
                self.record["schedule"]["p1_mode"] != "manual"
                or windows is None
                or not windows.p1_start.astimezone(UTC)
                <= now.astimezone(UTC)
                < windows.p1_end.astimezone(UTC)
                or self.record["values"]["p1_window_opened_today"]
            ):
                return False
            await self._start(decision)
            return True

    async def async_manual_complete(self) -> bool:
        async with self._lock, self.coordinator._accounting_lock:
            decision = self._decision(dt_util.now())
            if (
                not self.permits(decision.cycle_id)
                or self.coordinator.execution_active
                or not self.record["values"]["p1_active"]
            ):
                return False
            return await self._complete(decision)

    async def async_close(self) -> None:
        self._closed = True
        self.block("cycle_stopped_requires_review")
        async with self._lock:
            if self._write is not None and not self._write.done():
                # Storage failure must not prevent coordinator/pump cleanup.
                # Do not cancel unresolved writes or permit another owner.
                await asyncio.wait({self._write}, timeout=JOURNAL_TIMEOUT)
        # Keep the closed gate on its coordinator until that coordinator closes.


async def _async_unload_cycle(hass: Any, entry: Any) -> None:
    coordinator = hass.data.get(f"{DOMAIN}.development_shots", {}).get(entry.entry_id)
    if coordinator is not None and coordinator._cycle is not None:
        await coordinator._cycle.async_close()
