"""Native cycle ownership with real HA fixtures and simulated pump services."""

import asyncio
from copy import deepcopy
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.growassistant_crop_steering import async_unload_entry
from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.cycle import _NativeCycleController
from custom_components.growassistant_crop_steering.irrigation import (
    capture_inputs,
    evaluate,
)
from custom_components.growassistant_crop_steering.pump import PumpController
from custom_components.growassistant_crop_steering.scheduler import _NativeScheduler
from custom_components.growassistant_crop_steering.shot_engine import (
    ShotRejected,
    ShotState,
    _cycle_reset_started,
    _ShotCoordinator,
)

pytestmark = pytest.mark.asyncio


def change(engine, **values):
    engine.hass.config_entries.async_update_entry(
        engine.entry, options={**engine.entry.options, **values}
    )


async def settle(hass):
    await asyncio.sleep(0.01)
    await hass.async_block_till_done()


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def advance(hass, shot_clock, value):
    previous = shot_clock.at
    shot_clock.move_to(value)
    with patch(
        "pytest_homeassistant_custom_component.common.time",
        SimpleNamespace(time=lambda: previous.timestamp()),
    ):
        async_fire_time_changed(hass, shot_clock.at)
    await settle(hass)


@pytest.fixture
async def cycle(engine, shot_clock):
    shot_clock.move_to("2026-10-13T06:00:00+00:00")
    change(
        engine,
        p1_active=False,
        p1_done=False,
        p1_window_opened_today=False,
        p2_ref_vwc=0,
        p1_shots_done=0,
        p2_shots_done=0,
        p3_emergency_shots_done=0,
        p1_duration_min=120,
        p2_end_offset_min=120,
        p2_shots=3,
    )
    cycle = _NativeCycleController(engine)
    await cycle.async_initialize()
    yield cycle
    await cycle.async_close()


async def begin(cycle):
    assert await cycle.async_sunrise()
    assert cycle.phase == "p0_transpiration"


async def start(cycle, shot_clock):
    await begin(cycle)
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    result = await cycle.async_observe()
    assert result.eligible
    assert cycle.entry.options["p1_active"]


async def test_initialization_does_not_reset_or_activate(engine, cycle):
    assert cycle.record is None
    assert not engine.execution_available
    assert engine.calls == []
    with pytest.raises(ShotRejected, match="native_cycle_review_required"):
        await engine.async_execute()
    assert engine.entry.options["p1_active"] is False


async def test_sensor_transitions_and_cycle_reference(engine, cycle, shot_clock):
    await start(cycle, shot_clock)
    engine.hass.states.async_set("sensor.vwc", "60")
    decision = await cycle.async_observe()
    assert cycle.entry.options["p1_done"] is True
    assert cycle.entry.options["p1_active"] is False
    assert cycle.entry.options["p2_ref_vwc"] == 60
    assert cycle.record["reference_cycle"] == decision.cycle_id
    assert decision.phase == cycle.phase == "p2_midday"
    assert "p2_drop_not_reached" in decision.blocking_reasons
    engine.hass.states.async_set("sensor.vwc", "55")
    assert (await cycle.async_observe()).eligible
    shot_clock.move_to("2026-10-13T16:00:00+00:00")
    assert (await cycle.async_observe()).phase == "p3_dryback"
    engine.hass.states.async_set("sensor.vwc", "25")
    assert (await cycle.async_observe()).eligible
    shot_clock.move_to("2026-10-13T18:00:00+00:00")
    assert (await cycle.async_observe()).phase == "p3_dryback"


@pytest.mark.parametrize(
    "vwc,starts", [("49", True), ("50", True), ("50.1", False), ("60", False)]
)
async def test_start_threshold(engine, cycle, shot_clock, vwc, starts):
    await begin(cycle)
    engine.hass.states.async_set("sensor.vwc", vwc)
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    await cycle.async_observe()
    assert cycle.entry.options["p1_active"] is starts
    assert cycle.entry.options["p1_window_opened_today"] is starts


@pytest.mark.parametrize(
    "entity,value",
    [
        ("sensor.vwc", "unavailable"),
        ("sensor.vwc", "invalid"),
        ("sensor.vwc", "nan"),
        ("sensor.vwc", "101"),
        ("binary_sensor.drain", "unavailable"),
        ("binary_sensor.drain", "wet"),
        ("binary_sensor.drain", "invalid"),
        ("binary_sensor.tray", "unknown"),
        ("binary_sensor.tray", "on"),
    ],
)
async def test_all_start_gates(engine, cycle, shot_clock, entity, value):
    await begin(cycle)
    engine.hass.states.async_set(entity, value)
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    assert not (await cycle.async_observe()).p1_start_eligible
    assert not cycle.entry.options["p1_active"]
    assert engine.calls == []


async def test_duplicate_start_and_sunrise(engine, cycle, shot_clock):
    await start(cycle, shot_clock)
    record = deepcopy(cycle.record)
    assert not await cycle.async_sunrise()
    await cycle.async_observe()
    assert cycle.record == record
    assert not await cycle.async_manual_start()


@pytest.mark.parametrize("mode", ["sensor", "manual"])
async def test_manual_semantics(engine, cycle, shot_clock, mode):
    change(engine, p1_mode=mode)
    await begin(cycle)
    engine.hass.states.async_set("sensor.vwc", "55")  # Above start threshold.
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    await cycle.async_observe()
    assert not engine.entry.options["p1_active"]
    assert await cycle.async_manual_start() is (mode == "manual")
    if mode == "manual":
        assert not await cycle.async_manual_start()
        assert evaluate(
            capture_inputs(engine.hass, engine.entry), shot_clock.at
        ).eligible
        shot_clock.move_to("2026-10-13T09:00:00+00:00")
        decision = await cycle.async_observe()
        assert decision.phase == "p2_midday"
        assert "p1_not_done" in decision.blocking_reasons
        assert not engine.entry.options["p1_done"]
        assert engine.entry.options["p2_ref_vwc"] == 0
        assert await cycle.async_manual_complete()
        assert engine.entry.options["p2_ref_vwc"] == 55


@pytest.mark.parametrize("trigger", ["capacity", "limit", "drain"])
async def test_existing_completion_triggers(engine, cycle, shot_clock, trigger):
    if trigger == "limit":
        change(engine, p1_shots=1)
    await start(cycle, shot_clock)
    if trigger == "limit":
        result = await engine.async_execute("limit")
        assert result.accounted
    elif trigger == "capacity":
        engine.hass.states.async_set("sensor.vwc", "60")
    else:
        engine.hass.states.async_set("binary_sensor.drain", "on")
    await cycle.async_observe()
    assert engine.entry.options["p1_done"] is True
    assert engine.entry.options["p2_ref_vwc"] == (60 if trigger == "capacity" else 40)
    assert cycle.record["completion_reasons"] == [
        {
            "capacity": "field_capacity_reached",
            "limit": "shot_limit_reached",
            "drain": "drain_sensor_wet",
        }[trigger]
    ]


@pytest.mark.parametrize(
    "sensor,value",
    [
        ("sensor.vwc", "unavailable"),
        ("sensor.vwc", "nan"),
        ("binary_sensor.drain", "unavailable"),
        ("binary_sensor.tray", "wet"),
    ],
)
async def test_invalid_data_does_not_complete(engine, cycle, shot_clock, sensor, value):
    await start(cycle, shot_clock)
    engine.hass.states.async_set(sensor, value)
    await cycle.async_observe()
    assert not engine.entry.options["p1_done"]
    assert engine.entry.options["p2_ref_vwc"] == 0


async def test_capacity_during_shot_finishes_then_captures(engine, cycle, shot_clock):
    await start(cycle, shot_clock)
    task = asyncio.create_task(engine.async_execute("capacity"))
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    began = engine.hass.loop.time()
    engine.hass.states.async_set("sensor.vwc", "60")
    await cycle.async_observe()
    assert not engine.entry.options["p1_done"]
    assert engine.hass.states.get("switch.pump").state == "on"
    result = await task
    assert result.state == ShotState.COMPLETED and result.accounted
    assert engine.hass.loop.time() - began > 0.8
    await cycle.async_observe()
    assert engine.entry.options["p1_done"]
    assert engine.entry.options["p1_shots_done"] == 1
    assert engine.entry.options["p2_ref_vwc"] == 60


async def test_reset_new_cycle_retains_operations_and_soak(engine, cycle, shot_clock):
    await start(cycle, shot_clock)
    assert (await engine.async_execute("old")).accounted
    stamp = engine.entry.options["last_shot"]
    old_cycle = cycle.cycle_id
    shot_clock.move_to("2026-10-14T06:00:00+00:00")
    assert await cycle.async_sunrise()
    assert old_cycle != cycle.cycle_id
    assert engine.entry.options["p1_shots_done"] == 0
    assert engine.entry.options["last_shot"] == stamp
    assert engine._journal["operations"]["old"]["accounted"]
    assert engine._journal["last_accounting"]["cycle_id"] == cycle.cycle_id
    assert not await cycle.async_sunrise()
    assert cycle.phase == "p0_transpiration"


async def test_reset_during_active_shot_never_counts_or_reenables(
    engine, cycle, shot_clock
):
    await start(cycle, shot_clock)
    task = asyncio.create_task(engine.async_execute("old_running"))
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    old = cycle.cycle_id
    shot_clock.move_to("2026-10-14T06:00:00+00:00")
    assert not await cycle.async_sunrise()
    result = await task
    assert result.state == ShotState.INTERRUPTED and not result.accounted
    assert engine.entry.options["p1_shots_done"] == 0
    assert engine.hass.states.get("switch.pump").state == "off"
    assert cycle.cycle_id == old
    assert cycle.recovery_reason
    assert not engine.execution_available
    assert not await cycle.async_sunrise()
    assert "old_running" in engine._journal["operations"]


async def test_concurrent_sunrise_serialized(cycle):
    results = await asyncio.gather(*(cycle.async_sunrise() for _ in range(5)))
    assert results.count(True) == 1
    assert results.count(False) == 4


@pytest.mark.parametrize(
    "key,value",
    [
        ("p1_active", True),
        ("p2_ref_vwc", 55),
        ("p1_shots_done", 1),
        ("p1_mode", "manual"),
    ],
)
async def test_external_state_drift_blocks(engine, cycle, key, value):
    await begin(cycle)
    change(engine, **{key: value})
    assert not engine.execution_available
    assert cycle.recovery_reason
    with pytest.raises(ShotRejected):
        await engine.async_execute()


@pytest.mark.parametrize(
    "pending,mismatch", [(False, False), (True, False), (False, True)]
)
async def test_restart_restores_identity_without_replay(
    engine, cycle, pending, mismatch, monkeypatch
):
    await begin(cycle)
    stored = deepcopy(cycle.record)
    stored["pending"] = pending
    if mismatch:
        stored["values"]["p1_done"] = True
    engine._cycle = None  # Simulate a new process with the same entry options.
    restored = _NativeCycleController(engine)
    monkeypatch.setattr(restored.store, "async_load", AsyncMock(return_value=stored))
    await restored.async_initialize()
    assert restored.cycle_id == cycle.cycle_id
    assert restored.recovery_reason == (
        "cycle_persistence_inconsistent"
        if pending or mismatch
        else "cycle_restart_requires_review"
    )
    assert not engine.execution_available
    assert not await restored.async_sunrise()
    assert engine.calls == []
    await restored.async_close()


async def test_reload_during_transition_preserves_pending_intent(
    engine, cycle, shot_clock, monkeypatch
):
    await begin(cycle)
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []
    original_save = cycle.store.async_save

    async def delayed(record):
        saved.append(record)
        entered.set()
        await release.wait()
        await original_save(record)

    monkeypatch.setattr(cycle.store, "async_save", delayed)
    observe = asyncio.create_task(cycle.async_observe())
    await entered.wait()
    close = asyncio.create_task(cycle.async_close())
    await asyncio.sleep(0)
    with pytest.raises(ShotRejected):
        await engine.async_execute("during_transition")
    release.set()
    with pytest.raises(RuntimeError, match="interrupted"):
        await observe
    await close
    assert saved[0]["pending"]
    assert not engine.entry.options["p1_active"]
    assert cycle.recovery_reason


@pytest.mark.parametrize("failure", ["sensor", "drain"])
async def test_interrupted_shot_latches_cycle(engine, cycle, shot_clock, failure):
    await start(cycle, shot_clock)
    task = asyncio.create_task(engine.async_execute("fault"))
    await wait_for(lambda: engine.state == ShotState.RUNNING)
    engine.hass.states.async_set(
        "sensor.vwc" if failure == "sensor" else "binary_sensor.drain", "unavailable"
    )
    result = await task
    assert not result.accounted
    await cycle.async_observe()
    assert cycle.recovery_reason == "shot_result_requires_review"
    assert "fault" in engine._journal["operations"]
    shot_clock.move_to("2026-10-14T06:00:00+00:00")
    assert not await cycle.async_sunrise()


async def test_external_reset_blocks_native(engine, cycle):
    await begin(cycle)
    async with _cycle_reset_started(engine.hass, engine.entry):
        pass
    assert cycle.recovery_reason == "external_cycle_reset_requires_review"
    assert not engine.execution_available


@pytest.mark.parametrize("delay", ["06:00:02", "07:00:00", "19:00:00"])
async def test_missed_sunrise_never_catches_up(engine, cycle, shot_clock, delay):
    shot_clock.move_to(f"2026-10-13T{delay}+00:00")
    await cycle.async_observe()
    assert cycle.recovery_reason == "missed_sunrise_requires_review"
    assert cycle.record is None
    assert engine.calls == []


async def test_legacy_values_require_explicit_migration(engine, cycle):
    engine.hass.config_entries.async_update_entry(
        engine.entry, data={**engine.entry.data, "p1_active": "input_boolean.legacy"}
    )
    engine.hass.states.async_set("input_boolean.legacy", "off")
    assert not await cycle.async_sunrise()
    assert cycle.recovery_reason == "legacy_cycle_migration_required"
    assert engine.hass.states.get("input_boolean.legacy").state == "off"


@pytest.mark.parametrize(
    "day,at,sunrise,sunset,expected",
    [
        (
            "2026-10-13",
            "20:00:00+00:00",
            "20:00:00",
            "08:00:00",
            "2026-10-13T20:00:00+00:00",
        ),
        (
            "2026-03-29",
            "04:00:00+00:00",
            "06:00:00",
            "18:00:00",
            "2026-03-29T06:00:00+02:00",
        ),
        (
            "2026-10-25",
            "05:00:00+00:00",
            "06:00:00",
            "18:00:00",
            "2026-10-25T06:00:00+01:00",
        ),
    ],
)
async def test_overnight_and_berlin_cycles(
    engine, cycle, shot_clock, day, at, sunrise, sunset, expected
):
    await engine.hass.config.async_set_time_zone(
        "UTC" if sunrise == "20:00:00" else "Europe/Berlin"
    )
    engine.hass.states.async_set("input_datetime.sunrise", sunrise)
    engine.hass.states.async_set("input_datetime.sunset", sunset)
    shot_clock.move_to(f"{day}T{at}")
    await begin(cycle)
    assert cycle.cycle_id == expected
    if sunrise == "20:00:00":
        shot_clock.move_to("2026-10-14T01:00:00+00:00")
        assert (await cycle.async_observe()).cycle_id == expected


@pytest.mark.parametrize(
    "instant", ["2026-03-29T01:30:00+00:00", "2026-10-25T01:30:00+00:00"]
)
async def test_dst_ambiguous_schedule_blocks(engine, cycle, shot_clock, instant):
    await engine.hass.config.async_set_time_zone("Europe/Berlin")
    engine.hass.states.async_set("input_datetime.sunrise", "02:30:00")
    shot_clock.move_to(instant)
    assert not await cycle.async_sunrise()
    assert cycle.recovery_reason == "invalid_cycle_schedule"


async def test_scheduler_sole_timer_manages_phases(
    engine, cycle, shot_clock, monkeypatch
):
    hass = engine.hass
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
    scheduler = _NativeScheduler(hass, engine.entry, engine, cycle)
    await scheduler.async_initialize()
    try:
        await scheduler.async_start()
        await settle(hass)
        assert cycle.phase == "p0_transpiration"
        assert scheduler.next_evaluation.hour == 7
        await advance(hass, shot_clock, "2026-10-13T07:00:00+00:00")
        await wait_for(lambda: engine.state == ShotState.RUNNING)
        hass.states.async_set("sensor.vwc", "60")
        await wait_for(lambda: scheduler._task.done())
        assert engine.entry.options["p1_done"]
        assert scheduler.decision.phase == "p2_midday"
        assert scheduler._timer is not None
        assert not scheduler.retry_blocked
        assert len([call for call in engine.calls if call[0] == "turn_on"]) == 1
    finally:
        await scheduler.async_close()


async def test_unload_closes_cycle_before_coordinator(engine, cycle, monkeypatch):
    await begin(cycle)
    monkeypatch.setattr(
        engine.hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )
    assert await async_unload_entry(engine.hass, engine.entry)
    assert cycle._closed
    assert not engine.execution_available


async def test_reference_from_wrong_cycle_blocks(engine, cycle, shot_clock):
    await start(cycle, shot_clock)
    engine.hass.states.async_set("sensor.vwc", "60")
    await cycle.async_observe()
    cycle.record["reference_cycle"] = "2026-10-12T06:00:00+00:00"
    assert not engine.execution_available
    assert cycle.recovery_reason == "cycle_reference_stale"


async def test_storage_failure_blocks_without_options_change(
    engine, cycle, monkeypatch
):
    monkeypatch.setattr(
        cycle.store, "async_save", AsyncMock(side_effect=OSError("disk fault"))
    )
    with pytest.raises(OSError):
        await cycle.async_sunrise()
    assert cycle.recovery_reason
    assert cycle.record["pending"]
    assert not engine.execution_available
    assert engine.calls == []


@pytest.mark.parametrize(
    "entity,value",
    [
        ("sensor.vwc", "55"),
        ("sensor.vwc", "unavailable"),
        ("binary_sensor.drain", "on"),
        ("switch.pump", "on"),
    ],
)
async def test_readiness_revalidated_after_persistence(
    engine, cycle, shot_clock, monkeypatch, entity, value
):
    await begin(cycle)
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = cycle.store.async_save

    async def save(record):
        entered.set()
        await release.wait()
        await original_save(record)

    monkeypatch.setattr(cycle.store, "async_save", save)
    task = asyncio.create_task(cycle.async_observe())
    await entered.wait()
    engine.hass.states.async_set(entity, value)
    release.set()
    with pytest.raises(RuntimeError, match="readiness changed"):
        await task
    assert not engine.entry.options["p1_active"]
    assert cycle.recovery_reason
    assert engine.calls == []


async def test_completion_trigger_revalidated_after_persistence(
    engine, cycle, shot_clock, monkeypatch
):
    await start(cycle, shot_clock)
    engine.hass.states.async_set("binary_sensor.drain", "on")
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = cycle.store.async_save

    async def save(record):
        entered.set()
        await release.wait()
        await original_save(record)

    monkeypatch.setattr(cycle.store, "async_save", save)
    task = asyncio.create_task(cycle.async_observe())
    await entered.wait()
    engine.hass.states.async_set("binary_sensor.drain", "off")
    release.set()
    with pytest.raises(RuntimeError, match="completion condition changed"):
        await task
    assert not engine.entry.options["p1_done"]
    assert engine.entry.options["p2_ref_vwc"] == 0


async def test_new_cycle_during_final_accounting_compensates_old_shot(
    engine, cycle, shot_clock, monkeypatch
):
    await start(cycle, shot_clock)
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.store.async_save

    async def save(journal):
        if journal["operations"]["accounting"].get("accounted"):
            entered.set()
            await release.wait()
        await original(journal)

    monkeypatch.setattr(engine.store, "async_save", save)
    task = asyncio.create_task(engine.async_execute("accounting"))
    await entered.wait()
    assert engine.entry.options["p1_shots_done"] == 1
    shot_clock.move_to("2026-10-14T06:00:00+00:00")
    assert not await cycle.async_sunrise()
    release.set()
    result = await task
    assert not result.accounted
    assert engine.entry.options["p1_shots_done"] == 0
    assert engine._journal["native_counters"]["p1_shots_done"] == 0
    assert not engine.execution_available
    assert engine.hass.states.get("switch.pump").state == "off"


async def test_options_and_store_disagree_after_full_restart(
    engine, cycle, shot_clock, hass_storage
):
    await start(cycle, shot_clock)
    assert (await engine.async_execute("committed")).accounted
    key = f"{DOMAIN}.native_cycle.{engine.entry.entry_id}"
    persisted = deepcopy(hass_storage[key]["data"])
    await cycle.async_close()
    await engine.async_close()
    change(engine, p1_active=False)  # Config options restored inconsistently.
    pump = PumpController(engine.hass, engine.entry)
    await pump.async_initialize()
    restored_engine = _ShotCoordinator(engine.hass, engine.entry, pump)
    await restored_engine.async_initialize()
    restored = _NativeCycleController(restored_engine)
    await restored.async_initialize()
    assert restored.cycle_id == persisted["cycle_id"]
    assert restored.recovery_reason == "cycle_persistence_inconsistent"
    with pytest.raises(ShotRejected):
        await restored_engine.async_execute("must_not_replay")
    assert "committed" in restored_engine._journal["operations"]
    assert len([call for call in engine.calls if call[0] == "turn_on"]) == 1
    await restored.async_close()
    await restored_engine.async_close()


async def test_unresolved_storage_timeout_cannot_be_overtaken(
    engine, cycle, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []
    original_save = cycle.store.async_save

    async def save(record):
        saved.append(record)
        entered.set()
        await release.wait()
        await original_save(record)

    monkeypatch.setattr(cycle.store, "async_save", save)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.cycle.JOURNAL_TIMEOUT", 0.01
    )
    with pytest.raises(RuntimeError, match="journal_write_timeout"):
        await cycle.async_sunrise()
    assert entered.is_set()
    assert not await cycle.async_sunrise()
    assert len(saved) == 1
    assert not engine.execution_available
    release.set()
    await cycle._write


async def test_p2_limit_and_manual_mode_preserved(engine, cycle, shot_clock):
    change(engine, p2_shots=1)
    await start(cycle, shot_clock)
    engine.hass.states.async_set("sensor.vwc", "60")
    await cycle.async_observe()
    engine.hass.states.async_set("sensor.vwc", "55")
    assert (await engine.async_execute("p2")).accounted
    assert (await cycle.async_observe()).phase == "p3_dryback"
    assert engine.entry.options["p2_shots_done"] == 1


async def test_manual_p2_never_automatically_eligible(engine, cycle, shot_clock):
    change(engine, p2_mode="manual")
    await start(cycle, shot_clock)
    engine.hass.states.async_set("sensor.vwc", "60")
    await cycle.async_observe()
    engine.hass.states.async_set("sensor.vwc", "55")
    decision = await cycle.async_observe()
    assert "p2_manual" in decision.blocking_reasons
    with pytest.raises(ShotRejected, match="p2_manual"):
        await engine.async_execute()


async def test_available_sensor_mean_reference_preserved(engine, cycle, shot_clock):
    from custom_components.growassistant_crop_steering.native_safety import SensorPolicy

    engine.sensor_policy = SensorPolicy(minimum_percent=50)
    engine.hass.config_entries.async_update_entry(
        engine.entry,
        data={**engine.entry.data, "vwc_sensor": ["sensor.vwc", "sensor.missing"]},
    )
    await start(cycle, shot_clock)
    engine.hass.states.async_set("sensor.vwc", "60")
    await cycle.async_observe()
    assert engine.entry.options["p2_ref_vwc"] == 60


async def test_duplicate_cycle_owner_rejected(engine, cycle):
    second = _NativeCycleController(engine)
    with pytest.raises(RuntimeError, match="already owned"):
        await second.async_initialize()
    assert engine._cycle is cycle


async def test_scheduler_rejects_uninitialized_cycle(engine):
    cycle = _NativeCycleController(engine)
    scheduler = _NativeScheduler(engine.hass, engine.entry, engine, cycle)
    with pytest.raises(RuntimeError, match="cycle controller first"):
        await scheduler.async_initialize()
    assert not scheduler.running
    assert engine.calls == []


async def test_normal_setup_does_not_construct_cycle(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.growassistant_crop_steering import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        _NativeCycleController,
        "__init__",
        lambda *args: pytest.fail("production constructed native cycle"),
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering._CycleResetCoordinator.async_start",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    assert await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry)


@pytest.mark.parametrize(
    "date,first,next_at,elapsed_hours",
    [
        ("2026-03-28", "05:00:00", "2026-03-29T04:00:00+00:00", 23),
        ("2026-10-24", "04:00:00", "2026-10-25T05:00:00+00:00", 25),
    ],
)
async def test_berlin_sunrise_reset_across_dst(
    engine, cycle, shot_clock, date, first, next_at, elapsed_hours
):
    await engine.hass.config.async_set_time_zone("Europe/Berlin")
    shot_clock.move_to(f"{date}T{first}+00:00")
    await begin(cycle)
    previous = shot_clock.at
    old = cycle.cycle_id
    shot_clock.move_to(next_at)
    assert await cycle.async_sunrise()
    assert (
        shot_clock.at.astimezone(UTC) - previous.astimezone(UTC)
    ).total_seconds() == elapsed_hours * 3600
    assert cycle.record["previous_cycle_end"] == old
    assert cycle.cycle_id != old
