"""Cancellation, timeout, recovery and shutdown races without physical devices."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError

from custom_components.growassistant_crop_steering.const import CONF_PUMP_SWITCH, DOMAIN
from custom_components.growassistant_crop_steering.pump import (
    HARD_MAX_RUNTIME,
    PumpController,
    PumpError,
    PumpState,
)

pytestmark = pytest.mark.asyncio


async def test_normal_and_repeated_stop(hass, controller, hass_storage):
    assert controller.state == PumpState.IDLE
    operation = await controller.async_activate(10)
    record = controller.recovery_record
    assert record["operation_id"] == operation
    assert record["entity_id"] == "switch.pump"
    assert record["activated_at"] < record["deadline"]
    assert record["state"] == "running"
    assert hass.states.get("switch.pump").state == "on"
    assert await controller.async_stop()
    assert await controller.async_stop()
    assert controller.recovery_record is None
    assert controller.state == PumpState.IDLE
    assert (
        hass_storage[f"{DOMAIN}.pump_recovery.{controller.entry.entry_id}"]["data"]
        is None
    )


@pytest.mark.parametrize(
    "runtime", [0, -1, HARD_MAX_RUNTIME + 1, float("nan"), float("inf"), True, "1"]
)
async def test_invalid_runtime(controller, runtime):
    with pytest.raises(PumpError):
        await controller.async_activate(runtime)
    assert controller.state == PumpState.IDLE
    assert controller.recovery_record is None


async def test_runtime_deadline(hass, controller):
    await controller.async_activate(0.02)
    await asyncio.sleep(0.05)
    assert hass.states.get("switch.pump").state == "off"
    assert controller.state == PumpState.IDLE
    assert controller._deadline_handle is None


@pytest.mark.parametrize(
    "entity,state",
    [
        ("sensor.pump", "off"),
        ("not valid", "off"),
        ("switch.missing", None),
        ("switch.pump", "unavailable"),
        ("switch.pump", "unknown"),
        ("switch.pump", "on"),
    ],
)
async def test_invalid_or_unavailable_entity(hass, controller, entity, state):
    hass.config_entries.async_update_entry(
        controller.entry, options={CONF_PUMP_SWITCH: entity}
    )
    if state is not None and entity == "switch.pump":
        hass.states.async_set(entity, state)
    with patch.object(controller, "_service", new_callable=AsyncMock) as service:
        with pytest.raises(PumpError):
            await controller.async_activate(1)
        service.assert_not_awaited()


async def test_missing_services(hass, controller):
    hass.services.async_remove("switch", "turn_off")
    with pytest.raises(PumpError):
        await controller.async_activate(1)


async def test_input_boolean_selector_compatibility(hass, controller):
    hass.states.async_set("input_boolean.pump", "off")
    hass.config_entries.async_update_entry(
        controller.entry, options={CONF_PUMP_SWITCH: "input_boolean.pump"}
    )

    async def service(call):
        hass.states.async_set(
            call.data["entity_id"],
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )

    for action in ("turn_on", "turn_off"):
        hass.services.async_register("input_boolean", action, service)
    await controller.async_activate(1)
    assert await controller.async_stop()


async def test_pre_activation_record_is_saved(hass, controller, hass_storage):
    async def turn_on(call):
        saved = hass_storage[f"{DOMAIN}.pump_recovery.{controller.entry.entry_id}"][
            "data"
        ]
        assert saved["state"] == "starting"
        assert saved["entity_id"] == "switch.pump"
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", turn_on)
    await controller.async_activate(1)
    assert await controller.async_stop()


async def test_service_failure_reconciles(hass, controller):
    async def broken(call):
        hass.states.async_set("switch.pump", "on")
        raise HomeAssistantError("delivery may have occurred")

    hass.services.async_register("switch", "turn_on", broken)
    with pytest.raises(PumpError):
        await controller.async_activate(1)
    assert hass.states.get("switch.pump").state == "off"
    assert controller.state == PumpState.IDLE


async def test_unconfirmed_activation(hass, controller):
    hass.services.async_register("switch", "turn_on", AsyncMock())
    with pytest.raises(PumpError, match="not confirmed"):
        await controller.async_activate(1)
    assert controller.state == PumpState.IDLE


async def test_late_activation_cannot_be_cleared_by_earlier_off(hass, controller):
    release = asyncio.Event()

    async def delayed(call):
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", delayed)
    with pytest.raises(PumpError, match="timeout"):
        await controller.async_activate(1)
    assert controller.state == PumpState.FAULT
    assert controller.recovery_record["entity_id"] == "switch.pump"
    with pytest.raises(PumpError):
        await controller.async_activate(1)
    release.set()
    await hass.async_block_till_done()
    # Late activation completion itself triggers a second off reconciliation.
    assert hass.states.get("switch.pump").state == "off"
    assert await controller.async_stop()
    assert hass.states.get("switch.pump").state == "off"


@pytest.mark.parametrize("failure", ["error", "no_state", "unavailable"])
async def test_shutdown_faults(hass, controller, failure):
    await controller.async_activate(1)

    async def broken(call):
        if failure == "error":
            raise HomeAssistantError("failed")
        if failure == "unavailable":
            hass.states.async_set("switch.pump", "unavailable")

    hass.services.async_register("switch", "turn_off", broken)
    assert not await controller.async_stop()
    assert controller.state == PumpState.FAULT
    assert controller.recovery_record["state"] == "fault"
    with pytest.raises(PumpError):
        await controller.async_activate(1)


async def test_stale_off_is_not_confirmation(hass, controller):
    await controller.async_activate(1)
    hass.states.async_set("switch.pump", "off")
    hass.services.async_register("switch", "turn_off", AsyncMock())
    assert not await controller.async_stop()
    assert controller.state == PumpState.FAULT


async def test_off_timeout_even_with_fresh_off(hass, controller):
    release = asyncio.Event()
    await controller.async_activate(1)

    async def delayed(call):
        hass.states.async_set("switch.pump", "off", force_update=True)
        await release.wait()

    hass.services.async_register("switch", "turn_off", delayed)
    assert not await controller.async_stop()
    assert controller.state == PumpState.FAULT
    release.set()
    await hass.async_block_till_done()
    assert await controller.async_stop()


async def test_concurrent_activation_rejected(controller):
    results = await asyncio.gather(
        controller.async_activate(1),
        controller.async_activate(1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, PumpError) for result in results) == 1
    assert await controller.async_stop()


async def test_reassignment_uses_original_target(hass, controller):
    await controller.async_activate(1)
    record = controller.recovery_record
    record["entity_id"] = "switch.other"  # Public snapshot cannot modify ownership.
    hass.states.async_set("switch.other", "on")
    hass.config_entries.async_update_entry(
        controller.entry, options={CONF_PUMP_SWITCH: "switch.other"}
    )
    assert await controller.async_stop()
    assert hass.states.get("switch.pump").state == "off"
    assert hass.states.get("switch.other").state == "on"


async def test_cancel_activation(hass, controller):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", delayed)
    activation = asyncio.create_task(controller.async_activate(1))
    await entered.wait()
    activation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await activation
    assert hass.states.get("switch.pump").state == "off"
    assert controller.state == PumpState.IDLE


async def test_repeated_cancel_during_shutdown(hass, controller):
    entered, release = asyncio.Event(), asyncio.Event()
    await controller.async_activate(1)

    async def delayed(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "off")

    hass.services.async_register("switch", "turn_off", delayed)
    stop = asyncio.create_task(controller.async_stop())
    await entered.wait()
    stop.cancel()
    await asyncio.sleep(0)
    stop.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert controller.state == PumpState.IDLE
    assert hass.states.get("switch.pump").state == "off"


async def test_concurrent_stops_dispatch_once(hass, controller):
    await controller.async_activate(1)
    calls = []

    async def off(call):
        calls.append(call)
        await asyncio.sleep(0)
        hass.states.async_set("switch.pump", "off")

    hass.services.async_register("switch", "turn_off", off)
    assert all(await asyncio.gather(*(controller.async_stop() for _ in range(5))))
    assert len(calls) == 1


async def test_unload_reload(hass, controller):
    await controller.async_activate(1)
    assert await controller.async_close()
    assert controller.state == PumpState.CLOSED
    assert controller._remove_shutdown is None
    with pytest.raises(PumpError):
        await controller.async_activate(1)
    replacement = PumpController(hass, controller.entry)
    await replacement.async_initialize()
    assert replacement.state == PumpState.IDLE
    assert hass.states.get("switch.pump").state == "off"
    await replacement.async_close()


async def test_ha_shutdown(hass, controller):
    await controller.async_activate(1)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert controller.state == PumpState.CLOSED
    assert hass.states.get("switch.pump").state == "off"


async def test_restart_reconciles_recorded_target(hass, controller, hass_storage):
    await controller.async_activate(1)
    record = controller.recovery_record
    assert await controller.async_close()
    # Restore the pre-crash durable record; no live task from the old process.
    key = f"{DOMAIN}.pump_recovery.{controller.entry.entry_id}"
    hass_storage[key]["data"] = record
    hass.states.async_set("switch.pump", "on")
    hass.config_entries.async_update_entry(
        controller.entry, options={CONF_PUMP_SWITCH: "switch.reassigned"}
    )
    replacement = PumpController(hass, controller.entry)
    with patch.object(replacement, "_service", wraps=replacement._service) as calls:
        await replacement.async_initialize()
        assert all(call.args[1] == "turn_off" for call in calls.call_args_list)
        assert calls.call_args.args[0] == "switch.pump"
    assert replacement.state == PumpState.IDLE
    assert replacement.recovery_record is None
    await replacement.async_close()


async def test_failed_recovery_retains_target(hass, controller, hass_storage):
    await controller.async_activate(1)
    record = controller.recovery_record
    await controller.async_close()
    hass_storage[f"{DOMAIN}.pump_recovery.{controller.entry.entry_id}"]["data"] = record
    hass.states.async_set("switch.pump", "unavailable")
    hass.services.async_register("switch", "turn_off", AsyncMock())
    replacement = PumpController(hass, controller.entry)
    await replacement.async_initialize()
    assert replacement.state == PumpState.FAULT
    assert replacement.recovery_record["entity_id"] == "switch.pump"
    with pytest.raises(PumpError):
        await replacement.async_activate(1)
    await replacement.async_close()


async def test_storage_save_failure_never_activates(hass, controller):
    with (
        patch.object(controller.store, "async_save", side_effect=OSError("disk full")),
        patch.object(controller, "_service", wraps=controller._service) as calls,
    ):
        with pytest.raises(PumpError):
            await controller.async_activate(1)
        assert all(call.args[1] == "turn_off" for call in calls.call_args_list)
    assert controller.state == PumpState.FAULT
    assert await controller.async_stop()


async def test_durable_clear_failure_retains_target(controller):
    await controller.async_activate(1)
    save = controller.store.async_save

    async def fail_clear(data):
        if data is None:
            raise OSError("disk full")
        await save(data)

    with patch.object(controller.store, "async_save", side_effect=fail_clear):
        assert not await controller.async_stop()
    assert controller.state == PumpState.FAULT
    assert controller.recovery_record is not None
    assert await controller.async_stop()


async def test_load_failure_blocks(hass, controller):
    await controller.async_close()
    replacement = PumpController(hass, controller.entry)
    with patch.object(
        replacement.store, "async_load", side_effect=OSError("cannot load")
    ):
        await replacement.async_initialize()
    assert replacement.state == PumpState.FAULT
    assert not await replacement.async_stop()
    with pytest.raises(PumpError):
        await replacement.async_activate(1)
    await replacement.async_close()


async def test_multiple_owners_rejected(hass, controller):
    replacement = PumpController(hass, controller.entry)
    with pytest.raises(PumpError, match="Another"):
        await replacement.async_initialize()


async def test_cancel_before_activation_dispatch(hass, controller):
    entered, release = asyncio.Event(), asyncio.Event()
    save = controller.store.async_save

    async def delayed_save(data):
        if data and data["state"] == "starting":
            entered.set()
            await release.wait()
        await save(data)

    with (
        patch.object(controller.store, "async_save", side_effect=delayed_save),
        patch.object(controller, "_service", wraps=controller._service) as calls,
    ):
        activation = asyncio.create_task(controller.async_activate(1))
        await entered.wait()
        activation.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await activation
        assert all(call.args[1] == "turn_off" for call in calls.call_args_list)
    assert controller.state == PumpState.IDLE


async def test_stop_during_activation(hass, controller):
    entered, release = asyncio.Event(), asyncio.Event()

    async def on(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", on)
    activation = asyncio.create_task(controller.async_activate(1))
    await entered.wait()
    stop = asyncio.create_task(controller.async_stop())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(PumpError, match="Stop requested"):
        await activation
    assert await stop
    assert hass.states.get("switch.pump").state == "off"


async def test_cancel_unload_still_finishes(hass, controller):
    entered, release = asyncio.Event(), asyncio.Event()
    await controller.async_activate(1)

    async def off(call):
        entered.set()
        await release.wait()
        hass.states.async_set("switch.pump", "off")

    hass.services.async_register("switch", "turn_off", off)
    close = asyncio.create_task(controller.async_close())
    await entered.wait()
    close.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert controller.state == PumpState.CLOSED
    assert controller._remove_shutdown is None


async def test_unresolved_handler_blocks_reload(hass, controller):
    release = asyncio.Event()

    async def on(call):
        await release.wait()
        hass.states.async_set("switch.pump", "on")

    hass.services.async_register("switch", "turn_on", on)
    with pytest.raises(PumpError):
        await controller.async_activate(1)
    assert not await controller.async_close()
    replacement = PumpController(hass, controller.entry)
    with pytest.raises(PumpError, match="Another"):
        await replacement.async_initialize()
    release.set()
    await hass.async_block_till_done()
    assert await controller.async_close()
    await replacement.async_initialize()
    assert replacement.state == PumpState.IDLE
    await replacement.async_close()


@pytest.mark.parametrize("record", [{}, {"entity_id": "sensor.bad"}, ["invalid"]])
async def test_invalid_recovery_blocks(hass, controller, hass_storage, record):
    await controller.async_close()
    key = f"{DOMAIN}.pump_recovery.{controller.entry.entry_id}"
    await controller.store.async_save(record)
    replacement = PumpController(hass, controller.entry)
    await replacement.async_initialize()
    assert replacement.state == PumpState.FAULT
    assert not await replacement.async_stop()
    with pytest.raises(PumpError):
        await replacement.async_activate(1)
    assert hass_storage[key]["data"] == record
    await replacement.async_close()


async def test_storage_timeout_blocks_dispatch(hass, controller, monkeypatch):
    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.STORAGE_TIMEOUT", 0.02
    )

    release = asyncio.Event()

    async def hung(data):
        await release.wait()

    with (
        patch.object(controller.store, "async_save", side_effect=hung),
        patch.object(controller, "_service", wraps=controller._service) as calls,
    ):
        with pytest.raises(PumpError):
            await controller.async_activate(1)
        assert all(call.args[1] == "turn_off" for call in calls.call_args_list)
    assert controller.state == PumpState.FAULT
    assert not await controller.async_stop()
    release.set()
    await asyncio.sleep(0)
    assert await controller.async_stop()


async def test_service_self_cancellation_reconciles(hass, controller):
    async def on(call):
        hass.states.async_set("switch.pump", "on")
        raise asyncio.CancelledError

    hass.services.async_register("switch", "turn_on", on)
    with pytest.raises(PumpError, match="cancellation"):
        await controller.async_activate(1)
    assert controller.state == PumpState.IDLE
    assert hass.states.get("switch.pump").state == "off"


async def test_delayed_unchanged_off_report(hass, controller):
    await controller.async_activate(1)
    hass.states.async_set("switch.pump", "off")
    reporter = None

    async def off(call):
        nonlocal reporter

        async def report():
            await asyncio.sleep(0.005)
            # Same value: HA updates last_reported without firing state_changed.
            hass.states.async_set("switch.pump", "off")

        reporter = asyncio.create_task(report())

    hass.services.async_register("switch", "turn_off", off)
    assert await controller.async_stop()
    await reporter


async def test_runtime_bounds_running_persistence(hass, controller):
    save = controller.store.async_save
    release = asyncio.Event()

    async def hung_running(data):
        if data and data["state"] == "running":
            await release.wait()
        await save(data)

    with patch.object(controller.store, "async_save", side_effect=hung_running):
        with pytest.raises(PumpError):
            await controller.async_activate(0.02)
    assert hass.states.get("switch.pump").state == "off"
    assert not await controller.async_stop()
    assert controller.state == PumpState.FAULT
    release.set()
    await asyncio.sleep(0)
    assert await controller.async_stop()


async def test_idle_and_construction_have_no_service_effects(hass, controller):
    await controller.async_close()
    replacement = PumpController(hass, controller.entry)
    assert replacement.state == PumpState.NEW
    with patch.object(replacement, "_service", new_callable=AsyncMock) as service:
        await replacement.async_initialize()
        await replacement.async_close()
        service.assert_not_awaited()
