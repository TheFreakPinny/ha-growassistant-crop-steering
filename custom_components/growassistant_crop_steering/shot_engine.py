"""Development-only single shots. Never constructed by integration setup.

No scheduler or public activation surface. See docs/native-shot-engine.md for
ownership, active safety policy and the intentionally fail-closed crash journal.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_LAST_SHOT_UPDATED
from .irrigation import Decision, Inputs, capture_inputs, evaluate
from .number import SIGNAL_NUMBER_STATE_UPDATED
from .pump import HARD_MAX_RUNTIME, PumpController, PumpState, _protected

_KEY = f"{DOMAIN}.development_shots"
MONITOR_INTERVAL = 0.01
JOURNAL_TIMEOUT = 5.0
_PREFIX = {"p1_morning": "p1", "p2_midday": "p2", "p3_dryback": "p3_emergency"}


class ShotState(StrEnum):
    """Explicit execution lifecycle, independent of controller lifecycle."""

    IDLE = "idle"
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAULT = "fault"


class ShotRejected(Exception):
    """No reservation/activation was authorized."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(", ".join(reasons))


@dataclass(frozen=True)
class ShotSnapshot:
    """Immutable reservation; configuration and pump ownership cannot drift."""

    operation_id: str
    decision: Decision
    inputs: Inputs
    generation: int


@dataclass(frozen=True)
class ShotResult:
    """Only COMPLETED with accounted=True represents a successful counted shot."""

    operation_id: str
    cycle_id: str
    state: ShotState
    reason: str | None
    accounted: bool


def _owners(hass: HomeAssistant) -> dict[str, _ShotCoordinator]:
    return hass.data.setdefault(_KEY, {})


def _cycle_reset_started(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """Invalidate immediately, then serialize reset with final accounting."""
    owner = hass.data.get(_KEY, {}).get(entry.entry_id)
    if owner is None:
        return _NoopLock()
    owner._generation += 1
    owner._interrupt("cycle_reset")
    owner._cycle_id = evaluate(capture_inputs(hass, entry), dt_util.now()).cycle_id
    return owner._accounting_lock


class _NoopLock:
    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args: Any) -> None:
        pass


async def _async_unload_shots(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    owner = hass.data.get(_KEY, {}).get(entry.entry_id)
    return True if owner is None else await owner.async_close()


class _ShotCoordinator:
    """Internal harness API. Requires an explicitly initialized controller.

    Initializing asserts ownership was established outside production setup; it
    does not establish exclusive ownership against arbitrary user automations.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller: PumpController
    ) -> None:
        if controller.hass is not hass or controller.entry is not entry:
            raise ValueError("Controller must own this exact entry")
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.shot_journal.{entry.entry_id}"
        )
        self.state = ShotState.IDLE
        self.snapshot: ShotSnapshot | None = None
        self._journal: dict[str, Any] = {"operations": {}}
        self._accounting_lock = asyncio.Lock()
        self._generation = 0
        self._cycle_id: str | None = None
        self._task: asyncio.Task[ShotResult] | None = None
        self._abort = asyncio.Event()
        self._reason: str | None = None
        self._closing = False
        self._ready = False
        self._blocked = False
        self._remove_shutdown = None
        self._storage_task: asyncio.Task[Any] | None = None
        self._results: dict[str, ShotResult] = {}

    def _background(self, work: Any) -> asyncio.Task[Any]:
        return self.hass.async_create_background_task(work, "development single shot")

    async def async_initialize(self) -> None:
        """Load journal; unresolved operations require manual review, never replay."""
        if self._ready or self.entry.entry_id in _owners(self.hass):
            raise ShotRejected(("coordinator_already_owned",))
        if self.controller.state != PumpState.IDLE:
            raise ShotRejected(("pump_recovery_unresolved",))
        _owners(self.hass)[self.entry.entry_id] = self
        self._remove_shutdown = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._shutdown
        )
        try:
            self._cycle_id = evaluate(
                capture_inputs(self.hass, self.entry), dt_util.now()
            ).cycle_id
            async with asyncio.timeout(JOURNAL_TIMEOUT):
                journal = await self.store.async_load()
            if journal is not None:
                if (
                    not isinstance(journal, dict)
                    or not isinstance(journal.get("operations"), dict)
                    or any(
                        not isinstance(record, dict)
                        or record.get("state")
                        not in {state.value for state in ShotState}
                        for record in journal["operations"].values()
                    )
                ):
                    raise ValueError("invalid shot journal")
                self._journal = journal
                for record in journal["operations"].values():
                    if record["state"] not in {
                        ShotState.COMPLETED,
                        ShotState.INTERRUPTED,
                        ShotState.FAULT,
                    } or record.get("accounting_pending"):
                        record.update(
                            state=ShotState.INTERRUPTED.value,
                            reason="restart_requires_review",
                            recovery_required=True,
                        )
                    self._blocked |= bool(record.get("recovery_required"))
                checkpoint = journal.get("last_accounting")
                if checkpoint is not None:
                    if not isinstance(checkpoint, dict) or not isinstance(
                        checkpoint.get("values"), dict
                    ):
                        raise ValueError("invalid accounting checkpoint")
                    current = evaluate(
                        capture_inputs(self.hass, self.entry), dt_util.now()
                    )
                    if current.cycle_id == checkpoint.get("cycle_id") and any(
                        self.entry.options.get(key) != value
                        for key, value in checkpoint["values"].items()
                    ):
                        self._blocked = True
                        journal["recovery_required"] = True
                    elif current.cycle_id != checkpoint.get("cycle_id") and any(
                        capture_inputs(self.hass, self.entry).config[key] != 0
                        for key in (
                            "p1_shots_done",
                            "p2_shots_done",
                            "p3_emergency_shots_done",
                        )
                    ):
                        self._blocked = True
                        journal["recovery_required"] = True
                self._blocked |= bool(journal.get("recovery_required"))
                await self._save()
            self._ready = True
            if self._blocked:
                self.state = ShotState.FAULT
        except Exception:
            self._blocked = True
            self.state = ShotState.FAULT
            raise

    async def _save(self) -> None:
        # Do not cancel disk writes or allow late snapshots to overwrite newer ones.
        if self._storage_task is not None and not self._storage_task.done():
            raise RuntimeError("Previous journal write unresolved")
        self._storage_task = self._background(
            self.store.async_save(deepcopy(self._journal))
        )
        done, _ = await asyncio.wait({self._storage_task}, timeout=JOURNAL_TIMEOUT)
        if not done:
            self._storage_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            raise RuntimeError("Shot journal write timeout")
        self._storage_task.result()

    async def async_execute(self, operation_id: str | None = None) -> ShotResult:
        """One explicit internal request. Concurrent/duplicate calls never queue."""
        operation_id = operation_id or uuid4().hex
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
        ):
            raise ShotRejected(("invalid_operation_id",))
        if (
            not self._ready
            or self._blocked
            or self._closing
            or self.controller.state != PumpState.IDLE
        ):
            raise ShotRejected(("execution_unavailable",))
        if self._task is not None and not self._task.done():
            raise ShotRejected(("shot_in_progress",))
        if self._accounting_lock.locked():
            raise ShotRejected(("cycle_reset_in_progress",))
        if operation_id in self._journal["operations"]:
            raise ShotRejected(("duplicate_operation",))
        inputs = capture_inputs(self.hass, self.entry)
        decision = evaluate(inputs, dt_util.now())
        if decision.cycle_id != self._cycle_id:
            raise ShotRejected(("cycle_reset_required",))
        if not decision.eligible:
            raise ShotRejected(decision.blocking_reasons)
        if not 0 < decision.duration_s <= HARD_MAX_RUNTIME:
            raise ShotRejected(("controller_runtime_limit",))
        self.snapshot = ShotSnapshot(operation_id, decision, inputs, self._generation)
        self._drain_assignment = {
            key: self.entry.options.get(key, self.entry.data.get(key))
            for key in ("drain_sensor", "drain_tray_sensor")
        }
        self._abort = asyncio.Event()
        self._reason = None
        self.state = ShotState.RESERVED
        self._journal["operations"][operation_id] = {
            "state": self.state.value,
            "cycle_id": decision.cycle_id,
            "pump_entity": decision.pump_entity,
            "phase": decision.phase,
            "duration_s": decision.duration_s,
            "config": dict(inputs.config),
            "vwc": dict(inputs.vwc),
            "reserved_at": dt_util.utcnow().isoformat(),
            "accounted": False,
        }
        # No await before task assignment: reservation is serialized on HA's loop.
        self._task = self._background(self._execute(self.snapshot))
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            self._interrupt("cancelled")
            try:
                await _protected(self._task)
            except asyncio.CancelledError:
                pass
            raise

    @callback
    def _interrupt(self, reason: str) -> None:
        if self._task is not None and not self._task.done():
            self._reason = self._reason or reason
            self._abort.set()

    def _active_safety(self, snapshot: ShotSnapshot) -> str | None:
        if self._closing:
            return "integration_stopping"
        if snapshot.generation != self._generation:
            return "cycle_reset"
        inputs = capture_inputs(self.hass, self.entry)
        config = inputs.config
        if config["pump_switch"] != snapshot.decision.pump_entity:
            return "pump_reassigned"
        if config["pump_state"] not in {"on", "off"}:
            return "pump_unavailable"
        if (
            self.state == ShotState.RUNNING
            and self.controller.state == PumpState.RUNNING
            and config["pump_state"] != "on"
        ):
            return "pump_stopped_early"
        # Sensor ownership, drain requirements and light schedule are fixed.
        for key in ("vwc_sensor", "led_sunrise", "led_sunset"):
            if config[key] != snapshot.inputs.config[key]:
                return "safety_configuration_changed"
        prefix = _PREFIX[snapshot.decision.phase]
        for key in (
            ("drain_sensor", "drain_tray_sensor")
            if prefix == "p1"
            else ("drain_tray_sensor",)
        ):
            raw = self.entry.options.get(key, self.entry.data.get(key))
            if raw != self._drain_assignment[key]:
                return "drain_reassigned"
            if snapshot.inputs.config[key + "_configured"] and config[key] not in {
                "off",
                "dry",
                "closed",
                "clear",
            }:
                return key + "_unsafe"
        # Eligibility expects off. Substitute ONLY that expected running state.
        diagnostic = evaluate(
            Inputs({**config, "pump_state": "off"}, inputs.vwc), dt_util.now()
        )
        # Approved P1 policy: field_capacity_reached prevents a NEW shot only.
        # Do not shorten, extend or restart an authorized shot; independent
        # safety faults below still interrupt it. P2/P3 policy is unchanged.
        critical = [
            r
            for r in diagnostic.blocking_reasons
            if r.startswith("invalid_") or r == "vwc_unavailable"
        ]
        if critical:
            return critical[0]
        if diagnostic.cycle_id != snapshot.decision.cycle_id:
            return "cycle_changed"
        if self.controller.state in {PumpState.FAULT, PumpState.CLOSED}:
            return "controller_fault"
        return None

    def _revalidate(self, snapshot: ShotSnapshot) -> bool:
        reason = self._active_safety(snapshot)
        if reason:
            self._interrupt(reason)
        current = evaluate(capture_inputs(self.hass, self.entry), dt_util.now())
        return (
            not self._abort.is_set()
            and current.eligible
            and all(
                getattr(current, key) == getattr(snapshot.decision, key)
                for key in (
                    "phase",
                    "pump_entity",
                    "duration_s",
                    "cycle_id",
                    "shots_done",
                    "shot_limit",
                )
            )
            and dict(capture_inputs(self.hass, self.entry).config)
            == dict(snapshot.inputs.config)
        )

    async def _supervise(
        self, snapshot: ShotSnapshot, changed: asyncio.Event, deadline: float
    ) -> None:
        while not self._abort.is_set():
            if self.state == ShotState.STOPPING:
                return
            reason = self._active_safety(snapshot)
            if reason:
                self._interrupt(reason)
                break
            if (
                self.state == ShotState.RUNNING
                and self.controller.state == PumpState.IDLE
                and self.hass.loop.time() < deadline - MONITOR_INTERVAL
            ):
                self._interrupt("controller_stopped_early")
                break
            if self.hass.loop.time() >= deadline:
                self._interrupt("deadline_exceeded")
                break
            changed.clear()
            try:
                await asyncio.wait_for(changed.wait(), MONITOR_INTERVAL)
            except TimeoutError:
                pass
        if self._abort.is_set():
            await self.controller.async_stop()

    async def _execute(self, snapshot: ShotSnapshot) -> ShotResult:
        record = self._journal["operations"][snapshot.operation_id]
        changed = asyncio.Event()
        observed_vwc = dict(snapshot.inputs.vwc)

        @callback
        def state_changed(event: Any) -> None:
            changed.set()
            entity = event.data["entity_id"]
            state = event.data["new_state"]
            value = None if state is None else state.state
            # Latch transient dangerous reports even if a later clear report
            # arrives before the supervision coroutine resumes.
            if entity == snapshot.decision.pump_entity and value not in {"on", "off"}:
                self._interrupt("pump_unavailable")
            elif (
                entity == snapshot.decision.pump_entity
                and value == "off"
                and self.state == ShotState.RUNNING
                and self.controller.state == PumpState.RUNNING
            ):
                self._interrupt("pump_stopped_early")
            prefix = _PREFIX[snapshot.decision.phase]
            for key in (
                ("drain_sensor", "drain_tray_sensor")
                if prefix == "p1"
                else ("drain_tray_sensor",)
            ):
                if entity == self._drain_assignment[key] and value not in {
                    "off",
                    "dry",
                    "closed",
                    "clear",
                }:
                    self._interrupt(key + "_unsafe")
            if entity in observed_vwc:
                observed_vwc[entity] = value
                diagnostic = evaluate(
                    Inputs(snapshot.inputs.config, observed_vwc), dt_util.now()
                )
                for reason in ("invalid_vwc", "vwc_unavailable"):
                    if reason in diagnostic.blocking_reasons:
                        self._interrupt(reason)

        remove = self.hass.bus.async_listen("state_changed", state_changed)
        supervisor = None
        success = False
        try:
            await self._save()  # reservation must be durable before activation
            if not self._revalidate(snapshot):
                self._interrupt("reservation_changed")
                return await self._finish(snapshot, False)
            self.state = ShotState.STARTING
            record["state"] = self.state.value
            await self._save()
            started = self.hass.loop.time()
            # Controller's hard deadline includes activation latency. Allow only
            # its bounded shutdown verification after nominal shot duration.
            supervisor = self._background(
                self._supervise(
                    snapshot,
                    changed,
                    started + snapshot.decision.duration_s + MONITOR_INTERVAL,
                )
            )
            await self.controller.async_activate(
                snapshot.decision.duration_s,
                _entity_id=snapshot.decision.pump_entity,
                _operation_id=snapshot.operation_id,
                _revalidate=lambda: self._revalidate(snapshot),
            )
            if self._abort.is_set():
                return await self._finish(snapshot, False)
            self.state = ShotState.RUNNING
            record["state"] = self.state.value
            # Nominal duration is elapsed from controller activation request,
            # not another full duration after a delayed service completion.
            remaining = max(
                0, snapshot.decision.duration_s - (self.hass.loop.time() - started)
            )
            try:
                await asyncio.wait_for(self._abort.wait(), remaining)
            except TimeoutError:
                success = self.controller.state in {
                    PumpState.RUNNING,
                    PumpState.STOPPING,
                    PumpState.IDLE,
                }
            return await self._finish(snapshot, success)
        except Exception as err:
            self._reason = self._reason or f"execution_failed: {err}"
            return await self._finish(snapshot, False, fault=not self._abort.is_set())
        except asyncio.CancelledError:
            self._interrupt("execution_cancelled")
            return await _protected(self._background(self._finish(snapshot, False)))
        finally:
            remove()
            if supervisor is not None:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)

    async def _finish(
        self, snapshot: ShotSnapshot, success: bool, *, fault: bool = False
    ) -> ShotResult:
        if snapshot.operation_id in self._results:
            return self._results[snapshot.operation_id]
        record = self._journal["operations"][snapshot.operation_id]
        reason = self._active_safety(snapshot)
        if reason:
            self._interrupt(reason)
        self.state = ShotState.STOPPING
        stopped = await self.controller.async_stop()
        # Revalidate after off acknowledgement; a concurrent reset/cancellation
        # or configuration change must never be converted into success.
        reason = self._active_safety(snapshot)
        if reason:
            self._interrupt(reason)
        success = success and stopped and not self._abort.is_set()
        terminal = (
            ShotState.COMPLETED
            if success
            else ShotState.FAULT
            if fault or not stopped
            else ShotState.INTERRUPTED
        )
        if not stopped:
            self._reason = "pump_shutdown_unverified"
            self._blocked = True
        # Interrupted shots never write managed options and must not wait for a
        # potentially delayed legacy helper reset before finishing safe unload.
        async with self._accounting_lock if success else _NoopLock():
            if snapshot.operation_id in self._results:
                return self._results[snapshot.operation_id]
            if snapshot.generation != self._generation:
                success = False
                terminal = ShotState.INTERRUPTED if stopped else ShotState.FAULT
            record.update(
                state=terminal.value,
                reason=self._reason,
                finished_at=dt_util.utcnow().isoformat(),
                recovery_required=not stopped,
            )
            try:
                previous_options = dict(self.entry.options)
                previous_checkpoint = self._journal.get("last_accounting")
                accounting_values = None
                if success:
                    # Write intent before the single synchronous managed-options
                    # update. Crash anywhere in this window requires review.
                    record["accounting_pending"] = True
                    await self._save()
                    reason = self._active_safety(snapshot)
                    if reason:
                        self._interrupt(reason)
                    if self._abort.is_set() or snapshot.generation != self._generation:
                        terminal = ShotState.INTERRUPTED
                        record.update(
                            state=terminal.value,
                            accounting_pending=False,
                            reason=self._reason,
                        )
                    else:
                        prefix = _PREFIX[snapshot.decision.phase]
                        options = dict(self.entry.options)
                        current = float(
                            capture_inputs(self.hass, self.entry).config[
                                prefix + "_shots_done"
                            ]
                        )
                        if current != snapshot.decision.shots_done:
                            raise RuntimeError("Managed counter changed during shot")
                        accounting_values = {
                            prefix + "_shots_done": int(current) + 1,
                            "last_shot": record["finished_at"],
                            "last_shot_type": prefix,
                        }
                        options.update(accounting_values)
                        self.hass.config_entries.async_update_entry(
                            self.entry, options=options
                        )
                        record.update(accounted=True, accounting_pending=False)
                        self._journal["last_accounting"] = {
                            "cycle_id": snapshot.decision.cycle_id,
                            "values": accounting_values,
                        }
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_NUMBER_STATE_UPDATED}_{self.entry.entry_id}",
                        )
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_LAST_SHOT_UPDATED}_{self.entry.entry_id}",
                        )
                await self._save()
                if record["accounted"] and self._abort.is_set():
                    # Cancellation/reset while the final journal write was in
                    # flight cannot leave a successful result. The reset lock is
                    # still held; compensate ONLY our unchanged managed values.
                    record.update(
                        state=ShotState.INTERRUPTED.value,
                        accounting_pending=True,
                        reason=self._reason,
                    )
                    await self._save()
                    self._undo_accounting(
                        previous_options, accounting_values, previous_checkpoint
                    )
                    record.update(accounted=False, accounting_pending=False)
                    terminal = ShotState.INTERRUPTED
                    await self._save()
            except Exception as err:
                if record["accounted"] and accounting_values is not None:
                    try:
                        self._undo_accounting(
                            previous_options, accounting_values, previous_checkpoint
                        )
                        record["accounted"] = False
                    except Exception:
                        # External writers changed the managed values; do not
                        # overwrite them. Retain a manual-review recovery fault.
                        pass
                terminal = ShotState.FAULT
                self._blocked = True
                self._reason = f"accounting_or_journal_failed: {err}"
                record.update(
                    state=terminal.value, reason=self._reason, recovery_required=True
                )
                # Retain intent on disk; never replay a potentially applied count.
            self.state = terminal
            result = ShotResult(
                snapshot.operation_id,
                snapshot.decision.cycle_id,
                terminal,
                self._reason,
                bool(record["accounted"]) and terminal == ShotState.COMPLETED,
            )
            self._results[snapshot.operation_id] = result
            return result

    def _undo_accounting(
        self, previous: dict[str, Any], values: dict[str, Any], checkpoint: Any
    ) -> None:
        options = dict(self.entry.options)
        if any(options.get(key) != value for key, value in values.items()):
            raise RuntimeError("Cannot compensate changed accounting")
        for key in values:
            if key in previous:
                options[key] = previous[key]
            else:
                options.pop(key, None)
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        if checkpoint is None:
            self._journal.pop("last_accounting", None)
        else:
            self._journal["last_accounting"] = checkpoint
        async_dispatcher_send(
            self.hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{self.entry.entry_id}"
        )
        async_dispatcher_send(
            self.hass, f"{SIGNAL_LAST_SHOT_UPDATED}_{self.entry.entry_id}"
        )

    async def _shutdown(self, event: Any) -> None:
        self._remove_shutdown = None
        await self.async_close()

    async def async_close(self) -> bool:
        """Block requests immediately and drain execution before controller close."""
        self._closing = True
        self._interrupt("integration_stopping")
        return await _protected(self._background(self._close()))

    async def _close(self) -> bool:
        if self._task is not None:
            await asyncio.shield(self._task)
        stopped = await self.controller.async_close()
        if stopped:
            if self._remove_shutdown is not None:
                self._remove_shutdown()
                self._remove_shutdown = None
            if _owners(self.hass).get(self.entry.entry_id) is self:
                _owners(self.hass).pop(self.entry.entry_id)
        return stopped
