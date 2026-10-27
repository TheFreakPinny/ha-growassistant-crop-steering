"""Read-only snapshots against the fixed FakePump and actual controller paths."""

import asyncio
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from custom_components.growassistant_crop_steering.pump import PumpError
from custom_components.growassistant_crop_steering.recovery_snapshot import (
    ReasonCode,
    TaskState,
    validate_cycle,
    validate_pump,
    validate_safety,
    validate_shots,
)

from .harness import NativeHarness, wait_for
from .test_simulation import interrupted_p1, start_p1

pytestmark = pytest.mark.asyncio


def inspect(harness):
    return (
        harness.pump.safety_snapshot(),
        harness.shots.safety_snapshot(),
        harness.cycle.safety_snapshot(),
    )


def result(harness, snapshot, validator):
    assessment = validator(
        snapshot,
        harness.pump.safety_snapshot().binding,
        now=harness.clock.at,
        max_age=30,
    )
    assert assessment.native_activation_allowed is False
    return {finding.code for finding in assessment.findings}


async def test_idle_empty_journal_is_not_off_proof(simulation):
    snapshot = simulation.pump.safety_snapshot()
    assert snapshot.state == "idle" and snapshot.record is None
    assert snapshot.off.state == "off"
    assert snapshot.off.verified_report_at is None
    assert {
        ReasonCode.OFF_MISSING,
        ReasonCode.PHYSICAL_OFF_UNKNOWN,
        ReasonCode.REVISION_UNKNOWN,
    } <= result(simulation, snapshot, validate_pump)
    assert await simulation.pump.async_stop()
    assert simulation.pump.safety_snapshot().off.verified_report_at is None
    assert simulation.actor.commands == []


async def test_fresh_software_off_and_snapshot_isolation(simulation):
    await simulation.pump.async_activate(300)
    simulation.move_to("2026-10-13T06:00:01+00:00")
    assert await simulation.pump.async_stop()
    await simulation.hass.async_block_till_done()
    before = simulation.checkpoint()
    commands = list(simulation.actor.commands)
    snapshots = inspect(simulation)
    pump, shots, cycle = snapshots
    assert (
        pump.off.verified_report_at
        == simulation.hass.states.get(pump.binding.entity_id).last_reported
    )
    assert pump.off.independent_confirmed is None
    assert ReasonCode.PHYSICAL_OFF_UNKNOWN in result(simulation, pump, validate_pump)
    assert simulation.checkpoint() == before
    assert simulation.actor.commands == commands
    assert inspect(simulation) == snapshots
    with pytest.raises(FrozenInstanceError):
        pump.state = "running"
    with pytest.raises(TypeError):
        shots.journal["operations"]["new"] = {}
    simulation.move_to("2026-10-13T06:01:00+00:00")
    assert ReasonCode.OFF_STALE in result(
        simulation, simulation.pump.safety_snapshot(), validate_pump
    )


@pytest.mark.parametrize("late_report", [False, True])
async def test_missing_or_late_off_not_verified(simulation, monkeypatch, late_report):
    await simulation.pump.async_activate(300)
    simulation.actor.confirmation_delay["turn_off"] = None
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0
    )
    assert not await simulation.pump.async_stop()
    if late_report:
        simulation.actor.report("off")
    snapshot = simulation.pump.safety_snapshot()
    assert snapshot.off.verified_report_at is None
    assert ReasonCode.OFF_MISSING in result(simulation, snapshot, validate_pump)
    # Restore only FakePump behavior and timeout for the fixture's safe cleanup.
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.15
    )


async def test_open_and_late_service_work_is_visible(simulation, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = simulation.actor.async_call

    async def held(domain, service, *args, **kwargs):
        if service == "turn_on":
            entered.set()
            await release.wait()
        return await original(domain, service, *args, **kwargs)

    monkeypatch.setattr(simulation.actor, "async_call", held)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0
    )
    activation = asyncio.create_task(simulation.pump.async_activate(300))
    try:
        await entered.wait()
        snapshot = simulation.pump.safety_snapshot()
        assert any(
            work.kind == "turn_on" and work.state == TaskState.PENDING
            for work in snapshot.work
        )
        assert ReasonCode.COMMAND_UNRESOLVED in result(
            simulation, snapshot, validate_pump
        )
        with pytest.raises(PumpError):
            await activation
        assert any(work.timed_out for work in simulation.pump.safety_snapshot().work)
    finally:
        monkeypatch.setattr(
            "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.15
        )
        release.set()
        await asyncio.gather(activation, return_exceptions=True)
    await wait_for(
        lambda: not simulation.pump._timed_out and not simulation.pump._lock.locked()
    )
    # No automatic review/production permission, including after reconciliation.
    snapshot = simulation.pump.safety_snapshot()
    assert ReasonCode.PHYSICAL_OFF_UNKNOWN in result(
        simulation, snapshot, validate_pump
    )
    assert sum(c.service == "turn_on" for c in simulation.actor.commands) == 1


async def test_pending_storage_and_interrupted_delivery(simulation, monkeypatch):
    await start_p1(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = simulation.shots.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(simulation.shots.store, "async_save", held)
    execution = asyncio.create_task(simulation.shots.async_execute("held-storage"))
    try:
        await entered.wait()
        snapshots = inspect(simulation)
        before = simulation.checkpoint()
        commands = list(simulation.actor.commands)
        assert any(
            w.kind == "storage" and w.state == TaskState.PENDING
            for w in snapshots[1].work
        )
        assert ReasonCode.STORAGE_UNRESOLVED in result(
            simulation, snapshots[1], validate_shots
        )
        inspect(simulation)
        assert (
            simulation.checkpoint() == before and simulation.actor.commands == commands
        )
        simulation.vwc(40, "unavailable", 40)
    finally:
        release.set()
        await execution
    # A separate real attempt tests conservative possible-partial delivery.


async def test_interrupted_shot_remains_review_required(simulation):
    await interrupted_p1(simulation)
    before = simulation.checkpoint()
    commands = list(simulation.actor.commands)
    pump, shots, cycle = inspect(simulation)
    assessment = validate_safety(
        pump, shots, cycle, pump.binding, now=simulation.clock.at, max_age=30
    )
    assert {ReasonCode.PARTIAL_DELIVERY, ReasonCode.RECOVERY_OPEN} <= {
        f.code for f in assessment.findings
    }
    assert not assessment.native_activation_allowed and not assessment.diagnostic_ready
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert simulation.entry.options["p1_shots_done"] == 0


async def test_cycle_snapshot_does_not_latch_permits_side_effect(
    simulation, monkeypatch
):
    await simulation.observe()
    simulation.configure(p1_active=True)
    before = simulation.checkpoint()
    reason = simulation.cycle.recovery_reason
    forbidden = AsyncMock(side_effect=AssertionError("No store IO in snapshot"))
    monkeypatch.setattr(simulation.cycle.store, "async_save", forbidden)
    snapshot = simulation.cycle.safety_snapshot()
    assert ReasonCode.CYCLE_INCONSISTENT in result(simulation, snapshot, validate_cycle)
    assert simulation.cycle.recovery_reason == reason
    assert simulation.checkpoint() == before
    forbidden.assert_not_awaited()


async def test_restored_snapshots_keep_missing_proofs(
    simulation, monkeypatch, restarted_hass
):
    await interrupted_p1(simulation)
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        before = restored.checkpoint()
        pump, shots, cycle = inspect(restored)
        assert pump.off.verified_report_at is None
        assert ReasonCode.RECOVERY_OPEN in result(restored, shots, validate_shots)
        assert ReasonCode.RECOVERY_OPEN in result(restored, cycle, validate_cycle)
        assert restored.checkpoint() == before
        assert not restored.actor.commands
    finally:
        await restored.close()


async def test_timed_out_pump_write_remains_visible(simulation, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = simulation.pump.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(simulation.pump.store, "async_save", held)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.STORAGE_TIMEOUT", 0
    )
    activation = asyncio.create_task(simulation.pump.async_activate(300))
    try:
        await entered.wait()
        with pytest.raises(PumpError):
            await activation
        snapshot = simulation.pump.safety_snapshot()
        assert ReasonCode.STORAGE_UNRESOLVED in result(
            simulation, snapshot, validate_pump
        )
        assert not any(
            command.service == "turn_on" for command in simulation.actor.commands
        )
    finally:
        monkeypatch.setattr(
            "custom_components.growassistant_crop_steering.pump.STORAGE_TIMEOUT", 5
        )
        release.set()
        await asyncio.gather(activation, return_exceptions=True)
    await wait_for(
        lambda: all(task.done() for task in simulation.pump._storage_pending)
    )
    assert ReasonCode.PUMP_UNRESOLVED in result(
        simulation, simulation.pump.safety_snapshot(), validate_pump
    )


async def test_cycle_pending_transition_write_is_visible(simulation, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = simulation.cycle.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(simulation.cycle.store, "async_save", held)
    transition = asyncio.create_task(simulation.observe())
    try:
        await entered.wait()
        before = simulation.checkpoint()
        snapshot = simulation.cycle.safety_snapshot()
        assert snapshot.record["pending"] is True
        assert {ReasonCode.STORAGE_UNRESOLVED, ReasonCode.CYCLE_INCONSISTENT} <= result(
            simulation, snapshot, validate_cycle
        )
        assert simulation.checkpoint() == before
    finally:
        release.set()
        await transition
