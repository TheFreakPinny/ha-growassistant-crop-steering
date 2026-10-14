"""Run native policies through fixed fake hardware, never HA switching services."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import ServiceRegistry

from custom_components.growassistant_crop_steering.pump import (
    HARD_MAX_RUNTIME,
    PumpError,
    PumpState,
)
from custom_components.growassistant_crop_steering.shot_engine import (
    ShotRejected,
    ShotState,
)

from .harness import PUMP, SENSORS, NativeHarness, wait_for

pytestmark = pytest.mark.asyncio


async def start_p1(simulation):
    assert (await simulation.observe()).phase == "p0_transpiration"
    simulation.move_to("2026-10-13T07:00:00+00:00")
    decision = await simulation.observe()
    assert simulation.entry.options["p1_active"]
    assert decision.eligible


async def running(simulation, operation="shot"):
    task = asyncio.create_task(simulation.shots.async_execute(operation))
    await wait_for(lambda: simulation.shots.state == ShotState.RUNNING)
    return task


async def no_followup(simulation):
    count = len(simulation.actor.commands)
    with pytest.raises(ShotRejected):
        await simulation.shots.async_execute("forbidden_followup")
    assert len(simulation.actor.commands) == count


async def test_composition_has_no_real_dispatch_or_disk_io(simulation):
    assert simulation.actor.commands == []
    assert not simulation.scheduler.running
    await start_p1(simulation)
    result = await simulation.shots.async_execute("completed")
    assert result.accounted
    assert [cmd.service for cmd in simulation.actor.commands] == ["turn_on", "turn_off"]
    assert simulation.records["pump"] is None
    assert simulation.records["shots"]["operations"]["completed"]["shutdown_confirmed"]
    assert not simulation.shots.recovery_assessment.native_activation_allowed


@pytest.mark.parametrize(
    "values,starts",
    [
        ((49, 50, 51), True),
        ((50, 50, 50), True),
        ((50.1, 50.1, 50.1), False),
    ],
)
async def test_three_sensor_start_threshold(simulation, values, starts):
    await simulation.observe()
    simulation.vwc(*values)
    simulation.move_to("2026-10-13T07:00:00+00:00")
    await simulation.observe()
    assert simulation.shots.sensor_assessment().average == pytest.approx(
        sum(values) / 3
    )
    assert simulation.entry.options["p1_active"] is starts
    assert simulation.actor.commands == []
    if not starts:
        await no_followup(simulation)


async def test_p1_start_threshold_is_not_a_continuation_threshold(simulation):
    await start_p1(simulation)
    assert (await simulation.shots.async_execute("first")).accounted
    simulation.move_to("2026-10-13T07:01:00+00:00")
    simulation.vwc(55, 55, 55)
    assert (await simulation.observe()).eligible
    assert (await simulation.shots.async_execute("above_start")).accounted
    assert simulation.entry.options["p1_shots_done"] == 2


async def test_capacity_before_shot_blocks_p1(simulation):
    await start_p1(simulation)
    simulation.vwc(60, 60, 60)
    await no_followup(simulation)
    await simulation.observe()
    assert simulation.entry.options["p1_done"]
    assert simulation.entry.options["p2_ref_vwc"] == 60
    assert simulation.entry.options["p1_shots_done"] == 0


async def test_capacity_during_approved_shot_finishes_and_counts_after_off(simulation):
    await start_p1(simulation)
    task = await running(simulation)
    await simulation.ramp_vwc(60, 60, 60)
    await simulation.observe()
    assert not simulation.entry.options["p1_done"]
    assert simulation.entry.options["p1_shots_done"] == 0
    assert simulation.hass.states.get(PUMP).state == "on"
    result = await task
    assert result.state == ShotState.COMPLETED and result.accounted
    commands = simulation.actor.commands
    assert commands[1].monotonic - commands[0].monotonic >= 0.9
    assert simulation.hass.states.get(PUMP).state == "off"
    assert simulation.entry.options["p1_shots_done"] == 1
    await simulation.observe()
    assert simulation.entry.options["p1_done"]
    assert simulation.entry.options["p2_ref_vwc"] == 60
    await no_followup(simulation)


@pytest.mark.parametrize("fault", ["sensor", "tray", "ownership"])
async def test_capacity_never_masks_independent_active_safety_fault(simulation, fault):
    await start_p1(simulation)
    task = await running(simulation)
    await simulation.ramp_vwc(60, 60, 60)
    if fault == "sensor":
        simulation.vwc(60, "unavailable", 60)
    elif fault == "tray":
        simulation.hass.states.async_set("binary_sensor.native_harness_tray", "on")
    else:
        simulation.shots.ownership = None
    result = await task
    assert result.state == ShotState.INTERRUPTED and not result.accounted
    assert simulation.entry.options["p1_shots_done"] == 0
    assert simulation.hass.states.get(PUMP).state == "off"
    await no_followup(simulation)


async def test_soak_counter_and_limit(simulation):
    simulation.configure(p1_shots=2)
    await start_p1(simulation)
    assert (await simulation.shots.async_execute("first")).accounted
    await no_followup(simulation)
    simulation.move_to("2026-10-13T07:00:59+00:00")
    assert "soak_active" in (await simulation.observe()).blocking_reasons
    await no_followup(simulation)
    simulation.move_to("2026-10-13T07:01:00+00:00")
    assert (await simulation.shots.async_execute("second")).accounted
    assert simulation.entry.options["p1_shots_done"] == 2
    await simulation.observe()
    assert simulation.entry.options["p1_done"]
    simulation.move_to("2026-10-13T07:02:00+00:00")
    # P2 has its independent drop gate; the exhausted P1 budget cannot replay.
    await no_followup(simulation)
    assert sum(cmd.service == "turn_on" for cmd in simulation.actor.commands) == 2


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("value", [None, "unavailable", "unknown", "nan", 80])
async def test_each_sensor_fault_blocks_before_start(simulation, index, value):
    await start_p1(simulation)
    values = [40, 40, 40]
    values[index] = value
    simulation.vwc(*values)
    assert simulation.shots.sensor_assessment().reasons
    await no_followup(simulation)
    assert simulation.actor.commands == []


@pytest.mark.parametrize("index", range(3))
async def test_silent_communication_loss_stales_each_sensor(simulation, index):
    await start_p1(simulation)
    simulation.move_to("2026-10-13T07:05:01+00:00", refresh=False)
    for peer in range(3):
        if peer != index:
            simulation.hass.states.async_set(
                SENSORS[peer],
                "40",
                force_update=True,
                timestamp=simulation.clock.at.timestamp(),
            )
    assert simulation.shots.sensor_assessment().rejected == {SENSORS[index]: "stale"}
    await no_followup(simulation)


@pytest.mark.parametrize("failure", ["communication", "missing_on", "late_on"])
async def test_start_failure_retains_uncertainty_and_blocks_retries(
    simulation, failure
):
    await start_p1(simulation)
    if failure == "communication":
        simulation.actor.fail_on = True
    elif failure == "missing_on":
        simulation.actor.confirmation_delay["turn_on"] = None
    else:
        simulation.actor.service_delay["turn_on"] = 0.3
    result = await simulation.shots.async_execute("failed_start")
    assert not result.accounted
    await wait_for(lambda: not simulation.pump._pending)
    await simulation.pump.async_stop()
    assert simulation.hass.states.get(PUMP).state == "off"
    assert simulation.entry.options["p1_shots_done"] == 0
    assert simulation.records["shots"]["operations"]["failed_start"][
        "recovery_required"
    ]
    await no_followup(simulation)


async def test_delayed_off_ack_is_required_before_counting(simulation):
    await start_p1(simulation)
    simulation.actor.confirmation_delay["turn_off"] = 0.08
    task = await running(simulation)
    await wait_for(
        lambda: any(cmd.service == "turn_off" for cmd in simulation.actor.commands)
    )
    assert simulation.entry.options["p1_shots_done"] == 0
    assert simulation.hass.states.get(PUMP).state == "on"
    result = await task
    assert result.accounted
    assert simulation.entry.options["p1_shots_done"] == 1
    assert simulation.records["pump"] is None


@pytest.mark.parametrize("delay", [None, 0.5])
async def test_missing_or_too_late_off_ack_never_counts(simulation, delay):
    await start_p1(simulation)
    simulation.actor.confirmation_delay["turn_off"] = delay
    result = await simulation.shots.async_execute("unconfirmed")
    assert result.state == ShotState.FAULT and not result.accounted
    assert simulation.entry.options["p1_shots_done"] == 0
    assert simulation.records["pump"]["entity_id"] == PUMP
    assert (
        simulation.records["shots"]["operations"]["unconfirmed"]["outcome"]
        == "unconfirmed_shutdown"
    )
    simulation.actor.report("off")  # A later off cannot promote a failed shot.
    await no_followup(simulation)


async def test_hard_runtime_limit_rejects_before_dispatch(simulation):
    with pytest.raises(PumpError, match="Runtime"):
        await simulation.pump.async_activate(HARD_MAX_RUNTIME + 1)
    assert simulation.actor.commands == []
    assert simulation.pump.state == PumpState.IDLE


async def test_monotonic_watchdog_stops_without_a_caller_stop(simulation):
    await simulation.pump.async_activate(0.05)
    await wait_for(lambda: simulation.pump.state == PumpState.IDLE)
    assert [cmd.service for cmd in simulation.actor.commands] == ["turn_on", "turn_off"]
    assert (
        simulation.actor.commands[1].monotonic - simulation.actor.commands[0].monotonic
        >= 0.045
    )
    assert simulation.records["pump"] is None
    assert simulation.entry.options["p1_shots_done"] == 0


async def test_watchdog_overrun_during_start_is_not_a_counted_shot(
    simulation, monkeypatch
):
    await start_p1(simulation)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 2
    )
    # Service timeout longer than the 1s shot: the shot's own deadline wins.
    simulation.actor.service_delay["turn_on"] = 1.1
    result = await simulation.shots.async_execute("overrun")
    assert not result.accounted
    await wait_for(lambda: not simulation.pump._pending)
    await simulation.pump.async_stop()
    assert simulation.hass.states.get(PUMP).state == "off"
    await no_followup(simulation)


async def test_scheduler_p0_p1_p2_p3_with_independent_budgets(simulation):
    await simulation.scheduler.async_start()
    await wait_for(lambda: simulation.scheduler.decision is not None)
    assert simulation.scheduler.decision.phase == "p0_transpiration"
    simulation.move_to("2026-10-13T07:00:00+00:00")
    simulation.scheduler.async_request_evaluation()
    await wait_for(lambda: simulation.shots.state == ShotState.RUNNING)
    await simulation.ramp_vwc(60, 60, 60)
    await wait_for(lambda: simulation.entry.options["p1_done"])
    await wait_for(lambda: simulation.scheduler._task.done())
    assert simulation.scheduler.decision.phase == "p2_midday"
    assert simulation.entry.options["p1_shots_done"] == 1
    simulation.move_to("2026-10-13T07:01:00+00:00")
    simulation.vwc(55, 55, 55)
    simulation.scheduler.async_request_evaluation()
    await wait_for(lambda: simulation.entry.options["p2_shots_done"] == 1)
    await wait_for(lambda: simulation.scheduler._task.done())
    simulation.move_to("2026-10-13T16:00:00+00:00")
    simulation.vwc(25, 25, 25)
    simulation.scheduler.async_request_evaluation()
    await wait_for(lambda: simulation.entry.options["p3_emergency_shots_done"] == 1)
    await wait_for(lambda: simulation.scheduler._task.done())
    assert simulation.scheduler.decision.phase == "p3_dryback"
    simulation.move_to("2026-10-13T18:00:00+00:00")
    simulation.vwc(40, 40, 40)
    simulation.scheduler.async_request_evaluation()
    await wait_for(lambda: simulation.scheduler._task.done())
    assert not simulation.scheduler.retry_blocked
    assert sum(cmd.service == "turn_on" for cmd in simulation.actor.commands) == 3


async def test_scheduler_fault_latch_survives_new_healthy_reports(simulation):
    await simulation.observe()
    simulation.move_to("2026-10-13T07:00:00+00:00")
    await simulation.scheduler.async_start()
    await wait_for(lambda: simulation.shots.state == ShotState.RUNNING)
    simulation.vwc(40, "unavailable", 40)
    await wait_for(lambda: simulation.scheduler.retry_blocked)
    simulation.vwc(40, 40, 40)
    simulation.move_to("2026-10-13T08:00:00+00:00")
    simulation.scheduler.async_request_evaluation()
    await wait_for(lambda: simulation.scheduler._task.done())
    assert sum(cmd.service == "turn_on" for cmd in simulation.actor.commands) == 1
    await no_followup(simulation)


async def test_ha_stop_during_shot_preserves_interruption(simulation):
    await start_p1(simulation)
    task = await running(simulation)
    simulation.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    result = await task
    assert result.state == ShotState.INTERRUPTED and not result.accounted
    assert simulation.entry.options["p1_shots_done"] == 0
    await wait_for(lambda: simulation.pump.state == PumpState.CLOSED)
    assert simulation.records["shots"]["operations"]["shot"]["recovery_required"]
    await no_followup(simulation)


@pytest.mark.parametrize("off_missing", [False, True])
async def test_crash_checkpoint_restores_without_replay_or_review_clearance(
    simulation, monkeypatch, off_missing, restarted_hass
):
    await start_p1(simulation)
    task = await running(simulation, "crashed")
    # RUNNING is persisted at the pump, STARTING is the durable shot intent.
    saved = simulation.checkpoint()
    assert saved["pump_state"] == "on"
    assert saved["records"]["pump"]["state"] == "running"
    assert (
        saved["records"]["shots"]["operations"]["crashed"]["delivery"]
        == "possible_partial"
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await simulation.close()  # Clean up old tasks without modifying the checkpoint.
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=saved
    )
    assert restored.hass is not simulation.hass
    if off_missing:
        restored.actor.confirmation_delay["turn_off"] = None
    try:
        if off_missing:
            with pytest.raises(ShotRejected, match="pump_recovery_unresolved"):
                await restored.initialize(fresh=False)
            assert restored.pump.state == PumpState.FAULT
            assert restored.actor.commands and all(
                cmd.service == "turn_off" for cmd in restored.actor.commands
            )
            assert restored.records["shots"] == saved["records"]["shots"]
            restored.actor.confirmation_delay["turn_off"] = 0
            assert await restored.pump.async_stop()
            # Continue normal initialization only after verified reconciliation.
            await restored.shots.async_initialize()
            await restored.cycle.async_initialize()
            await restored.scheduler.async_initialize()
        else:
            await restored.initialize(fresh=False)
        assert restored.shots.ownership is None
        assert restored.entry.options["p1_shots_done"] == 0
        assert not restored.shots.execution_available
        record = restored.records["shots"]["operations"]["crashed"]
        assert record["state"] == "interrupted" and record["recovery_required"]
        assert not record["accounted"]
        assert not restored.shots.recovery_assessment.native_activation_allowed
        assert all(cmd.service == "turn_off" for cmd in restored.actor.commands)
        assert restored.pump.state == PumpState.IDLE
        await no_followup(restored)
        restored.actor.confirmation_delay["turn_off"] = 0
        assert await restored.pump.async_stop()
        # Reconciliation must not clear shot/cycle evidence or grant ownership.
        assert restored.shots.recovery_assessment.review_required
        await no_followup(restored)
    finally:
        await restored.close()


@pytest.mark.parametrize("target", ["switch.real", "input_boolean.real", [PUMP]])
async def test_actor_cannot_address_arbitrary_targets(simulation, target):
    with pytest.raises(ValueError, match="fixed virtual pump"):
        await simulation.hass.services.async_call(
            "switch", "turn_on", {"entity_id": target}, blocking=True
        )
    assert simulation.actor.commands == []


@pytest.mark.parametrize(
    "values",
    [
        {"pump_switch": "switch.real"},
        {"vwc_sensor": ["sensor.real"]},
        {"p1_shots": "input_number.real"},
        {"native_enabled": True},
    ],
)
async def test_harness_configuration_has_no_entity_or_activation_surface(
    simulation, values
):
    before = dict(simulation.entry.options)
    with pytest.raises(ValueError):
        simulation.configure(**values)
    assert dict(simulation.entry.options) == before


async def test_corrupt_recovery_target_is_not_forwarded(simulation, monkeypatch):
    saved = simulation.checkpoint()
    saved["records"]["pump"] = {"entity_id": "switch.real", "state": "running"}
    await simulation.close()
    restored = NativeHarness(
        simulation.hass, monkeypatch, simulation.clock, checkpoint=deepcopy(saved)
    )
    try:
        with pytest.raises(ShotRejected, match="pump_recovery_unresolved"):
            await restored.initialize(fresh=False)
        assert restored.pump.state == PumpState.FAULT
        assert restored.records["pump"]["entity_id"] == "switch.real"
        assert restored.actor.commands == []
        await no_followup(restored)
    finally:
        await restored.close()


async def test_underlying_ha_dispatcher_is_never_called(simulation, monkeypatch):
    real_dispatch = AsyncMock(
        side_effect=AssertionError("Real HA service dispatch forbidden")
    )
    monkeypatch.setattr(ServiceRegistry, "async_call", real_dispatch)
    await start_p1(simulation)
    assert (await simulation.shots.async_execute("isolated")).accounted
    real_dispatch.assert_not_awaited()


async def test_independent_abrupt_reports_still_trip_spread_policy(simulation):
    await start_p1(simulation)
    task = await running(simulation)
    simulation.vwc(60, 40, 40)
    result = await task
    assert result.reason == "sensor_disagreement"
    assert result.state == ShotState.INTERRUPTED and not result.accounted
    await no_followup(simulation)


async def test_virtual_overnight_light_schedule(simulation):
    simulation.lighting("20:00:00", "08:00:00")
    simulation.move_to("2026-10-13T20:00:00+00:00")
    assert (await simulation.observe()).phase == "p0_transpiration"
    simulation.move_to("2026-10-13T21:00:00+00:00")
    assert (await simulation.observe()).phase == "p1_morning"
    simulation.vwc(60, 60, 60)
    assert (await simulation.observe()).phase == "p2_midday"
    simulation.move_to("2026-10-14T06:00:00+00:00")
    assert (await simulation.observe()).phase == "p3_dryback"
    assert simulation.actor.commands == []


@pytest.mark.parametrize("vwc,eligible", [(55, True), (55.1, False)])
async def test_p2_reference_drop_boundary_is_preserved(simulation, vwc, eligible):
    await start_p1(simulation)
    simulation.vwc(60, 60, 60)
    await simulation.observe()
    simulation.vwc(vwc, vwc, vwc)
    decision = await simulation.observe()
    assert decision.phase == "p2_midday"
    assert decision.eligible is eligible
    if not eligible:
        assert "p2_drop_not_reached" in decision.blocking_reasons
        await no_followup(simulation)


async def test_p2_capacity_cap_still_blocks(simulation):
    simulation.configure(vwc_cap=55)
    await start_p1(simulation)
    simulation.vwc(60, 60, 60)
    await simulation.observe()
    simulation.vwc(55, 55, 55)
    decision = await simulation.observe()
    assert "vwc_cap_active" in decision.blocking_reasons
    await no_followup(simulation)


@pytest.mark.parametrize(
    "vwc,enabled,eligible", [(30, True, True), (30.1, True, False), (25, False, False)]
)
async def test_p3_emergency_threshold_and_enable_gate(
    simulation, vwc, enabled, eligible
):
    simulation.configure(p3_emergency_enabled=enabled)
    await start_p1(simulation)
    simulation.vwc(60, 60, 60)
    await simulation.observe()
    simulation.move_to("2026-10-13T16:00:00+00:00")
    simulation.vwc(vwc, vwc, vwc)
    decision = await simulation.observe()
    assert decision.phase == "p3_dryback"
    assert decision.eligible is eligible
    if not eligible:
        await no_followup(simulation)
