"""Native-harness-only read/compare diagnostics; no release or output capability."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from homeassistant.util import dt as dt_util

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
    OwnershipEpoch,
    OwnershipLease,
    validate_lease,
)
from custom_components.growassistant_crop_steering.recovery_participant import (
    ParticipantAggregation,
    PumpGateEvidence,
    aggregate_evidence,
)
from custom_components.growassistant_crop_steering.recovery_revision import (
    OwnershipGeneration,
)
from custom_components.growassistant_crop_steering.recovery_snapshot import (
    ResourceBinding,
)

from .fake_actuator_gateway import FakeGateway, Receipt, SafetyReceipt

# These are unresolved guarantees, even for a completely consistent observation.
RETAINED_BLOCKERS = (
    "partial_delivery_unresolved",
    "cycle_disposition_unproven",
    "external_writers_unproven",
    "ownership_epoch_unproven",
    "coordinated_resolution_unimplemented",
)


@dataclass(frozen=True)
class GatewayEvidence:
    actuator: ActuatorIdentity
    binding: ResourceBinding
    gateway_boot: str
    runtime: int
    storage_runtime: int
    storage_boot: int
    ownership_generation: int
    lease: OwnershipLease | None
    lease_status: str
    owner: str | None
    stopped: bool
    fault: bool
    local_stop_serial: int
    stop_serial: int
    journal_revision: int
    journal_schema: int
    records: tuple[tuple[str, Receipt], ...]
    stops: tuple[SafetyReceipt, ...]
    witness_present: bool
    witness_blocked: bool
    witness_matches: bool
    witness_identity: int
    storage_identity: int
    actor_identity: int
    actor_state: str
    off_confirmable: bool
    last_safety_off: SafetyReceipt | None
    open_commands: tuple[str, ...]
    uncertain_commands: tuple[str, ...]
    delayed_receipts: tuple[tuple[str, int], ...]
    last_sequence: int
    execution_sequence: int
    last_tick: float
    heartbeat: float
    watchdog_timeout: float
    time_basis: str
    checked_tick: float
    checked_at: datetime
    blockers: tuple[str, ...]

    def state_key(self):
        """Elapsed time is validated separately; every other observation is bound."""
        return replace(self, checked_tick=0, checked_at=datetime.min)


def capture_gateway(gateway: FakeGateway) -> GatewayEvidence:
    """Synchronous: no await between journal, fence, actor and witness reads.

    Malformed storage raises to the coordinator, which publishes incomplete
    evidence. No mutable journal dictionary escapes this reader.
    """
    store, clock = gateway.store, gateway.clock
    journal, witness = store.snapshot(), store.witness
    records = tuple(sorted(journal.records.items()))
    if any(type(key) is not str or type(r) is not Receipt for key, r in records):
        raise ValueError("gateway_journal_invalid")
    if any(type(r) is not SafetyReceipt for r in journal.stops):
        raise ValueError("gateway_stop_history_invalid")
    now, lease = clock.tick, gateway.lease
    reasons = []
    finite = all(
        type(v) in (int, float) and math.isfinite(v)
        for v in (
            now,
            gateway._last_tick,
            gateway._heartbeat,
            gateway._watchdog_timeout,
        )
    )
    if not finite or not (
        0 <= gateway._last_tick <= now
        and 0 <= gateway._heartbeat <= now
        and 0 < gateway._watchdog_timeout
        and now - gateway._heartbeat < gateway._watchdog_timeout
    ):
        reasons.append("gateway_time_invalid")
    matches = witness is not None and journal == witness.expected
    if witness is None or witness.blocked or not matches:
        reasons.append("gateway_witness_untrusted")
    if type(journal.schema) is not int or journal.schema != 1:
        reasons.append("gateway_schema_invalid")
    if any(
        type(v) is not int or v < 0
        for v in (journal.revision, journal.boot, journal.generation, store.stop_serial)
    ):
        reasons.append("gateway_revision_invalid")
    if not gateway._current() or gateway.boot == "uncommitted":
        reasons.append("gateway_runtime_obsolete")
    if gateway._stop_serial != store.stop_serial:
        reasons.append("gateway_stop_fence_changed")
    if gateway.fault:
        reasons.append("gateway_fault")
    status = "absent"
    if lease is not None:
        lease_reasons = validate_lease(
            lease,
            actuator=gateway.actuator,
            epoch=OwnershipEpoch(gateway.boot, OwnershipGeneration(store.generation)),
            ha_boot=lease.ha_boot,
            now_tick=now,
        ).blockers
        reasons.extend(lease_reasons)
        status = "invalid" if lease_reasons else "active"
        if gateway.stopped:
            reasons.append("gateway_fencing_conflict")
    if gateway.state != "off":
        reasons.append("gateway_off_unproven")
    off = gateway.off_receipts[-1] if gateway.off_receipts else None
    if (
        off is None
        or off.gateway_boot != gateway.boot
        or off.result != "software_off"
        or not finite
        or not 0 <= off.tick <= now
        or not journal.stops
        or journal.stops[-1] != off
    ):
        reasons.append("gateway_safety_off_unproven")
    opened = tuple(k for k, r in records if r.status in {"accepted", "executing"})
    uncertain = tuple(k for k, r in records if r.status == "uncertain")
    if opened:
        reasons.append("gateway_commands_open")
    if uncertain or store.delayed_receipts:
        reasons.append("gateway_commands_uncertain")
    if any(
        key != r.intent.command_id
        or r.status
        not in {"accepted", "executing", "executed", "uncertain", "rejected"}
        or r.intent.actuator != gateway.actuator
        for key, r in records
    ):
        reasons.append("gateway_command_history_conflict")
    return GatewayEvidence(
        gateway.actuator,
        gateway.actuator.binding,
        gateway.boot,
        gateway._runtime,
        store.runtime,
        store.boot,
        store.generation,
        lease,
        status,
        None if lease is None else lease.owner_id,
        gateway.stopped,
        gateway.fault,
        gateway._stop_serial,
        store.stop_serial,
        journal.revision,
        journal.schema,
        records,
        journal.stops,
        witness is not None,
        witness is None or witness.blocked,
        matches,
        id(witness),
        id(store),
        id(gateway._actor),
        gateway.state,
        gateway._actor.off_confirmable,
        off,
        opened,
        uncertain,
        tuple(store.delayed_receipts),
        gateway._last_sequence,
        gateway._execution_sequence,
        gateway._last_tick,
        gateway._heartbeat,
        gateway._watchdog_timeout,
        clock.time_basis,
        now,
        dt_util.utcnow(),
        tuple(dict.fromkeys(reasons)),
    )


def recovery_scope(result: ParticipantAggregation):
    """Pin full histories/revisions and local epochs, excluding per-attempt IDs."""
    request = result.request
    if request is None:
        raise ValueError("recovery_request_missing")
    return (
        request.binding,
        request.cycle_id,
        request.operations,
        request.revisions,
        request.runtime_epoch,
        request.safety_generation,
        request.boot_epoch,
        request.schema_version,
        tuple(
            (type(p).__name__, p.participant_epoch, p.runtime) for p in result.evidence
        ),
    )


@dataclass(frozen=True)
class DiagnosticBinding:
    """Explicit model reference, never a physical mapping or attestation.

    The gateway and HA ownership counters are independent namespaces. Retain
    both actual values instead of equating them or rewriting participant epochs.
    """

    gateway: GatewayEvidence
    recovery_scope: tuple

    @classmethod
    def capture(cls, gateway: FakeGateway, recovery: ParticipantAggregation):
        return cls(gateway.evidence_snapshot(), recovery_scope(recovery))


@dataclass(frozen=True)
class IntegrationDiagnostic:
    gateway: GatewayEvidence | None
    recovery: ParticipantAggregation | None
    gateway_evidence_complete: bool
    gateway_internally_consistent: bool
    recovery_participants_complete: bool
    evidence_conflict_free: bool
    blockers: tuple[str, ...]
    hardware_exclusivity_proven: Literal[False] = field(default=False, init=False)
    recovery_release_allowed: Literal[False] = field(default=False, init=False)
    cycle_release_allowed: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)
    release_permit: None = field(default=None, init=False)


# Structural contradictions are distinct from unresolved safety/domain findings.
CONFLICTS = frozenset(
    (
        "participant_missing",
        "participant_conflict",
        "participant_invalid",
        "revision_conflict",
        "runtime_epoch_conflict",
        "confirmation_binding_conflict",
        "confirmation_invalid",
        "confirmation_not_current",
        "binding_mismatch",
        "cycle_mismatch",
        "operation_scope_conflict",
        "operation_evidence_changed",
        "operation_cycle_conflict",
        "evidence_changed",
        "command_unresolved",
        "task_unresolved",
        "storage_unresolved",
        "scheduler_work_unresolved",
        "ownership_lost",
        "ownership_conflict",
        "ownership_epoch_changed",
        "internal_owner_unknown",
        "writer_conflict_detected",
        "writer_evidence_expired",
    )
)


class NativeGatewayRecoveryCoordinator:
    """Read-only wrapper around the existing participant diagnostic collector.

    One in-flight check per instance. Hooks expose deterministic boundaries only;
    final synchronous revalidation/publication has no await or output operation.
    """

    def __init__(self, gateway, recovery, binding: DiagnosticBinding):
        self.gateway, self.recovery, self.binding = gateway, recovery, binding
        self._busy = False
        self._last = None

    def _joint_conflicts(self, snapshot, result):
        reasons = []
        if snapshot.state_key() != self.binding.gateway.state_key():
            reasons.append("gateway_binding_changed")
        if snapshot.checked_tick < self.binding.gateway.checked_tick:
            reasons.append("gateway_time_reversed")
        if recovery_scope(result) != self.binding.recovery_scope:
            reasons.append("recovery_binding_changed")
        request = result.request
        pump = next((p for p in result.evidence if type(p) is PumpGateEvidence), None)
        if snapshot.binding != request.binding:
            reasons.append("joint_resource_conflict")
        if snapshot.lease is None:
            reasons.append("joint_lease_missing")
        elif (
            snapshot.lease.ha_boot != request.boot_epoch
            or pump is None
            or pump.runtime is None
            or snapshot.owner != pump.runtime.controller.value
        ):
            reasons.append("joint_owner_or_boot_conflict")
        if snapshot.records:
            # CommandIntent has no operation_id/cycle_id. Never invent a mapping.
            reasons.append("gateway_operation_provenance_unproven")
        return reasons

    async def async_check(
        self, *, boundary: Callable[[str], Awaitable[None]] | None = None
    ) -> IntegrationDiagnostic:
        if self._busy:
            raise ValueError("integration_check_busy")
        self._busy, self._last = True, None
        snapshot = result = None
        complete = consistent = participants_complete = conflict_free = False
        reasons = list(RETAINED_BLOCKERS)
        try:
            snapshot = self.gateway.evidence_snapshot()
            complete = True
            if boundary:
                await boundary("gateway_captured")
            result = await self.recovery.async_check()
            reasons.extend(result.blockers)
            if boundary:
                await boundary("recovery_captured")
                await boundary("before_publish")
            final = self.gateway.evidence_snapshot()
            reasons.extend(snapshot.blockers)
            reasons.extend(final.blockers)
            stable = (
                snapshot.state_key() == final.state_key()
                and 0 <= final.checked_tick - snapshot.checked_tick <= 30
                and 0 <= (final.checked_at - snapshot.checked_at).total_seconds() <= 30
            )
            if not stable:
                reasons.append("gateway_evidence_changed")
            consistent = stable and not snapshot.blockers and not final.blockers
            checked = aggregate_evidence(result.request, result.evidence)
            reasons.extend(checked.blockers)
            participants_complete = (
                result.collection_complete and checked.collection_complete
            )
            joint = self._joint_conflicts(final, result)
            reasons.extend(joint)
            live = self.recovery.confirmation_current(result)
            if not live:
                reasons.append("recovery_evidence_not_current")
            conflict_free = (
                consistent
                and participants_complete
                and live
                and not joint
                and not CONFLICTS.intersection(checked.blockers + result.blockers)
            )
        except Exception:
            consistent = conflict_free = False
            reasons.append("integration_evidence_invalid")
        finally:
            self._busy = False
        diagnostic = IntegrationDiagnostic(
            snapshot,
            result,
            complete,
            consistent,
            participants_complete,
            conflict_free,
            tuple(dict.fromkeys(reasons)),
        )
        if conflict_free:
            self._last = diagnostic
        return diagnostic

    def diagnostic_current(self, diagnostic: IntegrationDiagnostic) -> bool:
        """Transient identity/liveness only; explicitly NOT a release predicate."""
        if diagnostic is not self._last or self._busy or self._last is None:
            return False
        try:
            fresh = self.gateway.evidence_snapshot()
            current = (
                not fresh.blockers
                and fresh.state_key() == diagnostic.gateway.state_key()
                and 0 <= fresh.checked_tick - diagnostic.gateway.checked_tick <= 30
                and self.recovery.confirmation_current(diagnostic.recovery)
                and not self._joint_conflicts(fresh, diagnostic.recovery)
            )
        except Exception:
            current = False
        if not current:
            self._last = None
        return current
