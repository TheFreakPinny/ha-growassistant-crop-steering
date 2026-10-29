"""Read-only plans over isolated FakePump evidence; no real HA dispatch permitted."""

import asyncio
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from custom_components.growassistant_crop_steering.recovery_plan import (
    PlanFinding,
    _status,
    async_create_reconciliation_plan,
)

from .harness import NativeHarness
from .test_recovery_ledger import acknowledgment, case_for, ledger_for
from .test_simulation import assert_recovery_locked

pytestmark = pytest.mark.asyncio


def codes(plan):
    return {finding.code for finding in plan.findings}


async def assert_read_only(harness, ledger):
    before = harness.checkpoint(), deepcopy(ledger.store.records)
    audit = ledger.inspect()
    commands = list(harness.actor.commands)
    plan = await async_create_reconciliation_plan(ledger)
    assert (harness.checkpoint(), ledger.store.records) == before
    assert ledger.inspect() == audit
    assert harness.actor.commands == commands
    assert plan.native_activation_allowed is False
    assert_recovery_locked(harness)
    return plan


async def test_plan_binds_all_evidence_and_never_mutates(simulation):
    ledger, rid = await case_for(simulation)
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked"
    assert plan.recovery_ids == (rid,)
    assert plan.operation_ids == ("interrupted",)
    assert plan.cycle_ids == (simulation.cycle.cycle_id,)
    assert plan.binding == ledger.inspect()["cases"][rid]["binding"]
    assert plan.source_revisions["ledger"] == 1
    assert (
        plan.source_revisions["pump"]["revision"]
        == simulation.pump.safety_snapshot().version.revision
    )
    assert plan.source_revisions["pump"]["receipt"]["digest"]
    assert plan.source_revisions["pump"]["runtime"]
    assert plan.policy_version == "read_only_reconciliation_v1"
    assert plan.schema_version == 1
    assert plan.operations["interrupted"]["recovery_id"] == rid
    assert plan.operations["interrupted"]["volume"] is None
    assert "acknowledgment_missing" in codes(plan)
    assert "partial_delivery_unresolved" in codes(plan)
    assert len(plan.evidence_digests["participants"]) == 64
    assert plan.evidence_digests["cases"][rid] == plan.cases[rid]["evidence"]["digest"]
    with pytest.raises(TypeError):
        plan.evidence["participants"]["content"][1]["journal"]["operations"]["new"] = {}
    with pytest.raises(FrozenInstanceError):
        plan.status = "preparation_eligible"
    again = await assert_read_only(simulation, ledger)
    assert again.plan_id != plan.plan_id
    assert again.evidence_digests == plan.evidence_digests


async def test_acknowledgment_cannot_resolve_partial_delivery(simulation):
    ledger, rid = await case_for(simulation)
    await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
    plan = await assert_read_only(simulation, ledger)
    assert "acknowledgment_missing" not in codes(plan)
    assert "partial_delivery_unresolved" in codes(plan)
    assert plan.status == "blocked"
    assert plan.operations["interrupted"]["acknowledged"]
    assert plan.source_revisions["ledger"] == 2


@pytest.mark.parametrize("result", ["unknown", "failed"])
async def test_negative_shutdown_review_remains_blocked(simulation, result):
    ledger, rid = await case_for(simulation)
    request = acknowledgment(ledger, rid, simulation)
    request["review"]["shutdown"]["result"] = result
    await ledger.async_acknowledge(**request)
    plan = await assert_read_only(simulation, ledger)
    assert "review_shutdown_unresolved" in codes(plan)
    assert plan.status == "blocked"


@pytest.mark.parametrize(
    "change",
    [
        "accounting",
        "checkpoint",
        "counter",
        "cycle_counter",
        "cycle_phase",
        "p1",
        "p2_reference",
        "cycle_boundary",
        "off",
        "ownership",
        "legacy",
        "scheduler",
    ],
)
async def test_source_conflicts_are_visible_without_repair(
    simulation, monkeypatch, change
):
    ledger, _ = await case_for(simulation)
    if change in {"accounting", "checkpoint", "counter", "cycle_boundary"}:
        original = simulation.shots.safety_snapshot()
        journal = deepcopy(simulation.records["shots"])
        if change == "accounting":
            journal["operations"]["interrupted"]["accounting_pending"] = True
        elif change == "checkpoint":
            journal["last_accounting"] = {
                "cycle_id": "wrong",
                "values": {"p1_shots_done": 7},
            }
        elif change == "counter":
            journal["native_counters"] = {"p1_shots_done": -1}
        else:
            journal["missed_cycle_reset"] = True
        monkeypatch.setattr(
            simulation.shots,
            "safety_snapshot",
            lambda: replace(original, journal=journal),
        )
        monkeypatch.setattr(
            simulation.shots.store, "async_load", AsyncMock(return_value=journal)
        )
        expected = (
            "cycle_boundary_unresolved"
            if change == "cycle_boundary"
            else "accounting_inconsistent"
        )
    elif change.startswith("cycle_") or change in {"p1", "p2_reference"}:
        original = simulation.cycle.safety_snapshot()
        record = deepcopy(simulation.records["cycle"])
        if change == "cycle_phase":
            record["phase"] = "p3_dryback"
        elif change == "p1":
            record["values"]["p1_done"] = True
        elif change == "p2_reference":
            record["values"]["p2_ref_vwc"] = -1
        else:
            record["counters"]["p1_shots_done"] = 99
        monkeypatch.setattr(
            simulation.cycle,
            "safety_snapshot",
            lambda: replace(original, record=record),
        )
        monkeypatch.setattr(
            simulation.cycle.store, "async_load", AsyncMock(return_value=record)
        )
        expected = (
            "accounting_inconsistent"
            if change == "cycle_counter"
            else "cycle_inconsistent"
        )
    elif change == "off":
        original = simulation.pump.safety_snapshot()
        monkeypatch.setattr(
            simulation.pump,
            "safety_snapshot",
            lambda: replace(
                original, off=replace(original.off, state=None, verified_report_at=None)
            ),
        )
        expected = "off_missing"
    elif change in {"ownership", "legacy"}:
        evidence = simulation.shots.ownership
        simulation.shots.ownership = replace(
            evidence,
            **(
                {"pump_entity": "switch.other"}
                if change == "ownership"
                else {"legacy_relinquished": False}
            ),
        )
        expected = "ownership_conflict"
    else:
        simulation.scheduler._pending = lambda: None
        expected = "scheduler_unresolved"
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked"
    assert expected in codes(plan)
    assert "case_evidence_changed" in codes(plan) or change in {
        "ownership",
        "legacy",
        "scheduler",
    }


@pytest.mark.parametrize("damage", ["hash", "schema", "missing", "projection"])
async def test_durable_ledger_is_replayed_and_compared(simulation, damage):
    ledger, _ = await case_for(simulation)
    if damage == "hash":
        ledger.store.records["ledger"]["events"][0]["hash"] = "broken"
    elif damage == "schema":
        ledger.store.records["ledger"]["schema_version"] = 99
    elif damage == "missing":
        ledger.store.records.clear()
    else:
        ledger._cases.clear()
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked" and not plan.evidence
    assert codes(plan) & {
        "ledger_corrupt",
        "ledger_schema_unknown",
        "ledger_not_durable",
    }


@pytest.mark.parametrize(
    "change", ["ledger", "ownership", "disk_only", "live", "options"]
)
async def test_concurrent_changes_invalidate_entire_capture(
    simulation, monkeypatch, change
):
    ledger, rid = await case_for(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ledger.source.capture

    first = True

    async def held():
        nonlocal first
        value = await original()
        if first:
            first = False
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(ledger.source, "capture", held)
    before_commands = list(simulation.actor.commands)
    task = asyncio.create_task(async_create_reconciliation_plan(ledger))
    await entered.wait()
    if change == "ledger":
        await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
    elif change == "ownership":
        simulation.shots.ownership = None
    elif change == "disk_only":
        ledger.store.records["ledger"]["revision"] += 1
    elif change == "live":
        simulation.shots._journal["missed_cycle_reset"] = True
    else:
        simulation.hass.config_entries.async_update_entry(
            simulation.entry, options={**simulation.entry.options, "p1_shots_done": 7}
        )
    release.set()
    plan = await task
    assert plan.status == "blocked" and not plan.evidence
    assert codes(plan) & {"evidence_changed", "evidence_not_durable"}
    assert simulation.actor.commands == before_commands


async def test_restart_retains_history_without_recreating_authority(
    simulation, monkeypatch, restarted_hass
):
    records = {}
    ledger, rid = await case_for(simulation, records)
    await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
    before = await assert_read_only(simulation, ledger)
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        fresh = await ledger_for(restored, records)
        after = await assert_read_only(restored, fresh)
        assert after.status == "blocked"
        assert after.recovery_ids == before.recovery_ids
        assert after.source_revisions["ledger"] == 2
        assert "case_evidence_changed" in codes(after)
        assert "ownership_conflict" in codes(after)
        assert not restored.actor.commands
    finally:
        await restored.close()


async def test_real_store_plan_preserves_bytes_and_does_not_call_load(
    simulation, monkeypatch
):
    ledger, _ = await case_for(simulation)
    from homeassistant.helpers.storage import Store

    from custom_components.growassistant_crop_steering.const import DOMAIN

    stores = (
        simulation.pump.store,
        simulation.shots.store,
        simulation.cycle.store,
        ledger.store,
    )
    participants = (simulation.pump, simulation.shots, simulation.cycle, ledger)
    paths = []
    for participant, store, suffix in zip(
        participants,
        stores,
        ("pump_recovery", "shot_journal", "native_cycle", "recovery_ledger"),
        strict=True,
    ):
        real = Store(
            simulation.hass, 1, f"{DOMAIN}.{suffix}.{simulation.entry.entry_id}"
        )
        path = Path(real.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await store.async_load()
        path.write_text(json.dumps({"version": 1, "key": real.key, "data": data}))
        paths.append(path)
        monkeypatch.setattr(
            real,
            "async_load",
            AsyncMock(side_effect=AssertionError("mutating loader forbidden")),
        )
        monkeypatch.setattr(
            real,
            "async_save",
            AsyncMock(side_effect=AssertionError("writes forbidden")),
        )
        monkeypatch.setattr(participant, "store", real)
    before = {path: path.read_bytes() for path in paths}
    commands = list(simulation.actor.commands)
    plan = await async_create_reconciliation_plan(ledger)
    assert plan.evidence and plan.status == "blocked"
    assert {path: path.read_bytes() for path in paths} == before
    assert simulation.actor.commands == commands


async def test_status_policy_has_no_activation_capability():
    review = PlanFinding(
        "acknowledgment_missing", "ledger", "missing", "review", "review_required"
    )
    block = PlanFinding(
        "partial_delivery_unresolved", "shots", "unknown", "retain lock"
    )
    assert _status(()) == "preparation_eligible"
    assert _status((review,)) == "review_required"
    assert _status((review, block)) == "blocked"


async def test_masked_legacy_helpers_are_not_accepted_as_migrated(simulation):
    ledger, _ = await case_for(simulation)
    simulation.hass.config_entries.async_update_entry(
        simulation.entry,
        data={**simulation.entry.data, "p1_shots_done": "counter.old_p1"},
    )
    plan = await assert_read_only(simulation, ledger)
    assert "legacy_conflict" in codes(plan)


async def test_unregistered_failure_cannot_be_hidden_by_empty_ledger(simulation):
    from .test_simulation import interrupted_p1

    await interrupted_p1(simulation)
    await simulation.hass.async_block_till_done()
    ledger = await ledger_for(simulation)
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked"
    assert plan.recovery_ids == ()
    assert not plan.evidence
    assert "ledger_corrupt" in codes(plan)


async def test_healthy_entry_does_not_create_a_recovery_scope_or_permit(simulation):
    await simulation.observe()
    ledger = await ledger_for(simulation)
    before = simulation.checkpoint(), deepcopy(ledger.store.records)
    plan = await async_create_reconciliation_plan(ledger)
    assert plan.status != "preparation_eligible"
    assert "recovery_case_missing" in codes(plan) or not plan.evidence
    assert plan.native_activation_allowed is False
    assert not plan.recovery_ids and not plan.operation_ids
    assert (simulation.checkpoint(), ledger.store.records) == before
    assert not simulation.actor.commands


async def test_busy_ledger_does_not_wait_or_mutate(simulation):
    ledger, _ = await case_for(simulation)
    async with ledger._lock:
        plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked" and "ledger_busy" in codes(plan)


async def test_cancelled_capture_never_writes_or_dispatches(simulation, monkeypatch):
    ledger, _ = await case_for(simulation)
    entered = asyncio.Event()

    async def held():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ledger.source, "capture", held)
    before = simulation.checkpoint(), deepcopy(ledger.store.records)
    commands = list(simulation.actor.commands)
    task = asyncio.create_task(async_create_reconciliation_plan(ledger))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (simulation.checkpoint(), ledger.store.records) == before
    assert simulation.actor.commands == commands
    assert_recovery_locked(simulation)


@pytest.mark.parametrize("partial", ["unknown", "confirmed", "possible"])
async def test_review_never_converts_unknown_water_to_accounted_volume(
    simulation, partial
):
    ledger, rid = await case_for(simulation)
    request = acknowledgment(ledger, rid, simulation)
    request["review"]["partial_delivery"] = partial
    request["review"]["disposition"] = "abandon_cycle_requires_reconciliation"
    await ledger.async_acknowledge(**request)
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked"
    assert {"partial_delivery_unresolved", "cycle_disposition_required"} <= codes(plan)
    assert plan.operations["interrupted"]["volume"] is None
    assert plan.operations["interrupted"]["accounted"] is False


@pytest.mark.parametrize("work", ["pump", "shots", "cycle"])
async def test_active_participant_work_is_never_drained_by_planning(simulation, work):
    ledger, _ = await case_for(simulation)
    lock = {
        "pump": simulation.pump._lock,
        "shots": simulation.shots._accounting_lock,
        "cycle": simulation.cycle._lock,
    }[work]
    async with lock:
        plan = await assert_read_only(simulation, ledger)
        assert lock.locked()
    assert plan.status == "blocked" and "evidence_busy" in codes(plan)


async def test_completed_accounting_and_cycle_baseline_are_retained(simulation):
    from .test_simulation import running, start_p1

    await start_p1(simulation)
    completed = await simulation.shots.async_execute("completed")
    assert completed.accounted
    simulation.move_to("2026-10-13T07:01:00+00:00")
    task = await running(simulation, "interrupted")
    simulation.vwc(40, "unavailable", 40)
    assert not (await task).accounted
    await simulation.hass.async_block_till_done()
    ledger = await ledger_for(simulation)
    registered = await ledger.async_register(request_id="register", expected_revision=0)
    before = simulation.checkpoint(), deepcopy(ledger.store.records)
    commands = list(simulation.actor.commands)
    plan = await async_create_reconciliation_plan(ledger)
    assert plan.status == "blocked"
    assert plan.evidence
    assert "accounting_checkpoint_unbound" not in codes(plan)
    assert not any(
        f.code == "accounting_inconsistent" and f.source == "cycle"
        for f in plan.findings
    )
    # Existing validators still flag the interrupted operation's absent pending
    # marker; retaining the prior completed booking must not waive that finding.
    assert any(
        f.code == "accounting_inconsistent" and "interrupted" in f.detail
        for f in plan.findings
    )
    assert plan.operations["completed"]["disposition"] == "retain_accounted"
    assert plan.operations["completed"]["accounted"] is True
    assert plan.operations["interrupted"]["recovery_id"] == registered["recovery_id"]
    assert simulation.records["cycle"]["counters"]["p1_shots_done"] == 0
    assert simulation.entry.options["p1_shots_done"] == 1
    assert (simulation.checkpoint(), ledger.store.records) == before
    assert simulation.actor.commands == commands


@pytest.mark.parametrize(
    "checkpoint",
    [
        123,
        {"values": []},
        {
            "cycle_id": "wrong",
            "values": {
                "p1_shots_done": 1,
                "last_shot_type": "p1",
                "last_shot": "unknown",
            },
        },
    ],
)
async def test_malformed_or_unbound_checkpoints_never_appear_reconciled(
    simulation, monkeypatch, checkpoint
):
    ledger, _ = await case_for(simulation)
    original = simulation.shots.safety_snapshot()
    journal = deepcopy(simulation.records["shots"])
    journal["last_accounting"] = checkpoint
    monkeypatch.setattr(
        simulation.shots, "safety_snapshot", lambda: replace(original, journal=journal)
    )
    monkeypatch.setattr(
        simulation.shots.store, "async_load", AsyncMock(return_value=journal)
    )
    plan = await assert_read_only(simulation, ledger)
    assert plan.status == "blocked"
    assert codes(plan) & {"accounting_inconsistent", "accounting_checkpoint_unbound"}
