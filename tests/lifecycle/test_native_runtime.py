"""Compose actual native controllers on HA entries with simulated services only."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import ServiceRegistry

from custom_components import growassistant_crop_steering as integration
from custom_components.growassistant_crop_steering.native_runtime import (
    NativeRuntime,
)
from custom_components.growassistant_crop_steering.native_runtime import (
    NativeRuntimeState as State,
)
from custom_components.growassistant_crop_steering.native_safety import (
    OwnershipEvidence,
)
from custom_components.growassistant_crop_steering.pump import (
    _OWNER_KEY,
    PumpController,
)

# Reuse the native controller suite's civil clock; monotonic time stays real.
from tests.shot_engine import conftest as native_fixtures

shot_clock = native_fixtures.shot_clock

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def lifecycle_environment(enable_custom_integrations, shot_clock):
    """Override the Legacy suite's freezer for actual service timeouts."""


@pytest.fixture
async def runtime(hass, entry, monkeypatch):
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.SERVICE_TIMEOUT", 0.03
    )
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.03
    )
    pump = PumpController(hass, entry)
    pump.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    result = NativeRuntime(hass, entry, pump=pump)
    yield result

    # Repair simulated failures before HA's shutdown event.
    async def off(call):
        hass.states.async_set("switch.pump", "off", force_update=True)

    hass.services.async_register("switch", "turn_off", off)
    await result.async_close()


async def initialized(runtime):
    await runtime.async_initialize()
    return runtime


async def test_construction_and_stopped_initialization(
    hass, entry, runtime, monkeypatch
):
    dispatch = AsyncMock(side_effect=AssertionError("No service during setup"))
    monkeypatch.setattr(ServiceRegistry, "async_call", dispatch)
    assert runtime.state == State.CREATED
    assert entry.runtime_data is runtime
    assert _OWNER_KEY not in hass.data
    assert not runtime.shots.initialized
    await initialized(runtime)
    assert runtime.state == State.READY_STOPPED
    assert hass.data[_OWNER_KEY] is runtime.pump
    assert runtime.shots.initialized
    assert runtime.shots._cycle is runtime.cycle
    assert runtime.scheduler.cycle is runtime.cycle
    assert not runtime.scheduler.running
    assert runtime.scheduler._timer is None
    assert runtime.scheduler._pending is None
    assert runtime.scheduler._task is None
    assert runtime.scheduler._listeners == []
    assert runtime.scheduler.decision is None
    assert runtime.shots.snapshot is None
    assert runtime.pump._deadline_handle is None
    dispatch.assert_not_awaited()
    assert await runtime.async_close()
    assert await runtime.async_close()
    assert runtime.state == State.CLOSED
    assert _OWNER_KEY not in hass.data
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("stage", range(4))
@pytest.mark.parametrize("after", [False, True])
async def test_failure_at_each_stage_rolls_back(runtime, monkeypatch, stage, after):
    controllers = [runtime.pump, runtime.shots, runtime.cycle, runtime.scheduler]
    events = []
    for index, controller in enumerate(controllers):
        original_init = controller.async_initialize
        original_close = controller.async_close

        async def initialize(i=index, method=original_init):
            events.append(("init", i))
            if i == stage and not after:
                raise RuntimeError("injected")
            await method()
            if i == stage:
                raise RuntimeError("injected")

        async def close(i=index, method=original_close):
            events.append(("close", i))
            return await method()

        monkeypatch.setattr(controller, "async_initialize", initialize)
        monkeypatch.setattr(controller, "async_close", close)
    with pytest.raises(RuntimeError, match="injected"):
        await runtime.async_initialize()
    assert events[: stage + 1] == [("init", i) for i in range(stage + 1)]
    # Shot close owns pump close; no second controller system is used.
    assert events[stage + 1 :] == [("close", i) for i in range(stage, -1, -1)]
    assert runtime.state == State.CLOSED
    assert "injected" in runtime.error
    assert _OWNER_KEY not in runtime.hass.data
    assert await runtime.async_close()


async def test_requires_simulation_before_recovery(runtime):
    runtime.pump.ownership = OwnershipEvidence("switch.pump", True, True, True, False)
    with pytest.raises(ValueError, match="simulated"):
        await runtime.async_initialize()
    assert runtime.state == State.CREATED
    assert _OWNER_KEY not in runtime.hass.data


async def test_no_second_runtime_or_owner(hass, entry, runtime):
    with pytest.raises(RuntimeError, match="still owns"):
        NativeRuntime(hass, entry, pump=PumpController(hass, entry))
    await initialized(runtime)
    with pytest.raises(Exception, match="Another native pump"):
        await PumpController(hass, entry).async_initialize()
    assert hass.data[_OWNER_KEY] is runtime.pump


async def test_normal_setup_never_constructs_native(hass, entry, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Native construction in legacy setup")

    monkeypatch.setattr(PumpController, "__init__", forbidden)
    dispatch = AsyncMock(side_effect=AssertionError("Unexpected pump command"))
    monkeypatch.setattr(ServiceRegistry, "async_call", dispatch)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert getattr(entry, "runtime_data", None) is None
    assert _OWNER_KEY not in hass.data
    assert await hass.config_entries.async_unload(entry.entry_id)
    dispatch.assert_not_awaited()


async def test_clean_reload_and_stale_callbacks(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    first_pump = PumpController(hass, entry)
    first_pump.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    first = NativeRuntime(hass, entry, pump=first_pump)
    await initialized(first)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert first.state == State.CLOSED
    second_pump = PumpController(hass, entry)
    second_pump.ownership = first_pump.ownership
    second = NativeRuntime(hass, entry, pump=second_pump)
    await initialized(second)
    first.scheduler.async_request_evaluation()
    await first.scheduler.async_close()
    await first.pump.async_close()
    await first.shots.async_close()
    await hass.async_block_till_done()
    assert hass.data[_OWNER_KEY] is second.pump
    assert second.state == State.READY_STOPPED
    assert second.scheduler._task is None
    assert second.shots.snapshot is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unconfirmed_off_blocks_reload(hass, entry, runtime):
    await initialized(runtime)

    async def service(call):
        hass.states.async_set("switch.pump", "on", force_update=True)

    hass.services.async_register("switch", "turn_on", service)
    hass.services.async_register("switch", "turn_off", AsyncMock())
    await runtime.pump.async_activate(10)
    assert not await integration.async_unload_entry(hass, entry)
    assert runtime.state == State.BLOCKED
    assert runtime.error
    assert hass.data[_OWNER_KEY] is runtime.pump
    assert not await integration.async_setup_entry(hass, entry)
    with pytest.raises(RuntimeError, match="still owns"):
        NativeRuntime(hass, entry, pump=PumpController(hass, entry))
    assert not await runtime.async_close()


@pytest.mark.parametrize(
    "error", [RuntimeError("close failed"), asyncio.CancelledError("close failed")]
)
async def test_close_exception_still_closes_lower_stages(runtime, monkeypatch, error):
    await initialized(runtime)
    original = runtime.scheduler.async_close
    monkeypatch.setattr(
        runtime.scheduler,
        "async_close",
        AsyncMock(side_effect=error),
    )
    assert not await runtime.async_close()
    assert runtime.state == State.BLOCKED
    assert "close failed" in runtime.error
    assert _OWNER_KEY not in runtime.hass.data
    monkeypatch.setattr(runtime.scheduler, "async_close", original)
    assert await runtime.async_close()


@pytest.mark.parametrize("participant", ["cycle", "shots"])
async def test_pending_write_retains_owner(runtime, monkeypatch, participant):
    await initialized(runtime)
    release = asyncio.Event()
    pending = asyncio.create_task(release.wait())
    controller = getattr(runtime, participant)
    attr = "_write" if participant == "cycle" else "_storage_task"
    monkeypatch.setattr(controller, attr, pending)
    if participant == "cycle":
        monkeypatch.setattr(
            "custom_components.growassistant_crop_steering.cycle.JOURNAL_TIMEOUT", 0.01
        )
    assert not await runtime.async_close()
    assert runtime.state == State.BLOCKED
    assert not pending.done()
    assert runtime.hass.data[_OWNER_KEY] is runtime.pump
    release.set()
    await pending
    assert await runtime.async_close()


async def test_unload_drains_active_simulated_shot(hass, entry, runtime, shot_clock):
    # Prepare an actual clean cycle at sunrise; never start the scheduler.
    shot_clock.move_to("2026-10-13 08:00:00+00:00")
    hass.config_entries.async_update_entry(
        entry,
        options={
            "p1_active": False,
            "p1_done": False,
            "p1_window_opened_today": False,
            "p1_shots_done": 0,
            "p2_shots_done": 0,
            "p3_emergency_shots_done": 0,
            "p2_ref_vwc": 0,
            "p0_transpiration_min": 0,
            "p1_duration_min": 120,
            "p1_shot_duration_s": 10,
        },
    )
    hass.states.async_set("sensor.vwc", "40", force_update=True)
    calls = []
    active = asyncio.Event()

    async def service(call):
        calls.append(call.service)
        hass.states.async_set(
            "switch.pump",
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )
        if call.service == "turn_on":
            active.set()

    for action in ("turn_on", "turn_off"):
        hass.services.async_register("switch", action, service)
    await initialized(runtime)
    assert await runtime.cycle.async_sunrise()
    await runtime.cycle.async_observe()
    operation = asyncio.create_task(runtime.shots.async_execute())
    await asyncio.wait_for(active.wait(), 2)
    assert await integration.async_unload_entry(hass, entry)
    result = await operation
    assert not result.accounted
    assert runtime.state == State.CLOSED
    assert calls.count("turn_on") == 1
    assert "turn_off" in calls
    assert not entry.options["p1_shots_done"]
    assert await runtime.async_close()
    assert calls.count("turn_on") == 1


async def test_pending_service_keeps_old_owner_until_late_off(
    hass, entry, runtime, shot_clock
):
    await initialized(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def service(call):
        calls.append(call.service)
        if call.service == "turn_on":
            entered.set()
            await release.wait()
        shot_clock.move_to((shot_clock.at + timedelta(milliseconds=1)).isoformat())
        hass.states.async_set(
            "switch.pump",
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )

    for action in ("turn_on", "turn_off"):
        hass.services.async_register("switch", action, service)
    activation = asyncio.create_task(runtime.pump.async_activate(10))
    await entered.wait()
    assert not await runtime.async_close()
    assert hass.data[_OWNER_KEY] is runtime.pump
    assert any(not task.done() for task in runtime.pump._pending)
    assert not await integration.async_setup_entry(hass, entry)
    release.set()
    with pytest.raises(Exception, match="Activation failed"):
        await activation
    # HA's block_till_done excludes background safety tasks. Drain the actual
    # late-service reconciliation before asking for a new close confirmation.
    async with asyncio.timeout(2):
        while runtime.pump._safety_tasks:
            await asyncio.gather(*tuple(runtime.pump._safety_tasks))
            await asyncio.sleep(0)
    assert hass.states.get("switch.pump").state == "off"
    assert calls.count("turn_on") == 1
    assert await runtime.async_close()
    assert _OWNER_KEY not in hass.data


async def test_cancel_initialization_rolls_back(runtime, monkeypatch):
    entered = asyncio.Event()
    original = runtime.cycle.async_initialize

    async def initialize():
        await original()
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.cycle, "async_initialize", initialize)
    task = asyncio.create_task(runtime.async_initialize())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.state == State.CLOSED
    assert _OWNER_KEY not in runtime.hass.data


async def test_real_ha_reload_rejects_unconfirmed_shutdown(hass, entry, monkeypatch):
    assert await hass.config_entries.async_setup(entry.entry_id)
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.02
    )
    pump = PumpController(hass, entry)
    pump.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    runtime = NativeRuntime(hass, entry, pump=pump)
    await initialized(runtime)

    async def on(call):
        hass.states.async_set("switch.pump", "on", force_update=True)

    async def off(call):
        hass.states.async_set("switch.pump", "off", force_update=True)

    hass.services.async_register("switch", "turn_on", on)
    hass.services.async_register("switch", "turn_off", AsyncMock())
    await pump.async_activate(10)
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data is runtime
    assert runtime.state == State.BLOCKED
    assert hass.data[_OWNER_KEY] is pump
    with pytest.raises(RuntimeError, match="still owns"):
        NativeRuntime(hass, entry, pump=PumpController(hass, entry))
    hass.services.async_register("switch", "turn_off", off)
    assert await runtime.async_close()
    assert entry.state == ConfigEntryState.FAILED_UNLOAD
    with pytest.raises(OperationNotAllowed):
        await hass.config_entries.async_reload(entry.entry_id)
    # HA intentionally requires restart after FAILED_UNLOAD; clean fixture-owned
    # platforms directly without inventing a production retry/recovery path.
    assert await integration.async_unload_entry(hass, entry)


async def test_concurrent_close_finishes_despite_caller_cancellation(
    runtime, monkeypatch
):
    await initialized(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = runtime.pump.async_close
    calls = []

    async def close():
        calls.append("close")
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(runtime.pump, "async_close", close)
    first = asyncio.create_task(runtime.async_close())
    await entered.wait()
    second = asyncio.create_task(runtime.async_close())
    first.cancel()
    assert runtime.state == State.STOPPING
    assert not first.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second
    assert runtime.state == State.CLOSED
    assert calls == ["close"]
