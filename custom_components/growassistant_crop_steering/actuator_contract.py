"""Design-only actuator values and pure checks, never hardware authorization.

No issuer, transport, storage, gate mutation or production consumer exists.
Matching caller-supplied values proves consistency only, never authenticity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .recovery_participant import ParticipantAggregation, aggregate_evidence
from .recovery_revision import BootEpoch, JournalReceipt, OwnershipGeneration
from .recovery_snapshot import ResourceBinding


@dataclass(frozen=True)
class ActuatorIdentity:
    binding: ResourceBinding
    hardware_id: str
    channel: str
    enforcement_domain: str


@dataclass(frozen=True)
class OwnershipEpoch:
    gateway_boot: str
    generation: OwnershipGeneration


@dataclass(frozen=True)
class OwnershipLease:
    actuator: ActuatorIdentity
    epoch: OwnershipEpoch
    owner_id: str
    ha_boot: BootEpoch
    lease_id: str
    issued_tick: float
    expires_tick: float


@dataclass(frozen=True)
class FencingToken:
    lease_id: str
    epoch: OwnershipEpoch
    owner_id: str


@dataclass(frozen=True)
class CommandIntent:
    actuator: ActuatorIdentity
    command_id: str
    action: Literal["on", "off"]
    token: FencingToken
    ha_boot: BootEpoch
    sequence: int
    deadline_tick: float


@dataclass(frozen=True)
class CommandReceipt:
    intent: CommandIntent
    gateway_boot: str
    sequence: int
    result: Literal["accepted", "rejected", "software_off"]
    observed_state: Literal["on", "off", "unknown"]
    observed_tick: float


@dataclass(frozen=True)
class IsolationEvidence:
    """Proposed evidence references; strings are not verified attestations."""

    lease: OwnershipLease
    enforcement_reference: str
    excluded_paths: tuple[str, ...]
    checked_tick: float
    exclusive_isolation_confirmed: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class ContractCheck:
    blockers: tuple[str, ...]
    # Even an empty blocker tuple is ONLY data consistency.
    hardware_exclusivity_proven: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)


def _text(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _integer(value: int, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def validate_lease(
    lease: OwnershipLease,
    *,
    actuator: ActuatorIdentity,
    epoch: OwnershipEpoch,
    ha_boot: BootEpoch,
    now_tick: float,
) -> ContractCheck:
    """Ticks belong to the expected gateway boot's monotonic clock, not UTC."""
    reasons = []
    if lease.actuator != actuator or not all(
        _text(v)
        for v in (
            actuator.hardware_id,
            actuator.channel,
            actuator.enforcement_domain,
            actuator.binding.entry_id,
            actuator.binding.entity_id,
        )
    ):
        reasons.append("actuator_identity_conflict")
    if (
        lease.epoch != epoch
        or not _text(epoch.gateway_boot)
        or not _integer(epoch.generation.value, 1)
        or lease.ha_boot != ha_boot
        or not _text(ha_boot.value)
    ):
        reasons.append("ownership_epoch_conflict")
    if not _text(lease.owner_id) or not _text(lease.lease_id):
        reasons.append("lease_identity_invalid")
    if not all(_number(v) for v in (lease.issued_tick, lease.expires_tick, now_tick)):
        reasons.append("lease_time_invalid")
    elif not 0 <= lease.issued_tick <= now_tick < lease.expires_tick:
        reasons.append("lease_expired_or_invalid")
    return ContractCheck(tuple(reasons))


def validate_command(
    intent: CommandIntent,
    lease: OwnershipLease,
    *,
    actuator: ActuatorIdentity,
    epoch: OwnershipEpoch,
    ha_boot: BootEpoch,
    now_tick: float,
    last_sequence: int,
) -> ContractCheck:
    """Normal channel only. Emergency OFF has no implementation in this PR.

    Invalid normal OFF is not permission to prevent the separate safety stop.
    Exact duplicate handling requires a future durable gateway deduplication log.
    """
    reasons = list(
        validate_lease(
            lease, actuator=actuator, epoch=epoch, ha_boot=ha_boot, now_tick=now_tick
        ).blockers
    )
    if intent.actuator != actuator:
        reasons.append("command_target_conflict")
    if intent.token != FencingToken(lease.lease_id, epoch, lease.owner_id):
        reasons.append("fencing_conflict")
    if intent.ha_boot != ha_boot:
        reasons.append("command_boot_conflict")
    if not _text(intent.command_id) or intent.action not in {"on", "off"}:
        reasons.append("command_invalid")
    if (
        not _integer(last_sequence)
        or not _integer(intent.sequence, 1)
        or intent.sequence <= last_sequence
    ):
        reasons.append("command_sequence_conflict")
    if (
        not _number(intent.deadline_tick)
        or not _number(now_tick)
        or not now_tick < intent.deadline_tick <= lease.expires_tick
    ):
        reasons.append("command_deadline_invalid")
    return ContractCheck(tuple(dict.fromkeys(reasons)))


def validate_receipt(
    receipt: CommandReceipt,
    intent: CommandIntent,
    *,
    gateway_boot: str,
    now_tick: float,
    max_age: float,
) -> ContractCheck:
    """Receipt shape/freshness only; software_off is not physical isolation."""
    reasons = []
    if (
        receipt.intent != intent
        or receipt.gateway_boot != gateway_boot
        or gateway_boot != intent.token.epoch.gateway_boot
        or not _text(gateway_boot)
        or not _integer(receipt.sequence, 1)
        or receipt.sequence != intent.sequence
    ):
        reasons.append("receipt_binding_conflict")
    if receipt.result not in {"accepted", "rejected", "software_off"}:
        reasons.append("receipt_result_invalid")
    if receipt.result == "rejected":
        reasons.append("command_rejected")
    if receipt.observed_state not in {"on", "off"}:
        reasons.append("actuator_state_unknown")
    if receipt.result == "software_off" and (
        intent.action != "off" or receipt.observed_state != "off"
    ):
        reasons.append("receipt_off_conflict")
    if not all(_number(v) for v in (receipt.observed_tick, now_tick, max_age)) or not (
        max_age >= 0 and 0 <= now_tick - receipt.observed_tick <= max_age
    ):
        reasons.append("receipt_time_invalid")
    return ContractCheck(tuple(reasons))


def validate_gate_contract(
    aggregation: ParticipantAggregation,
    *,
    expected_revisions: tuple[JournalReceipt, ...],
    observed_revisions: tuple[JournalReceipt, ...],
) -> ContractCheck:
    """Preserve all actual participant blockers; never infer release readiness."""
    reasons = list(aggregation.blockers)
    if not aggregation.collection_complete or aggregation.request is None:
        reasons.append("gate_evidence_incomplete")
    else:
        checked = aggregate_evidence(aggregation.request, aggregation.evidence)
        reasons.extend(checked.blockers)
        if not checked.collection_complete:
            reasons.append("gate_evidence_incomplete")
        receipts = tuple(
            r.receipt for r in aggregation.request.revisions if r.receipt is not None
        )
        if set(receipts) != set(observed_revisions):
            reasons.append("revision_conflict")
    for revisions in (expected_revisions, observed_revisions):
        identities = [r.identity for r in revisions]
        if (
            not revisions
            or len(set(identities)) != len(identities)
            or any(
                not _integer(r.revision, 1) or not _text(r.digest) for r in revisions
            )
        ):
            reasons.append("revision_invalid")
    if set(expected_revisions) != set(observed_revisions):
        reasons.append("revision_conflict")
    reasons.extend(("enforcement_unproven", "coordinated_resolution_unimplemented"))
    return ContractCheck(tuple(dict.fromkeys(reasons)))
