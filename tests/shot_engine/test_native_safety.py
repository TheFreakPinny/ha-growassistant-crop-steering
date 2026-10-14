"""Phase 3B safety through HA fixtures; all pump services are simulated."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.cycle import _NativeCycleController
from custom_components.growassistant_crop_steering.native_safety import (
    SensorPolicy,
    assess_recovery,
    migration_eligibility,
)
from custom_components.growassistant_crop_steering.pump import (
    PumpController,
    PumpError,
    PumpState,
)
from custom_components.growassistant_crop_steering.shot_engine import (
    ShotRejected,
    ShotState,
    _ShotCoordinator,
)

pytestmark = pytest.mark.asyncio


def peers(engine, *entities):
    engine.hass.config_entries.async_update_entry(
        engine.entry, data={**engine.entry.data, "vwc_sensor": list(entities)}
    )


async def running(engine):
    task = asyncio.create_task(engine.async_execute("uncertain"))
    async with asyncio.timeout(2):
        while engine.state != ShotState.RUNNING:
            assert not task.done()
            await asyncio.sleep(0.001)
    return task


async def restore(engine):
    pump = PumpController(engine.hass, engine.entry)
    await pump.async_initialize()
    coordinator = _ShotCoordinator(engine.hass, engine.entry, pump)
    await coordinator.async_initialize()
    return coordinator


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "101", "-1", "bad"])
async def test_corrupt_sensor_blocks_even_with_other_valid_peer(engine, value):
    peers(engine, "sensor.vwc", "sensor.peer")
    engine.sensor_policy = SensorPolicy(minimum_percent=50)
    engine.hass.states.async_set("sensor.peer", value)
    assessment = engine.sensor_assessment()
    assert "invalid_vwc" in assessment.reasons
    with pytest.raises(ShotRejected, match="invalid_vwc"):
        await engine.async_execute()
    assert engine.calls == []


@pytest.mark.parametrize("value", ["unavailable", "unknown", "", None])
async def test_default_requires_all_configured_sensors(engine, value):
    peers(engine, "sensor.vwc", "sensor.peer")
    if value is not None:
        engine.hass.states.async_set("sensor.peer", value)
    assert engine.sensor_assessment().valid == 1
    with pytest.raises(ShotRejected, match="sensor_quorum_failed"):
        await engine.async_execute()
    assert engine.calls == []


@pytest.mark.parametrize(
    "age,valid", [(299, True), (300, True), (301, False), (-1, False)]
)
async def test_sensor_age_boundaries(engine, shot_clock, age, valid):
    engine.hass.states.async_set(
        "sensor.vwc",
        "40",
        force_update=True,
        timestamp=(shot_clock.at - timedelta(seconds=age)).timestamp(),
    )
    assessment = engine.sensor_assessment()
    assert (not assessment.reasons) is valid
    if not valid:
        assert assessment.rejected["sensor.vwc"] == (
            "stale" if age > 0 else "future_timestamp"
        )
        with pytest.raises(ShotRejected):
            await engine.async_execute()
        assert engine.calls == []


async def test_restored_reading_not_fresh_even_when_timestamp_is_current(engine):
    engine.hass.states.async_set("sensor.vwc", "40", {"restored": True})
    assert engine.sensor_assessment().rejected == {"sensor.vwc": "restored_value"}
    with pytest.raises(ShotRejected):
        await engine.async_execute()
    assert engine.calls == []


async def test_unchanged_report_does_not_refresh_historical_value(engine, shot_clock):
    before = engine.hass.states.get("sensor.vwc").last_updated
    # Advance without synthesizing the fresh physical reports used by move_to().
    shot_clock.at += timedelta(seconds=301)
    engine.hass.states.async_set("sensor.vwc", "40")
    state = engine.hass.states.get("sensor.vwc")
    assert state.last_updated == before
    assert state.last_reported == shot_clock.at
    assert engine.sensor_assessment().rejected["sensor.vwc"] == "stale"


@pytest.mark.parametrize(
    "policy",
    [
        SensorPolicy(maximum_age_s=0),
        SensorPolicy(maximum_age_s=float("inf")),
        SensorPolicy(minimum_valid=0),
        SensorPolicy(minimum_valid=True),
        SensorPolicy(minimum_percent=0),
        SensorPolicy(minimum_percent=101),
        SensorPolicy(maximum_spread=-1),
        SensorPolicy(maximum_spread=float("nan")),
    ],
)
async def test_invalid_sensor_policy_fails_closed(engine, policy):
    engine.sensor_policy = policy
    assert engine.sensor_assessment().reasons == ("invalid_sensor_policy",)
    with pytest.raises(ShotRejected, match="invalid_sensor_policy"):
        await engine.async_execute()
    assert engine.calls == []


@pytest.mark.parametrize(
    "minimum,percent,valid", [(1, 50, True), (2, 50, False), (1, 51, False)]
)
async def test_count_and_percentage_both_required(engine, minimum, percent, valid):
    peers(engine, "sensor.vwc", "sensor.peer")
    engine.sensor_policy = SensorPolicy(minimum_valid=minimum, minimum_percent=percent)
    assert (not engine.sensor_assessment().reasons) is valid
    assert engine.capture_validated_inputs().vwc["sensor.peer"] is None


async def test_average_excludes_stale_peers_with_explicit_quorum(engine, shot_clock):
    peers(engine, "sensor.vwc", "sensor.peer", "sensor.old")
    engine.sensor_policy = SensorPolicy(minimum_valid=2, minimum_percent=66)
    engine.hass.states.async_set("sensor.peer", "50")
    engine.hass.states.async_set(
        "sensor.old",
        "100",
        timestamp=(shot_clock.at - timedelta(seconds=301)).timestamp(),
    )
    assessment = engine.sensor_assessment()
    assert not assessment.reasons and assessment.average == 45
    result = await engine.async_execute("validated_mean")
    assert result.accounted
    assert engine.snapshot.decision.vwc_average == 45
    record = engine._journal["operations"]["validated_mean"]
    assert record["sensor_policy"]["minimum_valid"] == 2
    assert record["sensor_policy"]["minimum_percent"] == 66
    assert record["ownership_evidence"]["simulated_pump"] is True
    assert engine._journal["operations"]["validated_mean"]["vwc"]["sensor.old"] is None


@pytest.mark.parametrize("reading,blocked", [("50", False), ("50.1", True)])
async def test_disagreement_threshold_and_arithmetic_average(engine, reading, blocked):
    peers(engine, "sensor.vwc", "sensor.peer", "sensor.peer")
    engine.hass.states.async_set("sensor.peer", reading)
    assessment = engine.sensor_assessment()
    assert assessment.configured == 2  # duplicate IDs cannot manufacture quorum
    assert assessment.average == (40 + float(reading)) / 2
    assert ("sensor_disagreement" in assessment.reasons) is blocked
    if blocked:
        with pytest.raises(ShotRejected, match="sensor_disagreement"):
            await engine.async_execute()
        assert engine.calls == []


@pytest.mark.parametrize(
    "fault",
    ["missing", "invalid", "disagreement", "stale", "restored", "ownership", "policy"],
)
async def test_midshot_shared_safety_policy_and_persistent_delivery_block(
    engine, shot_clock, fault
):
    peers(engine, "sensor.vwc", "sensor.peer")
    engine.hass.states.async_set("sensor.peer", "40")
    task = await running(engine)
    if fault == "missing":
        engine.hass.states.async_remove("sensor.peer")
    elif fault == "invalid":
        engine.hass.states.async_set("sensor.peer", "nan")
        engine.hass.states.async_set("sensor.peer", "40")  # transient fault latched
    elif fault == "disagreement":
        engine.hass.states.async_set("sensor.peer", "70")
        engine.hass.states.async_set("sensor.peer", "40")
    elif fault == "stale":
        shot_clock.at += timedelta(seconds=301)
    elif fault == "restored":
        engine.hass.states.async_set("sensor.peer", "40", {"restored": True})
    elif fault == "ownership":
        engine.ownership = None
    else:
        engine.sensor_policy = SensorPolicy(minimum_percent=50)
    result = await task
    assert result.state == ShotState.INTERRUPTED and not result.accounted
    assert engine.hass.states.get("switch.pump").state == "off"
    record = engine._journal["operations"]["uncertain"]
    assert record["delivery"] == "possible_partial"
    assert record["delivered_volume"] is None
    assert record["shutdown_confirmed"] and record["recovery_required"]
    assert not engine.execution_available
    with pytest.raises(ShotRejected, match="execution_unavailable"):
        await engine.async_execute("replacement")
    await engine.async_close()
    restored = await restore(engine)
    assert restored._blocked
    assert "possible_partial_delivery" in restored.recovery_assessment.reasons
    assert restored._journal["operations"]["uncertain"]["accounted"] is False
    await restored.async_close()


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("unknown", "pump_ownership_unknown"),
        ("blueprint", "blueprint_ownership_unresolved"),
        ("external", "pump_ownership_unverified"),
        ("target", "pump_ownership_target_changed"),
        ("production", "production_activation_prohibited"),
    ],
)
async def test_ownership_rejects_uncertainty_and_production(engine, kind, reason):
    if kind == "unknown":
        engine.ownership = None
    else:
        fields = {
            "blueprint": {"legacy_relinquished": False},
            "external": {"exclusive_control_verified": False},
            "target": {"pump_entity": "switch.other"},
            "production": {"simulated_pump": False},
        }
        engine.ownership = replace(engine.ownership, **fields[kind])
    assert not engine.execution_available
    with pytest.raises(ShotRejected, match=reason):
        await engine.async_execute()
    assert engine.calls == []


async def test_direct_controller_cannot_bypass_ownership(engine):
    engine.controller.ownership = None
    with pytest.raises(PumpError, match="ownership"):
        await engine.controller.async_activate(1)
    assert engine.calls == [] and engine.controller.state == PumpState.IDLE


async def test_owner_revoked_during_pump_intent_never_dispatches_on(
    engine, monkeypatch
):
    save = engine.controller._save_record

    async def revoke():
        await save()
        engine.controller.ownership = None

    monkeypatch.setattr(engine.controller, "_save_record", revoke)
    result = await engine.async_execute("revoked")
    assert not result.accounted
    assert not [call for call in engine.calls if call[0] == "turn_on"]


async def test_clean_restart_is_diagnostic_only(engine):
    assert (await engine.async_execute("done")).accounted
    await engine.async_close()
    restored = await restore(engine)
    assessment = restored.recovery_assessment
    assert assessment.restart == "clean_restart" and not assessment.review_required
    assert not assessment.native_activation_allowed
    assert restored.ownership is None and not restored.execution_available
    assert restored.snapshot is None
    assert len([call for call in engine.calls if call[0] == "turn_on"]) == 1
    await restored.async_close()


async def test_shutdown_reconciliation_does_not_clear_shot_review(engine, hass_storage):
    await engine.async_close()
    key = f"{DOMAIN}.pump_recovery.{engine.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"entity_id": "switch.pump", "state": "running"},
    }
    restored = await restore(engine)
    assert restored.controller.state == PumpState.IDLE
    assert restored.controller.recovery_record is None
    assert restored._blocked
    assert (
        "pump_shutdown_reconciled_after_restart" in restored.recovery_assessment.reasons
    )
    assert not [call for call in engine.calls if call[0] == "turn_on"]
    await restored.async_close()
    again = await restore(engine)
    assert again._blocked  # review survives the next clean pump restart
    assert "pump_shutdown_reconciled_after_restart" in again.recovery_assessment.reasons
    await again.async_close()


@pytest.mark.parametrize("stage", ["reserved", "starting", "running", "stopping"])
async def test_restart_keeps_original_stage_and_never_replays(
    engine, hass_storage, stage
):
    await engine.async_close()
    key = f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "operations": {"old": {"state": stage, "accounted": False}},
        },
    }
    restored = await restore(engine)
    record = restored._journal["operations"]["old"]
    assert record["restart_original_state"] == stage
    assert record["state"] == "interrupted" and record["recovery_required"]
    assert "interrupted_shot" in restored.recovery_assessment.reasons
    assert restored.snapshot is None and not restored.execution_available
    assert engine.calls == []
    await restored.async_close()


async def test_failed_activation_has_no_success_and_unknown_volume(engine, monkeypatch):
    monkeypatch.setattr(
        engine.controller, "async_activate", AsyncMock(side_effect=PumpError("failed"))
    )
    result = await engine.async_execute("failed")
    record = engine._journal["operations"]["failed"]
    assert not result.accounted and record["outcome"] == "failed_activation"
    assert record["delivery_uncertain"] and record["delivered_volume"] is None
    assert engine._blocked and engine.calls == []


async def test_unconfirmed_shutdown_retains_history_and_blocks(engine, monkeypatch):
    task = await running(engine)
    monkeypatch.setattr(engine.controller, "async_stop", AsyncMock(return_value=False))
    engine._interrupt("test")
    result = await task
    record = engine._journal["operations"]["uncertain"]
    assert result.state == ShotState.FAULT and not result.accounted
    assert record["outcome"] == "unconfirmed_shutdown"
    assert not record["shutdown_confirmed"] and record["recovery_required"]
    assert engine._blocked and record["delivered_volume"] is None


async def test_missed_reset_blocks_even_when_counters_are_zero(
    engine, hass_storage, shot_clock
):
    await engine.async_close()
    key = f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "operations": {},
            "last_accounting": {"cycle_id": "2026-10-12T06:00:00+00:00", "values": {}},
        },
    }
    restored = await restore(engine)
    assert restored._blocked
    assert "missed_sunrise_or_cycle_reset" in restored.recovery_assessment.reasons
    assert engine.calls == []
    await restored.async_close()


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"pump_unresolved": True}, "unconfirmed_pump_shutdown"),
        ({"storage_failed": True}, "storage_inconsistency"),
        ({"cycle": {"pending": True}}, "incomplete_cycle_transition"),
        (
            {"cycle_reason": "missed_sunrise_requires_review"},
            "missed_sunrise_or_cycle_reset",
        ),
        (
            {
                "journal": {
                    "operations": {
                        "x": {"state": "completed", "accounting_pending": True}
                    }
                }
            },
            "incomplete_accounting",
        ),
        ({"sensor_reasons": ("sensor_quorum_failed",)}, "sensor_unavailability"),
    ],
)
async def test_structured_recovery_categories(change, reason):
    args = dict(
        journal={"operations": {}},
        pump_unresolved=False,
        cycle=None,
        cycle_reason=None,
        sensor_reasons=(),
    )
    args.update(change)
    result = assess_recovery(**args)
    assert reason in result.reasons
    assert result.restart == "unsafe_recovery" and result.review_required
    assert not result.diagnostic()["native_activation_allowed"]


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize(
    "unsafe", [None, "pump", "state", "ownership", "sensors", "recovery"]
)
async def test_migration_and_rollback_review_never_activate(engine, rollback, unsafe):
    if unsafe == "sensors":
        engine.hass.states.async_set("sensor.vwc", "unavailable")
    if unsafe == "recovery":
        engine._journal["recovery_required"] = True
    result = migration_eligibility(
        engine.recovery_assessment,
        engine.sensor_assessment(),
        None if unsafe == "ownership" else engine.ownership,
        "switch.pump",
        pump_off_verified=unsafe != "pump",
        state_reconciled=unsafe != "state",
        rollback=rollback,
    )
    assert result["review_eligible"] is (unsafe is None)
    assert result["procedure"] == ("rollback" if rollback else "migration")
    assert result["native_activation_allowed"] is False
    assert engine.calls == []


@pytest.mark.parametrize("fault", ["tray", "drain", "sensor", "stale"])
async def test_completion_trigger_with_fault_does_not_capture_reference(
    engine, shot_clock, fault
):
    shot_clock.move_to("2026-10-13T06:00:00+00:00")
    engine.hass.config_entries.async_update_entry(
        engine.entry,
        options={
            **engine.entry.options,
            "p1_active": False,
            "p1_done": False,
            "p1_window_opened_today": False,
            "p2_ref_vwc": 0,
            "p1_shots_done": 0,
            "p2_shots_done": 0,
            "p3_emergency_shots_done": 0,
        },
    )
    cycle = _NativeCycleController(engine)
    await cycle.async_initialize()
    assert await cycle.async_sunrise()
    shot_clock.move_to("2026-10-13T07:00:00+00:00")
    await cycle.async_observe()
    engine.hass.states.async_set("sensor.vwc", "60")
    if fault == "tray":
        engine.hass.states.async_set("binary_sensor.tray", "on")
    elif fault == "drain":
        engine.hass.states.async_set("binary_sensor.drain", "unavailable")
    elif fault == "sensor":
        peers(engine, "sensor.vwc", "sensor.missing")
    else:
        shot_clock.at += timedelta(seconds=301)
    await cycle.async_observe()
    assert (
        not engine.entry.options["p1_done"] and engine.entry.options["p2_ref_vwc"] == 0
    )
    await cycle.async_close()


@pytest.mark.parametrize("naive", [False, True])
async def test_timestamp_timezone_handling(engine, shot_clock, naive):
    from datetime import timezone

    from homeassistant.core import State

    stamp = (
        shot_clock.at.replace(tzinfo=None)
        if naive
        else shot_clock.at.astimezone(timezone(timedelta(hours=2)))
    )
    state = State("sensor.vwc", "40", last_updated=stamp)
    result = engine.sensor_assessment({"sensor.vwc": state})
    assert (not result.reasons) is (not naive)
    if naive:
        assert result.rejected["sensor.vwc"] == "invalid_timestamp"


async def test_stale_corruption_cannot_be_ignored_by_quorum(engine, shot_clock):
    peers(engine, "sensor.vwc", "sensor.peer")
    engine.sensor_policy = SensorPolicy(minimum_percent=50)
    engine.hass.states.async_set(
        "sensor.peer",
        "nan",
        timestamp=(shot_clock.at - timedelta(seconds=301)).timestamp(),
    )
    assert "invalid_vwc" in engine.sensor_assessment().reasons


@pytest.mark.parametrize(
    "field",
    [
        "legacy_relinquished",
        "related_writers_reviewed",
        "exclusive_control_verified",
        "simulated_pump",
    ],
)
async def test_truthy_nonboolean_acknowledgment_is_rejected(engine, field):
    engine.ownership = replace(engine.ownership, **{field: "false"})
    with pytest.raises(ShotRejected):
        await engine.async_execute()
    assert engine.calls == []


async def test_delivery_diagnostics_keep_attempts_separate_from_successes(
    engine, shot_clock
):
    assert (await engine.async_execute("done")).accounted
    shot_clock.move_to("2026-10-13T09:00:00+00:00")  # Respect existing soak.
    task = await running(engine)
    engine._interrupt("stop")
    assert not (await task).accounted
    assert engine.delivery_assessment == {
        "reservations": 2,
        "successful_shots": 1,
        "uncertain_attempts": 1,
        "delivered_volume": None,
        "replacement_blocked": True,
    }


async def test_clean_committed_cycle_still_requires_review_without_unsafe_classification():
    assessment = assess_recovery(
        journal={"operations": {}},
        pump_unresolved=False,
        cycle={"pending": False},
        cycle_reason="cycle_restart_requires_review",
        sensor_reasons=(),
    )
    assert assessment.restart == "clean_restart" and assessment.review_required
    assert not assessment.native_activation_allowed


async def test_completed_without_confirmed_accounting_requires_review(
    engine, hass_storage
):
    await engine.async_close()
    key = f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "operations": {"old": {"state": "completed", "accounted": False}},
        },
    }
    restored = await restore(engine)
    assert restored._blocked
    assert "incomplete_accounting" in restored.recovery_assessment.reasons
    assert engine.calls == []
    await restored.async_close()


async def test_missing_success_checkpoint_is_not_a_clean_restart(engine, hass_storage):
    assert (await engine.async_execute("done")).accounted
    await engine.async_close()
    key = f"{DOMAIN}.shot_journal.{engine.entry.entry_id}"
    del hass_storage[key]["data"]["last_accounting"]
    restored = await restore(engine)
    assert restored._blocked
    assert "incomplete_accounting" in restored.recovery_assessment.reasons
    assert restored._journal["operations"]["done"]["accounted"] is True
    assert len([call for call in engine.calls if call[0] == "turn_on"]) == 1
    await restored.async_close()
