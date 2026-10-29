"""Real plans and participants only; no synthetic positive recovery findings."""

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_transaction import (
    _RecoveryTransactionCoordinator,
    plan_digest,
)

from .fake_actuator_gateway import FakeGateway
from .fake_gateway_transactions import AuditWitness, NativeGatewayTransactionDiagnostics
from .harness import MemoryStore
from .test_fake_actuator_gateway import Barrier
from .test_fake_gateway_recovery import (
    assert_never_release,
    integration,  # noqa: F401 -- shared real participant fixture
    intent_for,
    unchanged_state,
)
from .test_simulation import assert_no_replay_or_accounting, assert_recovery_locked


@pytest.fixture
async def coupling(integration):  # noqa: F811
    coordinator, _ = integration
    tx = _RecoveryTransactionCoordinator(coordinator.recovery.ledger)
    tx.store = MemoryStore({}, "protocol")
    await tx.async_initialize()
    store = MemoryStore({}, "audit")
    witness = AuditWitness()
    adapter = NativeGatewayTransactionDiagnostics(coordinator, tx, store, witness)
    await adapter.async_initialize()
    plan = await async_create_reconciliation_plan(tx.ledger)
    yield adapter, plan
    for unsub in tx._unsub:
        unsub()


def prepare(adapter, plan, **overrides):
    return adapter.async_prepare(
        **{
            "plan": plan,
            "request_id": "diagnostic-request",
            "expected_plan_id": plan.plan_id,
            "expected_plan_digest": plan_digest(plan),
            "expected_ledger_revision": adapter.transaction.ledger.revision,
            **overrides,
        }
    )


async def test_consistent_audit_never_prepares_or_releases(coupling, simulation):
    adapter, plan = coupling
    before = unchanged_state(simulation, adapter.integration)
    protocol = adapter.transaction.inspect()
    result = await prepare(adapter, plan)
    assert result.evidence_consistent and result.prepare_blocked
    assert not result.audit_persisted
    assert plan.findings or plan.open_checks or plan.status != "preparation_eligible"
    assert_never_release(result)
    context = result.context
    assert context.plan_id == plan.plan_id and context.plan_digest == plan_digest(plan)
    assert context.request_id == "diagnostic-request"
    assert context.binding == adapter.integration.binding.gateway.binding
    assert context.ledger_revision == adapter.transaction.ledger.revision
    with pytest.raises(FrozenInstanceError):
        context.gateway_boot = "other"
    committed = await adapter.async_commit(result)
    assert committed.context is context
    assert committed.evidence_consistent and committed.audit_persisted
    assert_never_release(committed)
    assert unchanged_state(simulation, adapter.integration) == before
    assert adapter.transaction.inspect() == protocol
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)


@pytest.mark.parametrize(
    "change", ["plan_id", "digest", "ledger_revision", "pump", "participant", "request"]
)
async def test_bad_binding(coupling, change):
    adapter, plan = coupling
    overrides = {}
    if change == "plan_id":
        overrides["expected_plan_id"] = "old-plan"
    elif change == "digest":
        overrides["expected_plan_digest"] = "stale"
    elif change == "ledger_revision":
        overrides["expected_ledger_revision"] = -1
    elif change == "pump":
        plan = replace(plan, binding={"entry_id": "other", "entity_id": "switch.other"})
    elif change == "participant":
        plan = replace(
            plan, source_revisions={**plan.source_revisions, "pump": "obsolete"}
        )
    else:
        overrides["request_id"] = ""
    if change == "request":
        with pytest.raises(ValueError, match="identity_missing"):
            await prepare(adapter, plan, **overrides)
        return
    result = await prepare(adapter, plan, **overrides)
    assert not result.evidence_consistent
    assert_never_release(result)
    with pytest.raises(ValueError, match="prepare_identity"):
        await adapter.async_commit(result)


async def mutate(adapter, simulation, change):
    gateway = adapter.integration.gateway
    if change == "stop":
        gateway.safety_off("test-race")
    elif change == "boot":
        FakeGateway(gateway.actuator, gateway.clock, gateway.store)
    elif change == "owner":
        boot = gateway.lease.ha_boot
        gateway.revoke()
        assert gateway.recheck()
        gateway.grant("new-owner", boot, 8)
    elif change == "journal":
        gateway.store.write("audit", lambda: None)
    elif change == "rollback":
        old = gateway.store.snapshot()
        gateway.accept(intent_for(gateway))
        gateway.store.restore(old)
    elif change == "uncertain":
        gateway.store.fail = "accept:delay"
        gateway.accept(intent_for(gateway))
    elif change == "open":
        gateway.accept(intent_for(gateway))
    elif change == "ha_restart":
        simulation.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await simulation.hass.async_block_till_done()
    elif change == "participant":
        simulation.pump._runtime.ownership_changed()
    elif change == "expiry":
        gateway.clock.advance(9)
    elif change == "protocol":
        adapter.transaction.revoke("concurrent_diagnostic_revocation")
    else:
        gateway.store.witness = None


@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
@pytest.mark.parametrize(
    "change",
    [
        "stop",
        "boot",
        "owner",
        "journal",
        "rollback",
        "uncertain",
        "open",
        "ha_restart",
        "participant",
        "expiry",
        "protocol",
        "witness",
    ],
)
async def test_deterministic_races(coupling, simulation, stage, change):
    adapter, plan = coupling
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        prepared = await prepare(adapter, plan)
        assert prepared.evidence_consistent
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    try:
        await mutate(adapter, simulation, change)
        before = unchanged_state(simulation, adapter.integration)
    finally:
        barrier.release.set()
    result = await task
    assert not result.evidence_consistent
    assert result.audit_persisted is (stage == "commit_saved")
    assert_never_release(result)
    assert unchanged_state(simulation, adapter.integration) == before


@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
async def test_crash_cancellation_discards_session(coupling, stage):
    adapter, plan = coupling
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    prepared = None
    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        prepared = await prepare(adapter, plan)
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Restart can inspect history only. An uncertain persisted write without a
    # witness update is rejected; no attempt ever reconstitutes a live prepare.
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    if stage == "commit_saved":
        with pytest.raises(ValueError, match="witness_conflict"):
            await fresh.async_initialize()
    else:
        await fresh.async_initialize()
        with pytest.raises(ValueError, match="prepare_identity"):
            await fresh.async_commit(prepared)


async def test_receipt_restart_and_copied_prepare_never_authorize(coupling):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    result = await adapter.async_commit(prepared)
    assert result.audit_persisted
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await fresh.async_initialize()
    with pytest.raises(ValueError, match="prepare_identity"):
        await fresh.async_commit(prepared)
    assert_never_release(result)
    with pytest.raises(ValueError, match="unavailable"):
        await adapter.async_commit(prepared)


async def test_copy_is_not_same_attempt(coupling):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    with pytest.raises(ValueError, match="prepare_identity"):
        await adapter.async_commit(replace(prepared))


@pytest.mark.parametrize("stage", ["prepare_captured", "commit_checked"])
async def test_concurrent_attempts_rejected(coupling, stage):
    adapter, plan = coupling
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        prepared = await prepare(adapter, plan)
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    try:
        with pytest.raises(ValueError, match="unavailable"):
            await prepare(adapter, plan)
    finally:
        barrier.release.set()
    assert (await task).evidence_consistent


@pytest.mark.parametrize("persist_first", [False, True])
async def test_storage_failure_is_uncertain(coupling, monkeypatch, persist_first):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    save = adapter.store.async_save

    async def fail(data):
        if persist_first:
            await save(data)
        raise OSError("injected-write-failure")

    monkeypatch.setattr(adapter.store, "async_save", fail)
    with pytest.raises(OSError):
        await adapter.async_commit(prepared)
    assert adapter.witness.expected is None
    with pytest.raises(ValueError, match="unavailable"):
        await adapter.async_commit(prepared)


async def test_delayed_ack_is_obsolete(coupling):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)

    async def boundary(stage):
        if stage == "commit_saved":
            adapter.integration.gateway.clock.advance(9)

    result = await adapter.async_commit(prepared, boundary=boundary)
    assert result.audit_persisted and not result.evidence_consistent
    assert_never_release(result)


async def test_audit_storage_rollback(coupling):
    adapter, plan = coupling
    result = await adapter.async_commit(await prepare(adapter, plan))
    assert result.audit_persisted
    adapter.store.records.clear()
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    with pytest.raises(ValueError, match="witness_conflict"):
        await fresh.async_initialize()


async def test_cross_adapter_supersedes_prepare(coupling):
    adapter, plan = coupling
    first = await prepare(adapter, plan)
    other = NativeGatewayTransactionDiagnostics(
        adapter.integration,
        adapter.transaction,
        MemoryStore({}, "other"),
        AuditWitness(),
    )
    await other.async_initialize()
    second = await prepare(other, plan)
    assert second.evidence_consistent
    assert not (await adapter.async_commit(first)).evidence_consistent


async def test_no_output_or_protocol_writes(coupling, simulation, monkeypatch):
    adapter, plan = coupling
    before = adapter.transaction.inspect()

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnosis attempted mutation")

    for owner, names in (
        (
            adapter.transaction,
            ("async_prepare", "async_commit", "async_record_revocation"),
        ),
        (
            adapter.integration.gateway,
            ("poll", "recheck", "safety_off", "grant", "accept", "execute"),
        ),
        (simulation.scheduler, ("async_start",)),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    result = await adapter.async_commit(await prepare(adapter, plan))
    assert result.evidence_consistent and result.audit_persisted
    assert adapter.transaction.inspect() == before
    assert_never_release(result)
    assert_recovery_locked(simulation)


@pytest.mark.parametrize("stage", ["prepare_captured", "commit_checked"])
async def test_other_wrapper_cannot_overlap(coupling, stage):
    adapter, plan = coupling
    other = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await other.async_initialize()
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        prepared = await prepare(adapter, plan)
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    try:
        with pytest.raises(ValueError, match="unavailable"):
            await prepare(other, plan)
    finally:
        barrier.release.set()
    assert (await task).evidence_consistent


async def test_protocol_store_rollback_blocks(coupling):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    await adapter.transaction.store.async_save({"unexpected": "old-protocol"})
    result = await adapter.async_commit(prepared)
    assert not result.evidence_consistent and not result.audit_persisted
    assert_never_release(result)


async def test_actual_ledger_change_blocks(simulation):
    from custom_components.growassistant_crop_steering.actuator_contract import (
        ActuatorIdentity,
    )

    from .fake_actuator_gateway import FakeClock, FakePersistence
    from .fake_gateway_recovery import (
        DiagnosticBinding,
        NativeGatewayRecoveryCoordinator,
    )
    from .test_recovery_ledger import acknowledgment
    from .test_recovery_participant_gates import setup

    recovery, rid = await setup(simulation, review=False)
    tx = None
    try:
        initial = await recovery.async_check()
        runtime = simulation.pump.safety_snapshot().runtime
        gateway = FakeGateway(
            ActuatorIdentity(initial.request.binding, "fake", "1", "test"),
            FakeClock(),
            FakePersistence(),
        )
        assert gateway.recheck()
        gateway.grant(runtime.controller.value, runtime.boot, 8)
        integration = NativeGatewayRecoveryCoordinator(
            gateway, recovery, DiagnosticBinding.capture(gateway, initial)
        )
        tx = _RecoveryTransactionCoordinator(recovery.ledger)
        tx.store = MemoryStore({}, "protocol")
        await tx.async_initialize()
        adapter = NativeGatewayTransactionDiagnostics(
            integration, tx, MemoryStore({}, "audit"), AuditWitness()
        )
        await adapter.async_initialize()
        plan = await async_create_reconciliation_plan(recovery.ledger)
        prepared = await prepare(adapter, plan)
        assert prepared.evidence_consistent
        await recovery.ledger.async_acknowledge(
            **acknowledgment(recovery.ledger, rid, simulation)
        )
        result = await adapter.async_commit(prepared)
        assert not result.evidence_consistent and not result.audit_persisted
        assert_never_release(result)
    finally:
        recovery.close()
        if tx:
            for unsub in tx._unsub:
                unsub()


async def test_late_write_after_cancel_is_never_adopted(coupling, monkeypatch):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    barrier = Barrier()
    save = adapter.store.async_save
    pending = []

    async def delayed(data):
        async def physical_write():
            await barrier.wait()
            await save(data)

        task = asyncio.create_task(physical_write())
        pending.append(task)
        await asyncio.shield(task)

    monkeypatch.setattr(adapter.store, "async_save", delayed)
    task = asyncio.create_task(adapter.async_commit(prepared))
    await barrier.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    barrier.release.set()
    await pending[0]
    assert adapter.witness.expected is None
    with pytest.raises(ValueError, match="unavailable"):
        await adapter.async_commit(prepared)
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    with pytest.raises(ValueError, match="witness_conflict"):
        await fresh.async_initialize()


async def test_persisted_receipt_after_real_ha_restart(
    coupling, simulation, restarted_hass, monkeypatch
):
    from copy import deepcopy

    from custom_components.growassistant_crop_steering.recovery_participant import (
        _RecoveryGateCoordinator,
    )

    from .fake_gateway_recovery import NativeGatewayRecoveryCoordinator
    from .harness import NativeHarness
    from .test_recovery_ledger import ledger_for

    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    result = await adapter.async_commit(prepared)
    assert result.audit_persisted
    checkpoint = simulation.checkpoint()
    records = deepcopy(adapter.transaction.ledger.store.records)
    adapter.integration.recovery.close()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    recovery = tx = None
    try:
        await restored.initialize(fresh=False)
        recovery = _RecoveryGateCoordinator(
            await ledger_for(restored, records), restored.scheduler
        )
        integration = NativeGatewayRecoveryCoordinator(
            adapter.integration.gateway, recovery, adapter.integration.binding
        )
        tx = _RecoveryTransactionCoordinator(recovery.ledger)
        tx.store = adapter.transaction.store
        await tx.async_initialize()
        fresh = NativeGatewayTransactionDiagnostics(
            integration, tx, adapter.store, adapter.witness
        )
        await fresh.async_initialize()
        with pytest.raises(ValueError, match="prepare_identity"):
            await fresh.async_commit(prepared)
        assert_never_release(result)
        assert_recovery_locked(restored)
        assert not restored.actor.commands and not restored.scheduler.running
    finally:
        if recovery:
            recovery.close()
        if tx:
            for unsub in tx._unsub:
                unsub()
        await restored.close()


@pytest.mark.parametrize(
    "damage", ["schema", "hash", "release_flag", "witness_missing"]
)
async def test_invalid_audit_history_is_fail_closed(coupling, damage):
    from copy import deepcopy

    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    data = await adapter.store.async_load()
    if damage == "schema":
        data["schema"] = 2
    elif damage == "hash":
        data["receipts"][0]["hash"] = "wrong"
    elif damage == "release_flag":
        data["receipts"][0]["recovery_release_allowed"] = True
    await adapter.store.async_save(data)
    # Even an agreeing model witness cannot legitimize malformed history.
    adapter.witness.expected = None if damage == "witness_missing" else deepcopy(data)
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    with pytest.raises(ValueError, match="audit_"):
        await fresh.async_initialize()
