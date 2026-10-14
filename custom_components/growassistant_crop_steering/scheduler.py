"""Development-only event-driven scheduler; normal setup never constructs it.

Eligibility belongs to irrigation.py; execution and accounting belong exclusively
to the Phase 2B coordinator. External Blueprint ownership remains unresolved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import CONFIG_ENTRY_KEYS, DOMAIN
from .cycle import _NativeCycleController
from .irrigation import Decision, capture_inputs, evaluate
from .pump import _protected
from .shot_engine import (
    SIGNAL_SHOT_FINISHED,
    ShotRejected,
    ShotResult,
    ShotState,
    _ShotCoordinator,
)
from .vwc import normalize_vwc_sensors

_KEY = f"{DOMAIN}.development_schedulers"


def next_evaluation(decision: Decision, now: datetime) -> datetime | None:
    """Earliest future diagnostic boundary, never a promise of a shot.

    Compare UTC instants rather than local wall times across DST folds. Include
    soak even when other gates block: every wake captures fresh safety inputs.
    Past boundaries are discarded, so there is no missed-shot replay.
    """
    candidates = [decision.next_eligible_time, decision.soak_until]
    if decision.windows is not None:
        windows = decision.windows
        candidates.extend(
            (
                windows.cycle_start,
                windows.cycle_end,
                windows.p1_start,
                windows.p1_end,
                windows.p2_end,
                windows.sunset,
            )
        )
    instant = now.astimezone(UTC)
    future = [
        value.astimezone(UTC)
        for value in candidates
        if value is not None and value.astimezone(UTC) > instant
    ]
    return min(future) if future else None


async def _async_unload_scheduler(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cleanup hook only; never initializes a scheduler in production."""
    scheduler = hass.data.get(_KEY, {}).get(entry.entry_id)
    if scheduler is not None:
        await scheduler.async_close()


class _NativeScheduler:
    """Explicit internal fixture API. Construction/initialization cannot irrigate.

    start() is intentionally separate. No service, option or setup call exposes
    it. Interrupted/failed attempts latch this instance against automatic retry;
    stop/start does not clear the latch or coordinator recovery.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: _ShotCoordinator,
        cycle: _NativeCycleController | None = None,
    ) -> None:
        if coordinator.hass is not hass or coordinator.entry is not entry:
            raise ValueError("Coordinator must own this exact entry")
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        if cycle is not None and cycle.coordinator is not coordinator:
            raise ValueError("Cycle controller must own this coordinator")
        self.cycle = cycle
        self.decision: Decision | None = None
        self.next_evaluation: datetime | None = None
        self.retry_blocked = False
        self._initialized = False
        self._running = False
        self._closed = False
        self._generation = 0
        self._timer_revision = 0
        self._listeners: list[Callable[[], None]] = []
        self._timer: Callable[[], None] | None = None
        self._pending: Callable[[], None] | None = None
        self._task: asyncio.Task[bool] | None = None
        self._observed_options: dict[str, Any] | None = None
        self._observed_data: dict[str, Any] | None = None
        self._completed_operation: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def async_initialize(self) -> None:
        """Claim an internal harness owner; install nothing and execute nothing."""
        owners = self.hass.data.setdefault(_KEY, {})
        if self._closed or self._initialized or self.entry.entry_id in owners:
            raise RuntimeError("Scheduler already initialized or closed")
        if not self.coordinator.initialized:
            raise RuntimeError("Initialize the shot coordinator first")
        if self.cycle is not None and (
            self.coordinator._cycle is not self.cycle
            or not self.cycle._initialized
            or self.cycle._closed
        ):
            raise RuntimeError("Initialize this cycle controller first")
        owners[self.entry.entry_id] = self
        self._initialized = True

    async def async_start(self) -> None:
        """Explicit harness activation; evaluate present conditions only."""
        if not self._initialized or self._closed:
            raise RuntimeError("Scheduler must be initialized and open")
        if self._running:
            return
        if self._task is not None and not self._task.done():
            raise RuntimeError("Scheduler is still stopping")
        self._running = True
        self._generation += 1
        self._listeners = [
            self.hass.bus.async_listen("state_changed", self._state_changed),
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._shutdown),
            self.hass.bus.async_listen(
                EVENT_HOMEASSISTANT_STARTED, self._lifecycle_changed
            ),
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._lifecycle_changed
            ),
            self.entry.add_update_listener(self._options_changed),
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SHOT_FINISHED}_{self.entry.entry_id}",
                self._shot_finished,
            ),
        ]
        self.async_request_evaluation()

    def _entities(self) -> set[str]:
        """Resolve assignments on every event, including newly changed options."""
        raw = {**self.entry.data, **self.entry.options}
        entities = {
            value
            for key in (*CONFIG_ENTRY_KEYS, "last_shot", "last_shot_type")
            if isinstance(value := raw.get(key), str) and valid_entity_id(value)
        }
        entities.update(normalize_vwc_sensors(raw.get("vwc_sensor")))
        return entities

    @callback
    def _state_changed(self, event: Any) -> None:
        if event.data.get("entity_id") in self._entities():
            self.async_request_evaluation()

    async def _options_changed(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if (
            dict(entry.options) != self._observed_options
            or dict(entry.data) != self._observed_data
        ):
            self.async_request_evaluation()

    @callback
    def _shot_finished(self, result: ShotResult | None) -> None:
        if (
            result is None
            or result.state != ShotState.COMPLETED
            or not result.accounted
        ):
            self.retry_blocked = True
        if result is None or result.operation_id != self._completed_operation:
            self.async_request_evaluation()

    @callback
    def _lifecycle_changed(self, event: Any) -> None:
        self.async_request_evaluation()

    @callback
    def async_request_evaluation(self) -> None:
        """Coalesce a burst; discard triggers during execution, never queue shots."""
        if (
            not self._running
            or self._pending is not None
            or (self._task is not None and not self._task.done())
            or self.coordinator.execution_active
        ):
            return
        self._cancel_timer()
        generation = self._generation

        @callback
        def run(_: Any) -> None:
            if generation != self._generation or not self._running:
                return
            self._pending = None
            self._task = self.hass.async_create_background_task(
                self._evaluate(generation), "development irrigation scheduler"
            )
            self._task.add_done_callback(self._evaluation_finished)

        self._pending = async_call_later(self.hass, 0, run)

    @callback
    def _evaluation_finished(self, task: asyncio.Task[bool]) -> None:
        if not task.cancelled() and task.exception() is not None:
            # Unexpected failures must not turn subsequent events into retries.
            self.retry_blocked = True
        elif not task.cancelled() and task.result():
            # Successful zero-soak completion may authorize another fresh shot.
            # Only after this task has ended; active triggers were discarded.
            self.async_request_evaluation()

    async def _evaluate(self, generation: int) -> bool:
        attempted = False
        now = dt_util.now()
        if self.cycle is not None:
            await self.cycle.async_observe(now)
            now = dt_util.now()
        self.decision = evaluate(capture_inputs(self.hass, self.entry), now)
        self._observed_options = dict(self.entry.options)
        self._observed_data = dict(self.entry.data)
        if (
            self.decision.eligible
            and not self.retry_blocked
            and self.coordinator.execution_available
        ):
            attempted = True
            try:
                # Coordinator captures again and reserves without an intervening
                # await. This diagnostic never substitutes for its authorization.
                result = await self.coordinator.async_execute()
                self._completed_operation = result.operation_id
                if result.state != ShotState.COMPLETED or not result.accounted:
                    self.retry_blocked = True
            except ShotRejected:
                # A rejected request must not spin or retry automatically.
                self.retry_blocked = True
            except asyncio.CancelledError:
                self.retry_blocked = True
                raise
            except Exception:
                self.retry_blocked = True
                raise
            if generation != self._generation or not self._running:
                return False
            # Completion/interruption diagnostics are fresh. Do not recursively
            # dispatch, even with zero soak; each request needs a separate wake.
            now = dt_util.now()
            if self.cycle is not None:
                await self.cycle.async_observe(now)
            self.decision = evaluate(capture_inputs(self.hass, self.entry), now)
            self._observed_options = dict(self.entry.options)
            self._observed_data = dict(self.entry.data)
        if generation == self._generation and self._running:
            self._arm_timer(next_evaluation(self.decision, now))
        return attempted and self.decision.eligible and not self.retry_blocked

    @callback
    def _cancel_timer(self) -> None:
        self._timer_revision += 1
        if self._timer is not None:
            self._timer()
            self._timer = None
        self.next_evaluation = None

    @callback
    def _arm_timer(self, when: datetime | None) -> None:
        self._cancel_timer()
        if when is None:
            return
        self.next_evaluation = when
        generation = self._generation
        revision = self._timer_revision

        @callback
        def wake(_: datetime) -> None:
            if (
                generation != self._generation
                or revision != self._timer_revision
                or not self._running
            ):
                return
            self._timer = None
            self.next_evaluation = None
            self.async_request_evaluation()

        self._timer = async_track_point_in_utc_time(self.hass, wake, when)

    @callback
    def _deactivate(self) -> None:
        self._running = False
        self._generation += 1
        self._cancel_timer()
        if self._pending is not None:
            self._pending()
            self._pending = None
        for remove in self._listeners:
            remove()
        self._listeners.clear()

    async def async_stop(self) -> None:
        """Detach synchronously, then drain coordinator cancellation safely."""
        self._deactivate()
        if self.cycle is not None:
            self.cycle.block("scheduler_stopped_requires_review")
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await _protected(
                self.hass.async_create_background_task(
                    self._drain(), "development scheduler stop"
                )
            )

    async def _drain(self) -> None:
        await asyncio.gather(self._task, return_exceptions=True)

    async def _shutdown(self, event: Any) -> None:
        await self.async_close()

    async def async_close(self) -> None:
        """Close permanently; integration unload calls this before shot cleanup."""
        self._closed = True
        try:
            await self.async_stop()
        finally:
            owners = self.hass.data.get(_KEY, {})
            if owners.get(self.entry.entry_id) is self:
                owners.pop(self.entry.entry_id)
