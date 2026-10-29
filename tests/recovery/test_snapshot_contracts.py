"""Pure evidence validation: no HA runtime, commands, stores or gate changes."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.growassistant_crop_steering.recovery_snapshot import (
    CycleSafetySnapshot,
    OffEvidence,
    PumpSafetySnapshot,
    ReasonCode,
    ResourceBinding,
    ShotSafetySnapshot,
    TaskState,
    ValidationResult,
    VersionEvidence,
    WorkEvidence,
    validate_cycle,
    validate_pump,
    validate_safety,
    validate_shots,
)

NOW = datetime(2026, 10, 13, 7, tzinfo=UTC)
BINDING = ResourceBinding("entry", "switch.fake", "registry", "device")
COUNTERS = dict(p1_shots_done=0, p2_shots_done=0, p3_emergency_shots_done=0)
VALUES = dict(
    p1_active=False, p1_done=False, p1_window_opened_today=False, p2_ref_vwc=0
)
OPTIONS = {**VALUES, **COUNTERS}
CYCLE = "2026-10-13T06:00:00+00:00"
SCHEDULE = {
    "led_sunrise": "06:00:00",
    "led_sunset": "18:00:00",
    "p0_transpiration_min": 60,
    "p1_duration_min": 120,
    "p2_end_offset_min": 120,
    "p1_mode": "sensor",
    "p2_mode": "automatic",
}


def version(data, suffix):
    # Pure validator model: a well-formed receipt is not live issuance or a gate.
    from custom_components.growassistant_crop_steering.const import DOMAIN
    from custom_components.growassistant_crop_steering.recovery_revision import (
        JournalIdentity,
        JournalReceipt,
        content_digest,
    )

    try:
        digest = content_digest(data)
    except (TypeError, ValueError):
        return VersionEvidence(snapshot_schema=2)
    receipt = JournalReceipt(
        JournalIdentity("entry", "switch.fake", f"{DOMAIN}.{suffix}.entry", "1" * 32),
        1,
        digest,
    )
    return VersionEvidence(2, 1, 1, 1, receipt, "confirmed")


def pump():
    # Synthetic evidence contract; production snapshots cannot assert physical OFF.
    return PumpSafetySnapshot(
        BINDING,
        NOW,
        "idle",
        None,
        OffEvidence(BINDING, "off", NOW, False, NOW, True, True),
        (),
        False,
        False,
        False,
        False,
        (),
        version(None, "pump_recovery"),
    )


def shots(journal=None):
    return ShotSafetySnapshot(
        BINDING,
        NOW,
        "idle",
        journal if journal is not None else {"operations": {}},
        OPTIONS,
        CYCLE,
        (),
        False,
        True,
        False,
        (),
        version(journal if journal is not None else {"operations": {}}, "shot_journal"),
    )


def cycle(**updates):
    record = dict(
        cycle_id=CYCLE,
        phase="p0_transpiration",
        values=VALUES,
        counters=COUNTERS,
        schedule=SCHEDULE,
        reference_cycle=None,
        pending=False,
    )
    record.update(updates)
    return CycleSafetySnapshot(
        BINDING,
        NOW,
        record,
        OPTIONS,
        SCHEDULE,
        CYCLE,
        "p0_transpiration",
        (),
        False,
        True,
        (),
        version(record, "native_cycle"),
    )


def codes(result):
    assert result.native_activation_allowed is False
    return {finding.code for finding in result.findings}


def check(snapshot, validator):
    return validator(snapshot, BINDING, now=NOW, max_age=30)


def test_complete_contract_is_only_diagnostic():
    for snapshot, validator in (
        (pump(), validate_pump),
        (shots(), validate_shots),
        (cycle(), validate_cycle),
    ):
        result = check(snapshot, validator)
        assert result.diagnostic_ready and result.status == "diagnostic_ready"
        assert not result.native_activation_allowed
    result = validate_safety(pump(), shots(), cycle(), BINDING, now=NOW, max_age=30)
    assert ReasonCode.REQUIRED_CHECK_UNKNOWN in codes(result)
    assert not result.diagnostic_ready
    with pytest.raises(TypeError):
        ValidationResult((), (), native_activation_allowed=True)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"state": "on"}, ReasonCode.OFF_MISSING),
        ({"reported_at": None, "verified_report_at": None}, ReasonCode.OFF_MISSING),
        ({"reported_at": NOW - timedelta(seconds=31)}, ReasonCode.OFF_STALE),
        ({"reported_at": NOW + timedelta(seconds=1)}, ReasonCode.OFF_STALE),
        ({"reported_at": NOW.replace(tzinfo=None)}, ReasonCode.OFF_STALE),
        ({"restored": True}, ReasonCode.OFF_RESTORED),
        ({"restored": None}, ReasonCode.OFF_RESTORED),
        ({"after_commands": None}, ReasonCode.OFF_ORDER_UNKNOWN),
        ({"independent_confirmed": None}, ReasonCode.PHYSICAL_OFF_UNKNOWN),
        (
            {"binding": ResourceBinding("other", "switch.fake")},
            ReasonCode.BINDING_MISMATCH,
        ),
    ],
)
def test_off_evidence_fail_closed(change, reason):
    snapshot = pump()
    assert reason in codes(
        check(replace(snapshot, off=replace(snapshot.off, **change)), validate_pump)
    )


@pytest.mark.parametrize(
    "kind,state,reason",
    [
        ("turn_on", TaskState.PENDING, ReasonCode.COMMAND_UNRESOLVED),
        ("turn_off", TaskState.CANCELLED, ReasonCode.COMMAND_UNRESOLVED),
        ("activation", TaskState.UNKNOWN, ReasonCode.TASK_UNRESOLVED),
        ("late_reconciliation", TaskState.PENDING, ReasonCode.TASK_UNRESOLVED),
        ("storage", TaskState.PENDING, ReasonCode.STORAGE_UNRESOLVED),
        ("storage", TaskState.FAILED, ReasonCode.STORAGE_UNRESOLVED),
        ("storage", TaskState.CANCELLED, ReasonCode.STORAGE_UNRESOLVED),
    ],
)
def test_unresolved_work(kind, state, reason):
    snapshot = replace(pump(), work=(WorkEvidence(kind, state),))
    assert reason in codes(check(snapshot, validate_pump))


def test_late_completion_is_not_a_clearance():
    snapshot = replace(
        pump(), work=(WorkEvidence("turn_on", TaskState.COMPLETED, timed_out=True),)
    )
    assert ReasonCode.COMMAND_UNRESOLVED in codes(check(snapshot, validate_pump))


@pytest.mark.parametrize(
    "version,reason",
    [
        (VersionEvidence(store_schema=None, revision=1), ReasonCode.SCHEMA_UNKNOWN),
        (VersionEvidence(snapshot_schema=99, revision=1), ReasonCode.SCHEMA_UNKNOWN),
        (VersionEvidence(), ReasonCode.REVISION_UNKNOWN),
        (VersionEvidence(revision=True), ReasonCode.REVISION_UNKNOWN),
    ],
)
def test_unknown_versions(version, reason):
    assert reason in codes(check(replace(pump(), version=version), validate_pump))


@pytest.mark.parametrize(
    "binding",
    [
        ResourceBinding(None, None),
        ResourceBinding("other", "switch.fake"),
        ResourceBinding("entry", "switch.other"),
    ],
)
def test_exact_resource_binding(binding):
    result = check(replace(pump(), binding=binding), validate_pump)
    assert ReasonCode.BINDING_MISMATCH in codes(result)


def operation(**updates):
    record = dict(
        operation_id="one",
        entry_id="entry",
        pump_entity="switch.fake",
        cycle_id=CYCLE,
        state="completed",
        accounted=True,
        accounting_pending=False,
        recovery_required=False,
        delivery="software_completed",
        delivery_uncertain=False,
        shutdown_confirmed=True,
    )
    record.update(updates)
    return record


@pytest.mark.parametrize(
    "record,reason",
    [
        (
            operation(
                state="interrupted",
                accounted=False,
                delivery="possible_partial",
                delivery_uncertain=True,
            ),
            ReasonCode.PARTIAL_DELIVERY,
        ),
        (operation(accounted=False), ReasonCode.ACCOUNTING_INCONSISTENT),
        (operation(accounting_pending=True), ReasonCode.ACCOUNTING_INCONSISTENT),
        (operation(shutdown_confirmed=False), ReasonCode.ACCOUNTING_INCONSISTENT),
        (operation(state="running"), ReasonCode.SHOT_UNRESOLVED),
        (operation(pump_entity="switch.other"), ReasonCode.BINDING_MISMATCH),
        ({}, ReasonCode.ACCOUNTING_INCONSISTENT),
    ],
)
def test_shot_conflicts(record, reason):
    assert reason in codes(
        check(shots({"operations": {"one": record}}), validate_shots)
    )


def test_duplicate_accounting_and_missing_checkpoint():
    evidence = shots({"operations": {"one": operation(), "two": operation()}})
    assert ReasonCode.ACCOUNTING_INCONSISTENT in codes(check(evidence, validate_shots))


@pytest.mark.parametrize(
    "update",
    [
        {"pending": True},
        {"phase": "unknown"},
        {"cycle_id": "old"},
        {"values": {**VALUES, "p1_active": True, "p1_done": True}},
        {"values": {**VALUES, "p2_ref_vwc": float("nan")}},
        {"values": {**VALUES, "p1_done": True}, "reference_cycle": "old"},
        {"counters": {**COUNTERS, "p1_shots_done": -1}},
        {"schedule": {}},
    ],
)
def test_cycle_conflicts(update):
    assert ReasonCode.CYCLE_INCONSISTENT in codes(
        check(cycle(**update), validate_cycle)
    )


def test_unknown_and_incomplete_restart_evidence():
    result = check(
        replace(pump(), version=VersionEvidence(), restart_had_recovery=True),
        validate_pump,
    )
    assert {ReasonCode.REVISION_UNKNOWN, ReasonCode.PUMP_UNRESOLVED} <= codes(result)
    assert ReasonCode.CYCLE_INCONSISTENT in codes(
        check(replace(cycle(), record=None), validate_cycle)
    )


def test_nested_evidence_is_detached_and_immutable():
    journal = {"operations": {"one": operation()}}
    snapshot = shots(journal)
    journal["operations"]["one"]["state"] = "fault"
    assert snapshot.journal["operations"]["one"]["state"] == "completed"
    with pytest.raises(TypeError):
        snapshot.journal["operations"]["one"]["state"] = "fault"
    with pytest.raises(FrozenInstanceError):
        snapshot.blocked = False


@pytest.mark.parametrize("value", [{}, [], "bad", None])
def test_malformed_domain_values_return_findings(value):
    assert ReasonCode.SHOT_UNRESOLVED in codes(
        check(shots({"operations": {"one": operation(state=value)}}), validate_shots)
    )
    assert ReasonCode.CYCLE_INCONSISTENT in codes(
        check(cycle(phase=value), validate_cycle)
    )


def test_more_accounted_operations_than_counter_is_conflict():
    evidence = shots(
        {
            "operations": {
                "one": operation(phase="p1_morning"),
                "two": operation(operation_id="two", phase="p1_morning"),
            },
            "last_accounting": {"cycle_id": CYCLE, "values": COUNTERS},
            "native_counters": COUNTERS,
        }
    )
    assert ReasonCode.ACCOUNTING_INCONSISTENT in codes(check(evidence, validate_shots))
