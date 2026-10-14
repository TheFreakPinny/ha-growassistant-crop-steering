"""Real HA read-only diagnostic entities; no hardware or execution stack."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import DATA_DISPATCHER
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components import growassistant_crop_steering as integration
from custom_components.growassistant_crop_steering.const import (
    CONF_LAST_SHOT,
    CONF_LAST_SHOT_TYPE,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_SHOTS_DONE,
    CONF_VWC_SENSOR,
    DOMAIN,
)
from custom_components.growassistant_crop_steering.native_diagnostics import (
    BINARY_SENSOR_NAMES,
    DATA_DIAGNOSTICS,
    SENSOR_NAMES,
    NativeDiagnosticsCoordinator,
    _load_store_json,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.freeze_time(real_asyncio=True)]


@pytest.fixture(autouse=True)
def diagnostic_storage_reader(hass_storage):
    """Supply raw Store envelopes from HA's storage fixture without migration."""

    def load(path):
        return deepcopy(hass_storage.get(Path(path).name))

    with patch(
        "custom_components.growassistant_crop_steering.native_diagnostics._load_store_json",
        side_effect=load,
    ):
        yield


@pytest.fixture(autouse=True)
def forbid_native_execution(hass):
    """Any native construction/initialization or pump call fails this module."""
    module = "custom_components.growassistant_crop_steering"
    with ExitStack() as stack:
        for target in (
            "pump.PumpController",
            "shot_engine._ShotCoordinator",
            "scheduler._NativeScheduler",
            "cycle._NativeCycleController",
        ):
            stack.enter_context(
                patch(
                    f"{module}.{target}.__init__",
                    side_effect=AssertionError("diagnostics constructed native stack"),
                )
            )
        for target in (
            "pump.PumpController",
            "shot_engine._ShotCoordinator",
            "cycle._NativeCycleController",
        ):
            stack.enter_context(
                patch(
                    f"{module}.{target}.async_initialize",
                    side_effect=AssertionError("diagnostics initialized native stack"),
                )
            )
        calls = []
        for domain in ("switch", "homeassistant"):
            for service in ("turn_on", "turn_off"):
                mock = AsyncMock()
                calls.append(mock)
                hass.services.async_register(domain, service, mock)
        yield
        for mock in calls:
            mock.assert_not_awaited()


def coordinator(hass, entry):
    return hass.data[DOMAIN][DATA_DIAGNOSTICS][entry.entry_id]


def entities(hass, entry):
    return {
        entity.unique_id.removeprefix(f"{entry.entry_id}_"): entity
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


def state(hass, entry, key):
    return hass.states.get(entities(hass, entry)[key].entity_id)


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return coordinator(hass, entry)


def seed(hass_storage, entry, kind, value):
    hass_storage[f"{DOMAIN}.{kind}.{entry.entry_id}"] = {
        "version": 1,
        "data": deepcopy(value),
    }


def record(**updates):
    return {
        "state": "completed",
        "outcome": "completed",
        "accounted": True,
        "reserved_at": "2026-10-12T09:00:00+00:00",
        "finished_at": "2026-10-12T09:00:10+00:00",
        "delivery": "software_completed",
        "shutdown_confirmed": True,
        **updates,
    }


async def test_fresh_install_truthful_unknowns_and_stable_diagnostic_entities(
    hass, entry, hass_storage
):
    runtime = await setup(hass, entry)
    registered = entities(hass, entry)
    device = registered["status"].device_id
    for key in (*SENSOR_NAMES, *BINARY_SENSOR_NAMES):
        item = registered[key]
        assert item.unique_id == f"{entry.entry_id}_{key}"
        assert item.entity_category is EntityCategory.DIAGNOSTIC
        assert item.device_id == device
        assert item.domain == ("sensor" if key in SENSOR_NAMES else "binary_sensor")
        assert item.original_name is not None
    assert state(hass, entry, "native_engine_status").state == "disabled"
    assert (
        state(hass, entry, "native_engine_status").attributes["execution_state"]
        == "not_evaluated"
    )
    assert state(hass, entry, "native_activation_available").state == "off"
    assert state(hass, entry, "native_recovery_classification").state == "not_evaluated"
    assert state(hass, entry, "native_last_shot_outcome").state == "unknown"
    assert state(hass, entry, "native_vwc_disagreement").state == "unknown"
    assert state(hass, entry, "native_sensor_readiness").state == "on"
    assert state(hass, entry, "native_pump_ownership").state == "unverified"
    assert runtime.history_status == dict.fromkeys(
        ("pump_recovery", "shot_journal", "native_cycle"), "missing"
    )
    assert not any(
        "native_pump_owner" in key or "native_shot" in key for key in hass.data
    )
    assert not any(
        key.startswith(f"{DOMAIN}.shot_journal")
        or key.startswith(f"{DOMAIN}.pump_recovery")
        or key.startswith(f"{DOMAIN}.native_cycle")
        for key in hass_storage
    )
    assert len(runtime._remove_listeners) == 2


async def test_existing_blueprint_values_are_not_native_execution_evidence(hass, entry):
    hass.services.async_register("counter", "reset", AsyncMock())
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_LAST_SHOT: "input_datetime.last_shot",
            CONF_P1_SHOTS_DONE: "counter.p1",
        },
        options={
            CONF_LAST_SHOT: "2026-10-12T09:00:00+00:00",
            CONF_LAST_SHOT_TYPE: "p1",
        },
    )
    hass.states.async_set("counter.p1", "8")
    await setup(hass, entry)
    assert state(hass, entry, "last_shot").state != "unknown"
    assert state(hass, entry, "native_last_shot_outcome").state == "unknown"
    assert state(hass, entry, "native_recovery_classification").state == "not_evaluated"
    migration = state(hass, entry, "native_migration_readiness")
    assert migration.state == "ineligible"
    assert migration.attributes["blueprint_relinquishment"] == "unverified"
    assert migration.attributes["native_activation_allowed"] is False


@pytest.mark.parametrize("pump_state", ["off", "on", "unknown", "unavailable"])
async def test_pump_state_never_proves_ownership_or_physical_off(
    hass, entry, pump_state
):
    hass.states.async_set("switch.pump", pump_state)
    await setup(hass, entry)
    ownership = state(hass, entry, "native_pump_ownership")
    assert ownership.state == "unverified"
    assert ownership.attributes["observed_state"] == pump_state
    assert ownership.attributes["physical_pump_off_verified"] is False
    assert state(hass, entry, "native_activation_available").state == "off"
    migration = state(hass, entry, "native_migration_readiness").attributes
    assert migration["sensor_readiness"] == "ready"
    for key in ("recovery_readiness", "cycle_state_reconciliation"):
        assert migration[key] == "not_evaluated"
    for key in (
        "pump_off_verification",
        "blueprint_relinquishment",
        "exclusive_pump_ownership",
    ):
        assert migration[key] == "unverified"
    assert migration["overall_activation_eligibility"] is False
    assert migration["review_eligible"] is False
    assert {
        "pump_ownership_unknown",
        "pump_off_not_verified",
        "blueprint_ownership_unresolved",
        "state_not_reconciled",
        "recovery_not_evaluated",
        "production_activation_prohibited",
    } <= set(migration["blocking_reasons"])
    assert all(migration["blocking_explanations"])


@pytest.mark.parametrize(
    "updates,expected",
    [
        (
            {
                "state": "interrupted",
                "outcome": "interrupted",
                "delivery": "possible_partial",
            },
            {"interrupted_shot", "possible_partial_delivery", "shot_review_required"},
        ),
        (
            {"state": "running", "outcome": None},
            {"interrupted_shot", "possible_partial_delivery"},
        ),
        (
            {"state": "fault", "outcome": "unconfirmed_shutdown"},
            {"shot_review_required", "unconfirmed_pump_shutdown"},
        ),
        ({"accounted": False}, {"incomplete_accounting"}),
        ({"accounting_pending": True}, {"incomplete_accounting"}),
        ({"state": "reserved", "outcome": None}, {"interrupted_shot"}),
    ],
)
async def test_historical_faults_surface_without_rewriting_journals(
    hass, entry, hass_storage, updates, expected
):
    journal = {"operations": {"old-shot": record(**updates)}}
    seed(hass_storage, entry, "shot_journal", journal)
    runtime = await setup(hass, entry)
    recovery = state(hass, entry, "native_recovery_classification")
    assert recovery.state == "historical_review_required"
    assert expected <= set(recovery.attributes["blocking_reasons"])
    assert recovery.attributes["current_recovery"] == "not_evaluated"
    assert recovery.attributes["evidence"] == "persisted_snapshot"
    assert state(hass, entry, "native_last_shot_outcome").state == (
        updates.get("outcome", "completed") or "unknown"
    )
    assert runtime.history["shot_journal"] == journal
    assert hass_storage[f"{DOMAIN}.shot_journal.{entry.entry_id}"]["data"] == journal


async def test_completed_historical_record_does_not_authorize_activation(
    hass, entry, hass_storage
):
    # Existing auto reset would clear current counters; preload its existing marker.
    seed(
        hass_storage, entry, "cycle_reset", {"last_cycle": "2026-10-12T08:00:00+00:00"}
    )
    hass.config_entries.async_update_entry(entry, options={CONF_P1_SHOTS_DONE: 1})
    journal = {
        "operations": {"completed-shot": record()},
        "last_accounting": {
            "cycle_id": "2026-10-12T08:00:00+00:00",
            "values": {CONF_P1_SHOTS_DONE: 1},
        },
    }
    seed(hass_storage, entry, "shot_journal", journal)
    await setup(hass, entry)
    recovery = state(hass, entry, "native_recovery_classification")
    assert recovery.state == "historical_evidence_present"
    assert recovery.attributes["blocking_reasons"] == ()
    last = state(hass, entry, "native_last_shot_outcome")
    assert last.state == "completed"
    assert last.attributes["evidence"] == "persisted_history"
    assert last.attributes["accounted"] is True
    assert state(hass, entry, "native_activation_available").state == "off"
    assert (
        state(hass, entry, "native_migration_readiness").attributes[
            "recovery_readiness"
        ]
        == "not_evaluated"
    )


@pytest.mark.parametrize(
    "kind,value",
    [
        ("pump_recovery", []),
        ("pump_recovery", {"entity_id": "sensor.invalid"}),
        ("shot_journal", {"operations": []}),
        ("shot_journal", {"operations": {"bad": None}}),
        ("shot_journal", {"operations": {"bad": {"state": "made_up"}}}),
        ("native_cycle", {"cycle_id": "invalid"}),
        ("native_cycle", []),
    ],
)
async def test_corrupt_history_is_visible_and_preserved(
    hass, entry, hass_storage, kind, value
):
    seed(hass_storage, entry, kind, value)
    runtime = await setup(hass, entry)
    assert runtime.history_status[kind] == "invalid"
    assert (
        "storage_inconsistency"
        in state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
    )
    assert hass_storage[f"{DOMAIN}.{kind}.{entry.entry_id}"]["data"] == value


async def test_storage_load_failure_does_not_block_blueprint_setup(hass, entry):
    with patch(
        "custom_components.growassistant_crop_steering.native_diagnostics._load_store_json",
        side_effect=OSError("simulated unreadable journal"),
    ):
        runtime = await setup(hass, entry)
    assert runtime.history_status["shot_journal"] == "load_failed"
    assert state(hass, entry, "status").state == "ready"
    assert (
        "storage_inconsistency"
        in state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
    )


@pytest.mark.parametrize(
    "envelope",
    [
        {"version": 2, "data": {"operations": {}}},
        {"version": 1, "minor_version": 2, "data": {"operations": {}}},
        {"version": 1},
        [],
        {},
    ],
)
async def test_unsupported_storage_is_never_migrated(
    hass, entry, hass_storage, envelope
):
    key = f"{DOMAIN}.shot_journal.{entry.entry_id}"
    hass_storage[key] = deepcopy(envelope)
    await setup(hass, entry)
    assert (
        coordinator(hass, entry).history_status["shot_journal"]
        == "unsupported_or_invalid_envelope"
    )
    assert hass_storage[key] == envelope
    assert (
        "storage_inconsistency"
        in state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
    )


async def test_cleared_pump_record_is_not_an_unresolved_shutdown(
    hass, entry, hass_storage
):
    seed(hass_storage, entry, "pump_recovery", None)
    await setup(hass, entry)
    assert (
        coordinator(hass, entry).history_status["pump_recovery"] == "no_pending_record"
    )
    assert state(hass, entry, "native_recovery_classification").state == "not_evaluated"
    assert state(hass, entry, "native_pump_ownership").state == "unverified"


async def test_real_corrupt_file_is_never_renamed_removed_or_saved(
    hass, entry, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    storage = tmp_path / ".storage"
    storage.mkdir()
    journal = storage / f"{DOMAIN}.shot_journal.{entry.entry_id}"
    original_bytes = b'{"version":1,"data":broken json'
    journal.write_bytes(original_bytes)
    runtime = NativeDiagnosticsCoordinator(hass, entry)
    with (
        patch(
            "custom_components.growassistant_crop_steering.native_diagnostics._load_store_json",
            _load_store_json,
        ),
        patch.object(
            Store, "async_load", side_effect=AssertionError("Store may mutate history")
        ),
        patch.object(Store, "async_save", side_effect=AssertionError("history write")),
        patch.object(
            Store, "async_remove", side_effect=AssertionError("history removal")
        ),
        patch("os.rename", side_effect=AssertionError("history rename")),
    ):
        await runtime.async_load()
    assert runtime.history_status["shot_journal"] == "load_failed"
    assert journal.read_bytes() == original_bytes
    assert list(storage.iterdir()) == [journal]


async def test_failure_after_platform_forwarding_cleans_diagnostics(hass, entry):
    with patch.object(
        integration._CycleResetCoordinator,
        "async_start",
        side_effect=RuntimeError("reset setup failure"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN][DATA_DIAGNOSTICS]
    assert entry.entry_id not in hass.data[DOMAIN][integration.DATA_AUTO_RESET]
    assert not any(
        entry.entry_id in str(signal) for signal in hass.data.get(DATA_DISPATCHER, {})
    )


async def test_persisted_recovery_target_is_not_stopped_or_cleared(
    hass, entry, hass_storage
):
    evidence = {"entity_id": "switch.old_pump", "operation_id": "old-operation"}
    seed(hass_storage, entry, "pump_recovery", evidence)
    hass.states.async_set("switch.old_pump", "on")
    await setup(hass, entry)
    recovery = state(hass, entry, "native_recovery_classification")
    assert {"unconfirmed_pump_shutdown", "pump_restart_requires_review"} <= set(
        recovery.attributes["blocking_reasons"]
    )
    assert hass.states.get("switch.old_pump").state == "on"
    assert hass_storage[f"{DOMAIN}.pump_recovery.{entry.entry_id}"]["data"] == evidence


@pytest.mark.parametrize(
    "pending,cycle_id,reason",
    [
        (False, "2026-10-12T08:00:00+00:00", "cycle_restart_requires_review"),
        (True, "2026-10-12T08:00:00+00:00", "incomplete_cycle_transition"),
        (False, "2026-10-11T08:00:00+00:00", "missed_sunrise_or_cycle_reset"),
    ],
)
async def test_cycle_history_remains_unreconciled(
    hass, entry, hass_storage, pending, cycle_id, reason
):
    cycle = {
        "cycle_id": cycle_id,
        "phase": "p1_morning",
        "pending": pending,
        "values": {
            CONF_P1_ACTIVE: False,
            CONF_P1_DONE: False,
            "p1_window_opened_today": False,
            "p2_ref_vwc": 0,
        },
    }
    seed(hass_storage, entry, "native_cycle", cycle)
    await setup(hass, entry)
    assert (
        reason
        in state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
    )
    assert (
        state(hass, entry, "native_migration_readiness").attributes[
            "cycle_state_reconciliation"
        ]
        == "not_evaluated"
    )
    assert hass_storage[f"{DOMAIN}.native_cycle.{entry.entry_id}"]["data"] == cycle


@pytest.mark.parametrize(
    "second,valid,reason,disagreement",
    [
        ("47", 2, None, "off"),
        ("60", 2, "sensor_disagreement", "on"),
        ("unavailable", 1, "sensor_quorum_failed", "unknown"),
        ("unknown", 1, "sensor_quorum_failed", "unknown"),
        ("nan", 1, "invalid_vwc", "unknown"),
        ("101", 1, "invalid_vwc", "unknown"),
    ],
)
async def test_phase_3b_sensor_validation_display(
    hass, entry, second, valid, reason, disagreement
):
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_VWC_SENSOR: ["sensor.vwc", "sensor.second", "sensor.second"]},
    )
    hass.states.async_set("sensor.second", second)
    await setup(hass, entry)
    assert state(hass, entry, "native_vwc_configured_count").state == "2"
    assert state(hass, entry, "native_vwc_valid_count").state == str(valid)
    assert state(hass, entry, "native_vwc_rejected_count").state == str(2 - valid)
    assert state(hass, entry, "native_vwc_quorum").state == (
        "on" if valid == 2 else "off"
    )
    assert state(hass, entry, "native_vwc_disagreement").state == disagreement
    validation = state(hass, entry, "native_sensor_readiness")
    assert validation.state == ("on" if reason is None else "off")
    if reason:
        assert reason in validation.attributes["blocking_reasons"]
    assert validation.attributes["freshness_policy"] == {
        "maximum_age_s": 300,
        "minimum_valid": 1,
        "minimum_percent": 100,
        "maximum_spread": 10,
    }
    assert validation.attributes["evidence"] == "live_observation"
    assert state(hass, entry, "native_activation_available").state == "off"


async def test_timer_marks_unchanged_values_stale_without_journal_polling(
    hass, entry, freezer
):
    runtime = await setup(hass, entry)
    original_updated = hass.states.get("sensor.vwc").last_updated
    with patch(
        "custom_components.growassistant_crop_steering.native_diagnostics._load_store_json",
        side_effect=AssertionError("unexpected history polling"),
    ) as load:
        freezer.tick(timedelta(seconds=300))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        load.assert_not_called()
        assert state(hass, entry, "native_sensor_readiness").state == "on"
        # A repeated unchanged HA report does not update last_updated.
        hass.states.async_set("sensor.vwc", "45")
        assert hass.states.get("sensor.vwc").last_updated == original_updated
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert state(hass, entry, "native_sensor_readiness").state == "off"
    assert runtime.data["native_vwc_rejected_count"][1]["stale_count"] == 1
    hass.states.async_set("sensor.vwc", "46")
    await hass.async_block_till_done()
    assert state(hass, entry, "native_sensor_readiness").state == "on"


async def test_restored_value_and_missing_sensor_are_rejected(hass, entry):
    hass.states.async_set("sensor.vwc", "45", {"restored": True})
    await setup(hass, entry)
    assert state(hass, entry, "native_sensor_readiness").attributes["rejected"] == {
        "sensor.vwc": "restored_value"
    }
    hass.states.async_remove("sensor.vwc")
    await hass.async_block_till_done()
    assert (
        state(hass, entry, "native_vwc_rejected_count").attributes["unavailable_count"]
        == 1
    )


async def test_options_rebind_sensors_and_explicit_empty_selector(hass, entry):
    runtime = await setup(hass, entry)
    hass.states.async_set("sensor.new", "49")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_VWC_SENSOR: ["sensor.new"]}
    )
    await hass.async_block_till_done()
    assert coordinator(hass, entry) is runtime
    assert runtime.data["native_sensor_readiness"][1]["average"] == 49
    with patch.object(runtime, "async_refresh") as refresh:
        hass.states.async_set("sensor.vwc", "unavailable")
        await hass.async_block_till_done()
        refresh.assert_not_called()
    hass.states.async_set("sensor.new", "unavailable")
    await hass.async_block_till_done()
    assert state(hass, entry, "native_sensor_readiness").state == "off"
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_VWC_SENSOR: []}
    )
    await hass.async_block_till_done()
    assert state(hass, entry, "native_vwc_configured_count").state == "0"
    assert state(hass, entry, "native_vwc_quorum").state == "off"


@pytest.mark.parametrize(
    "new_record",
    [
        record(reserved_at=None),
        record(reserved_at="not-a-time"),
        record(reserved_at="2026-10-12T09:00:00"),
        record(outcome=None),
        record(outcome="invented"),
        record(state="running", outcome=None, reserved_at="2026-10-12T09:30:00+00:00"),
    ],
)
async def test_last_outcome_requires_unambiguous_persisted_evidence(
    hass, entry, hass_storage, new_record
):
    seed(
        hass_storage,
        entry,
        "shot_journal",
        {"operations": {"old": record(), "new": new_record}},
    )
    await setup(hass, entry)
    assert state(hass, entry, "native_last_shot_outcome").state == "unknown"


async def test_last_outcome_is_latest_reservation_not_dictionary_order(
    hass, entry, hass_storage
):
    seed(
        hass_storage,
        entry,
        "shot_journal",
        {
            "operations": {
                "new": record(
                    state="interrupted",
                    outcome="interrupted",
                    reserved_at="2026-10-12T09:30:00+00:00",
                ),
                "old": record(),
            }
        },
    )
    await setup(hass, entry)
    assert state(hass, entry, "native_last_shot_outcome").state == "interrupted"
    assert (
        state(hass, entry, "native_last_shot_outcome").attributes["operation_id"]
        == "new"
    )


async def test_reload_refreshes_history_preserves_ids_and_unload_removes_listeners(
    hass, entry, hass_storage, freezer
):
    old = await setup(hass, entry)
    before = {key: item.entity_id for key, item in entities(hass, entry).items()}
    seed(
        hass_storage,
        entry,
        "shot_journal",
        {"operations": {"new": record(state="interrupted", outcome="interrupted")}},
    )
    old.async_refresh()
    assert old.data["native_last_shot_outcome"][0] is None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = coordinator(hass, entry)
    assert new is not old and old._closed
    assert old._remove_listeners == [] and old._remove_states is None
    assert before == {
        key: item.entity_id for key, item in entities(hass, entry).items()
    }
    assert state(hass, entry, "native_last_shot_outcome").state == "interrupted"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert new._closed and new._remove_listeners == [] and new._remove_states is None
    assert entry.entry_id not in hass.data[DOMAIN][DATA_DIAGNOSTICS]
    assert new.signal not in hass.data.get(DATA_DISPATCHER, {})
    with patch.object(new, "async_refresh") as refresh:
        hass.states.async_set("sensor.vwc", "41")
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_VWC_SENSOR: []}
        )
        freezer.tick(timedelta(seconds=120))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        refresh.assert_not_called()


async def test_failed_platform_unload_retains_diagnostics(hass, entry):
    runtime = await setup(hass, entry)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await integration.async_unload_entry(hass, entry)
    assert coordinator(hass, entry) is runtime and not runtime._closed
    assert len(runtime._remove_listeners) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_failure_leaves_no_diagnostic_listeners_or_coordinator(hass, entry):
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("test setup failure"),
    ):
        with pytest.raises(RuntimeError, match="test setup failure"):
            await integration.async_setup_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN][DATA_DIAGNOSTICS]
    await setup(hass, entry)


async def test_sensor_events_do_not_clear_historical_faults(hass, entry, hass_storage):
    seed(
        hass_storage,
        entry,
        "shot_journal",
        {
            "operations": {
                "old": record(
                    state="interrupted",
                    outcome="interrupted",
                    delivery="possible_partial",
                )
            }
        },
    )
    await setup(hass, entry)
    original = state(hass, entry, "native_recovery_classification").attributes[
        "blocking_reasons"
    ]
    hass.states.async_set("sensor.vwc", "unavailable")
    await hass.async_block_till_done()
    assert (
        state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
        == original
    )
    hass.states.async_set("sensor.vwc", "46")
    await hass.async_block_till_done()
    assert (
        state(hass, entry, "native_recovery_classification").attributes[
            "blocking_reasons"
        ]
        == original
    )
    assert state(hass, entry, "native_activation_available").state == "off"
