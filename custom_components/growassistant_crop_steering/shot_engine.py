"""Development-only single shots. Never constructed by integration setup.

No public activation surface. See docs/native-shot-engine.md for
ownership, active safety policy and the intentionally fail-closed crash journal.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .config import configured_entity_value
from .const import CONF_PUMP_SWITCH, DOMAIN, SIGNAL_LAST_SHOT_UPDATED
from .irrigation import Decision, Inputs, capture_inputs, evaluate
from .native_safety import (
    OwnershipEvidence,
    RecoveryAssessment,
    SensorAssessment,
    SensorPolicy,
    assess_recovery,
    assess_sensors,
)
from .number import SIGNAL_NUMBER_STATE_UPDATED
from .pump import HARD_MAX_RUNTIME, PumpController, PumpState, _protected
from .recovery_participant import (
    AccountingGateEvidence,
    GateRequest,
    ShotGateEvidence,
    request_current,
    shot_evidence,
)
from .recovery_revision import JournalRevision, ParticipantRuntime, _ParticipantStore
from .recovery_snapshot import (
    ResourceBinding,
    ShotSafetySnapshot,
    WorkEvidence,
    freeze,
    task_state,
    version_evidence,
)

_KEY = f"{DOMAIN}.development_shots"
SIGNAL_SHOT_FINISHED = f"{DOMAIN}.development_shot_finished"
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
    sensor_policy: SensorPolicy
    ownership: OwnershipEvidence


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
    if owner._cycle is not None:
        owner._cycle.block("external_cycle_reset_requires_review")
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
        self.store = _ParticipantStore(hass, f"{DOMAIN}.shot_journal.{entry.entry_id}")
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
        self._cycle: Any = None
        self.sensor_policy = SensorPolicy()
        self._ownership: OwnershipEvidence | None = None
        self._storage_failed = False
        self._recovery_epoch = uuid4().hex
        self._runtime = ParticipantRuntime(self.hass, self.entry, controller._runtime)
        self._revision = JournalRevision(
            self.entry.entry_id,
            configured_entity_value(self.entry, CONF_PUMP_SWITCH),
            f"{DOMAIN}.shot_journal.{self.entry.entry_id}",
        )

    @property
    def ownership(self) -> OwnershipEvidence | None:
        return self._ownership

    @ownership.setter
    def ownership(self, evidence: OwnershipEvidence | None) -> None:
        if evidence is not self._ownership:
            self._runtime.ownership_changed()
        self._ownership = evidence

    def recovery_gate_evidence(
        self,
        request: GateRequest,
    ) -> tuple[ShotGateEvidence, AccountingGateEvidence]:
        """Review operations and accounting without rewriting either history."""
        return shot_evidence(
            request,
            self._recovery_epoch,
            self.safety_snapshot(),
            _owners(self.hass).get(self.entry.entry_id) is self,
            self._ownership_reasons(request.binding.entity_id),
            request_current(self.hass, request),
        )

    def safety_snapshot(self) -> ShotSafetySnapshot:
        """Detach journal/options without readiness methods that may mutate gates."""
        reasons = tuple(
            reason
            for reason in (
                self._reason,
                "storage_failed" if self._storage_failed else None,
                "closing" if self._closing else None,
            )
            if reason
        )
        return ShotSafetySnapshot(
            binding=ResourceBinding(
                self.entry.entry_id,
                configured_entity_value(self.entry, CONF_PUMP_SWITCH),
            ),
            captured_at=dt_util.utcnow(),
            version=version_evidence(
                self._revision,
                self._journal,
                configured_entity_value(self.entry, CONF_PUMP_SWITCH),
            ),
            runtime=self._runtime.snapshot(
                configured_entity_value(self.entry, CONF_PUMP_SWITCH),
                self._revision.generation,
            ),
            state=self.state.value,
            journal=freeze(self._journal),
            options=freeze(self.entry.options),
            cycle_id=self._cycle_id,
            work=(
                WorkEvidence("shot", task_state(self._task)),
                WorkEvidence("storage", task_state(self._storage_task)),
            ),
            accounting_locked=self._accounting_lock.locked(),
            initialized=self._ready and not self._closing,
            blocked=self._blocked,
            reasons=reasons,
        )

    def _background(self, work: Any) -> asyncio.Task[Any]:
        return self.hass.async_create_background_task(work, "development single shot")

    @property
    def initialized(self) -> bool:
        """Whether initialization finished and the coordinator remains open."""
        return self._ready and not self._closing

    @property
    def execution_active(self) -> bool:
        """A reservation or execution is pending; requests must never queue."""
        return self._task is not None and not self._task.done()

    @property
    def execution_available(self) -> bool:
        """Read-only readiness hint; async_execute still enforces every gate."""
        return (
            self._ready
            and not self._blocked
            and not self._closing
            and not self.execution_active
            and not self._accounting_lock.locked()
            and not self.sensor_assessment().reasons
            and not self._ownership_reasons(
                self.capture_validated_inputs().config["pump_switch"]
            )
            and self.controller.state == PumpState.IDLE
            and (self._cycle is None or self._cycle.permits(self._cycle_id))
        )

    def capture_validated_inputs(self) -> Inputs:
        """Preserve arithmetic mean over only the policy's validated peers."""
        inputs = capture_inputs(self.hass, self.entry)
        assessment = self.sensor_assessment()
        return Inputs(
            inputs.config,
            {
                entity: None if entity in assessment.rejected else value
                for entity, value in inputs.vwc.items()
            },
        )

    def validated_decision(self, now: datetime) -> Decision:
        """Add native policy diagnostics without changing Blueprint decisions."""
        decision = evaluate(self.capture_validated_inputs(), now)
        reasons = self.sensor_assessment().reasons
        if not reasons:
            return decision
        return replace(
            decision,
            eligible=False,
            p1_start_eligible=False,
            blocking_reasons=tuple(
                dict.fromkeys((*decision.blocking_reasons, *reasons))
            ),
            p1_start_blocking_reasons=tuple(
                dict.fromkeys((*decision.p1_start_blocking_reasons, *reasons))
            ),
        )

    def sensor_assessment(
        self, overrides: dict[str, Any] | None = None
    ) -> SensorAssessment:
        """Same freshness/quorum policy for authorization and supervision."""
        configured = self.entry.options.get(
            "vwc_sensor", self.entry.data.get("vwc_sensor")
        )
        return assess_sensors(
            self.hass, configured, dt_util.utcnow(), self.sensor_policy, overrides
        )

    @property
    def recovery_assessment(self) -> RecoveryAssessment:
        cycle = self._cycle
        return assess_recovery(
            journal=self._journal,
            pump_unresolved=self.controller.state in {PumpState.FAULT, PumpState.NEW},
            pump_restart_unresolved=self.controller.restart_had_recovery,
            pump_reasons=self.controller.recovery_reasons,
            cycle=None if cycle is None else cycle.record,
            cycle_reason=None if cycle is None else cycle.recovery_reason,
            sensor_reasons=self.sensor_assessment().reasons,
            storage_failed=self._storage_failed,
        )

    @property
    def delivery_assessment(self) -> dict[str, Any]:
        """Attempt history is separate from successful-shot budget counters.

        Any uncertain attempt blocks all further execution; no volume is inferred
        and no reset erases attempts. A future meter can supply a separate ledger.
        """
        records = list(self._journal["operations"].values())
        return {
            "reservations": len(records),
            "successful_shots": sum(
                record.get("state") == ShotState.COMPLETED
                and record.get("accounted") is True
                for record in records
            ),
            "uncertain_attempts": sum(
                bool(record.get("delivery_uncertain"))
                or (
                    record.get("state") in {ShotState.INTERRUPTED, ShotState.FAULT}
                    and record.get("delivery") != "not_requested"
                )
                for record in records
            ),
            "delivered_volume": None,
            "replacement_blocked": self._blocked,
        }

    def _ownership_reasons(self, pump_entity: str | None) -> tuple[str, ...]:
        reasons = (
            ("pump_ownership_unknown",)
            if self.ownership is None
            else self.ownership.reasons(pump_entity)
        )
        controller_reasons = (
            ("pump_ownership_unknown",)
            if self.controller.ownership is None
            else self.controller.ownership.reasons(pump_entity)
        )
        return tuple(dict.fromkeys((*reasons, *controller_reasons)))

    async def async_initialize(self) -> None:
        """Load journal; unresolved operations require manual review, never replay."""
        self._runtime.start()
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
                self.capture_validated_inputs(), dt_util.now()
            ).cycle_id
            async with asyncio.timeout(JOURNAL_TIMEOUT):
                journal = await self._revision.load(self.store)
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
                if self.controller.restart_had_recovery:
                    journal["pump_restart_requires_review"] = True
                    self._blocked = True
                for record in journal["operations"].values():
                    if record["state"] not in {
                        ShotState.COMPLETED,
                        ShotState.INTERRUPTED,
                        ShotState.FAULT,
                    } or record.get("accounting_pending"):
                        if (
                            record["state"]
                            in {
                                ShotState.STARTING,
                                ShotState.RUNNING,
                                ShotState.STOPPING,
                                ShotState.COMPLETED,
                            }
                            or record.get("activation_requested_at")
                            or record.get("accounting_pending")
                        ):
                            record.update(
                                delivery="possible_partial",
                                delivery_uncertain=True,
                                delivered_volume=None,
                            )
                        record["restart_original_state"] = record["state"]
                        record.update(
                            state=ShotState.INTERRUPTED.value,
                            reason="restart_requires_review",
                            recovery_required=True,
                            restart_original_reason=record.get("reason"),
                        )
                    if (
                        record["state"] == ShotState.COMPLETED
                        and record.get("accounted") is not True
                    ):
                        record["recovery_required"] = True
                        journal["accounting_inconsistent"] = True
                    if record["state"] in {ShotState.INTERRUPTED, ShotState.FAULT}:
                        record["recovery_required"] = True
                    self._blocked |= bool(record.get("recovery_required"))
                checkpoint = journal.get("last_accounting")
                if checkpoint is None and any(
                    record["state"] == ShotState.COMPLETED
                    and record.get("accounted") is True
                    for record in journal["operations"].values()
                ):
                    self._blocked = True
                    journal["recovery_required"] = True
                    journal["accounting_inconsistent"] = True
                if checkpoint is not None:
                    if not isinstance(checkpoint, dict) or not isinstance(
                        checkpoint.get("values"), dict
                    ):
                        raise ValueError("invalid accounting checkpoint")
                    if not checkpoint["values"]:
                        self._blocked = True
                        journal["recovery_required"] = True
                        journal["accounting_inconsistent"] = True
                    current = evaluate(self.capture_validated_inputs(), dt_util.now())
                    if current.cycle_id == checkpoint.get("cycle_id") and any(
                        self.entry.options.get(key) != value
                        for key, value in checkpoint["values"].items()
                    ):
                        self._blocked = True
                        journal["recovery_required"] = True
                        journal["accounting_inconsistent"] = True
                    elif current.cycle_id != checkpoint.get("cycle_id"):
                        self._blocked = True
                        journal["recovery_required"] = True
                        journal["missed_cycle_reset"] = True
                self._blocked |= bool(journal.get("recovery_required"))
                self._blocked |= bool(journal.get("pump_restart_requires_review"))
                await self._save()
            if self.controller.restart_had_recovery:
                self._blocked = True
                self._journal["pump_restart_requires_review"] = True
                await self._save()
            self._ready = True
            if self._blocked:
                self.state = ShotState.FAULT
        except Exception:
            self._storage_failed = True
            self._blocked = True
            self.state = ShotState.FAULT
            raise

    async def _save(self) -> None:
        # Do not cancel disk writes or allow late snapshots to overwrite newer ones.
        if self._storage_task is not None and not self._storage_task.done():
            raise RuntimeError("Previous journal write unresolved")
        await self._revision.save(
            self.store,
            self._journal,
            timeout=JOURNAL_TIMEOUT,
            create_task=self._revision_task,
        )

    def _revision_task(self, work):
        self._storage_task = self._background(work)
        return self._storage_task

    async def async_execute(self, operation_id: str | None = None) -> ShotResult:
        """One explicit internal request. Concurrent/duplicate calls never queue."""
        operation_id = operation_id or uuid4().hex
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
        ):
            raise ShotRejected(("invalid_operation_id",))
        if operation_id in self._journal["operations"]:
            raise ShotRejected(("duplicate_operation",))
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
        inputs = self.capture_validated_inputs()
        decision = evaluate(inputs, dt_util.now())
        if self._cycle is not None and not self._cycle.permits(decision.cycle_id):
            raise ShotRejected(("native_cycle_review_required",))
        if decision.cycle_id != self._cycle_id:
            raise ShotRejected(("cycle_reset_required",))
        sensor_reasons = self.sensor_assessment().reasons
        if sensor_reasons:
            raise ShotRejected(sensor_reasons)
        if not decision.eligible:
            raise ShotRejected(decision.blocking_reasons)
        if not 0 < decision.duration_s <= HARD_MAX_RUNTIME:
            raise ShotRejected(("controller_runtime_limit",))
        ownership_reasons = self._ownership_reasons(decision.pump_entity)
        if ownership_reasons:
            raise ShotRejected(ownership_reasons)
        self.snapshot = ShotSnapshot(
            operation_id,
            decision,
            inputs,
            self._generation,
            self.sensor_policy,
            self.ownership,
        )
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
            "delivery": "not_requested",
            "delivered_volume": None,
            "sensor_validation": self.sensor_assessment().diagnostic(),
            "sensor_policy": asdict(self.sensor_policy),
            "ownership_evidence": asdict(self.ownership),
        }
        # No await before task assignment: reservation is serialized on HA's loop.
        self._task = self._background(self._execute(self.snapshot))
        self._task.add_done_callback(self._notify_finished)
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
    def _notify_finished(self, task: asyncio.Task[ShotResult]) -> None:
        result = (
            None if task.cancelled() or task.exception() is not None else task.result()
        )
        async_dispatcher_send(
            self.hass, f"{SIGNAL_SHOT_FINISHED}_{self.entry.entry_id}", result
        )

    @callback
    def _interrupt(self, reason: str) -> None:
        self._runtime.invalidate()
        if self._task is not None and not self._task.done():
            self._reason = self._reason or reason
            self._abort.set()

    def _active_safety(self, snapshot: ShotSnapshot) -> str | None:
        if self._closing:
            return "integration_stopping"
        if self._cycle is not None and not self._cycle.permits(
            snapshot.decision.cycle_id
        ):
            return "native_cycle_review_required"
        if snapshot.generation != self._generation:
            return "cycle_reset"
        inputs = self.capture_validated_inputs()
        config = inputs.config
        ownership = self._ownership_reasons(snapshot.decision.pump_entity)
        if ownership:
            return ownership[0]
        if (
            self.ownership != snapshot.ownership
            or self.sensor_policy != snapshot.sensor_policy
        ):
            return "safety_policy_changed"
        sensors = self.sensor_assessment()
        if sensors.reasons:
            return sensors.reasons[0]
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
        current = evaluate(self.capture_validated_inputs(), dt_util.now())
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
            and dict(self.capture_validated_inputs().config)
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
                assessment = self.sensor_assessment({entity: state})
                if assessment.reasons:
                    self._interrupt(assessment.reasons[0])

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
            # Durable conservative intent: a crash after this may deliver water.
            record["delivery"] = "possible_partial"
            record["activation_requested_at"] = dt_util.utcnow().isoformat()
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
            record["activation_confirmed_at"] = dt_util.utcnow().isoformat()
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
            uncertain = not success and record.get("delivery") == "possible_partial"
            self._blocked |= uncertain
            record.update(
                state=terminal.value,
                shutdown_confirmed=stopped,
                delivery_uncertain=uncertain,
                delivery="software_completed"
                if success
                else record.get("delivery", "not_requested"),
                outcome=(
                    "completed"
                    if success
                    else "unconfirmed_shutdown"
                    if not stopped
                    else "interrupted"
                    if record.get("activation_confirmed_at")
                    else "failed_activation"
                ),
                reason=self._reason,
                finished_at=dt_util.utcnow().isoformat(),
                recovery_required=not stopped or uncertain,
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
                            self.capture_validated_inputs().config[
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
                        if self._cycle is not None:
                            self._journal["native_counters"] = {
                                key: options[key]
                                for key in (
                                    "p1_shots_done",
                                    "p2_shots_done",
                                    "p3_emergency_shots_done",
                                )
                            }
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_NUMBER_STATE_UPDATED}_{self.entry.entry_id}",
                        )
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_LAST_SHOT_UPDATED}_{self.entry.entry_id}",
                        )
                if terminal != ShotState.COMPLETED and record.get(
                    "activation_requested_at"
                ):
                    self._blocked = True
                    record.update(
                        delivery="possible_partial",
                        delivery_uncertain=True,
                        recovery_required=True,
                        outcome="unconfirmed_shutdown"
                        if not stopped
                        else "interrupted"
                        if record.get("activation_confirmed_at")
                        else "failed_activation",
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
                    self._blocked = True
                    record.update(
                        delivery="possible_partial",
                        delivery_uncertain=True,
                        recovery_required=True,
                        outcome="interrupted",
                    )
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
                self._storage_failed = True
                self._reason = f"accounting_or_journal_failed: {err}"
                record.update(
                    state=terminal.value,
                    reason=self._reason,
                    recovery_required=True,
                    outcome="accounting_or_storage_fault",
                    delivery="possible_partial"
                    if record.get("activation_requested_at")
                    else "not_requested",
                    delivery_uncertain=bool(record.get("activation_requested_at")),
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
        if self._cycle is not None:
            self._journal["native_counters"] = {
                key: options[key]
                for key in ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")
            }
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
        self._runtime.close()
        self._interrupt("integration_stopping")
        return await _protected(self._background(self._close()))

    async def _close(self) -> bool:
        if self._task is not None:
            await asyncio.shield(self._task)
        if self._storage_task is not None and not self._storage_task.done():
            await self.controller.async_stop()
            return False
        stopped = await self.controller.async_close()
        if stopped:
            if self._remove_shutdown is not None:
                self._remove_shutdown()
                self._remove_shutdown = None
            if _owners(self.hass).get(self.entry.entry_id) is self:
                _owners(self.hass).pop(self.entry.entry_id)
        return stopped
