"""Opt-in pump safety primitive; deliberately unused by integration setup.

One controller owns the native pump resource per HA instance. Callers must not
share that resource with the legacy Blueprint when enabling native execution.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import StrEnum
from typing import Any, TypeVar
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config import configured_entity_value
from .const import CONF_PUMP_SWITCH, DOMAIN
from .native_safety import OwnershipEvidence

HARD_MAX_RUNTIME = 300.0
SERVICE_TIMEOUT = 5.0
CONFIRM_TIMEOUT = 5.0
STORAGE_TIMEOUT = 5.0
_OWNER_KEY = f"{DOMAIN}.native_pump_owner"
T = TypeVar("T")


class PumpState(StrEnum):
    """Public states; faults are cleared only by successful reconciliation."""

    NEW = "new"
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAULT = "fault"
    CLOSED = "closed"


class PumpError(Exception):
    """Activation rejected or safety operation failed."""


async def _protected(task: asyncio.Task[T]) -> T:
    """Finish safety work even under repeated caller cancellation, then propagate it."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve errors as well, avoiding unobserved task exceptions.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class PumpController:
    """Serialize explicit activation, bounded shutdown and durable reconciliation.

    Construction has no effects. async_initialize loads recovery and installs the
    shutdown hook, but never turns on a pump. async_close is the unload hook.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.pump_recovery.{entry.entry_id}"
        )
        self.state = PumpState.NEW
        self.fault: str | None = None
        self.recovery_reasons: tuple[str, ...] = ()
        self.restart_had_recovery = False
        self.ownership: OwnershipEvidence | None = None
        self._record: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._stop_requested = False
        self._closing = False
        self._cancelled_activations = 0
        self._stop_task: asyncio.Task[bool] | None = None
        self._deadline_handle: asyncio.TimerHandle | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self._timed_out: dict[asyncio.Task[Any], str] = {}
        self._storage_pending: set[asyncio.Task[Any]] = set()
        self._remove_shutdown = None

    @property
    def recovery_record(self) -> dict[str, Any] | None:
        """Return a copy so callers cannot reassign an active recovery target."""
        return dict(self._record) if isinstance(self._record, dict) else None

    def _task(self, work: Awaitable[T]) -> asyncio.Task[T]:
        return self.hass.async_create_background_task(work, "native pump safety")

    async def async_initialize(self) -> None:
        """Explicitly opt in to recovery only; no integration path calls this yet."""
        await _protected(self._task(self._initialize()))

    async def _initialize(self) -> None:
        async with self._lock:
            if self.state != PumpState.NEW:
                raise PumpError("Controller already initialized")
            if _OWNER_KEY in self.hass.data:
                raise PumpError("Another native pump controller owns this HA instance")
            self.hass.data[_OWNER_KEY] = self
            self._remove_shutdown = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._shutdown
            )
            try:
                async with asyncio.timeout(STORAGE_TIMEOUT):
                    self._record = await self.store.async_load()
            except Exception as err:
                self._set_fault(f"Recovery load failed: {err}", "storage_inconsistency")
                return
            if self._record is None:
                self.state = PumpState.IDLE
                return
            self.restart_had_recovery = True
            # Never discard unreadable records or trust a stored 'idle' state.
            if not isinstance(self._record, dict) or not self._valid_entity(
                self._record.get("entity_id")
            ):
                self._set_fault(
                    "Invalid recovery record; manual repair required",
                    "storage_inconsistency",
                )
                return
            await self._stop_locked()

    async def async_activate(
        self,
        runtime: float,
        *,
        _entity_id: str | None = None,
        _operation_id: str | None = None,
        _revalidate: Callable[[], bool] | None = None,
    ) -> str:
        """Start the currently selected pump for at most runtime seconds.

        Returns an operation ID after service completion and observed on state.
        Cancellation requests shutdown and waits for safety work before propagating.
        Private keywords bind a development-shot reservation and its final
        synchronous safety check; ordinary Phase 1 callers are unchanged.
        """
        if (
            isinstance(runtime, bool)
            or not isinstance(runtime, (int, float))
            or not math.isfinite(runtime)
            or not 0 < runtime <= HARD_MAX_RUNTIME
        ):
            raise PumpError(
                f"Runtime must be finite and within (0, {HARD_MAX_RUNTIME}]"
            )
        task = self._task(
            self._activate(float(runtime), _entity_id, _operation_id, _revalidate)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._stop_requested = True
            self._cancelled_activations += 1
            try:
                try:
                    await _protected(task)
                except (PumpError, asyncio.CancelledError):
                    pass
                await self.async_stop()
            finally:
                self._cancelled_activations -= 1
            raise

    @staticmethod
    def _valid_entity(entity_id: Any) -> bool:
        return (
            isinstance(entity_id, str)
            and valid_entity_id(entity_id)
            and entity_id.split(".", 1)[0] in {"switch", "input_boolean"}
        )

    async def _activate(
        self,
        runtime: float,
        expected_entity: str | None = None,
        reserved_operation: str | None = None,
        revalidate: Callable[[], bool] | None = None,
    ) -> str:
        async with self._lock:
            if (
                self.state != PumpState.IDLE
                or self.restart_had_recovery
                or self._closing
                or self._cancelled_activations
                or self._stop_requested
                or (self._stop_task is not None and not self._stop_task.done())
            ):
                raise PumpError(f"Activation blocked: {self.state}, {self.fault}")
            entity_id = configured_entity_value(self.entry, CONF_PUMP_SWITCH)
            if expected_entity is not None and entity_id != expected_entity:
                raise PumpError("Reserved pump assignment changed")
            state = (
                self.hass.states.get(entity_id)
                if self._valid_entity(entity_id)
                else None
            )
            if state is None or state.state != "off":
                raise PumpError("Pump must be a valid, available, observed-off entity")
            domain = entity_id.split(".", 1)[0]
            if not all(
                self.hass.services.has_service(domain, service)
                for service in ("turn_on", "turn_off")
            ):
                raise PumpError("Pump on/off services are unavailable")
            ownership = self.ownership
            if ownership is None or ownership.reasons(entity_id):
                raise PumpError(
                    "Pump ownership unknown, unverified or production activation prohibited"
                )
            monotonic_start = self.hass.loop.time()
            now = dt_util.utcnow()
            operation_id = reserved_operation or uuid4().hex
            self._record = {
                "entity_id": entity_id,
                "operation_id": operation_id,
                "activated_at": now.isoformat(),
                "deadline": (now + timedelta(seconds=runtime)).isoformat(),
                "state": PumpState.STARTING.value,
            }
            self.state = PumpState.STARTING
            try:
                await self._save_record()
                # The monotonic deadline includes storage and activation latency.
                remaining = runtime - (self.hass.loop.time() - monotonic_start)
                if self._closing or self._stop_requested or remaining <= 0:
                    raise PumpError("Activation aborted before service dispatch")
                if self.ownership != ownership or ownership.reasons(entity_id):
                    raise PumpError("Pump ownership changed before dispatch")
                if revalidate is not None and not revalidate():
                    raise PumpError("Final shot safety revalidation failed")
                self._deadline_handle = self.hass.loop.call_later(
                    remaining, self._deadline_expired
                )
                await self._service(
                    entity_id, "turn_on", min(SERVICE_TIMEOUT, remaining)
                )
                if self._stop_requested:
                    raise PumpError("Stop requested during activation")
                observed = self.hass.states.get(entity_id)
                if observed is None or observed.state != "on":
                    raise PumpError("Activation state was not confirmed")
                self.state = PumpState.RUNNING
                self._record["state"] = self.state.value
                remaining = runtime - (self.hass.loop.time() - monotonic_start)
                async with asyncio.timeout(max(0, remaining)):
                    await self._save_record()
                if self._stop_requested:
                    raise PumpError("Stop requested during activation persistence")
                return operation_id
            except Exception as err:
                await self._stop_locked()
                raise PumpError(f"Activation failed: {err}") from err

    def _deadline_expired(self) -> None:
        self._deadline_handle = None
        self._stop_requested = True
        self._ensure_stop_task()

    async def _service(self, entity_id: str, service: str, timeout: float) -> None:
        task = self._task(
            self.hass.services.async_call(
                entity_id.split(".", 1)[0],
                service,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        )
        self._pending.add(task)
        # Do not cancel timed-out HA handlers: cancellation cannot undo delivery.
        task.add_done_callback(self._service_finished)
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            self._timed_out[task] = self._record["operation_id"]
            raise PumpError(f"Ambiguous {service} timeout")
        self._pending.discard(task)
        if task.cancelled():
            raise PumpError(f"Ambiguous {service} service cancellation")
        task.result()

    def _service_finished(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        operation_id = self._timed_out.pop(task, None)
        if operation_id is not None:
            self._task(self._reconcile_late_service(operation_id))

    async def _reconcile_late_service(self, operation_id: str) -> None:
        """A late on completion must trigger another off, never another shot."""
        async with self._lock:
            if (
                self._record is not None
                and self._record.get("operation_id") == operation_id
            ):
                await self._stop_locked()

    def _ensure_stop_task(self) -> asyncio.Task[bool]:
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = self._task(self._stop())
        return self._stop_task

    async def async_stop(self) -> bool:
        """Idempotent stop/reconcile. True means fresh observed off and durable clear."""
        self._stop_requested = True
        return await _protected(self._ensure_stop_task())

    async def _stop(self) -> bool:
        async with self._lock:
            return await self._stop_locked()

    def _set_fault(self, reason: str, code: str = "unconfirmed_pump_shutdown") -> None:
        self.state = PumpState.FAULT
        self.fault = reason
        self.recovery_reasons = (code,)

    async def _stop_locked(self) -> bool:
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
            self._deadline_handle = None
        if self._record is None:
            if self.state == PumpState.FAULT:
                return False  # A failed load must never become a clean slate.
            if not self._closing and self.state != PumpState.NEW:
                self.state = PumpState.IDLE
            self._stop_requested = False
            return True
        if not isinstance(self._record, dict) or not self._valid_entity(
            self._record.get("entity_id")
        ):
            self._set_fault("Invalid recovery target", "storage_inconsistency")
            return False
        self.state = PumpState.STOPPING
        self._record["state"] = self.state.value
        entity_id = self._record["entity_id"]
        changed = asyncio.Event()
        remove = async_track_state_change_event(
            self.hass, [entity_id], lambda event: changed.set()
        )
        before = self.hass.states.get(entity_id)
        prior_report = before.last_reported if before is not None else None
        failure = None
        try:
            # Attempt off immediately, but latch ambiguity for this reconciliation.
            # An older call can finish after off; only a later retry may clear it.
            self._pending = {task for task in self._pending if not task.done()}
            earlier_unresolved = bool(self._pending)
            try:
                await self._service(entity_id, "turn_off", SERVICE_TIMEOUT)
            except Exception as err:
                failure = str(err)
            async with asyncio.timeout(CONFIRM_TIMEOUT):
                while True:
                    changed.clear()
                    observed = self.hass.states.get(entity_id)
                    if (
                        observed is not None
                        and observed.state == "off"
                        and (
                            observed is not before
                            or prior_report is None
                            or observed.last_reported > prior_report
                        )
                    ):
                        break
                    # HA may report an unchanged off state without state_changed.
                    # Recheck last_reported too, without blocking the event loop.
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=0.01)
                    except TimeoutError:
                        pass
            self._pending = {task for task in self._pending if not task.done()}
            if failure or earlier_unresolved or self._pending:
                raise PumpError(failure or "Earlier service still unresolved")
            # Only a durable clear releases the gate; failure retains the target.
            await self._write(None)
            self._record = None
            self.state = PumpState.IDLE
            self.fault = None
            self.recovery_reasons = ()
            self._stop_requested = False
            return True
        except Exception as err:
            self._set_fault(f"Shutdown unverified: {err}")
            self._record["state"] = PumpState.FAULT.value
            try:
                await self._save_record()
            except Exception:
                pass
            return False
        finally:
            remove()

    async def _save_record(self) -> None:
        """Bound storage waits; preserve an independent snapshot for serialization."""
        await self._write(dict(self._record))

    async def _write(self, data: dict[str, Any] | None) -> None:
        # Store can offload disk IO. Cancelling its await does not undo the write;
        # never let a late old snapshot overwrite a newer operation's record.
        self._storage_pending = {
            task for task in self._storage_pending if not task.done()
        }
        if self._storage_pending:
            raise PumpError("Previous recovery write remains unresolved")
        task = self._task(self.store.async_save(data))
        self._storage_pending.add(task)
        task.add_done_callback(
            lambda finished: finished.exception() if not finished.cancelled() else None
        )
        done, _ = await asyncio.wait({task}, timeout=STORAGE_TIMEOUT)
        if not done:
            raise PumpError("Recovery write timeout")
        self._storage_pending.discard(task)
        if task.cancelled():
            raise PumpError("Recovery write cancelled")
        task.result()

    async def _shutdown(self, event: Any) -> None:
        # The once-listener has already removed itself before invoking us.
        self._remove_shutdown = None
        await self.async_close()

    async def async_close(self) -> bool:
        """Unload/shutdown hook; block starts immediately and finish shutdown safely."""
        self._closing = True
        return await _protected(self._task(self._close()))

    async def _close(self) -> bool:
        confirmed = await self.async_stop()
        if confirmed and self._remove_shutdown is not None:
            self._remove_shutdown()
            self._remove_shutdown = None
        self._pending = {task for task in self._pending if not task.done()}
        if confirmed and not self._pending and self.hass.data.get(_OWNER_KEY) is self:
            self.hass.data.pop(_OWNER_KEY)
        if confirmed:
            self.state = PumpState.CLOSED
        return confirmed


async def async_unload_controller(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Close an explicitly initialized owner before entry unload; otherwise no-op."""
    owner = hass.data.get(_OWNER_KEY)
    if owner is not None and owner.entry.entry_id == entry.entry_id:
        return await owner.async_close()
    return True
