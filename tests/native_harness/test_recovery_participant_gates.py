"""Actual participant diagnostics; never replace flags, findings or journals."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.growassistant_crop_steering.recovery_ledger import LedgerError
from custom_components.growassistant_crop_steering.recovery_participant import (
    AccountingGateEvidence,
    CycleGateEvidence,
    PumpGateEvidence,
    SchedulerGateEvidence,
    ShotGateEvidence,
    _RecoveryGateCoordinator,
    aggregate_evidence,
)
from custom_components.growassistant_crop_steering.shot_engine import ShotRejected

from .harness import NativeHarness
from .test_recovery_ledger import acknowledgment, case_for, ledger_for
from .test_simulation import assert_no_replay_or_accounting, assert_recovery_locked

pytestmark = pytest.mark.asyncio


async def setup(harness, *, review=True):
    ledger, rid = await case_for(harness)
    if review:
        await ledger.async_acknowledge(**acknowledgment(ledger, rid, harness))
    coordinator = _RecoveryGateCoordinator(ledger, harness.scheduler)
    return coordinator, rid


def proof(result, kind):
    return next(e for e in result.evidence if type(e) is kind)


async def test_successful_diagnostic_collection_is_not_readiness_or_release(simulation):
    coordinator, _ = await setup(simulation)
    before = simulation.checkpoint(), deepcopy(coordinator.ledger.store.records)
    commands = list(simulation.actor.commands)
    result = await coordinator.async_check()
    assert result.collection_complete
    assert not result.coordination_ready
    assert coordinator.confirmation_current(result)
    assert len(result.evidence) == 5
    assert (
        result.native_activation_allowed
        is coordinator.native_activation_allowed
        is False
    )
    assert "durable_participant_revisions_unproven" not in result.blockers
    assert all(
        r.receipt is not None
        for r in result.request.revisions
        if r.source in {"pump", "shots", "accounting", "cycle"}
    )
    assert "external_writers_unproven" in result.blockers
    assert proof(result, SchedulerGateEvidence).quiesce_observed
    assert any(c.passed for c in proof(result, SchedulerGateEvidence).checks)
    assert (simulation.checkpoint(), coordinator.ledger.store.records) == before
    assert simulation.actor.commands == commands
    assert not simulation.scheduler.running
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)
    coordinator.close()


async def test_history_review_blocker_and_confirmation_are_distinct(simulation):
    coordinator, rid = await setup(simulation, review=False)
    first = await coordinator.async_check()
    shots = proof(first, ShotGateEvidence)
    assert shots.historical_errors
    assert "acknowledgment_missing" in shots.blockers
    assert not shots.dispositions[0].checked
    await coordinator.ledger.async_acknowledge(
        **acknowledgment(coordinator.ledger, rid, simulation)
    )
    assert not coordinator.confirmation_current(first)
    second = await coordinator.async_check()
    reviewed = proof(second, ShotGateEvidence)
    assert reviewed.historical_errors == shots.historical_errors
    assert reviewed.dispositions[0].checked
    assert reviewed.dispositions[0].reviewed
    assert "acknowledgment_missing" not in reviewed.blockers
    assert "partial_delivery_unresolved" in reviewed.blockers
    assert coordinator.confirmation_current(second)
    assert not second.coordination_ready
    coordinator.close()


async def test_partial_delivery_consumed_identity_and_no_new_baseline(simulation):
    coordinator, _ = await setup(simulation)
    before = simulation.checkpoint()
    result = await coordinator.async_check()
    disposition = proof(result, ShotGateEvidence).dispositions[0]
    assert disposition.operation_id == "interrupted"
    assert disposition.historical_state == "interrupted"
    assert disposition.delivery not in {"not_requested", "software_completed"}
    assert disposition.volume is None and disposition.operation_consumed
    assert disposition.disposition == "retain_locked"
    cycle = proof(result, CycleGateEvidence)
    assert cycle.baseline_required and not cycle.baseline_established
    assert "cycle_disposition_unproven" in cycle.blockers
    with pytest.raises(ShotRejected, match="duplicate_operation"):
        await simulation.shots.async_execute("interrupted")
    assert simulation.checkpoint() == before
    assert_recovery_locked(simulation)
    coordinator.close()


@pytest.mark.parametrize(
    "kind",
    [
        PumpGateEvidence,
        ShotGateEvidence,
        AccountingGateEvidence,
        CycleGateEvidence,
        SchedulerGateEvidence,
    ],
)
async def test_missing_and_duplicate_confirmation_block(simulation, kind):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    missing = tuple(e for e in result.evidence if type(e) is not kind)
    aggregated = aggregate_evidence(result.request, missing)
    assert "participant_missing" in aggregated.blockers
    assert not aggregated.collection_complete and not aggregated.coordination_ready
    duplicate = aggregate_evidence(
        result.request, result.evidence + (proof(result, kind),)
    )
    assert "participant_conflict" in duplicate.blockers
    assert "partial_delivery_unresolved" in duplicate.blockers
    coordinator.close()


@pytest.mark.parametrize(
    "change", ["cycle", "target", "operations", "revision", "runtime", "generation"]
)
async def test_contradictory_or_stale_binding_never_accepted(simulation, change):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    request = result.request
    altered = {
        "cycle": replace(request, cycle_id="different"),
        "target": replace(
            request, binding=replace(request.binding, entity_id="switch.other")
        ),
        "operations": replace(request, operations=()),
        "revision": replace(request, revisions=()),
        "runtime": replace(request, runtime_epoch="previous-boot"),
        "generation": replace(request, safety_generation=request.safety_generation + 1),
    }[change]
    # Negative contract inputs only; no fabricated positive participant evidence.
    changed = replace(proof(result, ShotGateEvidence), request=altered)
    evidence = tuple(
        changed if type(e) is ShotGateEvidence else e for e in result.evidence
    )
    aggregate = aggregate_evidence(request, evidence)
    assert "confirmation_binding_conflict" in aggregate.blockers
    assert not aggregate.coordination_ready
    assert not coordinator.confirmation_current(replace(result, evidence=evidence))
    coordinator.close()


@pytest.mark.parametrize(
    "change",
    ["cycle", "operation_cycle", "operation_digest", "scope", "revision", "target"],
)
async def test_owner_rejects_invalid_domain_request(simulation, change):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    request = result.request
    scope = request.operations[0]
    if change == "cycle":
        request = replace(request, cycle_id="other")
        expected = "cycle_mismatch"
    elif change == "operation_cycle":
        request = replace(request, operations=(replace(scope, cycle_id="other"),))
        expected = "operation_cycle_conflict"
    elif change == "operation_digest":
        request = replace(
            request, operations=(replace(scope, evidence_digest="stale"),)
        )
        expected = "operation_evidence_changed"
    elif change == "scope":
        request = replace(request, operations=())
        expected = "operation_scope_conflict"
    elif change == "revision":
        request = replace(request, revisions=())
        expected = "revision_conflict"
    else:
        request = replace(
            request, binding=replace(request.binding, entity_id="switch.other")
        )
        expected = "binding_mismatch"
    shots, _ = simulation.shots.recovery_gate_evidence(request)
    assert expected in shots.blockers
    assert "partial_delivery_unresolved" in shots.blockers
    coordinator.close()


async def test_evidence_is_immutable_and_equal_copy_is_not_current(simulation):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    with pytest.raises(FrozenInstanceError):
        result.coordination_ready = True
    with pytest.raises(FrozenInstanceError):
        result.evidence[0].blockers = ()
    with pytest.raises(FrozenInstanceError):
        result.request.operations[0].reviewed = False
    assert not coordinator.confirmation_current(replace(result))
    coordinator.revoke()
    assert not coordinator.confirmation_current(result)
    coordinator.close()


@pytest.mark.parametrize(
    "change", ["sensor", "pump_aba", "ownership", "legacy", "cycle", "revocation"]
)
async def test_changes_during_await_reject_whole_collection(
    simulation, monkeypatch, change
):
    coordinator, _ = await setup(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.ledger.source._read

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(coordinator.ledger.source, "_read", held)
    task = asyncio.create_task(coordinator.async_check())
    await entered.wait()
    if change == "sensor":
        simulation.vwc(40, "unavailable", 40)
    elif change == "pump_aba":
        simulation.actor.report("on")
        simulation.actor.report("off")
    elif change == "ownership":
        simulation.pump.ownership = None
    elif change == "legacy":
        simulation.hass.config_entries.async_update_entry(
            simulation.entry,
            data={**simulation.entry.data, "p1_shots_done": "counter.legacy"},
        )
    elif change == "cycle":
        simulation.clock.move_to("2026-10-14T06:00:00+00:00")
    else:
        coordinator.revoke("new_safety_failure")
    await simulation.hass.async_block_till_done()
    release.set()
    result = await task
    assert not result.coordination_ready
    assert "evidence_changed" in result.blockers
    assert not coordinator.confirmation_current(result)
    assert_recovery_locked(simulation)
    coordinator.close()


@pytest.mark.parametrize("owner", ["pump", "shots", "cycle", "scheduler"])
async def test_lost_registration_blocks_confirmation(simulation, owner):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    # Use lifecycle APIs to lose ownership; never patch the owner registry.
    if owner == "pump":
        await simulation.pump.async_close()
    elif owner == "shots":
        await simulation.shots.async_close()
    elif owner == "cycle":
        await simulation.cycle.async_close()
    else:
        await simulation.scheduler.async_close()
    assert not coordinator.confirmation_current(result)
    result = await coordinator.async_check()
    assert not result.coordination_ready
    assert any(
        "ownership_lost" in p.blockers or "recovery_open" in p.blockers
        for p in result.evidence
    )
    coordinator.close()


async def test_concurrent_attempts_are_rejected_not_queued(simulation, monkeypatch):
    coordinator, _ = await setup(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.ledger.source._read

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(coordinator.ledger.source, "_read", held)
    task = asyncio.create_task(coordinator.async_check())
    await entered.wait()
    with pytest.raises(LedgerError, match="participant_check_busy"):
        await coordinator.async_check()
    with pytest.raises(LedgerError, match="writer_exists"):
        _RecoveryGateCoordinator(coordinator.ledger, simulation.scheduler)
    release.set()
    assert (await task).collection_complete
    coordinator.close()


async def test_storage_read_failure_preserves_history_and_all_gates(
    simulation, monkeypatch
):
    coordinator, _ = await setup(simulation)
    before = simulation.checkpoint(), deepcopy(coordinator.ledger.store.records)

    async def broken(*args):
        raise OSError("evidence medium unavailable")

    monkeypatch.setattr(coordinator.ledger.source, "_read", broken)
    result = await coordinator.async_check()
    assert not result.coordination_ready
    assert result.blockers
    assert (simulation.checkpoint(), coordinator.ledger.store.records) == before
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_cancel_during_collection_installs_no_confirmation(
    simulation, monkeypatch
):
    coordinator, _ = await setup(simulation)
    entered = asyncio.Event()

    async def held(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(coordinator.ledger.source, "_read", held)
    task = asyncio.create_task(coordinator.async_check())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator._last is None
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_restart_never_restores_participant_confirmation(
    simulation, monkeypatch, restarted_hass
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    checkpoint = simulation.checkpoint()
    ledger_records = deepcopy(coordinator.ledger.store.records)
    coordinator.close()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        ledger = await ledger_for(restored, ledger_records)
        fresh = _RecoveryGateCoordinator(ledger, restored.scheduler)
        assert fresh.runtime_epoch != coordinator.runtime_epoch
        assert not fresh.confirmation_current(result)
        new = await fresh.async_check()
        assert not new.coordination_ready
        assert not restored.actor.commands and not restored.scheduler.running
        assert_recovery_locked(restored)
        fresh.close()
    finally:
        await restored.close()


@pytest.mark.parametrize(
    "kind", ["pump_service", "pump_storage", "shot_storage", "scheduler_cycle_storage"]
)
async def test_real_pending_work_is_blocking(simulation, monkeypatch, kind):
    from .test_simulation import start_p1

    if kind != "scheduler_cycle_storage":
        await start_p1(simulation)
    else:
        await simulation.observe()
    ledger = await ledger_for(simulation)
    coordinator = _RecoveryGateCoordinator(ledger, simulation.scheduler)
    entered, release = asyncio.Event(), asyncio.Event()
    if kind == "pump_service":
        owner, method = simulation.actor, "async_call"
    else:
        component = {
            "pump_storage": simulation.pump,
            "shot_storage": simulation.shots,
            "scheduler_cycle_storage": simulation.cycle,
        }[kind]
        owner, method = component.store, "async_save"
    original = getattr(owner, method)

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method, held)
    if kind == "scheduler_cycle_storage":
        simulation.move_to("2026-10-13T07:00:00+00:00")
        await simulation.scheduler.async_start()
        task = None
    else:
        task = asyncio.create_task(simulation.shots.async_execute("pending"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        commands = list(simulation.actor.commands)
        before = simulation.checkpoint()
        result = await coordinator.async_check()
        assert not result.coordination_ready
        assert simulation.checkpoint() == before
        assert simulation.actor.commands == commands
        if kind == "pump_service":
            assert "command_unresolved" in proof(result, PumpGateEvidence).blockers
        elif kind == "pump_storage":
            assert "storage_unresolved" in proof(result, PumpGateEvidence).blockers
        elif kind == "shot_storage":
            assert "storage_unresolved" in proof(result, ShotGateEvidence).blockers
        else:
            assert (
                "scheduler_work_unresolved"
                in proof(result, SchedulerGateEvidence).blockers
            )
            assert "storage_unresolved" in proof(result, CycleGateEvidence).blockers
    finally:
        simulation.vwc(40, "unavailable", 40)
        release.set()
        if task is not None:
            await task
        await simulation.scheduler.async_stop()
        coordinator.close()


@pytest.mark.parametrize("kind", ["ownership", "legacy"])
async def test_actual_conflicts_are_visible(simulation, kind):
    coordinator, _ = await setup(simulation)
    if kind == "ownership":
        simulation.pump.ownership = None
    else:
        simulation.hass.config_entries.async_update_entry(
            simulation.entry,
            data={**simulation.entry.data, "p1_shots_done": "counter.legacy"},
        )
    await simulation.hass.async_block_till_done()
    result = await coordinator.async_check()
    assert (
        "ownership_conflict" if kind == "ownership" else "legacy_conflict"
    ) in result.blockers
    assert "partial_delivery_unresolved" in result.blockers
    assert not result.coordination_ready
    coordinator.close()


async def test_invalid_evidence_value_does_not_hide_other_participant_failure(
    simulation,
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    invalid = aggregate_evidence(result.request, result.evidence + (None,))
    assert "participant_invalid" in invalid.blockers
    assert "partial_delivery_unresolved" in invalid.blockers
    assert not invalid.collection_complete and not invalid.coordination_ready
    coordinator.close()


async def test_request_reconstruction_cannot_forge_checked_review(simulation):
    coordinator, _ = await setup(simulation, review=False)
    result = await coordinator.async_check()
    fake_scope = replace(result.request.operations[0], reviewed=True)
    fake = replace(result.request, operations=(fake_scope,))
    shots, _ = simulation.shots.recovery_gate_evidence(fake)
    assert "confirmation_not_current" in shots.blockers
    assert not shots.dispositions[0].checked and not shots.dispositions[0].reviewed
    coordinator.close()


async def test_confirmation_expires_without_runtime_event(simulation):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    simulation.clock.move_to("2026-10-13T07:00:31+00:00")
    assert not coordinator.confirmation_current(result)
    coordinator.close()


async def test_failed_participant_write_remains_blocking(simulation, monkeypatch):
    from .test_simulation import start_p1

    await start_p1(simulation)
    ledger = await ledger_for(simulation)
    coordinator = _RecoveryGateCoordinator(ledger, simulation.scheduler)

    async def broken(data):
        raise OSError("participant persistence failed")

    monkeypatch.setattr(simulation.shots.store, "async_save", broken)
    failed = await simulation.shots.async_execute("failed-write")
    assert not failed.accounted
    result = await coordinator.async_check()
    assert "storage_unresolved" in proof(result, ShotGateEvidence).blockers
    assert not result.coordination_ready
    assert not simulation.actor.commands
    assert simulation.shots.safety_snapshot().blocked
    coordinator.close()


async def test_late_participant_write_does_not_install_recovery_confirmation(
    simulation, monkeypatch
):
    from .test_simulation import start_p1

    await start_p1(simulation)
    ledger = await ledger_for(simulation)
    coordinator = _RecoveryGateCoordinator(ledger, simulation.scheduler)
    entered, release = asyncio.Event(), asyncio.Event()
    original = simulation.shots.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(simulation.shots.store, "async_save", held)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.shot_engine.JOURNAL_TIMEOUT",
        0.01,
    )
    task = asyncio.create_task(simulation.shots.async_execute("late-write"))
    try:
        await entered.wait()
        failed = await task
        assert not failed.accounted
        result = await coordinator.async_check()
        assert "storage_unresolved" in proof(result, ShotGateEvidence).blockers
        assert not result.coordination_ready
        release.set()
        await simulation.shots._storage_task
        assert not coordinator.confirmation_current(result)
        again = await coordinator.async_check()
        assert not again.coordination_ready
        assert simulation.shots.safety_snapshot().blocked
        assert not simulation.actor.commands
    finally:
        release.set()
        await task
        coordinator.close()


@pytest.mark.parametrize("kind", ["digest", "missing_identity", "operation_cycle"])
async def test_aggregation_validates_proof_contents(simulation, kind):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    shots = proof(result, ShotGateEvidence)
    if kind == "digest":
        altered = replace(shots, observed_digest="older-revision")
        expected = "revision_conflict"
    elif kind == "missing_identity":
        altered = replace(shots, participant_epoch="")
        expected = "confirmation_invalid"
    else:
        altered = replace(
            shots, dispositions=(replace(shots.dispositions[0], cycle_id="other"),)
        )
        expected = "operation_scope_conflict"
    aggregate = aggregate_evidence(
        result.request, tuple(altered if e is shots else e for e in result.evidence)
    )
    assert expected in aggregate.blockers
    assert "partial_delivery_unresolved" in aggregate.blockers
    assert not aggregate.coordination_ready
    coordinator.close()


async def test_new_real_fault_during_check_invalidates_previous_scope(
    simulation, monkeypatch
):
    from .test_simulation import running, start_p1

    await start_p1(simulation)
    ledger = await ledger_for(simulation)
    coordinator = _RecoveryGateCoordinator(ledger, simulation.scheduler)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ledger.source._read

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(ledger.source, "_read", held)
    check = asyncio.create_task(coordinator.async_check())
    await entered.wait()
    attempt = await running(simulation, "new-fault")
    simulation.vwc(40, "unavailable", 40)
    assert not (await attempt).accounted
    await simulation.hass.async_block_till_done()
    # Register the actual new case; registration uses the same reader barrier.
    release.set()
    await ledger.async_register(request_id="new-case", expected_revision=0)
    result = await check
    assert not result.coordination_ready
    assert not coordinator.confirmation_current(result)
    assert "evidence_changed" in result.blockers
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_scheduler_retry_latch_survives_diagnostic_check(simulation):
    from custom_components.growassistant_crop_steering.shot_engine import ShotState

    from .harness import wait_for
    from .test_simulation import start_p1

    await start_p1(simulation)
    await simulation.scheduler.async_start()
    await wait_for(lambda: simulation.shots.state == ShotState.RUNNING)
    simulation.vwc(40, "unavailable", 40)
    await wait_for(lambda: simulation.scheduler.retry_blocked)
    await simulation.scheduler.async_stop()
    ledger = await ledger_for(simulation)
    await ledger.async_register(request_id="scheduler-case", expected_revision=0)
    coordinator = _RecoveryGateCoordinator(ledger, simulation.scheduler)
    before = simulation.checkpoint()
    commands = list(simulation.actor.commands)
    result = await coordinator.async_check()
    assert proof(result, SchedulerGateEvidence).retry_blocked
    assert simulation.scheduler.retry_blocked
    assert not simulation.scheduler.running
    assert (simulation.checkpoint(), simulation.actor.commands) == (before, commands)
    assert not result.coordination_ready
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_final_sensor_change_is_seen_before_event_callback_runs(
    simulation, monkeypatch
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    # No yield: the generation listener has not run. Synchronous state capture
    # must already invalidate the old diagnostic confirmation.
    simulation.vwc(40, "unavailable", 41)
    assert not coordinator.confirmation_current(result)
    coordinator.close()
