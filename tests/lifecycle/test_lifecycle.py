"""Exercise integration lifecycle through Home Assistant's entry manager."""

from datetime import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import DATA_DISPATCHER
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components import growassistant_crop_steering as integration
from custom_components.growassistant_crop_steering.button import (
    SERVICE_BUTTON_DESCRIPTIONS,
)
from custom_components.growassistant_crop_steering.const import (
    BOOLEAN_STATE_DESCRIPTIONS,
    CONF_LED_SUNRISE,
    CONF_P1_ACTIVE,
    CONF_P1_MODE,
    CONF_P1_SHOTS,
    CONF_P1_SHOTS_DONE,
    DOMAIN,
    NUMERIC_SETTING_DESCRIPTIONS,
)
from custom_components.growassistant_crop_steering.native_diagnostics import (
    BINARY_SENSOR_NAMES,
    SENSOR_NAMES,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.freeze_time(real_asyncio=True)]


def registered(hass, entry):
    """Return this entry's registered entities indexed by stable unique ID."""
    return {
        entity.unique_id.removeprefix(f"{entry.entry_id}_"): entity
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


def coordinator(hass, entry):
    return hass.data[DOMAIN][integration.DATA_AUTO_RESET][entry.entry_id]


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return coordinator(hass, entry)


async def test_setup_registers_all_entities_and_device(hass, entry, pump):
    """Real forwarding initializes all four platforms and every expected entity."""
    runtime = await setup(hass, entry)
    entities = registered(hass, entry)
    expected = {
        "binary_sensor": set(BINARY_SENSOR_NAMES),
        "button": {d.key for d in SERVICE_BUTTON_DESCRIPTIONS},
        "number": {d.key for d in NUMERIC_SETTING_DESCRIPTIONS},
        "switch": {d.key for d in BOOLEAN_STATE_DESCRIPTIONS},
        "sensor": {
            "status",
            "phase",
            "last_shot",
            "p1_soak_remaining",
            "p2_soak_remaining",
            "p3_emergency_soak_remaining",
            "block_reason",
            "p1_debug",
            "debug",
            *SENSOR_NAMES,
        },
    }
    assert {platform.value for platform in integration.PLATFORMS} == set(expected)
    assert len(entities) == sum(map(len, expected.values()))
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    for domain, keys in expected.items():
        assert {key for key, item in entities.items() if item.domain == domain} == keys
        for key in keys:
            assert entities[key].device_id == device.id
            assert hass.states.get(entities[key].entity_id) is not None
    assert runtime.last_cycle == "2026-10-12T08:00:00+00:00"
    assert len(runtime._remove_listeners) == 2
    pump.assert_not_awaited()


async def test_services_and_entity_options_operate_without_reload(hass, entry, pump):
    """HA entity services persist values; integration buttons dispatch actions."""
    runtime = await setup(hass, entry)
    entities = registered(hass, entry)
    await hass.services.async_call(
        "number",
        "set_value",
        {
            "entity_id": entities[CONF_P1_SHOTS].entity_id,
            "value": 7,
        },
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_on",
        {
            "entity_id": entities[CONF_P1_ACTIVE].entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_P1_SHOTS] == 7
    assert hass.states.get(entities[CONF_P1_SHOTS].entity_id).state == "7.0"
    assert hass.states.get(entities[CONF_P1_ACTIVE].entity_id).state == "on"
    assert coordinator(hass, entry) is runtime
    await hass.services.async_call(
        "button",
        "press",
        {
            "entity_id": entities["stop_pump"].entity_id,
        },
        blocking=True,
    )
    pump.assert_awaited_once()
    assert pump.call_args.args[0].data == {"entity_id": "switch.pump"}


async def test_coordinator_updates_persists_and_reschedules(
    hass, entry, freezer, hass_storage
):
    """Real state events and clock callbacks reset once and save durable markers."""
    runtime = await setup(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_P1_SHOTS_DONE: 3}
    )
    assert not await runtime.async_check()
    assert entry.options[CONF_P1_SHOTS_DONE] == 3
    hass.states.async_set("input_datetime.sunrise", "09:00:00")
    await hass.async_block_till_done()
    assert runtime._sunrise == time(9)
    assert len(runtime._remove_listeners) == 2
    assert runtime.last_cycle == "2026-10-12T09:00:00+00:00"
    assert entry.options[CONF_P1_SHOTS_DONE] == 3
    freezer.move_to("2026-10-13 09:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert entry.options[CONF_P1_SHOTS_DONE] == 0
    assert runtime.last_cycle == "2026-10-13T09:00:00+00:00"
    stored = hass_storage[f"{integration.STORAGE_KEY_PREFIX}.{entry.entry_id}"]
    assert stored["data"] == {"last_cycle": runtime.last_cycle}


async def test_options_flow_and_explicit_reload(hass, entry):
    """Options persist live; explicit HA reload replaces coordinator and listeners."""
    old = await setup(hass, entry)
    before = {key: item.entity_id for key, item in registered(hass, entry).items()}
    hass.states.async_set("input_datetime.new_sunrise", "07:00:00")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            **{key: value for key, value in entry.data.items() if key != "name"},
            CONF_LED_SUNRISE: "input_datetime.new_sunrise",
            CONF_P1_MODE: "manual",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_P1_MODE] == "manual"
    assert coordinator(hass, entry) is old
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = coordinator(hass, entry)
    assert new is not old
    assert old._remove_listeners == []
    assert new._sunrise == time(7)
    assert before == {
        key: item.entity_id for key, item in registered(hass, entry).items()
    }
    hass.states.async_set("input_datetime.sunrise", "06:00:00")
    await hass.async_block_till_done()
    assert new._sunrise == time(7)


async def test_unload_cleans_listeners_entities_and_scheduled_callbacks(
    hass, entry, freezer
):
    """Unloaded entities leave the registry but lose states and all subscriptions."""
    runtime = await setup(hass, entry)
    entities = registered(hass, entry)
    marker = runtime.last_cycle
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN][integration.DATA_AUTO_RESET]
    assert runtime._remove_listeners == []
    assert all(
        (state := hass.states.get(item.entity_id)) is None
        or state.state == "unavailable"
        for item in entities.values()
    )
    assert not any(
        entry.entry_id in str(signal) for signal in hass.data.get(DATA_DISPATCHER, {})
    )
    with patch.object(runtime, "async_check", new_callable=AsyncMock) as check:
        hass.states.async_set("input_datetime.sunrise", "09:00:00")
        freezer.move_to("2026-10-13 08:00:00+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        check.assert_not_awaited()
    assert runtime.last_cycle == marker
    # Domain services are HA-wide and intentionally survive entry unload.
    assert hass.services.has_service(DOMAIN, "reset_cycle")
    fresh = await setup(hass, entry)
    assert fresh is not runtime
    assert len(fresh._remove_listeners) == 2


async def test_unload_failure_retains_active_resources(hass, entry):
    """A failed platform unload must keep the coordinator available for retry."""
    runtime = await setup(hass, entry)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await integration.async_unload_entry(hass, entry)
    assert coordinator(hass, entry) is runtime
    assert len(runtime._remove_listeners) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert runtime._remove_listeners == []


async def test_setup_not_ready_recovers_on_retry(hass, entry):
    """A transient forwarding failure enters HA retry state and later loads."""
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=ConfigEntryNotReady("temporary test failure"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data.get(DOMAIN, {}).get(
        integration.DATA_AUTO_RESET, {}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert registered(hass, entry)


async def test_unavailable_helpers_recover_via_real_state_events(hass, entry):
    """Unavailable external times do not prevent setup and recover without reload."""
    hass.states.async_set("input_datetime.sunrise", "unavailable")
    runtime = await setup(hass, entry)
    assert runtime.last_cycle is None
    assert len(runtime._remove_listeners) == 1
    hass.states.async_set("input_datetime.sunrise", "08:00:00")
    await hass.async_block_till_done()
    assert runtime.last_cycle == "2026-10-12T08:00:00+00:00"
    assert len(runtime._remove_listeners) == 2


async def test_same_cycle_reload_restores_marker_without_reset(hass, entry):
    """Reload reads durable storage and preserves work done in the same cycle."""
    old = await setup(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_P1_SHOTS_DONE: 4}
    )
    marker = old.last_cycle
    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        current = coordinator(hass, entry)
        assert current is not old
        assert old._remove_listeners == []
        assert current.last_cycle == marker
        assert entry.options[CONF_P1_SHOTS_DONE] == 4
        assert len(current._remove_listeners) == 2
        old = current


async def test_entry_data_change_applies_on_explicit_reload(hass, entry):
    """Changing entry data retains old subscriptions until an explicit reload."""
    old = await setup(hass, entry)
    hass.states.async_set("input_datetime.replacement", "06:00:00")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LED_SUNRISE: "input_datetime.replacement"}
    )
    await hass.async_block_till_done()
    assert coordinator(hass, entry) is old
    assert old._sunrise == time(8)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    current = coordinator(hass, entry)
    assert current._sunrise == time(6)
    assert old._remove_listeners == []
    hass.states.async_set("input_datetime.replacement", "07:00:00")
    await hass.async_block_till_done()
    assert current._sunrise == time(7)


async def test_user_flow_creates_entry_and_prevents_duplicate(hass, entry):
    """Exercise HA's user flow manager, setup of its result, and unique ID guard."""
    from homeassistant import config_entries

    data = dict(entry.data)
    await hass.config_entries.async_remove(entry.entry_id)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=data
    )
    assert result["type"] == "create_entry"
    created = result["result"]
    await hass.async_block_till_done()
    assert created.state is ConfigEntryState.LOADED
    assert created.unique_id == DOMAIN
    assert created.data[CONF_LED_SUNRISE] == data[CONF_LED_SUNRISE]
    assert registered(hass, created)
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert duplicate["type"] == "abort"
    assert duplicate["reason"] == "already_configured"


async def test_explicit_native_controller_unload_and_reload(hass, entry, pump):
    """Native pump ownership is opt-in and is safely closed by actual HA reload."""
    from custom_components.growassistant_crop_steering.pump import (
        PumpController,
        PumpState,
    )

    await setup(hass, entry)
    from custom_components.growassistant_crop_steering.native_safety import (
        OwnershipEvidence,
    )

    owner = PumpController(hass, entry)
    owner.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    await owner.async_initialize()

    async def switch_service(call):
        hass.states.async_set(
            "switch.pump", "on" if call.service == "turn_on" else "off"
        )

    hass.services.async_register("switch", "turn_on", switch_service)
    hass.services.async_register("switch", "turn_off", switch_service)
    await owner.async_activate(10)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert owner.state == PumpState.CLOSED
    assert hass.states.get("switch.pump").state == "off"
    # Reload does not create, enable, or activate another native owner.
    assert f"{DOMAIN}.native_pump_owner" not in hass.data
    pump.assert_not_awaited()


async def test_native_shutdown_fault_prevents_entry_unload(hass, entry, monkeypatch):
    """Keep the entry and owner available for off reconciliation after failure."""
    from custom_components.growassistant_crop_steering.pump import (
        PumpController,
        PumpState,
    )

    monkeypatch.setattr(
        "custom_components.growassistant_crop_steering.pump.CONFIRM_TIMEOUT", 0.03
    )
    runtime = await setup(hass, entry)
    from freezegun.api import real_monotonic

    # HA's fixture overrides loop.time after freezer applies real_asyncio.
    # Pump safety timeouts need a monotonic clock that continues to advance.
    monkeypatch.setattr(hass.loop, "time", real_monotonic)
    from custom_components.growassistant_crop_steering.native_safety import (
        OwnershipEvidence,
    )

    owner = PumpController(hass, entry)
    owner.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    await owner.async_initialize()

    async def on(call):
        hass.states.async_set("switch.pump", "on")

    async def off(call):
        hass.states.async_set("switch.pump", "off")

    hass.services.async_register("switch", "turn_on", on)
    hass.services.async_register("switch", "turn_off", AsyncMock())
    await owner.async_activate(10)
    assert not await integration.async_unload_entry(hass, entry)
    assert owner.state == PumpState.FAULT
    assert coordinator(hass, entry) is runtime
    assert hass.data[f"{DOMAIN}.native_pump_owner"] is owner
    hass.services.async_register("switch", "turn_off", off)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert owner.state == PumpState.CLOSED
