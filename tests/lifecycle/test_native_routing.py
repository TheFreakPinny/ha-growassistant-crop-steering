"""R2 uses actual native controllers and HA services with synthetic pumps."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components import growassistant_crop_steering as integration
from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.irrigation import capture_inputs
from custom_components.growassistant_crop_steering.native_configuration import (
    validated_configuration,
)
from custom_components.growassistant_crop_steering.native_runtime import (
    NativeRuntime,
    NativeRuntimeState,
)
from custom_components.growassistant_crop_steering.native_safety import (
    OwnershipEvidence,
)
from custom_components.growassistant_crop_steering.ownership_routing import (
    ControlRoute,
    control_route,
)
from custom_components.growassistant_crop_steering.pump import PumpController
from tests.shot_engine import conftest as native_fixtures

shot_clock = native_fixtures.shot_clock
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def lifecycle_environment(enable_custom_integrations, shot_clock):
    """Use native civil time while leaving service timeouts monotonic."""


@pytest.fixture
async def runtime(hass, entry):
    pump = PumpController(hass, entry)
    pump.ownership = OwnershipEvidence("switch.pump", True, True, True, True)
    runtime = NativeRuntime(hass, entry, pump=pump)
    yield runtime
    await runtime.async_close()


async def test_snapshot_options_and_empty_overrides(hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "drain_sensor": "binary_sensor.old", "p1_start_vwc": 42},
        options={"drain_sensor": None, "p1_start_vwc": 39},
    )
    snapshot = validated_configuration(hass, entry)
    assert snapshot == capture_inputs(hass, entry)
    assert snapshot.config["p1_start_vwc"] == 39
    assert not snapshot.config["drain_sensor_configured"]
    assert snapshot.config["drain_sensor"] is None


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("pump_switch", None, "missing_pump_switch"),
        ("pump_switch", ["switch.a", "switch.b"], "invalid_pump_switch_domain"),
        ("pump_switch", "light.pump", "invalid_pump_switch_domain"),
        ("vwc_sensor", [], "missing_vwc_sensor"),
        ("vwc_sensor", ["input_number.vwc"], "invalid_vwc_sensor_domain"),
        ("led_sunrise", "sensor.time", "invalid_led_sunrise_domain"),
        ("p1_mode", "unknown", "invalid_p1_mode"),
        ("p1_shot_duration_s", 0, "pump_hardlimit"),
        ("p2_shot_duration_s", 301, "pump_hardlimit"),
        ("p3_emergency_shot_duration_s", float("nan"), "pump_hardlimit"),
        ("p1_start_vwc", 80, "above_field_capacity"),
        ("vwc_cap", 101, "invalid_vwc_cap"),
        ("p2_soak_min", None, "invalid_p2_soak_min"),
        ("p1_shots", 1.5, "invalid_p1_shots"),
        ("p1_duration_min", 1440, "inconsistent_phase_windows"),
    ],
)
async def test_invalid_configuration_blocks_before_initialization(
    hass, entry, runtime, key, value, reason
):
    hass.config_entries.async_update_entry(entry, options={key: value})
    with pytest.raises(ValueError, match=reason):
        await runtime.async_initialize()
    assert control_route(entry) == ControlRoute.BLOCKED
    assert not runtime.shots.initialized
    assert runtime.error


@pytest.mark.parametrize("duration", [1, 300])
async def test_valid_pump_limits(hass, entry, duration):
    hass.config_entries.async_update_entry(
        entry, options={"p1_shot_duration_s": duration}
    )
    assert validated_configuration(hass, entry).config["p1_shot_duration_s"] == duration


async def test_native_routes_block_legacy_and_stale_daily_reset(hass, entry, runtime):
    coordinator = integration._CycleResetCoordinator(hass, entry)
    assert control_route(entry) == ControlRoute.BLOCKED
    await runtime.async_initialize()
    assert control_route(entry) == ControlRoute.NATIVE_SIMULATED
    before = dict(entry.options)
    assert not await coordinator.async_check()
    assert coordinator.last_cycle is None
    await integration.async_setup(hass, {})
    for service in (
        "reset_cycle",
        "set_last_shot_now",
        "clear_last_shot",
        "start_p1",
        "complete_p1",
    ):
        with pytest.raises(HomeAssistantError, match="blocked"):
            await hass.services.async_call(
                DOMAIN, service, {"config_entry_id": entry.entry_id}, blocking=True
            )
    assert dict(entry.options) == before
    assert not runtime.scheduler.running


async def test_blocked_stop_reaches_native_controller(
    hass, entry, runtime, monkeypatch
):
    await runtime.async_initialize()
    runtime.state = NativeRuntimeState.BLOCKED
    off = AsyncMock()
    monkeypatch.setattr(runtime.pump, "async_stop", off)
    await integration.async_setup(hass, {})
    await hass.services.async_call(
        DOMAIN, "stop_pump", {"config_entry_id": entry.entry_id}, blocking=True
    )
    off.assert_awaited_once()
    with pytest.raises(HomeAssistantError):
        await integration._reset_cycle_for_entry(hass, entry)


@pytest.mark.parametrize(
    "key,value",
    [
        ("pump_switch", "switch.other"),
        ("vwc_sensor", ["sensor.other"]),
        ("led_sunrise", "input_datetime.other"),
        ("drain_tray_sensor", "binary_sensor.tray"),
        ("p1_mode", "manual"),
        ("led_day_sensor", "binary_sensor.day"),
    ],
)
async def test_critical_change_keeps_runtime_and_invalidates(
    hass, entry, runtime, key, value
):
    await runtime.async_initialize()
    generation = runtime.shots._generation
    hass.states.async_set("input_datetime.other", "09:00:00")
    hass.config_entries.async_update_entry(entry, options={key: value})
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime
    assert control_route(entry) == ControlRoute.BLOCKED
    assert runtime.shots._generation > generation
    runtime.scheduler.async_request_evaluation()
    await hass.async_block_till_done()
    assert runtime.shots.snapshot is None
    assert not runtime.scheduler.running


async def test_uncritical_parameters_refresh_without_reconstruction(
    hass, entry, runtime
):
    await runtime.async_initialize()
    controllers = (runtime.pump, runtime.shots, runtime.cycle, runtime.scheduler)
    hass.config_entries.async_update_entry(entry, options={"p1_start_vwc": 40})
    await hass.async_block_till_done()
    assert runtime._configuration.config["p1_start_vwc"] == 40
    assert control_route(entry) == ControlRoute.NATIVE_SIMULATED
    assert controllers == (
        runtime.pump,
        runtime.shots,
        runtime.cycle,
        runtime.scheduler,
    )
    assert runtime.scheduler.decision is None


async def test_legacy_reset_in_progress_prevents_owner_transfer(
    hass, entry, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    original = integration._reset_cycle_helpers

    async def delayed(hass, entry):
        entered.set()
        await release.wait()
        await original(hass, entry)

    monkeypatch.setattr(integration, "_reset_cycle_helpers", delayed)
    task = asyncio.create_task(integration._reset_cycle_for_entry(hass, entry))
    await entered.wait()
    with pytest.raises(RuntimeError, match="Legacy service still in progress"):
        NativeRuntime(hass, entry, pump=PumpController(hass, entry))
    assert control_route(entry) == ControlRoute.LEGACY
    release.set()
    await task
    assert control_route(entry) == ControlRoute.LEGACY


async def prepare_clean_cycle(hass, entry, runtime, *, manual=False):
    """Supply explicit clean managed state, never infer a legacy baseline."""
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
            "p1_mode": "manual" if manual else "sensor",
        },
    )
    hass.states.async_set("sensor.vwc", "40", force_update=True)
    await runtime.async_initialize()
    assert await runtime.cycle.async_sunrise()


async def test_manual_services_use_cycle_without_backdating(hass, entry, runtime):
    await prepare_clean_cycle(hass, entry, runtime, manual=True)
    await integration.async_setup(hass, {})
    assert runtime.cycle.record["phase"] == "p0_transpiration"
    await hass.services.async_call(
        DOMAIN, "start_p1", {"config_entry_id": entry.entry_id}, blocking=True
    )
    assert entry.options["p1_active"]
    assert runtime.cycle.record["phase"] == "p1_morning"
    assert "last_shot" not in entry.options
    await hass.services.async_call(
        DOMAIN, "complete_p1", {"config_entry_id": entry.entry_id}, blocking=True
    )
    assert entry.options["p1_done"]
    assert entry.options["p2_ref_vwc"] == 40
    assert runtime.cycle.record["values"]["p2_ref_vwc"] == 40
    assert runtime.shots.snapshot is None
    assert not runtime.scheduler.running


async def test_reset_cannot_overlap_native_accounting(hass, entry, runtime):
    await prepare_clean_cycle(hass, entry, runtime)
    before = dict(entry.options)
    async with runtime.shots._accounting_lock:
        with pytest.raises(HomeAssistantError, match="reset_cycle"):
            await integration._reset_cycle_for_entry(hass, entry)
    assert dict(entry.options) == before
    assert runtime.cycle.cycle_id


@pytest.mark.parametrize(
    "change",
    [
        {"pump_switch": "switch.other"},
        {"p1_shot_duration_s": 20},
        {"vwc_sensor": ["sensor.other"]},
    ],
)
async def test_critical_change_during_actual_reservation_stops_original_pump(
    hass, entry, runtime, change
):
    await prepare_clean_cycle(hass, entry, runtime)
    await runtime.cycle.async_observe()
    active = asyncio.Event()
    commands = []

    async def service(call):
        commands.append((call.service, call.data["entity_id"]))
        hass.states.async_set(
            "switch.pump",
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )
        if call.service == "turn_on":
            active.set()

    hass.services.async_register("switch", "turn_on", service)
    hass.services.async_register("switch", "turn_off", service)
    operation = asyncio.create_task(runtime.shots.async_execute())
    await asyncio.wait_for(active.wait(), 2)
    assert runtime.shots.execution_active
    with pytest.raises(HomeAssistantError):
        await integration._reset_cycle_for_entry(hass, entry)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **change})
    await hass.async_block_till_done()
    result = await asyncio.wait_for(operation, 2)
    assert not result.accounted
    assert control_route(entry) == ControlRoute.BLOCKED
    assert entry.options["p1_shots_done"] == 0
    assert commands.count(("turn_on", "switch.pump")) == 1
    assert ("turn_off", "switch.pump") in commands
    assert all(entity == "switch.pump" for _, entity in commands)


async def test_schedule_rebinding_with_identical_clock_still_blocks(
    hass, entry, runtime
):
    await runtime.async_initialize()
    hass.states.async_set("input_datetime.identical", "08:00:00")
    hass.config_entries.async_update_entry(
        entry, options={"led_sunrise": "input_datetime.identical"}
    )
    await hass.async_block_till_done()
    assert control_route(entry) == ControlRoute.BLOCKED


async def test_light_helper_state_change_blocks(hass, entry, runtime):
    await runtime.async_initialize()
    hass.states.async_set("input_datetime.sunrise", "09:00:00")
    await hass.async_block_till_done()
    assert control_route(entry) == ControlRoute.BLOCKED


async def test_entity_counter_and_flag_edits_are_blocked(hass, entry, runtime):
    from custom_components.growassistant_crop_steering.const import (
        BOOLEAN_STATE_DESCRIPTIONS,
        NUMERIC_SETTING_DESCRIPTIONS,
    )
    from custom_components.growassistant_crop_steering.number import (
        GrowAssistantSettingNumber,
    )
    from custom_components.growassistant_crop_steering.switch import (
        GrowAssistantStateSwitch,
    )

    await runtime.async_initialize()
    number = GrowAssistantSettingNumber(
        hass,
        entry,
        next(s for s in NUMERIC_SETTING_DESCRIPTIONS if s.key == "p1_shots_done"),
    )
    switch = GrowAssistantStateSwitch(hass, entry, BOOLEAN_STATE_DESCRIPTIONS[0])
    before = dict(entry.options)
    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(0)
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    assert dict(entry.options) == before


async def test_stale_callbacks_after_close_cannot_affect_new_runtime(
    hass, entry, runtime
):
    await runtime.async_initialize()
    assert await runtime.async_close()
    pump = PumpController(hass, entry)
    pump.ownership = runtime.pump.ownership
    replacement = NativeRuntime(hass, entry, pump=pump)
    try:
        await replacement.async_initialize()
        await runtime._options_changed(hass, entry)
        runtime.scheduler.async_request_evaluation()
        await hass.async_block_till_done()
        assert control_route(entry) == ControlRoute.NATIVE_SIMULATED
        assert replacement.shots.snapshot is None
        assert entry.runtime_data is replacement
    finally:
        await replacement.async_close()


async def test_alias_entry_cannot_use_legacy_actions_for_native_pump(
    hass, entry, runtime
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    alias = MockConfigEntry(domain=DOMAIN, data=dict(entry.data))
    alias.add_to_hass(hass)
    await runtime.async_initialize()
    before = dict(alias.options)
    with pytest.raises(HomeAssistantError, match="another native owner"):
        await integration._start_p1_for_entry(hass, alias)
    with pytest.raises(HomeAssistantError, match="another native owner"):
        await integration._reset_cycle_for_entry(hass, alias)
    assert dict(alias.options) == before
    await integration._stop_pump_for_entry(hass, alias)
    assert control_route(entry) == ControlRoute.BLOCKED


async def test_numeric_legacy_helper_resolves_and_invalid_state_is_not_defaulted(
    hass, entry
):
    hass.states.async_set("input_number.duration", "25")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "p1_shot_duration_s": "input_number.duration"}
    )
    assert validated_configuration(hass, entry).config["p1_shot_duration_s"] == "25"
    hass.states.async_set("input_number.duration", "unavailable")
    with pytest.raises(ValueError, match="invalid_p1_shot_duration_s"):
        validated_configuration(hass, entry)
    hass.config_entries.async_update_entry(entry, options={"p1_shot_duration_s": 20})
    assert validated_configuration(hass, entry).config["p1_shot_duration_s"] == 20


async def test_configuration_blocks_p3_without_required_tray(hass, entry):
    hass.config_entries.async_update_entry(
        entry, options={"p3_emergency_enabled": True}
    )
    with pytest.raises(ValueError, match="requires_drain_tray_sensor"):
        validated_configuration(hass, entry)


async def test_contradictory_flags_cannot_become_native_baseline(hass, entry):
    hass.config_entries.async_update_entry(
        entry, options={"p1_active": True, "p1_done": True}
    )
    with pytest.raises(ValueError, match="contradictory_p1_flags"):
        validated_configuration(hass, entry)


async def test_reset_during_actual_accounting_commit_is_rejected(
    hass, entry, runtime, monkeypatch
):
    await prepare_clean_cycle(hass, entry, runtime)
    await runtime.cycle.async_observe()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.shots._save
    captured = []

    async def save():
        operations = runtime.shots._journal["operations"].values()
        if any(record.get("accounting_pending") for record in operations):
            entered.set()
            await release.wait()
        await original()

    # The completion path holds the real accounting lock during its durable save.
    monkeypatch.setattr(runtime.shots, "_save", save)

    async def service(call):
        captured.append(call.service)
        hass.states.async_set(
            "switch.pump",
            "on" if call.service == "turn_on" else "off",
            force_update=True,
        )

    hass.services.async_register("switch", "turn_on", service)
    hass.services.async_register("switch", "turn_off", service)
    # Keep this test short while preserving the real reservation/execution path.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "p1_shot_duration_s": 1}
    )
    await hass.async_block_till_done()
    operation = asyncio.create_task(runtime.shots.async_execute())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert runtime.shots._accounting_lock.locked()
        with pytest.raises(HomeAssistantError, match="reset_cycle"):
            await integration._reset_cycle_for_entry(hass, entry)
    finally:
        release.set()
        result = await operation
    assert result.accounted
    assert entry.options["p1_shots_done"] == 1
    assert captured.count("turn_on") == 1


async def test_cancelled_legacy_reset_keeps_transfer_blocked(hass, entry, monkeypatch):
    entered = asyncio.Event()

    async def partial(hass, entry):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(integration, "_reset_cycle_helpers", partial)
    operation = asyncio.create_task(integration._reset_cycle_for_entry(hass, entry))
    await entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    with pytest.raises(RuntimeError, match="Legacy service still in progress"):
        NativeRuntime(hass, entry, pump=PumpController(hass, entry))


async def test_empty_optional_override_masks_legacy_mapping(hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "drain_sensor": "binary_sensor.missing"},
        options={"drain_sensor": ""},
    )
    assert not validated_configuration(hass, entry).config["drain_sensor_configured"]
