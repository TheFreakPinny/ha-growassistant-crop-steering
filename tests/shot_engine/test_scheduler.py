"""Development scheduler tests: real HA events/timers, synthetic pump only."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.growassistant_crop_steering import async_unload_entry
from custom_components.growassistant_crop_steering.irrigation import (
    capture_inputs,
    evaluate,
)
from custom_components.growassistant_crop_steering.pump import PumpState
from custom_components.growassistant_crop_steering.scheduler import (
    _async_unload_scheduler,
    _NativeScheduler,
    next_evaluation,
)
from custom_components.growassistant_crop_steering.shot_engine import (
    ShotState,
    _ShotCoordinator,
)

pytestmark = pytest.mark.asyncio


def change(engine, **values):
    engine.hass.config_entries.async_update_entry(
        engine.entry, options={**engine.entry.options, **values}
    )


async def settle(hass):
    # Let native zero-delay timers enqueue their background evaluations.
    await asyncio.sleep(0.01)
    await hass.async_block_till_done()


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def advance(hass, shot_clock, value):
    previous = shot_clock.at
    shot_clock.move_to(value)
    # Translate the wall-clock jump into due loop timers using HA's helper.
    with patch(
        "pytest_homeassistant_custom_component.common.time",
        SimpleNamespace(time=lambda: previous.timestamp()),
    ):
        async_fire_time_changed(hass, shot_clock.at)
    await settle(hass)


@pytest.fixture
async def scheduler(hass, engine, shot_clock, monkeypatch):
    # HA timers use time.time plus cached tracker functions, independent of
    # dt_util.now. Freeze those wall clocks too, leaving loop.time monotonic.
    monkeypatch.setattr(
        "homeassistant.helpers.event.time",
        SimpleNamespace(time=lambda: shot_clock.at.timestamp()),
    )
    monkeypatch.setattr(
        "homeassistant.helpers.event.time_tracker_timestamp",
        lambda: shot_clock.at.timestamp(),
    )
    monkeypatch.setattr(
        "homeassistant.helpers.event.time_tracker_utcnow", lambda: shot_clock.at
    )
    change(engine, p1_duration_min=120, p2_end_offset_min=120, p2_shots=3)
    await hass.async_block_till_done(wait_background_tasks=True)
    scheduler = _NativeScheduler(hass, engine.entry, engine)
    await scheduler.async_initialize()
    yield scheduler
    await scheduler.async_close()


async def test_inactive_until_explicit_start(hass, engine, scheduler):
    await settle(hass)
    assert not scheduler.running
    assert scheduler.decision is None
    assert not scheduler._listeners and scheduler._timer is None
    hass.states.async_set("sensor.vwc", "35")
    await settle(hass)
    assert engine.calls == []
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    assert engine.calls == [("turn_on", "switch.pump")]


@pytest.mark.parametrize(
    "phase,prefix", [("p1", "p1"), ("p2", "p2"), ("p3", "p3_emergency")]
)
async def test_phases_execute_account_and_arm_soak(
    hass, engine, scheduler, shot_clock, phase, prefix
):
    if phase == "p2":
        change(engine, p1_active=False, p1_done=True)
    elif phase == "p3":
        shot_clock.move_to("2026-10-13T19:00:00+00:00")
        hass.states.async_set("sensor.vwc", "25")
    await scheduler.async_start()
    await wait_for(
        lambda: scheduler.decision is not None and engine.state == ShotState.RUNNING
    )
    await wait_for(lambda: scheduler._task.done())
    assert engine.entry.options[prefix + "_shots_done"] == 1
    assert scheduler.decision.phase == engine.snapshot.decision.phase
    assert "soak_active" in scheduler.decision.blocking_reasons
    assert scheduler.next_evaluation == scheduler.decision.soak_until.astimezone(UTC)
    assert not scheduler.retry_blocked
    await advance(hass, shot_clock, scheduler.next_evaluation.isoformat())
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 2


@pytest.mark.parametrize(
    "at,flags,expected,boundary",
    [
        ("05:30", {"p1_active": False}, "p3_dryback", "06:00"),
        ("06:00", {"p1_active": False}, "p0_transpiration", "07:00"),
        ("07:00", {"p1_active": False}, "p1_morning", "09:00"),
        ("09:00", {"p1_active": False, "p1_done": True}, "p2_midday", "16:00"),
        ("16:00", {"p1_active": False, "p1_done": True}, "p3_dryback", "18:00"),
        ("18:00", {"p1_active": False}, "p3_dryback", "06:00"),
    ],
)
async def test_boundary_timer_without_sensor_events(
    hass, engine, scheduler, shot_clock, at, flags, expected, boundary
):
    change(engine, **flags)
    hass.states.async_set("sensor.vwc", "70")
    shot_clock.move_to(f"2026-10-13T{at}:00+00:00")
    await scheduler.async_start()
    await settle(hass)
    assert scheduler.decision.phase == expected
    assert scheduler.next_evaluation.strftime("%H:%M") == boundary
    when = scheduler.next_evaluation
    await advance(hass, shot_clock, when.isoformat())
    assert scheduler.decision.windows is not None
    assert scheduler.next_evaluation > when
    assert engine.calls == []


async def test_p0_to_p1_timer_executes_only_if_currently_eligible(
    hass, engine, scheduler, shot_clock
):
    shot_clock.move_to("2026-10-13T06:59:00+00:00")
    await scheduler.async_start()
    await settle(hass)
    assert scheduler.decision.phase == "p0_transpiration"
    await advance(hass, shot_clock, "2026-10-13T07:00:00+00:00")
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    assert engine.snapshot.decision.phase == "p1_morning"


@pytest.mark.parametrize(
    "entity,state",
    [
        ("sensor.vwc", "40"),
        ("binary_sensor.drain", "off"),
        ("binary_sensor.tray", "off"),
        ("switch.pump", "off"),
    ],
)
async def test_relevant_sensor_updates(hass, engine, scheduler, entity, state):
    hass.states.async_set(entity, "unavailable")
    await scheduler.async_start()
    await settle(hass)
    assert not scheduler.decision.eligible and engine.calls == []
    hass.states.async_set(entity, state)
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1


async def test_bursts_and_concurrent_requests_never_queue(
    hass, engine, scheduler, monkeypatch
):
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    module = "custom_components.growassistant_crop_steering.scheduler.evaluate"
    evaluations = []

    def spy(inputs, now):
        evaluations.append(now)
        return evaluate(inputs, now)

    monkeypatch.setattr(module, spy)
    for i in range(50):
        hass.states.async_set("sensor.vwc", str(40 + i % 2))
        scheduler.async_request_evaluation()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    assert len(evaluations) == 1
    task = scheduler._task
    for i in range(50):
        scheduler.async_request_evaluation()
        hass.states.async_set("sensor.vwc", str(40 + i % 2))
    await settle(hass)
    assert scheduler._pending is None and scheduler._task is task
    await wait_for(task.done)
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1
    await settle(hass)
    assert len(evaluations) == 2  # one request and a fresh completion diagnostic
    assert scheduler._pending is None


async def test_unrelated_events_do_not_wake_or_replace_timer(hass, engine, scheduler):
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    task, timer = scheduler._task, scheduler._timer
    for i in range(30):
        hass.states.async_set("sensor.unrelated", str(i))
    await settle(hass)
    assert scheduler._task is task and scheduler._timer is timer


async def test_options_rebind_new_sensor_and_helpers(hass, engine, scheduler):
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    change(
        engine,
        vwc_sensor=["sensor.new_vwc"],
        led_sunset="17:00:00",
        p1_active=False,
        p1_done=True,
    )
    hass.states.async_set("sensor.new_vwc", "70")
    await settle(hass)
    assert scheduler.decision.windows.sunset.hour == 17
    assert scheduler.decision.phase == "p2_midday"
    assert scheduler.next_evaluation.hour == 9
    hass.states.async_set("sensor.new_vwc", "40")
    await wait_for(lambda: engine.state == ShotState.RUNNING)


@pytest.mark.parametrize(
    "key,value",
    [
        ("p1_active", True),
        ("p1_done", False),
        ("p1_shots_done", 0),
        ("last_shot", None),
    ],
)
async def test_legacy_flag_counter_timestamp_events(
    hass, engine, scheduler, key, value
):
    # Map one setting to a legacy HA helper and prove its state event wakes us.
    entity = "input_text.test_helper"
    options = dict(engine.entry.options)
    options.pop(key, None)
    engine.hass.config_entries.async_update_entry(
        engine.entry, data={**engine.entry.data, key: entity}, options=options
    )
    hass.states.async_set(entity, "unavailable")
    await scheduler.async_start()
    await settle(hass)
    old_task = scheduler._task
    hass.states.async_set(entity, "" if value is None else str(value).lower())
    await settle(hass)
    assert scheduler._task is not old_task


@pytest.mark.parametrize("at", ["17:00", "19:00"])
async def test_delayed_window_has_no_catchup(hass, engine, scheduler, shot_clock, at):
    change(engine, p1_active=False, p1_done=True, p3_emergency_enabled=False)
    hass.states.async_set("sensor.vwc", "70")
    shot_clock.move_to("2026-10-13T09:00:00+00:00")
    await scheduler.async_start()
    await settle(hass)
    # A delayed timer wakes well after P2 cutoff; current P3 decision blocks.
    hass.states.async_set("sensor.vwc", "25")
    await advance(hass, shot_clock, f"2026-10-13T{at}:00+00:00")
    assert scheduler.decision.phase == "p3_dryback"
    assert "p3_emergency_disabled" in scheduler.decision.blocking_reasons
    assert engine.calls == []


@pytest.mark.parametrize(
    "fault", ["controller", "recovery", "cycle_reset", "uninitialized"]
)
async def test_readiness_blocks_without_polling_or_retry(
    hass, engine, scheduler, shot_clock, fault
):
    if fault == "controller":
        engine.controller.state = PumpState.FAULT
    elif fault == "recovery":
        engine._blocked = True
    elif fault == "uninitialized":
        engine._ready = False
    else:
        shot_clock.move_to("2026-10-14T08:00:00+00:00")
    await scheduler.async_start()
    await settle(hass)
    task = scheduler._task
    await settle(hass)
    assert task.done() and scheduler._task is task
    assert engine.calls == [] and scheduler._pending is None
    if fault == "cycle_reset":
        assert scheduler.retry_blocked


@pytest.mark.parametrize(
    "entity,value",
    [
        ("sensor.vwc", "unavailable"),
        ("binary_sensor.tray", "on"),
        ("binary_sensor.drain", "unavailable"),
        ("switch.pump", "unavailable"),
    ],
)
async def test_interruption_latches_no_auto_retry(
    hass, engine, scheduler, entity, value
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    hass.states.async_set(entity, value)
    await wait_for(lambda: scheduler._task.done())
    assert scheduler.retry_blocked
    assert engine.state == ShotState.INTERRUPTED
    for sensor, state in {
        "sensor.vwc": "40",
        "binary_sensor.tray": "off",
        "binary_sensor.drain": "off",
        "switch.pump": "off",
    }.items():
        hass.states.async_set(sensor, state)
    await settle(hass)
    await scheduler.async_stop()
    await scheduler.async_start()
    await settle(hass)
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert scheduler.decision is not None


async def test_execution_failure_is_not_retried(hass, engine, scheduler):
    async def broken(call):
        raise RuntimeError("synthetic failure")

    hass.services.async_register("switch", "turn_on", broken)
    await scheduler.async_start()
    await wait_for(lambda: scheduler.retry_blocked)
    await settle(hass)
    assert engine.state == ShotState.FAULT
    hass.services.async_register("switch", "turn_on", engine.simulated_service)
    for _ in range(20):
        scheduler.async_request_evaluation()
    await settle(hass)
    assert engine.calls == [("turn_off", "switch.pump")]


async def test_fresh_coordinator_revalidation(hass, engine, scheduler, monkeypatch):
    original = engine.async_execute

    async def race():
        hass.states.async_set("sensor.vwc", "70")
        return await original()

    monkeypatch.setattr(engine, "async_execute", race)
    await scheduler.async_start()
    await settle(hass)
    assert scheduler.retry_blocked
    assert engine.calls == []
    assert not scheduler.decision.eligible


@pytest.mark.parametrize(
    "cleanup", ["stop", "shutdown", "unload", "integration_unload"]
)
async def test_lifecycle_drain_active_and_cleanup(
    hass, engine, scheduler, monkeypatch, cleanup
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    if cleanup == "stop":
        await scheduler.async_stop()
    elif cleanup == "shutdown":
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await wait_for(lambda: not scheduler.running and scheduler._task.done())
    elif cleanup == "unload":
        await _async_unload_scheduler(hass, engine.entry)
    else:
        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        )
        assert await async_unload_entry(hass, engine.entry)
    assert not scheduler.running
    assert (
        not scheduler._listeners
        and scheduler._timer is None
        and scheduler._pending is None
    )
    assert hass.states.get("switch.pump").state == "off"
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    calls = list(engine.calls)
    hass.states.async_set("sensor.vwc", "35")
    scheduler.async_request_evaluation()
    await settle(hass)
    assert engine.calls == calls


async def test_restart_never_replays_interrupted_journal(
    hass, engine, scheduler, hass_storage
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    await scheduler.async_close()
    await engine.async_close()
    # Simulate a persisted in-flight record from a crash, with pump already off.
    journal_key = f"growassistant_crop_steering.shot_journal.{engine.entry.entry_id}"
    record = next(iter(hass_storage[journal_key]["data"]["operations"].values()))
    record["state"] = "running"
    from custom_components.growassistant_crop_steering.pump import PumpController

    pump = PumpController(hass, engine.entry)
    await pump.async_initialize()
    restored = _ShotCoordinator(hass, engine.entry, pump)
    await restored.async_initialize()
    new = _NativeScheduler(hass, engine.entry, restored)
    await new.async_initialize()
    assert not new.running
    await new.async_start()
    await settle(hass)
    assert restored._blocked and not restored.execution_available
    assert restored.snapshot is None
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1
    await new.async_close()
    await restored.async_close()


async def test_pending_and_timer_cancellation_and_restart(
    hass, engine, scheduler, shot_clock
):
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await scheduler.async_stop()  # cancel before zero-delay callback fires
    await settle(hass)
    assert scheduler.decision is None and engine.calls == []
    await scheduler.async_start()
    await settle(hass)
    assert scheduler._timer is not None
    task = scheduler._task
    await scheduler.async_stop()
    await advance(hass, shot_clock, "2026-10-13T09:00:00+00:00")
    assert scheduler._task is task and scheduler._timer is None
    await scheduler.async_start()
    await settle(hass)
    assert scheduler._task is not task


@pytest.mark.parametrize("vwc", ["60", "61"])
async def test_approved_field_capacity_finishes_active_p1_blocks_next(
    hass, engine, scheduler, shot_clock, vwc
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    start = hass.loop.time()
    hass.states.async_set("sensor.vwc", vwc)
    await settle(hass)
    assert engine.state == ShotState.RUNNING
    await wait_for(lambda: scheduler._task.done())
    assert hass.loop.time() - start >= 0.95
    assert engine.state == ShotState.COMPLETED
    assert engine.entry.options["p1_shots_done"] == 1
    assert "field_capacity_reached" in scheduler.decision.blocking_reasons
    await advance(hass, shot_clock, scheduler.next_evaluation.isoformat())
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1


async def test_independent_fault_still_interrupts_at_field_capacity(
    hass, engine, scheduler
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    hass.states.async_set("sensor.vwc", "60")
    hass.states.async_set("binary_sensor.tray", "on")
    await wait_for(lambda: scheduler._task.done())
    assert scheduler.retry_blocked and engine.state == ShotState.INTERRUPTED
    assert engine.entry.options.get("p1_shots_done", 0) == 0


@pytest.mark.parametrize(
    "date,last,at,until",
    [
        (
            "2026-03-29",
            "2026-03-29T01:55:00+01:00",
            "2026-03-29T03:00:00+02:00",
            "2026-03-29T01:05:00+00:00",
        ),
        (
            "2026-10-25",
            "2026-10-25T02:55:00+02:00",
            "2026-10-25T02:00:00+01:00",
            "2026-10-25T01:05:00+00:00",
        ),
    ],
)
async def test_berlin_dst_soak_uses_elapsed_utc(
    hass, engine, scheduler, shot_clock, date, last, at, until
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    change(
        engine, last_shot=last, last_shot_type="p3_emergency", p3_emergency_soak_min=10
    )
    shot_clock.move_to(at)
    await scheduler.async_start()
    await settle(hass)
    assert "soak_active" in scheduler.decision.blocking_reasons
    assert scheduler.next_evaluation == datetime.fromisoformat(until)
    assert engine.calls == []


@pytest.mark.parametrize("date", ["2026-03-29", "2026-10-25"])
async def test_berlin_unsafe_wall_boundaries_fail_closed(
    hass, engine, scheduler, shot_clock, date
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    change(engine, led_sunrise="02:30:00")
    shot_clock.move_to(f"{date}T04:00:00+00:00")
    await scheduler.async_start()
    await settle(hass)
    assert "invalid_schedule" in scheduler.decision.blocking_reasons
    assert scheduler.next_evaluation is None and engine.calls == []


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-03-28T19:00:00+01:00", "2026-03-29T04:00:00+00:00"),
        ("2026-10-24T19:00:00+02:00", "2026-10-25T05:00:00+00:00"),
    ],
)
async def test_berlin_sunrise_timer_crosses_dst(
    hass, engine, scheduler, shot_clock, at, expected
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.states.async_set("sensor.vwc", "70")
    shot_clock.move_to(at)
    await scheduler.async_start()
    await settle(hass)
    assert scheduler.next_evaluation == datetime.fromisoformat(expected)
    assert scheduler.next_evaluation.tzinfo is UTC


async def test_next_evaluation_compares_instants_in_fold(hass, engine):
    change(
        engine,
        last_shot="2026-10-25T02:55:00+02:00",
        last_shot_type="p3_emergency",
        p3_emergency_soak_min=10,
    )
    now = datetime(2026, 10, 25, 2, 0, tzinfo=ZoneInfo("Europe/Berlin"), fold=1)
    decision = evaluate(capture_inputs(hass, engine.entry), now)
    assert next_evaluation(decision, now) == datetime(2026, 10, 25, 1, 5, tzinfo=UTC)


async def test_invalid_zero_soak_never_spins_or_executes(hass, engine, scheduler):
    change(engine, p1_soak_min=0, p1_shots=2)
    await scheduler.async_start()
    await settle(hass)
    task = scheduler._task
    await settle(hass)
    assert "invalid_p1_soak_min" in scheduler.decision.blocking_reasons
    assert scheduler._task is task and scheduler._pending is None
    assert engine.calls == []


async def test_external_active_coordinator_discards_triggers_then_wakes(
    hass, engine, scheduler
):
    external = asyncio.create_task(engine.async_execute())
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    await scheduler.async_start()
    for _ in range(20):
        scheduler.async_request_evaluation()
    assert scheduler._task is None and scheduler._pending is None
    await external
    await settle(hass)
    assert "soak_active" in scheduler.decision.blocking_reasons
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1


async def test_initialize_ownership_and_closed_instances(hass, engine, scheduler):
    duplicate = _NativeScheduler(hass, engine.entry, engine)
    with pytest.raises(RuntimeError):
        await duplicate.async_initialize()
    with pytest.raises(RuntimeError):
        await duplicate.async_start()
    await scheduler.async_close()
    with pytest.raises(RuntimeError):
        await scheduler.async_start()
    with pytest.raises(RuntimeError):
        await scheduler.async_initialize()


async def test_production_setup_has_no_scheduler_activation():
    from pathlib import Path

    source = Path(
        "custom_components/growassistant_crop_steering/__init__.py"
    ).read_text()
    assert "_NativeScheduler" not in source
    assert "scheduler.async_start" not in source


async def test_timer_replacement_rejects_stale_callback(
    hass, engine, scheduler, monkeypatch
):
    import custom_components.growassistant_crop_steering.scheduler as module

    native = module.async_track_point_in_utc_time
    callbacks = []

    def track(hass, action, when):
        callbacks.append((action, when))
        return native(hass, action, when)

    monkeypatch.setattr(module, "async_track_point_in_utc_time", track)
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    stale, old_when = callbacks[-1]
    change(engine, p1_duration_min=180)
    await settle(hass)
    timer, when = scheduler._timer, scheduler.next_evaluation
    assert when != old_when
    stale(old_when)  # a callback dispatched just before cancellation
    assert scheduler._timer is timer and scheduler.next_evaluation == when
    assert scheduler._pending is None and engine.calls == []


async def test_soak_boundary_cannot_override_wet_safety_gate(
    hass, engine, scheduler, shot_clock
):
    change(
        engine,
        last_shot="2026-10-13T07:55:00+00:00",
        last_shot_type="p1",
        p1_soak_min=10,
    )
    hass.states.async_set("binary_sensor.tray", "on")
    await scheduler.async_start()
    await settle(hass)
    assert scheduler.next_evaluation == datetime(2026, 10, 13, 8, 5, tzinfo=UTC)
    await advance(hass, shot_clock, scheduler.next_evaluation.isoformat())
    assert "soak_active" not in scheduler.decision.blocking_reasons
    assert "drain_tray_sensor_wet" in scheduler.decision.blocking_reasons
    assert engine.calls == []


async def test_active_controller_fault_without_state_event(hass, engine, scheduler):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    engine.controller.state = PumpState.FAULT
    await wait_for(lambda: scheduler._task.done())
    assert scheduler.retry_blocked
    assert engine.state == ShotState.INTERRUPTED
    assert hass.states.get("switch.pump").state == "off"
    assert engine.entry.options.get("p1_shots_done", 0) == 0


async def test_active_options_safety_change_interrupts_not_queues(
    hass, engine, scheduler
):
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    change(engine, drain_tray_sensor="binary_sensor.replacement")
    hass.states.async_set("binary_sensor.replacement", "off")
    await wait_for(lambda: scheduler._task.done())
    await settle(hass)
    assert scheduler.retry_blocked
    assert engine.state == ShotState.INTERRUPTED
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1


async def test_close_then_reload_constructs_inactive_scheduler(hass, engine, scheduler):
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    await _async_unload_scheduler(hass, engine.entry)
    fresh = _NativeScheduler(hass, engine.entry, engine)
    await fresh.async_initialize()
    hass.states.async_set("sensor.vwc", "40")
    await settle(hass)
    assert not fresh.running and fresh.decision is None
    assert engine.calls == []
    await fresh.async_close()


@pytest.mark.parametrize("mode", ["p1", "p2"])
async def test_manual_phase_policies_remain_ineligible(
    hass, engine, scheduler, shot_clock, mode
):
    if mode == "p1":
        change(engine, p1_mode="manual", p1_active=False)
    else:
        change(engine, p1_active=False, p1_done=True, p2_mode="manual")
    await scheduler.async_start()
    await settle(hass)
    reason = "p1_not_active" if mode == "p1" else "p2_manual"
    assert reason in scheduler.decision.blocking_reasons
    await advance(hass, shot_clock, "2026-10-13T09:00:00+00:00")
    assert engine.calls == []


async def test_double_stop_and_caller_cancellation_drain_off(
    hass, engine, scheduler, monkeypatch
):
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.5
    )
    off_entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_off(call):
        off_entered.set()
        await release.wait()
        await engine.simulated_service(call)

    hass.services.async_register("switch", "turn_off", delayed_off)
    await scheduler.async_start()
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    stopping = asyncio.create_task(scheduler.async_stop())
    await off_entered.wait()
    again = asyncio.create_task(scheduler.async_stop())
    stopping.cancel()
    stopping.cancel()
    await asyncio.sleep(0.01)
    assert not stopping.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await again
    assert hass.states.get("switch.pump").state == "off"
    assert scheduler._task.done() and not scheduler._listeners


async def test_stop_removes_exact_listeners_and_option_hook(hass, engine, scheduler):
    before = dict(hass.bus.async_listeners())
    update_listeners = list(engine.entry.update_listeners)
    hass.states.async_set("sensor.vwc", "70")
    await scheduler.async_start()
    await settle(hass)
    assert len(engine.entry.update_listeners) == len(update_listeners) + 1
    await scheduler.async_stop()
    assert list(engine.entry.update_listeners) == update_listeners
    assert dict(hass.bus.async_listeners()) == before


async def test_external_interrupted_completion_cannot_auto_retry(
    hass, engine, scheduler
):
    external = asyncio.create_task(engine.async_execute())
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    await scheduler.async_start()
    hass.states.async_set("binary_sensor.tray", "on")
    await external
    await settle(hass)
    hass.states.async_set("binary_sensor.tray", "off")
    await settle(hass)
    assert scheduler.retry_blocked
    assert len([c for c in engine.calls if c[0] == "turn_on"]) == 1


async def test_normal_setup_never_initializes_native_owners(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.growassistant_crop_steering import async_setup_entry
    from custom_components.growassistant_crop_steering.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering._CycleResetCoordinator.async_start",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, entry)
    for key in ("development_schedulers", "development_shots", "native_pump_owner"):
        assert not hass.data.get(f"{DOMAIN}.{key}")
    assert await async_unload_entry(hass, entry)
