"""Read-only recovery evidence. No permit, mutation, IO or hardware authority."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from .const import DOMAIN
from .recovery_revision import (
    JournalReceipt,
    RuntimeEpochs,
    decode_receipt,
    encode_receipt,
)


def freeze(value: object) -> object:
    """Detach journal evidence recursively without duplicating its domain model."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return UnknownEvidence(type(value).__name__)


@dataclass(frozen=True)
class UnknownEvidence:
    """Unsupported journal value, never an empty/clean replacement."""

    type_name: str


@dataclass(frozen=True)
class ResourceBinding:
    entry_id: str | None
    entity_id: str | None
    registry_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class VersionEvidence:
    snapshot_schema: int = 1
    store_schema: int | None = 1
    revision: int | None = None
    revision_schema: int | None = None
    receipt: JournalReceipt | None = None
    status: str = "unknown"


def version_evidence(journal, data, entity) -> VersionEvidence:
    receipt = journal.evidence(data, entity)
    return VersionEvidence(
        2,
        1,
        None if receipt is None else receipt.revision,
        1,
        receipt,
        "confirmed" if receipt is not None else "unknown",
    )


class TaskState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


def task_state(task: asyncio.Task | None) -> TaskState:
    if task is None:
        return TaskState.ABSENT
    if not isinstance(task, asyncio.Task):
        return TaskState.UNKNOWN
    if not task.done():
        return TaskState.PENDING
    if task.cancelled():
        return TaskState.CANCELLED
    return TaskState.FAILED if task.exception() is not None else TaskState.COMPLETED


@dataclass(frozen=True)
class WorkEvidence:
    kind: str
    state: TaskState
    entity_id: str | None = None
    operation_id: str | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class OffEvidence:
    """HA report and independent shutdown evidence are deliberately separate."""

    binding: ResourceBinding
    state: str | None
    reported_at: datetime | None
    restored: bool | None
    verified_report_at: datetime | None = None
    after_commands: bool | None = None
    independent_confirmed: bool | None = None


@dataclass(frozen=True)
class PumpSafetySnapshot:
    binding: ResourceBinding
    captured_at: datetime
    state: str
    record: object
    off: OffEvidence
    work: tuple[WorkEvidence, ...]
    deadline_pending: bool
    stop_requested: bool
    lock_held: bool
    restart_had_recovery: bool
    reasons: tuple[str, ...]
    version: VersionEvidence = field(default_factory=VersionEvidence)
    runtime: RuntimeEpochs | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "record", freeze(self.record))


@dataclass(frozen=True)
class ShotSafetySnapshot:
    binding: ResourceBinding
    captured_at: datetime
    state: str
    journal: object
    options: object
    cycle_id: str | None
    work: tuple[WorkEvidence, ...]
    accounting_locked: bool
    initialized: bool
    blocked: bool
    reasons: tuple[str, ...]
    version: VersionEvidence = field(default_factory=VersionEvidence)
    runtime: RuntimeEpochs | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "journal", freeze(self.journal))
        object.__setattr__(self, "options", freeze(self.options))


@dataclass(frozen=True)
class CycleSafetySnapshot:
    binding: ResourceBinding
    captured_at: datetime
    record: object
    options: object
    schedule: object
    current_cycle_id: str | None
    current_phase: str | None
    work: tuple[WorkEvidence, ...]
    lock_held: bool
    initialized: bool
    reasons: tuple[str, ...]
    version: VersionEvidence = field(default_factory=VersionEvidence)
    runtime: RuntimeEpochs | None = None

    def __post_init__(self) -> None:
        for name in ("record", "options", "schedule"):
            object.__setattr__(self, name, freeze(getattr(self, name)))


class ReasonCode(StrEnum):
    BINDING_UNKNOWN = "binding_unknown"
    BINDING_MISMATCH = "binding_mismatch"
    ACTOR_IDENTITY_UNKNOWN = "actor_identity_unknown"
    SCHEMA_UNKNOWN = "schema_unknown"
    REVISION_UNKNOWN = "revision_unknown"
    REVISION_CONFLICT = "revision_conflict"
    SNAPSHOT_TIME_INVALID = "snapshot_time_invalid"
    OFF_MISSING = "off_missing"
    OFF_STALE = "off_stale"
    OFF_RESTORED = "off_restored"
    OFF_ORDER_UNKNOWN = "off_order_unknown"
    PHYSICAL_OFF_UNKNOWN = "physical_off_unknown"
    COMMAND_UNRESOLVED = "command_unresolved"
    TASK_UNRESOLVED = "task_unresolved"
    STORAGE_UNRESOLVED = "storage_unresolved"
    PUMP_UNRESOLVED = "pump_unresolved"
    ACCOUNTING_INCONSISTENT = "accounting_inconsistent"
    SHOT_UNRESOLVED = "shot_unresolved"
    PARTIAL_DELIVERY = "partial_delivery"
    CYCLE_INCONSISTENT = "cycle_inconsistent"
    RECOVERY_OPEN = "recovery_open"
    REQUIRED_CHECK_UNKNOWN = "required_check_unknown"


@dataclass(frozen=True)
class Finding:
    code: ReasonCode
    source: str
    detail: str
    unknown: bool = False


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[Finding, ...]
    required_checks: tuple[str, ...]
    native_activation_allowed: Literal[False] = field(default=False, init=False)

    @property
    def diagnostic_ready(self) -> bool:
        return not self.findings

    @property
    def status(self) -> str:
        if not self.findings:
            return "diagnostic_ready"
        return "unknown" if all(f.unknown for f in self.findings) else "negative"


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _common(
    snapshot: PumpSafetySnapshot | ShotSafetySnapshot | CycleSafetySnapshot,
    expected: ResourceBinding,
    now: datetime,
    max_age: float,
) -> tuple[list[Finding], Callable[..., None]]:
    source = type(snapshot).__name__
    findings: list[Finding] = []

    def add(code: ReasonCode, detail: str, unknown: bool = False) -> None:
        findings.append(Finding(code, source, detail, unknown))

    binding = snapshot.binding
    if (
        not expected.entry_id
        or not expected.entity_id
        or not binding.entry_id
        or not binding.entity_id
    ):
        add(ReasonCode.BINDING_UNKNOWN, "Entry/target absent", True)
    if binding != expected:
        add(ReasonCode.BINDING_MISMATCH, "Exact resource binding differs")
    if binding.registry_id is None or binding.device_id is None:
        add(
            ReasonCode.ACTOR_IDENTITY_UNKNOWN,
            "Stable physical actor binding unavailable",
            True,
        )
    version = snapshot.version
    if (
        type(version.snapshot_schema) is not int
        or version.snapshot_schema not in {1, 2}
        or type(version.store_schema) is not int
        or version.store_schema != 1
    ):
        add(ReasonCode.SCHEMA_UNKNOWN, "Unsupported or missing schema", True)
    payload = (
        snapshot.journal
        if isinstance(snapshot, ShotSafetySnapshot)
        else snapshot.record
    )
    receipt = version.receipt
    try:
        suffix = (
            "shot_journal"
            if isinstance(snapshot, ShotSafetySnapshot)
            else (
                "pump_recovery"
                if isinstance(snapshot, PumpSafetySnapshot)
                else "native_cycle"
            )
        )
        receipt_valid = (
            isinstance(receipt, JournalReceipt)
            and decode_receipt(
                encode_receipt(receipt),
                payload,
                binding.entry_id,
                binding.entity_id,
                f"{DOMAIN}.{suffix}.{binding.entry_id}",
            )
            == receipt
        )
    except (TypeError, ValueError, RuntimeError, AttributeError):
        receipt_valid = False
    # Schema 1 remains a historical diagnostic, never newly certified evidence.
    if (
        version.snapshot_schema != 2
        or type(version.revision_schema) is not int
        or version.revision_schema != 1
        or version.status != "confirmed"
        or not receipt_valid
        or type(version.revision) is not int
        or version.revision < 1
        or receipt.revision != version.revision
        or receipt.identity.entry_id != binding.entry_id
        or receipt.identity.pump_entity != binding.entity_id
    ):
        add(ReasonCode.REVISION_UNKNOWN, "No durable participant revision", True)
    if (
        not _aware(now)
        or not _aware(snapshot.captured_at)
        or not math.isfinite(max_age)
        or max_age <= 0
    ):
        add(
            ReasonCode.SNAPSHOT_TIME_INVALID,
            "Aware time and positive finite age required",
        )
    elif not 0 <= (now - snapshot.captured_at).total_seconds() <= max_age:
        add(ReasonCode.SNAPSHOT_TIME_INVALID, "Snapshot expired or future")
    for work in snapshot.work:
        if work.kind in {"turn_on", "turn_off"}:
            if work.entity_id is None:
                add(ReasonCode.BINDING_UNKNOWN, "Command target absent", True)
            elif work.entity_id != expected.entity_id:
                add(ReasonCode.BINDING_MISMATCH, "Command targets another actor")
        if work.kind == "storage":
            if work.state not in {TaskState.ABSENT, TaskState.COMPLETED}:
                add(ReasonCode.STORAGE_UNRESOLVED, str(work.state))
        elif (
            work.state not in {TaskState.ABSENT, TaskState.COMPLETED} or work.timed_out
        ):
            add(
                ReasonCode.COMMAND_UNRESOLVED
                if work.kind in {"turn_on", "turn_off"}
                else ReasonCode.TASK_UNRESOLVED,
                work.kind,
            )
    for reason in snapshot.reasons:
        add(ReasonCode.RECOVERY_OPEN, reason)
    return findings, add


def validate_pump(
    snapshot: PumpSafetySnapshot,
    expected: ResourceBinding,
    *,
    now: datetime,
    max_age: float,
) -> ValidationResult:
    findings, add = _common(snapshot, expected, now, max_age)
    off = snapshot.off
    if snapshot.record is not None:
        record = _mapping(snapshot.record)
        if not record.get("entity_id"):
            add(ReasonCode.BINDING_UNKNOWN, "Recovery journal target absent", True)
        elif record.get("entity_id") != expected.entity_id:
            add(ReasonCode.BINDING_MISMATCH, "Recovery journal retains another target")
    if off.binding != expected:
        add(ReasonCode.BINDING_MISMATCH, "OFF refers to another resource")
    if off.state != "off" or off.reported_at is None or off.verified_report_at is None:
        add(ReasonCode.OFF_MISSING, "No verified fresh OFF exchange", True)
    for instant in (off.reported_at, off.verified_report_at):
        if instant is not None and (
            not _aware(instant)
            or not _aware(now)
            or not 0 <= (now - instant).total_seconds() <= max_age
        ):
            add(ReasonCode.OFF_STALE, "OFF timestamp stale, future or naive")
    if off.reported_at != off.verified_report_at:
        add(
            ReasonCode.OFF_ORDER_UNKNOWN,
            "Current report differs from verified exchange",
            True,
        )
    if off.restored is not False:
        add(
            ReasonCode.OFF_RESTORED,
            "Restored/unknown source cannot prove fresh OFF",
            off.restored is None,
        )
    if off.after_commands is not True:
        add(
            ReasonCode.OFF_ORDER_UNKNOWN, "OFF after all commands not established", True
        )
    if off.independent_confirmed is not True:
        add(
            ReasonCode.PHYSICAL_OFF_UNKNOWN,
            "No independent physical shutdown evidence",
            True,
        )
    if (
        snapshot.state != "idle"
        or snapshot.record is not None
        or snapshot.restart_had_recovery
    ):
        add(
            ReasonCode.PUMP_UNRESOLVED,
            "Pump lifecycle/journal/restart remains unresolved",
        )
    if snapshot.deadline_pending or snapshot.stop_requested or snapshot.lock_held:
        add(ReasonCode.TASK_UNRESOLVED, "Pump deadline, stop or serialized work active")
    return ValidationResult(
        tuple(findings),
        ("fresh_off", "physical_off", "commands_drained", "durable_revision"),
    )


COUNTER_KEYS = ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")


CYCLE_SCHEDULE_KEYS = (
    "led_sunrise",
    "led_sunset",
    "p0_transpiration_min",
    "p1_duration_min",
    "p2_end_offset_min",
    "p1_mode",
    "p2_mode",
)


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def validate_shots(
    snapshot: ShotSafetySnapshot,
    expected: ResourceBinding,
    *,
    now: datetime,
    max_age: float,
) -> ValidationResult:
    findings, add = _common(snapshot, expected, now, max_age)
    journal = _mapping(snapshot.journal)
    operations = journal.get("operations")
    options = _mapping(snapshot.options)
    if not isinstance(operations, Mapping):
        add(ReasonCode.ACCOUNTING_INCONSISTENT, "Missing/malformed operations")
        operations = {}
    seen = set()
    accounted = False
    for key, raw in operations.items():
        record = _mapping(raw)
        operation = record.get("operation_id", key)
        if (
            not isinstance(operation, str)
            or not operation
            or key != operation
            or operation in seen
        ):
            add(
                ReasonCode.ACCOUNTING_INCONSISTENT,
                "Duplicate/mismatched operation identity",
            )
        if isinstance(operation, str):
            seen.add(operation)
        if record.get("entry_id") is None:
            add(
                ReasonCode.BINDING_UNKNOWN,
                f"Shot {key}: persisted entry binding absent",
                True,
            )
        if (
            record.get("pump_entity") != expected.entity_id
            or record.get("entry_id", expected.entry_id) != expected.entry_id
        ):
            add(
                ReasonCode.BINDING_MISMATCH,
                f"Shot {key}: entry/target missing or changed",
            )
        state = record.get("state")
        booked = record.get("accounted")
        if (
            type(booked) is not bool
            or (state == "completed") != (booked is True)
            or record.get("accounting_pending") is not False
        ):
            add(
                ReasonCode.ACCOUNTING_INCONSISTENT,
                f"Shot {key}: accounting conflict or intent",
            )
        accounted |= booked is True
        if state not in ("completed", "interrupted", "fault"):
            add(ReasonCode.SHOT_UNRESOLVED, f"Shot {key}: nonterminal/unknown state")
        if record.get("recovery_required") is not False or state in (
            "interrupted",
            "fault",
        ):
            add(ReasonCode.RECOVERY_OPEN, f"Shot {key}: unresolved review")
        if record.get("delivery_uncertain") is not False or record.get(
            "delivery"
        ) not in ("not_requested", "software_completed"):
            add(ReasonCode.PARTIAL_DELIVERY, f"Shot {key}: possible/unknown delivery")
        if booked is True and (
            record.get("shutdown_confirmed") is not True
            or record.get("delivery") != "software_completed"
        ):
            add(
                ReasonCode.ACCOUNTING_INCONSISTENT,
                f"Shot {key}: success without shutdown/delivery evidence",
            )
    if "last_accounting" in journal and not isinstance(
        journal["last_accounting"], Mapping
    ):
        add(ReasonCode.ACCOUNTING_INCONSISTENT, "Malformed accounting checkpoint")
    checkpoint = _mapping(journal.get("last_accounting"))
    if accounted or checkpoint:
        values = _mapping(checkpoint.get("values"))
        if (
            not values
            or checkpoint.get("cycle_id") != snapshot.cycle_id
            or any(options.get(k) != v for k, v in values.items())
        ):
            add(
                ReasonCode.ACCOUNTING_INCONSISTENT,
                "Accounting checkpoint differs from cycle/options",
            )
    counters = journal.get("native_counters")
    if counters is not None and (
        not isinstance(counters, Mapping)
        or any(
            type(counters.get(k)) is not int
            or counters.get(k) < 0
            or options.get(k) != counters.get(k)
            for k in COUNTER_KEYS
        )
    ):
        add(
            ReasonCode.ACCOUNTING_INCONSISTENT,
            "Native counters invalid or differ from options",
        )
    if isinstance(counters, Mapping):
        for phase, counter in zip(
            ("p1_morning", "p2_midday", "p3_dryback"), COUNTER_KEYS, strict=True
        ):
            known = sum(
                1
                for raw in operations.values()
                if _mapping(raw).get("accounted") is True
                and _mapping(raw).get("cycle_id") == snapshot.cycle_id
                and _mapping(raw).get("phase") == phase
            )
            if type(counters.get(counter)) is int and counters[counter] < known:
                add(
                    ReasonCode.ACCOUNTING_INCONSISTENT,
                    "More accounted operations than counter permits",
                )
    if any(
        journal.get(k)
        for k in (
            "recovery_required",
            "pump_restart_requires_review",
            "accounting_inconsistent",
            "missed_cycle_reset",
        )
    ):
        add(ReasonCode.RECOVERY_OPEN, "Journal recovery markers")
    if snapshot.blocked or not snapshot.initialized:
        add(ReasonCode.RECOVERY_OPEN, "Coordinator blocked/uninitialized")
    if snapshot.accounting_locked:
        add(ReasonCode.TASK_UNRESOLVED, "Accounting/reset active")
    return ValidationResult(
        tuple(findings),
        (
            "operations_unique",
            "accounting_consistent",
            "delivery_reviewed",
            "durable_revision",
        ),
    )


def validate_cycle(
    snapshot: CycleSafetySnapshot,
    expected: ResourceBinding,
    *,
    now: datetime,
    max_age: float,
) -> ValidationResult:
    findings, add = _common(snapshot, expected, now, max_age)
    record = _mapping(snapshot.record)
    values = _mapping(record.get("values"))
    options = _mapping(snapshot.options)
    phase = record.get("phase")
    invalid = (
        not record
        or record.get("pending") is not False
        or record.get("cycle_id") != snapshot.current_cycle_id
        or phase != snapshot.current_phase
    )
    invalid |= phase not in (
        "p0_transpiration",
        "p1_morning",
        "p2_midday",
        "p3_dryback",
        "ended",
    )
    invalid |= any(
        type(values.get(k)) is not bool or options.get(k) != values.get(k)
        for k in ("p1_active", "p1_done", "p1_window_opened_today")
    )
    active, done, opened = (
        values.get(k) for k in ("p1_active", "p1_done", "p1_window_opened_today")
    )
    invalid |= bool(active and (done or not opened or phase != "p1_morning")) or bool(
        done and not opened
    )
    reference = values.get("p2_ref_vwc")
    invalid |= (
        type(reference) not in {int, float}
        or not math.isfinite(reference)
        or not 0 <= reference <= 100
        or options.get("p2_ref_vwc") != reference
    )
    if done:
        invalid |= record.get("reference_cycle") != record.get("cycle_id")
    else:
        invalid |= record.get("reference_cycle") is not None or reference != 0
    counters = _mapping(record.get("counters"))
    invalid |= any(
        type(counters.get(k)) is not int or counters.get(k) < 0 for k in COUNTER_KEYS
    )
    schedule = _mapping(record.get("schedule"))
    invalid |= (
        any(key not in schedule for key in CYCLE_SCHEDULE_KEYS)
        or schedule != snapshot.schedule
    )
    if phase == "p0_transpiration":
        invalid |= any(
            values.get(key) is not False
            for key in ("p1_active", "p1_done", "p1_window_opened_today")
        )
    try:
        identity = datetime.fromisoformat(record.get("cycle_id"))
        invalid |= not _aware(identity)
        previous = record.get("previous_cycle_end")
        if previous is not None:
            previous_instant = datetime.fromisoformat(previous)
            invalid |= (
                not _aware(previous_instant)
                or not _aware(identity)
                or previous_instant >= identity
            )
    except (ValueError, TypeError):
        invalid = True
    if invalid:
        add(
            ReasonCode.CYCLE_INCONSISTENT,
            "Cycle identity, phase, flags, reference, pending or schedule inconsistent",
        )
    if not snapshot.initialized:
        add(ReasonCode.RECOVERY_OPEN, "Cycle uninitialized/closed")
    if snapshot.lock_held:
        add(ReasonCode.TASK_UNRESOLVED, "Cycle transition active")
    return ValidationResult(
        tuple(findings),
        ("cycle_consistent", "accounting_consistent", "durable_revision"),
    )


def validate_safety(
    pump: PumpSafetySnapshot,
    shots: ShotSafetySnapshot,
    cycle: CycleSafetySnapshot,
    expected: ResourceBinding,
    *,
    now: datetime,
    max_age: float,
) -> ValidationResult:
    """Aggregate diagnostics only; never a recovery or production authorization."""
    results = (
        validate_pump(pump, expected, now=now, max_age=max_age),
        validate_shots(shots, expected, now=now, max_age=max_age),
        validate_cycle(cycle, expected, now=now, max_age=max_age),
    )
    findings = [finding for result in results for finding in result.findings]
    journal = _mapping(shots.journal)
    record = _mapping(cycle.record)
    counters = _mapping(journal.get("native_counters", record.get("counters")))
    options = _mapping(cycle.options)
    if (
        shots.cycle_id != cycle.current_cycle_id
        or any(options.get(k) != counters.get(k) for k in COUNTER_KEYS)
        or shots.options != cycle.options
    ):
        findings.append(
            Finding(
                ReasonCode.ACCOUNTING_INCONSISTENT,
                "participants",
                "Cycle/counters/options disagree",
            )
        )
    # Stage one has no scheduler drain, fresh ownership epoch or sensor/legacy
    # participant contract. Do not claim that the full release checklist passed.
    findings.append(
        Finding(
            ReasonCode.REQUIRED_CHECK_UNKNOWN,
            "participants",
            "Scheduler drain, sensor/drain policy, ownership epoch and external writers unproven",
            True,
        )
    )
    return ValidationResult(
        tuple(findings),
        tuple(
            dict.fromkeys(
                check for result in results for check in result.required_checks
            )
        )
        + ("scheduler_drained", "sensors_and_drain", "ownership_and_legacy"),
    )
