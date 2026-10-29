"""Actual recovery owners plus isolated fake; positives certify diagnostics only."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
    CommandIntent,
    FencingToken,
)
from custom_components.growassistant_crop_steering.recovery_revision import BootEpoch

from .fake_actuator_gateway import FakeClock, FakeGateway, FakePersistence
from .fake_gateway_recovery import (
    RETAINED_BLOCKERS,
    DiagnosticBinding,
    NativeGatewayRecoveryCoordinator,
)
from .harness import NativeHarness
from .test_fake_actuator_gateway import Barrier
from .test_recovery_ledger import acknowledgment, ledger_for
from .test_recovery_participant_gates import setup
from .test_simulation import assert_no_replay_or_accounting, assert_recovery_locked


@pytest.fixture
async def integration(simulation):
    recovery, rid = await setup(simulation)
    initial = await recovery.async_check()
    pump = simulation.pump.safety_snapshot()
    gateway = FakeGateway(
        ActuatorIdentity(initial.request.binding, "fake-pump", "1", "test-only"),
        FakeClock(),
        FakePersistence(),
    )
    assert gateway.recheck()
    gateway.grant(pump.runtime.controller.value, pump.runtime.boot, 8)
    binding = DiagnosticBinding.capture(gateway, initial)
    coordinator = NativeGatewayRecoveryCoordinator(gateway, recovery, binding)
    yield coordinator, rid
    recovery.close()


def intent_for(gateway):
    lease = gateway.lease
    return CommandIntent(
        gateway.actuator,
        "pending",
        "on",
        FencingToken(lease.lease_id, lease.epoch, lease.owner_id),
        lease.ha_boot,
        1,
        7,
    )


def assert_never_release(result):
    assert result.native_activation_allowed is False
    assert result.coordination_ready is False
    assert result.hardware_exclusivity_proven is False
    assert result.recovery_release_allowed is False
    assert result.cycle_release_allowed is False
    assert result.release_permit is None
    assert set(RETAINED_BLOCKERS) <= set(result.blockers)


def unchanged_state(simulation, coordinator):
    gateway = coordinator.gateway
    return (
        simulation.checkpoint(),
        deepcopy(coordinator.recovery.ledger.store.records),
        tuple(simulation.actor.commands),
        simulation.scheduler.running,
        simulation.scheduler.retry_blocked,
        gateway.store.snapshot(),
        deepcopy(gateway.store.witness),
        gateway.events,
        gateway.lease,
        gateway.stopped,
        gateway.fault,
        gateway.store.stop_serial,
        tuple(gateway.off_receipts),
        gateway._last_tick,
        gateway._heartbeat,
    )


async def test_consistent_is_read_only_and_never_release(
    simulation, integration, monkeypatch
):
    coordinator, _ = integration
    gateway = coordinator.gateway
    before = unchanged_state(simulation, coordinator)

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic attempted mutation or output")

    for owner, names in (
        (gateway, ("poll", "recheck", "grant", "safety_off", "accept", "execute")),
        (gateway.store, ("check", "lookup", "write", "poison", "stage", "deliver")),
        (simulation.scheduler, ("async_start",)),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    result = await coordinator.async_check()
    assert result.gateway_evidence_complete
    assert result.gateway_internally_consistent
    assert result.recovery_participants_complete
    assert result.evidence_conflict_free
    assert coordinator.diagnostic_current(result)
    assert_never_release(result)
    assert unchanged_state(simulation, coordinator) == before
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)


async def test_snapshot_is_deeply_immutable_and_detached(integration):
    coordinator, _ = integration
    gateway = coordinator.gateway
    snapshot = gateway.evidence_snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.stop_serial = 0
    with pytest.raises(FrozenInstanceError):
        snapshot.lease.owner_id = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.last_safety_off.reason = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.actuator.hardware_id = "changed"
    gateway.accept(intent_for(gateway))
    assert snapshot.records == snapshot.open_commands == ()
    assert gateway.evidence_snapshot().open_commands == ("pending",)


@pytest.mark.parametrize(
    "field", ["binding", "hardware_id", "channel", "enforcement_domain"]
)
async def test_identity_requires_more_than_ha_entity(integration, field):
    coordinator, _ = integration
    actuator = coordinator.gateway.actuator
    value = (
        replace(actuator.binding, entity_id="switch.other")
        if field == "binding"
        else "other"
    )
    coordinator.gateway.actuator = replace(actuator, **{field: value})
    result = await coordinator.async_check()
    assert not result.evidence_conflict_free
    assert "gateway_binding_changed" in result.blockers
    assert_never_release(result)


@pytest.mark.parametrize(
    "change",
    [
        "lease_expired",
        "ownership",
        "gateway_restart",
        "safety_off",
        "witness_lost",
        "rollback",
        "journal_change",
        "ha_restart",
        "resource",
        "clock_backwards",
        "clock_nan",
        "watchdog_expired",
        "uncertain_commit",
        "open_command",
    ],
)
@pytest.mark.parametrize(
    "stage", ["gateway_captured", "recovery_captured", "before_publish"]
)
async def test_races_are_blocking(integration, simulation, change, stage):
    coordinator, _ = integration
    gateway = coordinator.gateway
    old = gateway.store.snapshot()
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    task = asyncio.create_task(coordinator.async_check(boundary=boundary))
    await barrier.entered.wait()
    try:
        if change == "lease_expired":
            gateway.clock.advance(8)
        elif change == "ownership":
            owner, boot = gateway.lease.owner_id, gateway.lease.ha_boot
            gateway.revoke()
            assert gateway.recheck()
            gateway.grant(owner, boot, 8)
        elif change == "gateway_restart":
            FakeGateway(gateway.actuator, gateway.clock, gateway.store)
        elif change == "safety_off":
            gateway.safety_off("concurrent_stop")
        elif change == "witness_lost":
            gateway.store.witness = None
        elif change == "rollback":
            gateway.accept(intent_for(gateway))
            gateway.store.restore(old)
        elif change == "journal_change":
            gateway.store.write("audit", lambda: None)
        elif change == "ha_restart":
            simulation.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await simulation.hass.async_block_till_done()
        elif change == "resource":
            simulation.hass.config_entries.async_update_entry(
                simulation.entry,
                data={**simulation.entry.data, "pump_switch": "switch.other"},
            )
            await simulation.hass.async_block_till_done()
        elif change == "clock_backwards":
            gateway.clock.tick = -1
        elif change == "clock_nan":
            gateway.clock.tick = float("nan")
        elif change == "watchdog_expired":
            gateway.clock.advance(10)
        elif change == "uncertain_commit":
            gateway.store.fail = "accept:delay"
            gateway.accept(intent_for(gateway))
        else:
            gateway.accept(intent_for(gateway))
        # Mutations above are injected events; the diagnostic itself adds none.
        before = unchanged_state(simulation, coordinator)
    finally:
        barrier.release.set()
    result = await task
    assert not result.evidence_conflict_free
    assert not coordinator.diagnostic_current(result)
    assert unchanged_state(simulation, coordinator) == before
    assert_never_release(result)


async def test_safety_off_invalidates_prior_diagnostic(integration):
    coordinator, _ = integration
    result = await coordinator.async_check()
    assert coordinator.diagnostic_current(result)
    coordinator.gateway.safety_off("between_checks")
    assert not coordinator.diagnostic_current(result)
    assert not (await coordinator.async_check()).evidence_conflict_free
    assert_never_release(result)


async def test_old_ha_boot_lease_blocks(integration):
    coordinator, _ = integration
    gateway = coordinator.gateway
    gateway.revoke()
    assert gateway.recheck()
    gateway.grant("old-controller", BootEpoch("previous-ha"), 8)
    initial = await coordinator.recovery.async_check()
    coordinator.binding = DiagnosticBinding.capture(gateway, initial)
    result = await coordinator.async_check()
    assert "joint_owner_or_boot_conflict" in result.blockers
    assert not result.evidence_conflict_free
    assert_never_release(result)


async def test_uncertain_on_and_late_ack_never_certify_history(integration):
    coordinator, _ = integration
    gateway = coordinator.gateway
    intent = intent_for(gateway)
    gateway.accept(intent)
    gateway.store.fail = "finish:delay"
    assert (await gateway.execute(intent)).status == "uncertain"
    before = gateway.events
    result = await coordinator.async_check()
    assert "gateway_witness_untrusted" in result.blockers
    assert "gateway_commands_uncertain" in result.blockers
    assert not result.gateway_internally_consistent
    gateway.store.acknowledge()
    again = await coordinator.async_check()
    assert not again.evidence_conflict_free
    assert gateway.events == before
    assert_never_release(result)
    assert_never_release(again)


@pytest.mark.parametrize("stage", ["before_apply", "after_apply"])
async def test_pending_execution_is_not_completed_by_diagnosis(integration, stage):
    coordinator, _ = integration
    gateway = coordinator.gateway
    intent = intent_for(gateway)
    gateway.accept(intent)
    barrier = Barrier()
    task = asyncio.create_task(gateway.execute(intent, **{stage: barrier.wait}))
    await barrier.entered.wait()
    before = gateway.events, gateway.store.snapshot()
    try:
        result = await coordinator.async_check()
        assert result.gateway.open_commands == ("pending",)
        assert "gateway_commands_open" in result.blockers
        assert not result.evidence_conflict_free
        assert (gateway.events, gateway.store.snapshot()) == before
        assert_never_release(result)
    finally:
        gateway.safety_off("test_cleanup")
        barrier.release.set()
        await task


async def test_multiple_requests_supersede_diagnostic_identity(integration):
    coordinator, _ = integration
    first = await coordinator.async_check()
    second = await coordinator.async_check()
    assert first.evidence_conflict_free and second.evidence_conflict_free
    assert not coordinator.diagnostic_current(first)
    assert not coordinator.diagnostic_current(replace(second))
    assert coordinator.diagnostic_current(second)
    assert_never_release(first)
    assert_never_release(second)


async def test_overlapping_requests_rejected_without_queue(integration):
    coordinator, _ = integration
    barrier = Barrier()

    async def boundary(stage):
        if stage == "gateway_captured":
            await barrier.wait()

    task = asyncio.create_task(coordinator.async_check(boundary=boundary))
    await barrier.entered.wait()
    try:
        with pytest.raises(ValueError, match="integration_check_busy"):
            await coordinator.async_check()
    finally:
        barrier.release.set()
    assert (await task).evidence_conflict_free


async def test_cancellation_cannot_publish_cached_confirmation(integration):
    coordinator, _ = integration
    first = await coordinator.async_check()
    barrier = Barrier()

    async def boundary(stage):
        await barrier.wait()

    task = asyncio.create_task(coordinator.async_check(boundary=boundary))
    await barrier.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.diagnostic_current(first)
    assert (await coordinator.async_check()).evidence_conflict_free


@pytest.mark.parametrize("change", ["operation", "revision", "ha_generation"])
async def test_stale_recovery_reference_blocks(integration, simulation, change):
    coordinator, _ = integration
    if change == "operation":
        # Negative input only: alter the expected reference, never owner flags.
        scope = list(coordinator.binding.recovery_scope)
        op = scope[2][0]
        scope[2] = (replace(op, evidence_digest="old-history"),)
        coordinator.binding = replace(coordinator.binding, recovery_scope=tuple(scope))
    elif change == "revision":
        scope = list(coordinator.binding.recovery_scope)
        scope[3] = tuple(replace(r, digest="stale") for r in scope[3])
        coordinator.binding = replace(coordinator.binding, recovery_scope=tuple(scope))
    else:
        simulation.pump._runtime.ownership_changed()
    result = await coordinator.async_check()
    assert not result.evidence_conflict_free
    assert "recovery_binding_changed" in result.blockers
    assert_never_release(result)


async def test_review_changes_history_reference_without_resolving_shot(simulation):
    recovery, rid = await setup(simulation, review=False)
    try:
        first = await recovery.async_check()
        runtime = simulation.pump.safety_snapshot().runtime
        gateway = FakeGateway(
            ActuatorIdentity(first.request.binding, "fake", "1", "test"),
            FakeClock(),
            FakePersistence(),
        )
        assert gateway.recheck()
        gateway.grant(runtime.controller.value, runtime.boot, 8)
        coordinator = NativeGatewayRecoveryCoordinator(
            gateway, recovery, DiagnosticBinding.capture(gateway, first)
        )
        await recovery.ledger.async_acknowledge(
            **acknowledgment(recovery.ledger, rid, simulation)
        )
        result = await coordinator.async_check()
        assert "recovery_binding_changed" in result.blockers
        assert not result.evidence_conflict_free
        assert_never_release(result)
        assert_recovery_locked(simulation)
    finally:
        recovery.close()


async def test_real_ha_restart_discards_diagnostics(
    integration, simulation, restarted_hass, monkeypatch
):
    from custom_components.growassistant_crop_steering.recovery_participant import (
        _RecoveryGateCoordinator,
    )

    coordinator, _ = integration
    old = await coordinator.async_check()
    checkpoint = simulation.checkpoint()
    records = deepcopy(coordinator.recovery.ledger.store.records)
    coordinator.recovery.close()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    recovery = None
    try:
        await restored.initialize(fresh=False)
        recovery = _RecoveryGateCoordinator(
            await ledger_for(restored, records), restored.scheduler
        )
        fresh = NativeGatewayRecoveryCoordinator(
            coordinator.gateway, recovery, coordinator.binding
        )
        assert not fresh.diagnostic_current(old)
        result = await fresh.async_check()
        assert not result.evidence_conflict_free
        assert "joint_owner_or_boot_conflict" in result.blockers
        assert_never_release(result)
        assert not restored.actor.commands and not restored.scheduler.running
        assert_recovery_locked(restored)
    finally:
        if recovery:
            recovery.close()
        await restored.close()


async def test_malformed_journal_is_incomplete_and_does_not_poison(integration):
    coordinator, _ = integration
    coordinator.gateway.store.records = {"corrupt": None}
    witness = deepcopy(coordinator.gateway.store.witness)
    result = await coordinator.async_check()
    assert not result.gateway_evidence_complete
    assert not result.evidence_conflict_free
    assert coordinator.gateway.store.witness == witness
    assert_never_release(result)


@pytest.mark.parametrize(
    "change", ["missing", "revision", "boot", "operation", "cycle"]
)
async def test_returned_participant_contradictions_are_rechecked(
    integration, monkeypatch, change
):
    coordinator, _ = integration
    result = await coordinator.recovery.async_check()
    proof = result.evidence[1]
    if change == "missing":
        changed = replace(result, evidence=result.evidence[:-1])
    else:
        if change == "revision":
            proof = replace(proof, revision=replace(proof.revision, digest="obsolete"))
        elif change == "boot":
            proof = replace(
                proof, runtime=replace(proof.runtime, boot=BootEpoch("old"))
            )
        elif change == "operation":
            proof = replace(proof, dispositions=())
        else:
            proof = replace(proof, request=replace(proof.request, cycle_id="other"))
        changed = replace(
            result, evidence=(result.evidence[0], proof, *result.evidence[2:])
        )

    async def stale():
        return changed

    monkeypatch.setattr(coordinator.recovery, "async_check", stale)
    diagnostic = await coordinator.async_check()
    assert not diagnostic.evidence_conflict_free
    assert not coordinator.diagnostic_current(diagnostic)
    if change == "missing":
        assert not diagnostic.recovery_participants_complete
    assert_never_release(diagnostic)


async def test_completed_command_has_no_cycle_or_operation_provenance(integration):
    coordinator, _ = integration
    gateway = coordinator.gateway
    intent = replace(intent_for(gateway), action="off")
    gateway.accept(intent)
    assert (await gateway.execute(intent)).status == "executed"
    reference = await coordinator.recovery.async_check()
    coordinator.binding = DiagnosticBinding.capture(gateway, reference)
    result = await coordinator.async_check()
    assert result.gateway_internally_consistent
    assert "gateway_operation_provenance_unproven" in result.blockers
    assert not result.evidence_conflict_free
    assert_never_release(result)


async def test_lost_witness_reader_never_sets_fault_or_calls_off(integration):
    coordinator, _ = integration
    gateway = coordinator.gateway
    gateway.store.witness = None
    before = gateway.events, gateway.fault, gateway.store.snapshot(), gateway.stopped
    snapshot = gateway.evidence_snapshot()
    assert "gateway_witness_untrusted" in snapshot.blockers
    assert (
        gateway.events,
        gateway.fault,
        gateway.store.snapshot(),
        gateway.stopped,
    ) == before


async def test_time_basis_replacement_blocks_even_with_equal_tick(integration):
    coordinator, _ = integration
    old = await coordinator.async_check()
    coordinator.gateway.clock = FakeClock()
    assert not coordinator.diagnostic_current(old)
    result = await coordinator.async_check()
    assert "gateway_binding_changed" in result.blockers
    assert not result.evidence_conflict_free
    assert_never_release(result)


async def test_actual_ledger_revision_change_during_check_blocks(simulation):
    recovery, rid = await setup(simulation, review=False)
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
        coordinator = NativeGatewayRecoveryCoordinator(
            gateway, recovery, DiagnosticBinding.capture(gateway, initial)
        )

        async def boundary(stage):
            if stage == "recovery_captured":
                await recovery.ledger.async_acknowledge(
                    **acknowledgment(recovery.ledger, rid, simulation)
                )

        result = await coordinator.async_check(boundary=boundary)
        assert not result.evidence_conflict_free
        assert "recovery_evidence_not_current" in result.blockers
        assert_never_release(result)
        assert_recovery_locked(simulation)
    finally:
        recovery.close()
