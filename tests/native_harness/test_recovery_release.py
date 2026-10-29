"""Phase five refuses actual incomplete proofs; never models an eligible plan."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from custom_components.growassistant_crop_steering.recovery_ledger import (
    LedgerError,
    TrustedReviewContext,
)
from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_release import (
    _CycleReleaseCoordinator,
    _HarnessReleaseAuthority,
)
from custom_components.growassistant_crop_steering.recovery_transaction import (
    _RecoveryTransactionCoordinator,
)

from .harness import MemoryStore, NativeHarness
from .test_recovery_ledger import acknowledgment, case_for, ledger_for
from .test_simulation import assert_no_replay_or_accounting, assert_recovery_locked

pytestmark = pytest.mark.asyncio


async def setup(harness):
    ledger, rid = await case_for(harness)
    await ledger.async_acknowledge(**acknowledgment(ledger, rid, harness))
    tx = _RecoveryTransactionCoordinator(ledger)
    tx.store = MemoryStore({}, "transaction")
    await tx.async_initialize()
    authority = _HarnessReleaseAuthority()
    release = _CycleReleaseCoordinator(tx, authority)
    release.store = MemoryStore({}, "release")
    await release.async_initialize()
    plan = await async_create_reconciliation_plan(ledger)
    request = {
        "actor": authority.issue("harness-reviewer", "explicit-second-decision"),
        "plan": plan,
        "request_id": str(uuid4()),
        "transaction_id": str(uuid4()),
        "binding": tx.binding,
        "cycle_id": harness.cycle.cycle_id,
        "expected_ledger_revision": ledger.revision,
        "expected_transaction_revision": 0,
        "boot_id": authority.boot_id,
        "ownership_epoch": "unproven-requested-epoch",
        "decision": "release",
        "rationale": "Explicit decision; incomplete participant proofs must refuse",
    }
    return release, request


async def test_acknowledgment_without_second_decision_stays_locked(simulation):
    release, _ = await setup(simulation)
    assert not release.inspect()["events"]
    assert release.permit is None
    assert_recovery_locked(simulation)


async def test_actual_reviewed_partial_delivery_cannot_release(simulation):
    release, request = await setup(simulation)
    before = simulation.checkpoint()
    commands = list(simulation.actor.commands)
    assert any(
        f.code == "partial_delivery_unresolved" for f in request["plan"].findings
    )
    result = await release.async_request(**request)
    assert result["reason"] == "plan_blocked"
    assert result["released"] is result["native_activation_allowed"] is False
    assert release.permit is None and not release.native_activation_allowed
    assert release.inspect()["effective_state"] == "locked"
    assert simulation.checkpoint() == before
    assert simulation.actor.commands == commands
    assert not simulation.scheduler.running
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)


@pytest.mark.parametrize(
    "actor", [None, "admin", TrustedReviewContext("user", "context")]
)
async def test_untrusted_actor(simulation, actor):
    release, request = await setup(simulation)
    request["actor"] = actor
    with pytest.raises(LedgerError, match="actor_unauthenticated"):
        await release.async_request(**request)
    assert not release.store.records


@pytest.mark.parametrize("other", [False, True])
async def test_equal_or_foreign_capability_is_not_authenticated(simulation, other):
    release, request = await setup(simulation)
    request["actor"] = (
        _HarnessReleaseAuthority().issue("harness-reviewer", "explicit-second-decision")
        if other
        else replace(request["actor"])
    )
    with pytest.raises(LedgerError, match="actor_unauthenticated"):
        await release.async_request(**request)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("expected_ledger_revision", 0, "revision_conflict"),
        ("expected_transaction_revision", 1, "revision_conflict"),
        ("expected_ledger_revision", True, "request_invalid"),
        ("boot_id", "old-boot", "request_invalid"),
        ("cycle_id", "other-cycle", "binding_conflict"),
        ("binding", {}, "request_invalid"),
        ("request_id", "", "request_invalid"),
        ("ownership_epoch", "", "request_invalid"),
        ("rationale", "", "request_invalid"),
        ("decision", "ack", "request_invalid"),
    ],
)
async def test_invalid_or_stale_request(simulation, field, value, error):
    release, request = await setup(simulation)
    request[field] = value
    with pytest.raises(LedgerError, match=error):
        await release.async_request(**request)
    assert not release.inspect()["events"] and release.permit is None


async def test_stale_real_plan(simulation):
    release, request = await setup(simulation)
    # A real state report changes evidence. Do not edit the plan or journals.
    simulation.hass.states.async_set("sensor.release_new_observation", "1")
    # Registering another actual request is unnecessary: changed runtime source
    # policy is enough to require a new plan.
    simulation.hass.config_entries.async_update_entry(
        simulation.entry,
        data={**simulation.entry.data, "release_observation": "changed"},
    )
    with pytest.raises(LedgerError, match="plan_stale"):
        await release.async_request(**request)


async def test_identical_retry_and_conflicting_content(simulation):
    release, request = await setup(simulation)
    result = await release.async_request(**request)
    assert await release.async_request(**request) == result
    assert len(release.inspect()["events"]) == 1
    with pytest.raises(LedgerError, match="request_conflict"):
        await release.async_request(**{**request, "rationale": "different"})
    release.transaction.revoke("explicit-revocation")
    assert await release.async_request(**request) == result
    assert release.permit is None


async def test_deny_is_a_durable_negative_decision(simulation):
    release, request = await setup(simulation)
    result = await release.async_request(**{**request, "decision": "deny"})
    assert result["reason"] == "operator_denied"
    assert release.store.records and release.permit is None


@pytest.mark.parametrize(
    "change", ["revoke", "sensor", "ownership", "target", "command", "none"]
)
async def test_changes_during_audit_and_concurrent_requests(simulation, change):
    release, request = await setup(simulation)
    entered, finish = asyncio.Event(), asyncio.Event()
    save = release.store.async_save

    async def barrier(data):
        entered.set()
        await finish.wait()
        await save(data)

    release.store.async_save = barrier
    pending = asyncio.create_task(release.async_request(**request))
    await entered.wait()
    try:
        with pytest.raises(LedgerError, match="transaction_busy"):
            await release.async_request(**{**request, "request_id": "competitor"})
        if change == "revoke":
            release.transaction.revoke("stop")
        elif change == "sensor":
            simulation.hass.states.async_set(
                simulation.entry.data["drain_sensor"], "on"
            )
        elif change == "ownership":
            simulation.pump.ownership = replace(
                simulation.pump.ownership, exclusive_control_verified=False
            )
        elif change == "target":
            simulation.hass.config_entries.async_update_entry(
                simulation.entry,
                data={**simulation.entry.data, "pump_switch": "switch.other"},
            )
        elif change == "command":
            await simulation.pump.async_stop()
    finally:
        finish.set()
    result = await pending
    assert not result["released"] and release.permit is None
    assert not simulation.scheduler.running


@pytest.mark.parametrize("failure", ["error", "timeout", "cancel"])
async def test_storage_uncertainty_never_overtaken(simulation, failure):
    release, request = await setup(simulation)
    entered, finish = asyncio.Event(), asyncio.Event()
    save = release.store.async_save

    async def barrier(data):
        entered.set()
        await finish.wait()
        if failure == "error":
            raise OSError("injected disk failure")
        await save(data)

    release.store.async_save = barrier
    pending = asyncio.create_task(
        release.async_request(**request, timeout=0.01 if failure == "timeout" else 5)
    )
    await entered.wait()
    if failure == "error":
        finish.set()
    if failure == "cancel":
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else LedgerError):
        await pending
    with pytest.raises(LedgerError, match="release_unavailable"):
        await release.async_request(**{**request, "request_id": "younger"})
    finish.set()
    await simulation.hass.async_block_till_done()
    assert release.permit is None
    assert not release.inspect()["events"]
    assert (
        release.transaction.inspect()["blocked_reason"] == "release_storage_uncertain"
    )


async def test_negative_history_validates_and_never_contains_permit(simulation):
    release, request = await setup(simulation)
    await release.async_request(**request)
    data = deepcopy(next(iter(release.store.records.values())))
    # Replay is a pure validator; never installs runtime ownership or capability.
    assert len(release._validate(data)) == 1
    assert "permit" not in data and release.permit is None
    data["events"][0]["reason"] = "released"
    with pytest.raises(LedgerError, match="release_corrupt"):
        release._validate(data)


@pytest.mark.parametrize("persist_decision", [False, True])
async def test_restart_before_and_after_decision(
    simulation, monkeypatch, restarted_hass, persist_decision
):
    release, request = await setup(simulation)
    if persist_decision:
        await release.async_request(**request)
    records = deepcopy(release.store.records)
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        ledger = await ledger_for(
            restored, deepcopy(release.transaction.ledger.store.records)
        )
        tx = _RecoveryTransactionCoordinator(ledger)
        tx.store = MemoryStore({}, "transaction")
        await tx.async_initialize()
        authority = _HarnessReleaseAuthority()
        fresh = _CycleReleaseCoordinator(tx, authority)
        fresh.store = MemoryStore(records, "release")
        await fresh.async_initialize()
        assert fresh.permit is None and fresh.inspect()["effective_state"] == "locked"
        assert len(fresh.inspect()["events"]) == int(persist_decision)
        with pytest.raises(LedgerError, match="actor_unauthenticated"):
            await fresh.async_request(**request)
        assert not restored.actor.commands and not restored.scheduler.running
        assert_recovery_locked(restored)
    finally:
        await restored.close()


async def test_revocation_at_planner_await_boundary(simulation, monkeypatch):
    release, request = await setup(simulation)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def observed(ledger):
        result = await async_create_reconciliation_plan(ledger)
        entered.set()
        await finish.wait()
        return result

    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.recovery_release.async_create_reconciliation_plan",
        observed,
    )
    pending = asyncio.create_task(release.async_request(**request))
    await entered.wait()
    release.transaction.revoke("ownership-lost-before-decision")
    finish.set()
    with pytest.raises(LedgerError, match="release_revoked"):
        await pending
    assert not release.store.records and release.permit is None
    assert_recovery_locked(simulation)
