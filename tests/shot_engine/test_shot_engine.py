"""Exercise actual controller safety and accounting through local HA fixtures."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.growassistant_crop_steering import _reset_cycle_for_entry
from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.pump import PumpController, PumpState
from custom_components.growassistant_crop_steering.shot_engine import (
    ShotRejected,
    ShotState,
    _async_unload_shots,
    _ShotCoordinator,
)

pytestmark = pytest.mark.asyncio


def change(engine, **values):
    engine.hass.config_entries.async_update_entry(
        engine.entry, options={**engine.entry.options, **values}
    )


async def running(engine, operation="shot"):
    task = asyncio.create_task(engine.async_execute(operation))
    async with asyncio.timeout(2):
        while engine.state != ShotState.RUNNING:
            assert not task.done(), task.result() if task.done() else None
            await asyncio.sleep(0.001)
    return task


@pytest.mark.parametrize(
    "phase,prefix", [("p1", "p1"), ("p2", "p2"), ("p3", "p3_emergency")]
)
async def test_success(hass, engine, shot_clock, hass_storage, phase, prefix):
    if phase == "p2":
        change(engine, p1_active=False, p1_done=True)
    if phase == "p3":
        shot_clock.move_to("2026-10-13 19:00:00+00:00")
        hass.states.async_set("sensor.vwc", "25")
    result = await engine.async_execute("success")
    assert result.state == ShotState.COMPLETED
    assert result.accounted
    assert engine.entry.options[prefix + "_shots_done"] == 1
    assert engine.entry.options["last_shot_type"] == prefix
    assert hass.states.get("switch.pump").state == "off"
    assert engine.controller.recovery_record is None
    saved = hass_storage[f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"]["data"][
        "operations"
    ]["success"]
    assert saved["accounted"] and not saved["accounting_pending"]
    before = dict(engine.entry.options)
    with pytest.raises(ShotRejected, match="duplicate_operation"):
        await engine.async_execute("success")
    assert dict(engine.entry.options) == before


@pytest.mark.parametrize(
    "entity,state,reason",
    [
        ("binary_sensor.tray", "on", "drain_tray_sensor_wet"),
        ("binary_sensor.drain", "unavailable", "drain_sensor_unavailable"),
        ("sensor.vwc", "unavailable", "vwc_unavailable"),
        ("sensor.vwc", "60", "field_capacity_reached"),
        ("switch.pump", "on", "pump_not_off"),
    ],
)
async def test_reject_unsafe(hass, engine, entity, state, reason):
    hass.states.async_set(entity, state)
    with pytest.raises(ShotRejected, match=reason):
        await engine.async_execute()
    assert engine.calls == []


async def test_concurrent_and_cancellation(engine, hass):
    task = await running(engine)
    with pytest.raises(ShotRejected, match="shot_in_progress|execution_unavailable"):
        await engine.async_execute("other")
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.state == ShotState.INTERRUPTED
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert hass.states.get("switch.pump").state == "off"
    assert engine._journal["operations"]["shot"]["reason"] == "cancelled"


@pytest.mark.parametrize(
    "entity,state",
    [
        ("binary_sensor.tray", "on"),
        ("binary_sensor.drain", "unavailable"),
        ("sensor.vwc", "unavailable"),
        ("sensor.vwc", "nan"),
        ("switch.pump", "unavailable"),
    ],
)
async def test_active_safety(hass, engine, entity, state):
    task = await running(engine)
    hass.states.async_set(entity, state)
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert not result.accounted
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert hass.states.get("switch.pump").state == "off"


async def test_field_capacity_only_prevents_next(hass, engine):
    task = await running(engine)
    hass.states.async_set("sensor.vwc", "60")
    result = await task
    assert result.state == ShotState.COMPLETED
    with pytest.raises(ShotRejected, match="field_capacity_reached"):
        await engine.async_execute("next")


async def test_pump_reassignment_uses_original_owner(hass, engine):
    task = await running(engine)
    hass.states.async_set("switch.replacement", "off")
    change(engine, pump_switch="switch.replacement")
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert result.reason == "pump_reassigned"
    assert engine.snapshot.decision.pump_entity == "switch.pump"
    assert all(entity == "switch.pump" for _, entity in engine.calls)
    assert hass.states.get("switch.pump").state == "off"


@pytest.mark.parametrize("action", ["unload", "shutdown", "close"])
async def test_lifecycle_cancel(hass, engine, action):
    task = await running(engine)
    if action == "unload":
        assert await _async_unload_shots(hass, engine.entry)
    elif action == "shutdown":
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done(wait_background_tasks=True)
    else:
        assert await engine.async_close()
    assert (await task).state == ShotState.INTERRUPTED
    assert hass.states.get("switch.pump").state == "off"
    with pytest.raises(ShotRejected):
        await engine.async_execute("later")


async def test_cycle_reset_does_not_count_into_new_cycle(hass, engine):
    task = await running(engine)
    await _reset_cycle_for_entry(hass, engine.entry)
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert result.reason == "cycle_reset"
    assert engine.entry.options["p1_shots_done"] == 0
    assert engine.entry.options["p2_shots_done"] == 0
    assert "last_shot" not in engine.entry.options


async def test_cycle_rollover(hass, engine, shot_clock):
    task = await running(engine)
    shot_clock.move_to("2026-10-14 08:00:00+00:00")
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert result.reason == "cycle_changed"


async def test_start_failure(hass, engine):
    async def failed(call):
        raise RuntimeError("mock start failure")

    hass.services.async_register("switch", "turn_on", failed)
    result = await engine.async_execute()
    assert result.state == ShotState.FAULT
    assert not result.accounted
    assert engine.controller.state == PumpState.IDLE
    assert hass.states.get("switch.pump").state == "off"


async def test_stop_failure_blocks_execution(hass, engine):
    task = await running(engine)
    hass.services.async_register("switch", "turn_off", AsyncMock())
    engine._interrupt("test_stop")
    result = await task
    assert result.state == ShotState.FAULT
    assert not result.accounted
    assert engine.controller.state == PumpState.FAULT
    with pytest.raises(ShotRejected):
        await engine.async_execute("blocked")


async def test_late_start_never_success_or_replay(hass, engine):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", delayed)
    task = asyncio.create_task(engine.async_execute("late"))
    await entered.wait()
    result = await task
    assert not result.accounted
    assert result.state == ShotState.FAULT
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("switch.pump").state == "off"
    assert engine.controller.recovery_record is None
    with pytest.raises(ShotRejected):
        await engine.async_execute("late")


async def test_final_revalidation_after_pump_storage(hass, engine, monkeypatch):
    original = engine.controller._save_record

    async def save():
        await original()
        hass.states.async_set("binary_sensor.tray", "on")

    monkeypatch.setattr(engine.controller, "_save_record", save)
    result = await engine.async_execute()
    assert not result.accounted
    assert not any(action == "turn_on" for action, _ in engine.calls)


@pytest.mark.parametrize("stage", ["reserved", "starting", "running", "completed"])
async def test_restart_requires_review_no_accounting_replay(
    hass, engine, hass_storage, stage
):
    await engine.async_close()
    key = f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "operations": {
                "old": {
                    "state": stage,
                    "accounting_pending": stage == "completed",
                    "accounted": False,
                }
            }
        },
    }
    pump = PumpController(hass, engine.entry)
    await pump.async_initialize()
    restored = _ShotCoordinator(hass, engine.entry, pump)
    await restored.async_initialize()
    assert restored.state == ShotState.FAULT
    assert restored._journal["operations"]["old"]["state"] == "interrupted"
    with pytest.raises(ShotRejected):
        await restored.async_execute("never_replayed")
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert engine.calls == []
    await restored.async_close()


async def test_interruption_persists_and_duplicates_survive_restart(hass, engine):
    task = await running(engine, "interrupted")
    engine._interrupt("test")
    assert (await task).state == ShotState.INTERRUPTED
    await engine.async_close()
    pump = PumpController(hass, engine.entry)
    await pump.async_initialize()
    restored = _ShotCoordinator(hass, engine.entry, pump)
    await restored.async_initialize()
    with pytest.raises(ShotRejected, match="duplicate_operation"):
        await restored.async_execute("interrupted")
    assert restored._journal["operations"]["interrupted"]["accounted"] is False
    await restored.async_close()


async def test_journal_failure_prevents_on(engine, monkeypatch):
    monkeypatch.setattr(
        engine.store, "async_save", AsyncMock(side_effect=OSError("disk fault"))
    )
    result = await engine.async_execute()
    assert result.state == ShotState.FAULT
    assert not result.accounted
    assert engine.calls == []
    with pytest.raises(ShotRejected):
        await engine.async_execute("second")


async def test_duplicate_completion_callbacks(engine):
    result = await engine.async_execute("once")
    before = dict(engine.entry.options)
    repeated = await asyncio.gather(
        engine._finish(engine.snapshot, True), engine._finish(engine.snapshot, True)
    )
    assert repeated == [result, result]
    assert dict(engine.entry.options) == before
    assert engine.entry.options["p1_shots_done"] == 1


async def test_transient_drain_fault_is_latched(hass, engine):
    task = await running(engine)
    hass.states.async_set("binary_sensor.tray", "on")
    hass.states.async_set("binary_sensor.tray", "off")
    assert (await task).state == ShotState.INTERRUPTED


async def test_external_pump_off_is_not_success(hass, engine):
    task = await running(engine)
    hass.states.async_set("switch.pump", "off")
    assert (await task).state == ShotState.INTERRUPTED


async def test_delayed_but_confirmed_activation(hass, engine):
    async def delayed(call):
        await asyncio.sleep(0.02)
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", delayed)
    result = await engine.async_execute()
    assert result.state == ShotState.COMPLETED
    assert engine.entry.options["p1_shots_done"] == 1
    assert hass.states.get("switch.pump").state == "off"


async def test_cancel_during_activation_drains_late_on(hass, engine):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", delayed)
    task = asyncio.create_task(engine.async_execute("cancel_start"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("switch.pump").state == "off"
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert engine.state != ShotState.COMPLETED


@pytest.mark.parametrize("stage", ["prepare", "commit"])
async def test_cancel_during_accounting_never_counts(engine, monkeypatch, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.store.async_save

    async def delayed(data):
        record = data["operations"]["accounting"]
        if record["state"] == "completed" and (
            record.get("accounted", False) == (stage == "commit")
        ):
            entered.set()
            await release.wait()
        await original(data)

    monkeypatch.setattr(engine.store, "async_save", delayed)
    task = asyncio.create_task(engine.async_execute("accounting"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert "last_shot" not in engine.entry.options
    assert engine._journal["operations"]["accounting"]["state"] == "interrupted"
    assert engine._journal["operations"]["accounting"]["accounted"] is False


async def test_counter_changed_does_not_overwrite(engine):
    task = await running(engine)
    change(engine, p1_shots_done=3)
    result = await task
    assert result.state == ShotState.FAULT
    assert not result.accounted
    assert engine.entry.options["p1_shots_done"] == 3


async def test_configuration_invalid_midshot(engine):
    task = await running(engine)
    change(engine, field_capacity_vwc="invalid")
    assert (await task).state == ShotState.INTERRUPTED


async def test_new_phase_eligibility_does_not_interrupt(engine):
    task = await running(engine)
    change(engine, p1_active=False, p1_done=True)
    assert (await task).state == ShotState.COMPLETED
    assert engine.entry.options["last_shot_type"] == "p1"
    assert engine.entry.options["p1_shots_done"] == 1


async def test_successful_journal_but_options_not_persisted_requires_review(
    hass, engine
):
    before = dict(engine.entry.options)
    assert (await engine.async_execute("done")).accounted
    await engine.async_close()
    hass.config_entries.async_update_entry(engine.entry, options=before)
    pump = PumpController(hass, engine.entry)
    await pump.async_initialize()
    restored = _ShotCoordinator(hass, engine.entry, pump)
    await restored.async_initialize()
    assert restored.state == ShotState.FAULT
    with pytest.raises(ShotRejected):
        await restored.async_execute("later")
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    await restored.async_close()


async def test_controller_fault_interrupts_and_does_not_count(engine):
    task = await running(engine)
    engine.controller._set_fault("simulated runtime fault")
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert not result.accounted


async def test_activation_overrun_never_starts(hass, engine, monkeypatch):
    original = engine.controller._save_record

    async def delayed():
        await original()
        await asyncio.sleep(1.03)

    monkeypatch.setattr(engine.controller, "_save_record", delayed)
    result = await engine.async_execute("overrun")
    assert not result.accounted
    assert result.reason == "deadline_exceeded"
    assert not any(action == "turn_on" for action, _ in engine.calls)
    assert hass.states.get("switch.pump").state == "off"


async def test_delayed_stop_never_counts_until_confirmed(hass, engine):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_off(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "off", force_update=True)

    task = await running(engine)
    hass.services.async_register("switch", "turn_off", delayed_off)
    await entered.wait()
    assert not task.done()
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    release.set()
    result = await task
    assert result.state == ShotState.COMPLETED
    assert result.accounted
    assert hass.states.get("switch.pump").state == "off"


async def test_journal_timeout_late_write_remains_blocked(engine, monkeypatch):
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.shot_engine.JOURNAL_TIMEOUT",
        0.02,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.store.async_save

    async def delayed(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(engine.store, "async_save", delayed)
    task = asyncio.create_task(engine.async_execute("storage_timeout"))
    await entered.wait()
    result = await task
    assert result.state == ShotState.FAULT
    assert engine.calls == []
    release.set()
    await engine.hass.async_block_till_done(wait_background_tasks=True)
    with pytest.raises(ShotRejected):
        await engine.async_execute("cannot_overwrite")


async def test_final_accounting_persistence_failure_compensates(engine, monkeypatch):
    original = engine.store.async_save

    async def failed(data):
        if data["operations"]["failed_commit"].get("accounted"):
            raise OSError("final commit failed")
        await original(data)

    monkeypatch.setattr(engine.store, "async_save", failed)
    result = await engine.async_execute("failed_commit")
    assert result.state == ShotState.FAULT
    assert not result.accounted
    assert engine.entry.options.get("p1_shots_done", 0) == 0
    assert "last_shot" not in engine.entry.options
    with pytest.raises(ShotRejected):
        await engine.async_execute("blocked")


async def test_real_integration_reload_cancels_native_and_keeps_it_disabled(
    hass, engine, enable_custom_integrations
):
    from homeassistant.setup import async_setup_component

    from custom_components.growassistant_crop_steering import shot_engine

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    # Loading HA's switch platform installs its entity service dispatcher.
    # Reattach the simulated external pump after platform setup.
    for action in ("turn_on", "turn_off"):
        hass.services.async_register("switch", action, engine.simulated_service)
    change(engine, p1_active=True)
    task = await running(engine, "reload")

    assert await hass.config_entries.async_reload(engine.entry.entry_id)
    result = await task
    assert result.state == ShotState.INTERRUPTED
    assert not result.accounted
    assert hass.states.get("switch.pump").state == "off"
    assert engine.entry.entry_id not in hass.data.get(shot_engine._KEY, {})
    assert not hass.services.has_service(DOMAIN, "execute_shot")
    assert not hass.services.has_service(DOMAIN, "start_shot")
    assert engine.controller.state == PumpState.CLOSED
    assert await hass.config_entries.async_unload(engine.entry.entry_id)


async def test_next_cycle_requires_reset(engine, shot_clock):
    shot_clock.move_to("2026-10-14 08:00:00+00:00")
    with pytest.raises(ShotRejected, match="cycle_reset_required"):
        await engine.async_execute("new_cycle")
    await _reset_cycle_for_entry(engine.hass, engine.entry)
    change(engine, p1_active=True)
    assert (await engine.async_execute("after_reset")).state == ShotState.COMPLETED


async def test_reservation_pump_reassignment_cannot_start_replacement(
    hass, engine, monkeypatch
):
    original = engine.controller._save_record

    async def save():
        await original()
        hass.states.async_set("switch.replacement", "off")
        change(engine, pump_switch="switch.replacement")

    monkeypatch.setattr(engine.controller, "_save_record", save)
    result = await engine.async_execute("reservation")
    assert not result.accounted
    assert not any(action == "turn_on" for action, _ in engine.calls)
    assert all(entity == "switch.pump" for _, entity in engine.calls)


async def test_delayed_legacy_reset_does_not_delay_native_shutdown(hass, engine):
    hass.states.async_set("counter.legacy", "0")
    hass.config_entries.async_update_entry(
        engine.entry, data={**engine.entry.data, "p1_shots_done": "counter.legacy"}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_reset(call):
        entered.set()
        await release.wait()

    hass.services.async_register("counter", "reset", delayed_reset)
    shot = await running(engine)
    reset = asyncio.create_task(_reset_cycle_for_entry(hass, engine.entry))
    await entered.wait()
    try:
        result = await asyncio.wait_for(shot, 2)
        assert result.state == ShotState.INTERRUPTED
        assert not reset.done()
        assert hass.states.get("switch.pump").state == "off"
    finally:
        release.set()
        await reset
    assert engine.entry.options["p1_shots_done"] == 0


async def test_repeated_cancellation_during_shutdown_waits_for_off(hass, engine):
    task = await running(engine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_off(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "off", force_update=True)

    hass.services.async_register("switch", "turn_off", delayed_off)
    task.cancel()
    await entered.wait()
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.state == ShotState.INTERRUPTED
    assert hass.states.get("switch.pump").state == "off"
    assert engine.entry.options.get("p1_shots_done", 0) == 0
