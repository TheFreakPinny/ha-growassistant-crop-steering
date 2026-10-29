"""Participant-owned diagnostics. No release, gate mutation, writes or permits."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal
from uuid import uuid4

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery_ledger import LedgerError, _digest, _json
from .recovery_plan import async_create_reconciliation_plan
from .recovery_revision import BootEpoch, JournalReceipt, RuntimeEpochs
from .recovery_snapshot import (
    CycleSafetySnapshot,
    PumpSafetySnapshot,
    ResourceBinding,
    ShotSafetySnapshot,
    TaskState,
    validate_cycle,
    validate_pump,
    validate_shots,
)
from .vwc import normalize_vwc_sensors

Gate = Literal["pump", "shots", "accounting", "cycle", "scheduler"]
GATES: tuple[Gate, ...] = ("pump", "shots", "accounting", "cycle", "scheduler")


@dataclass(frozen=True)
class EvidenceRevision:
    source: str
    digest: str
    durable_revision: int | None
    receipt: JournalReceipt | None = None
    runtime: RuntimeEpochs | None = None
    volatile_generation: tuple[int, int] | None = None


@dataclass(frozen=True)
class OperationScope:
    operation_id: str
    cycle_id: str | None
    evidence_digest: str
    reviewed: bool
    disposition: str


@dataclass(frozen=True)
class GateRequest:
    """Full observation scope, never a caller-selected subset of operations."""

    attempt_id: str
    binding: ResourceBinding
    cycle_id: str | None
    operations: tuple[OperationScope, ...]
    revisions: tuple[EvidenceRevision, ...]
    runtime_epoch: str
    safety_generation: int
    captured_at: datetime
    boot_epoch: BootEpoch | None = None
    schema_version: int = 2


@dataclass(frozen=True)
class GateCheck:
    code: str
    passed: bool | None
    detail: str


@dataclass(frozen=True)
class OperationDisposition:
    operation_id: str
    cycle_id: str | None
    historical_state: str
    delivery: str | None
    reviewed: bool
    checked: bool
    disposition: str
    # Unknown delivered volume is never inferred from software duration.
    volume: None = field(default=None, init=False)
    operation_consumed: Literal[True] = field(default=True, init=False)


@dataclass(frozen=True)
class ParticipantEvidence:
    request: GateRequest
    participant_epoch: str
    observed_digest: str
    historical_errors: tuple[str, ...]
    blockers: tuple[str, ...]
    checks: tuple[GateCheck, ...]
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    runtime: RuntimeEpochs | None = field(default=None, kw_only=True)
    revision: EvidenceRevision | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class PumpGateEvidence(ParticipantEvidence):
    off_reported_at: datetime | None
    independent_off_confirmed: bool | None


@dataclass(frozen=True)
class ShotGateEvidence(ParticipantEvidence):
    dispositions: tuple[OperationDisposition, ...]


@dataclass(frozen=True)
class AccountingGateEvidence(ParticipantEvidence):
    reconciliation_required: bool


@dataclass(frozen=True)
class CycleGateEvidence(ParticipantEvidence):
    baseline_required: bool
    baseline_established: Literal[False] = field(default=False, init=False)
    required_new_cycle_checks: tuple[str, ...] = field(
        default=(
            "agronomic_effects",
            "substrate_drain_tray",
            "water_budget_disposition",
            "waiting_observation_decision",
            "explicit_new_cycle_baseline",
            "accounting_provenance",
            "fresh_off_ownership_and_sensors",
        ),
        init=False,
    )


@dataclass(frozen=True)
class SchedulerGateEvidence(ParticipantEvidence):
    admission_generation: int
    retry_blocked: bool
    quiesce_observed: bool


_TYPES = (
    PumpGateEvidence,
    ShotGateEvidence,
    AccountingGateEvidence,
    CycleGateEvidence,
    SchedulerGateEvidence,
)


def observation_digest(snapshot) -> str:
    data = _json(snapshot)
    data.pop("captured_at", None)
    return _digest(data)


def _base(request, epoch, gate, snapshot, owned, findings, runtime_current):
    """Shared shape checks; domain checks are supplied by the owning component."""
    digest = observation_digest(snapshot)
    revision = next((r for r in request.revisions if r.source == gate), None)
    blockers = [f.code.value for f in findings]
    if snapshot.binding != request.binding:
        blockers.append("binding_mismatch")
    if revision != evidence_revision(gate, snapshot):
        blockers.append("revision_conflict")
    if not owned:
        blockers.append("ownership_lost")
    if not runtime_current:
        blockers.append("confirmation_not_current")
    checks = (
        GateCheck(
            "runtime_confirmation", runtime_current, "Exact issued request and runtime"
        ),
        GateCheck("local_owner", owned, "Current component registration"),
        GateCheck(
            "durable_revision",
            snapshot.version.receipt is not None
            and not any(
                f.code.value
                in {"revision_unknown", "revision_conflict", "schema_unknown"}
                for f in findings
            ),
            "Completed local atomic journal write; no external writer isolation",
        ),
    )
    return dict(
        request=request,
        participant_epoch=epoch,
        observed_digest=digest,
        historical_errors=tuple(snapshot.reasons),
        blockers=tuple(dict.fromkeys(blockers)),
        checks=checks,
        runtime=snapshot.runtime,
        revision=evidence_revision(gate, snapshot),
    )


def pump_evidence(
    request: GateRequest,
    epoch: str,
    snapshot: PumpSafetySnapshot,
    owned: bool,
    ownership_reasons: tuple[str, ...],
    runtime_current: bool,
) -> PumpGateEvidence:
    """Called only by PumpController with its own freshly captured evidence."""
    result = validate_pump(snapshot, request.binding, now=dt_util.utcnow(), max_age=30)
    base = _base(
        request, epoch, "pump", snapshot, owned, result.findings, runtime_current
    )
    base["blockers"] += tuple(ownership_reasons)
    base["checks"] += (
        GateCheck(
            "off_report_verified",
            not any(f.code.value.startswith("off_") for f in result.findings),
            "Fresh ordered software OFF observation",
        ),
        GateCheck(
            "independent_off",
            snapshot.off.independent_confirmed,
            "Physical OFF remains unproven by software alone",
        ),
    )
    return PumpGateEvidence(
        **base,
        off_reported_at=snapshot.off.reported_at,
        independent_off_confirmed=snapshot.off.independent_confirmed,
    )


def shot_evidence(
    request: GateRequest,
    epoch: str,
    snapshot: ShotSafetySnapshot,
    owned: bool,
    ownership_reasons: tuple[str, ...],
    runtime_current: bool,
) -> tuple[ShotGateEvidence, AccountingGateEvidence]:
    result = validate_shots(snapshot, request.binding, now=dt_util.utcnow(), max_age=30)
    base = _base(
        request, epoch, "shots", snapshot, owned, result.findings, runtime_current
    )
    blockers = list(base["blockers"]) + list(ownership_reasons)
    dispositions = []
    historical = list(base["historical_errors"])
    operations = snapshot.journal.get("operations", {})
    if set(operations) != {o.operation_id for o in request.operations}:
        blockers.append("operation_scope_conflict")
    for op, raw in operations.items():
        scope = next((o for o in request.operations if o.operation_id == op), None)
        matches = scope is not None and scope.evidence_digest == _digest(_json(raw))
        if not matches:
            blockers.append("operation_evidence_changed")
        if scope is None or scope.cycle_id != raw.get("cycle_id"):
            blockers.append("operation_cycle_conflict")
        failed = raw.get("state") in {"fault", "interrupted"}
        if failed:
            historical.append(f"{op}:{raw.get('state')}:{raw.get('reason')}")
        reviewed = runtime_current and matches and scope.reviewed
        if failed and not reviewed:
            blockers.append("acknowledgment_missing")
        uncertain = raw.get("delivery_uncertain") is not False or raw.get(
            "delivery"
        ) not in {"not_requested", "software_completed"}
        if uncertain:
            blockers.append("partial_delivery_unresolved")
        # A reviewed keep-locked disposition can be checked successfully while
        # the operation's original failure and unresolved effects remain visible.
        checked = bool(reviewed and scope.disposition == "retain_locked" and failed)
        dispositions.append(
            OperationDisposition(
                op,
                raw.get("cycle_id"),
                raw.get("state", "unknown"),
                raw.get("delivery"),
                bool(reviewed),
                checked,
                "retain_locked" if failed else "retain_accounted",
            )
        )
    if snapshot.cycle_id != request.cycle_id:
        blockers.append("cycle_mismatch")
    base["historical_errors"] = tuple(historical)
    base["blockers"] = tuple(dict.fromkeys(blockers))
    shots = ShotGateEvidence(**base, dispositions=tuple(dispositions))
    accounting_base = _base(
        request, epoch, "accounting", snapshot, owned, result.findings, runtime_current
    )
    accounting_base["blockers"] += tuple(ownership_reasons)
    accounting_base["checks"] += (
        GateCheck(
            "accounting_consistent",
            not any(f.code.value == "accounting_inconsistent" for f in result.findings),
            "Local counters, intents and checkpoints; cross-store provenance remains separate",
        ),
    )
    accounting = AccountingGateEvidence(**accounting_base, reconciliation_required=True)
    return shots, accounting


def cycle_evidence(
    request: GateRequest,
    epoch: str,
    snapshot: CycleSafetySnapshot,
    owned: bool,
    runtime_current: bool,
) -> CycleGateEvidence:
    result = validate_cycle(snapshot, request.binding, now=dt_util.utcnow(), max_age=30)
    base = _base(
        request, epoch, "cycle", snapshot, owned, result.findings, runtime_current
    )
    if snapshot.current_cycle_id != request.cycle_id:
        base["blockers"] += ("cycle_mismatch",)
    base["checks"] += (
        GateCheck(
            "cycle_consistent",
            not any(f.code.value == "cycle_inconsistent" for f in result.findings),
            "Phase, flags, schedule, cycle and reference",
        ),
    )
    # No structured agronomic/water-budget review or baseline transition exists.
    base["blockers"] += ("cycle_disposition_unproven",)
    return CycleGateEvidence(**base, baseline_required=bool(request.operations))


@dataclass(frozen=True)
class SchedulerObservation:
    binding: ResourceBinding
    cycle_id: str | None
    initialized: bool
    owned: bool
    running: bool
    task: TaskState
    timer_pending: bool
    evaluation_pending: bool
    generation: int
    timer_revision: int
    retry_blocked: bool
    runtime: RuntimeEpochs | None = None
    schema_version: int = 2


def evidence_revision(gate, snapshot):
    if isinstance(snapshot, SchedulerObservation):
        return EvidenceRevision(
            gate,
            observation_digest(snapshot),
            None,
            runtime=snapshot.runtime,
            volatile_generation=(snapshot.generation, snapshot.timer_revision),
        )
    return EvidenceRevision(
        gate,
        observation_digest(snapshot),
        snapshot.version.revision,
        snapshot.version.receipt,
        snapshot.runtime,
    )


def _revision_shape(revision, binding):
    """Do not let Python's bool/int equality certify a malformed revision."""
    if not isinstance(revision, EvidenceRevision):
        return False
    if revision.source == "scheduler":
        return revision.durable_revision is None and revision.receipt is None
    if revision.source == "ledger":
        return type(revision.durable_revision) is int and revision.durable_revision >= 0
    if revision.durable_revision is None:
        return revision.receipt is None
    receipt = revision.receipt
    return (
        type(revision.durable_revision) is int
        and revision.durable_revision >= 1
        and isinstance(receipt, JournalReceipt)
        and type(receipt.revision) is int
        and receipt.revision == revision.durable_revision
        and receipt.identity.entry_id == binding.entry_id
        and receipt.identity.pump_entity == binding.entity_id
    )


def scheduler_evidence(
    request: GateRequest,
    epoch: str,
    snapshot: SchedulerObservation,
    runtime_current: bool,
) -> SchedulerGateEvidence:
    idle = (
        not snapshot.running
        and not snapshot.timer_pending
        and not snapshot.evaluation_pending
        and snapshot.task in {TaskState.ABSENT, TaskState.COMPLETED}
    )
    blockers = ["scheduler_admission_epoch_unproven"]
    if not runtime_current:
        blockers.append("confirmation_not_current")
    if not idle:
        blockers.append("scheduler_work_unresolved")
    if not snapshot.initialized or not snapshot.owned:
        blockers.append("ownership_lost")
    if snapshot.binding != request.binding:
        blockers.append("binding_mismatch")
    if snapshot.cycle_id != request.cycle_id:
        blockers.append("cycle_mismatch")
    revision = next((r for r in request.revisions if r.source == "scheduler"), None)
    digest = observation_digest(snapshot)
    if revision != evidence_revision("scheduler", snapshot):
        blockers.append("revision_conflict")
    return SchedulerGateEvidence(
        request,
        epoch,
        digest,
        ("retry_blocked",) if snapshot.retry_blocked else (),
        tuple(blockers),
        (GateCheck("scheduler_idle", idle, "Local work only"),),
        snapshot.generation,
        snapshot.retry_blocked,
        idle,
        runtime=snapshot.runtime,
        revision=evidence_revision("scheduler", snapshot),
    )


@dataclass(frozen=True)
class ParticipantAggregation:
    request: GateRequest | None
    evidence: tuple[ParticipantEvidence, ...]
    blockers: tuple[str, ...]
    collection_complete: bool
    coordination_ready: bool
    native_activation_allowed: Literal[False] = field(default=False, init=False)


def aggregate_evidence(
    request: GateRequest,
    evidence: tuple[ParticipantEvidence, ...],
    blockers: tuple[str, ...] = (),
) -> ParticipantAggregation:
    """Union, never last-success-wins. Values alone cannot prove live issuance."""
    reasons = list(blockers)
    complete = True
    for expected in _TYPES:
        matches = [e for e in evidence if type(e) is expected]
        if len(matches) != 1:
            complete = False
            reasons.append(
                "participant_missing" if not matches else "participant_conflict"
            )
    revision_sources = tuple(r.source for r in request.revisions)
    if set(revision_sources) != set((*GATES, "ledger")) or len(revision_sources) != 6:
        reasons.append("revision_conflict")
    if any(not _revision_shape(r, request.binding) for r in request.revisions):
        reasons.append("revision_conflict")
    shots_revision = next((r for r in request.revisions if r.source == "shots"), None)
    accounting_revision = next(
        (r for r in request.revisions if r.source == "accounting"), None
    )
    if shots_revision is not None and accounting_revision != replace(
        shots_revision, source="accounting"
    ):
        reasons.append("revision_conflict")
    for proof in evidence:
        if type(proof) not in _TYPES:
            reasons.append("participant_invalid")
            complete = False
            continue
        if proof.request != request:
            reasons.append("confirmation_binding_conflict")
        gate = GATES[_TYPES.index(type(proof))]
        revision = next((r for r in request.revisions if r.source == gate), None)
        if (
            revision is None
            or revision.digest != proof.observed_digest
            or revision != proof.revision
            or proof.runtime != revision.runtime
        ):
            reasons.append("revision_conflict")
        if (
            proof.runtime is None
            or request.boot_epoch != proof.runtime.boot
            or request.schema_version != 2
            or proof.runtime.resource.entry_id != request.binding.entry_id
            or proof.runtime.resource.entity_id != request.binding.entity_id
        ):
            reasons.append("runtime_epoch_conflict")
        if isinstance(proof, ShotGateEvidence):
            expected_scope = {(o.operation_id, o.cycle_id) for o in request.operations}
            actual_scope = {(o.operation_id, o.cycle_id) for o in proof.dispositions}
            if expected_scope != actual_scope or len(actual_scope) != len(
                proof.dispositions
            ):
                reasons.append("operation_scope_conflict")
        if not proof.participant_epoch or not proof.observed_digest:
            reasons.append("confirmation_invalid")
        reasons.extend(proof.blockers)
        reasons.extend(c.code for c in proof.checks if c.passed is not True)
    durable_complete = all(
        len(matches := [p for p in evidence if type(p) is kind]) == 1
        and (revision := next((r for r in request.revisions if r.source == gate), None))
        is not None
        and _revision_shape(revision, request.binding)
        and revision.receipt is not None
        and matches[0].revision == revision
        and any(
            c.code == "durable_revision" and c.passed is True for c in matches[0].checks
        )
        for gate, kind in zip(GATES[:4], _TYPES[:4], strict=True)
    )
    if not durable_complete:
        reasons.append("durable_participant_revisions_unproven")
    resources = {
        p.runtime.resource
        for p in evidence
        if type(p) in _TYPES and p.runtime is not None
    }
    if len(resources) != 1:
        reasons.append("runtime_epoch_conflict")
    # Local epochs do not establish global ownership or resolve recovery.
    reasons.extend(
        (
            "ownership_epoch_unproven",
            "external_writers_unproven",
            "coordinated_resolution_unimplemented",
        )
    )
    return ParticipantAggregation(
        request, evidence, tuple(dict.fromkeys(reasons)), complete, False
    )


def request_current(hass, request: GateRequest) -> bool:
    """Internal identity check, not authentication for an HA/public boundary."""
    owner = hass.data.get(f"{DOMAIN}.recovery_gate_coordinators", {}).get(
        request.binding
    )
    return bool(
        owner is not None
        and not owner._closed
        and owner._request is request
        and owner.runtime_epoch == request.runtime_epoch
        and owner._generation == request.safety_generation
        and request.boot_epoch == owner.source.pump.safety_snapshot().runtime.boot
        and owner._revisions_current(request)
        and 0 <= (dt_util.utcnow() - request.captured_at).total_seconds() <= 30
    )


class _RecoveryGateCoordinator:
    """Internal read-only collector. Never touches participant gates or journals.

    One owner per resource/HA lifetime; no persisted confirmations, automatic
    resumption, release API or positive authority. Runtime events invalidate the
    entire attempt, including ABA events whose end values happen to match.
    """

    def __init__(self, ledger, scheduler):
        self.ledger = ledger
        self.source = ledger.source
        self.hass = ledger.hass
        if (
            scheduler.coordinator is not self.source.shots
            or scheduler.cycle is not self.source.cycle
        ):
            raise LedgerError("participant_owner_conflict")
        self.scheduler = scheduler
        owners = self.hass.data.setdefault(f"{DOMAIN}.recovery_gate_coordinators", {})
        key = self.source.binding
        if key in owners:
            raise LedgerError("writer_exists")
        owners[key] = self
        self.runtime_epoch = uuid4().hex
        self._generation = 0
        self._busy = False
        self._revocations: list[str] = []
        self._last: ParticipantAggregation | None = None
        self._closed = False
        self._request: GateRequest | None = None
        self._remove = [
            self.hass.bus.async_listen("state_changed", self._changed),
            self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._changed),
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._changed),
            self.source.shots.entry.add_update_listener(self._entry_changed),
        ]

    @property
    def native_activation_allowed(self) -> Literal[False]:
        return False

    @callback
    def _changed(self, event) -> None:
        # All state events intentionally conservative; no exclusion can hide a
        # newly configured sensor, writer or target during an await.
        self.revoke("runtime_event")

    async def _entry_changed(self, hass, entry) -> None:
        self.revoke("configuration_event")

    def revoke(self, reason: str = "confirmation_revoked") -> None:
        """Invalidate diagnostic evidence only; revocation never awaits a lock."""
        self._generation += 1
        self._revocations.append(reason)
        self._last = None
        self._request = None

    def close(self) -> None:
        self.revoke("coordinator_closed")
        self._closed = True
        for remove in self._remove:
            remove()
        self._remove.clear()
        # Lifetime reservation is retained, preventing old proof/owner reuse.

    def _observations(self):
        return (*self.source._snapshots(), self.scheduler.recovery_gate_snapshot())

    def _sensor_observations(self):
        raw = {**self.source.shots.entry.data, **self.source.shots.entry.options}
        entities = (
            *normalize_vwc_sensors(raw.get("vwc_sensor")),
            raw.get("drain_sensor"),
            raw.get("drain_tray_sensor"),
        )
        return tuple(
            (
                entity,
                None
                if (state := self.hass.states.get(entity)) is None
                else (
                    state.state,
                    bool(state.attributes.get("restored")),
                    state.last_updated,
                    state.last_reported,
                ),
            )
            for entity in entities
            if isinstance(entity, str)
        )

    def _guard(self):
        return _digest(
            _json(
                {
                    "participants": [
                        observation_digest(s) for s in self._observations()
                    ],
                    "ledger": self.ledger.inspect(),
                    "pump_ownership": self.source.pump.ownership,
                    "shot_ownership": self.source.shots.ownership,
                    "sensor_policy": self.source.shots.sensor_policy,
                    "sensors": self._sensor_observations(),
                    "sensor_assessment": self.source.shots.sensor_assessment().diagnostic(),
                    "data": dict(self.source.shots.entry.data),
                    "generation": self._generation,
                }
            )
        )

    def _collect(self, request):
        shots, accounting = self.source.shots.recovery_gate_evidence(request)
        return (
            self.source.pump.recovery_gate_evidence(request),
            shots,
            accounting,
            self.source.cycle.recovery_gate_evidence(request),
            self.scheduler.recovery_gate_evidence(request),
        )

    def _revisions_current(self, request):
        snapshots = self._observations()
        ledger_revision = next(
            (r for r in request.revisions if r.source == "ledger"), None
        )
        if ledger_revision != EvidenceRevision(
            "ledger", _digest(_json(self.ledger.inspect())), self.ledger.revision
        ):
            return False
        return all(
            next((r for r in request.revisions if r.source == gate), None)
            == evidence_revision(gate, snapshots[index])
            for gate, index in (
                ("pump", 0),
                ("shots", 1),
                ("accounting", 1),
                ("cycle", 2),
                ("scheduler", 3),
            )
        )

    def confirmation_current(self, result: ParticipantAggregation) -> bool:
        """Diagnostic identity/liveness check, never an authorization predicate."""
        if (
            result is not self._last
            or self._busy
            or self._closed
            or result.request is None
            or result.request.runtime_epoch != self.runtime_epoch
            or result.request.safety_generation != self._generation
            or not 0
            <= (dt_util.utcnow() - result.request.captured_at).total_seconds()
            <= 30
        ):
            return False
        try:
            return (
                self._guard() == self._last_guard
                and self._collect(result.request) == result.evidence
            )
        except Exception:
            return False

    async def async_check(self) -> ParticipantAggregation:
        if self._closed:
            raise LedgerError("coordinator_closed")
        if self._busy:
            raise LedgerError("participant_check_busy")
        self._busy = True
        self._last = None
        self._request = None
        generation = self._generation
        request = None
        evidence = ()
        try:
            guard = self._guard()
            before = await async_create_reconciliation_plan(self.ledger)
            snapshots = self._observations()
            revisions = tuple(
                evidence_revision(gate, snapshots[index])
                for gate, index in (
                    ("pump", 0),
                    ("shots", 1),
                    ("accounting", 1),
                    ("cycle", 2),
                    ("scheduler", 3),
                )
            )
            revisions += (
                EvidenceRevision(
                    "ledger",
                    _digest(_json(self.ledger.inspect())),
                    self.ledger.revision,
                ),
            )
            request = GateRequest(
                uuid4().hex,
                self.source.binding,
                snapshots[2].current_cycle_id,
                tuple(
                    OperationScope(
                        op,
                        raw.get("cycle_id"),
                        _digest(_json(snapshots[1].journal["operations"][op])),
                        raw["acknowledged"],
                        raw["disposition"],
                    )
                    for op, raw in before.operations.items()
                ),
                revisions,
                self.runtime_epoch,
                generation,
                dt_util.utcnow(),
                snapshots[0].runtime.boot,
            )
            self._request = request
            evidence = self._collect(request)
            after = await async_create_reconciliation_plan(self.ledger)
            blockers = [f.code for f in before.findings] + list(before.open_checks)
            blockers += [f.code for f in after.findings] + list(after.open_checks)
            # No await after final validation/publication. Recapture every owner
            # so ownership loss, local flags and pending work cannot be hidden.
            if (
                generation != self._generation
                or guard != self._guard()
                or before.evidence_digests != after.evidence_digests
                or before.source_revisions != after.source_revisions
                or self._collect(request) != evidence
            ):
                blockers.append("evidence_changed")
            result = aggregate_evidence(request, evidence, tuple(blockers))
            if "evidence_changed" not in blockers:
                self._last_guard = self._guard()
                self._last = result
            return result
        except Exception as err:
            code = (
                err.code
                if isinstance(err, LedgerError)
                else "participant_evidence_invalid"
            )
            self._request = None
            reasons = tuple(
                dict.fromkeys(
                    (code,)
                    + tuple(reason for proof in evidence for reason in proof.blockers)
                )
            )
            return ParticipantAggregation(request, evidence, reasons, False, False)
        finally:
            if self._last is None:
                self._request = None
            self._busy = False
