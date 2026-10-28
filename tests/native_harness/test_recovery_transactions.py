"""Phase four: only fixed FakePump, read-only participants and barrier-controlled IO."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from custom_components.growassistant_crop_steering.recovery_ledger import LedgerError
from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_transaction import (
    _RecoveryTransactionCoordinator,
    plan_digest,
)

from .harness import MemoryStore, NativeHarness
from .test_recovery_ledger import case_for, ledger_for

pytestmark = pytest.mark.asyncio


async def setup(harness, monkeypatch=None):
    await harness.observe()
    await harness.hass.async_block_till_done()
    assert await harness.pump.async_stop()
    await harness.hass.async_block_till_done()
    ledger = await ledger_for(harness)
    # Establish a real durable empty ledger through its Store, not a fabricated
    # eligible plan. No fault/case/journal is cleared or repaired.
    await ledger.store.async_save(
        {"schema_version": 1, "binding": ledger.binding, "revision": 0, "events": []}
    )
    plan = await async_create_reconciliation_plan(ledger)
    if monkeypatch is not None:
        # Protocol-model fixture ONLY. Current participant policy cannot supply
        # complete release proofs. Keep the real captured evidence; replace the
        # planner at the internal boundary to test persistence state transitions.
        # Real planner rejection is tested separately below; this is no proof of
        # safe recovery or of a preparation-eligible real participant set.
        plan = replace(plan, status="preparation_eligible", findings=(), open_checks=())

        async def model_plan(_ledger):
            return plan

        monkeypatch.setattr(
            "custom_components.growassistant_crop_steering.recovery_transaction.async_create_reconciliation_plan",
            model_plan,
        )
        # HA state timestamps use a different clock than the simulation clock.
        # Publish actual fresh reports through HA's normal state API.
        for key in ("drain_sensor", "drain_tray_sensor"):
            harness.hass.states.async_set(
                harness.entry.data[key],
                "off",
                force_update=True,
                timestamp=harness.clock.at.timestamp(),
            )
        await harness.hass.async_block_till_done()
    coordinator = _RecoveryTransactionCoordinator(ledger)
    coordinator.store = MemoryStore({}, "transactions")
    await coordinator.async_initialize()
    return coordinator, plan


def prepare(plan, **updates):
    return {
        "plan": plan,
        "expected_plan_digest": plan_digest(plan),
        "expected_revision": plan.source_revisions.get("ledger"),
        "request_id": "prepare",
        **updates,
    }


def commit(result, plan, **updates):
    return {
        "transaction_id": result["transaction_id"],
        "request_id": "commit",
        "expected_plan_digest": plan_digest(plan),
        **updates,
    }


async def test_technical_commit_is_only_diagnostic(simulation, monkeypatch):
    tx, plan = await setup(simulation, monkeypatch)
    assert plan.status == "preparation_eligible", ", ".join(
        str(f.code) for f in plan.findings
    )
    before, commands = simulation.checkpoint(), list(simulation.actor.commands)
    ready = await tx.async_prepare(**prepare(plan))
    result = await tx.async_commit(**commit(ready, plan))
    assert result["state"] == "committed" and result["historical_only"]
    assert result["native_activation_allowed"] is tx.native_activation_allowed is False
    assert tx.inspect()["effective_state"] == "locked"
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert not simulation.scheduler.running
    assert [e["kind"] for e in tx.inspect()["events"]] == [
        "prepared",
        "participants_checked",
        "committed",
    ]


async def test_actual_participants_cannot_prepare(simulation):
    tx, plan = await setup(simulation)
    before, commands = simulation.checkpoint(), list(simulation.actor.commands)
    with pytest.raises(LedgerError, match="plan_blocked"):
        await tx.async_prepare(**prepare(plan))
    assert not tx.inspect()["events"] and not tx.store.records
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert not tx.native_activation_allowed


async def test_real_recovery_case_retains_every_gate(simulation):
    from .test_simulation import assert_no_replay_or_accounting, assert_recovery_locked

    ledger, _ = await case_for(simulation)
    plan = await async_create_reconciliation_plan(ledger)
    tx = _RecoveryTransactionCoordinator(ledger)
    tx.store = MemoryStore({}, "transactions")
    await tx.async_initialize()
    before, commands = simulation.checkpoint(), list(simulation.actor.commands)
    with pytest.raises(LedgerError, match="plan_blocked"):
        await tx.async_prepare(**prepare(plan))
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)


async def test_idempotency_is_a_historical_receipt(simulation, monkeypatch):
    tx, plan = await setup(simulation, monkeypatch)
    request = prepare(plan)
    prepared = await tx.async_prepare(**request)
    assert await tx.async_prepare(**request) == prepared
    committed = await tx.async_commit(**commit(prepared, plan))
    assert await tx.async_commit(**commit(prepared, plan)) == committed
    tx.revoke("stop")
    assert await tx.async_prepare(**request) == prepared
    assert await tx.async_commit(**commit(prepared, plan)) == committed
    assert len(tx.inspect()["events"]) == 3
    assert tx.inspect()["blocked_reason"] == "stop"
    assert not committed["native_activation_allowed"]


@pytest.mark.parametrize("change", ["revision", "plan", "digest", "method"])
async def test_request_id_content_conflicts(simulation, monkeypatch, change):
    tx, plan = await setup(simulation, monkeypatch)
    result = await tx.async_prepare(**prepare(plan))
    request = prepare(plan)
    if change == "revision":
        request["expected_revision"] = True
    elif change == "plan":
        request["plan"] = replace(plan, plan_id="another")
    elif change == "digest":
        request["expected_plan_digest"] = "changed"
    else:
        with pytest.raises(LedgerError, match="request_conflict"):
            await tx.async_commit(**commit(result, plan, request_id="prepare"))
        return
    with pytest.raises(LedgerError, match="request_conflict"):
        await tx.async_prepare(**request)
    assert len(tx.inspect()["events"]) == 1


@pytest.mark.parametrize("identical", [False, True])
async def test_competing_requests_never_queue(simulation, monkeypatch, identical):
    tx, plan = await setup(simulation, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(tx.store, "async_save", held)
    first = asyncio.create_task(tx.async_prepare(**prepare(plan)))
    await entered.wait()
    with pytest.raises(LedgerError, match="transaction_busy"):
        await tx.async_prepare(
            **prepare(plan, request_id="prepare" if identical else "other")
        )
    release.set()
    result = await first
    assert len(tx.inspect()["events"]) == 1
    if identical:
        assert await tx.async_prepare(**prepare(plan)) == result
    with pytest.raises(LedgerError, match="resource_reserved"):
        await tx.async_prepare(**prepare(plan, request_id="next"))


async def test_resource_writer_lifetime_includes_failed_owner(simulation, monkeypatch):
    tx, _ = await setup(simulation, monkeypatch)
    tx.revoke("stop")
    with pytest.raises(LedgerError, match="writer_exists"):
        _RecoveryTransactionCoordinator(tx.ledger)


@pytest.mark.parametrize(
    "change", ["revision", "digest", "plan", "binding", "cycle", "checks"]
)
async def test_stale_or_altered_plan_rejected(simulation, monkeypatch, change):
    tx, plan = await setup(simulation, monkeypatch)
    request = prepare(plan)
    if change == "revision":
        request["expected_revision"] += 1
    elif change == "digest":
        request["expected_plan_digest"] = "bad"
    else:
        altered = {
            "plan": {"operations": {"hidden": {"cycle_id": "wrong"}}},
            "binding": {"binding": {**dict(plan.binding), "entity_id": "switch.other"}},
            "cycle": {"cycle_ids": ("wrong",)},
            "checks": {"open_checks": ("unresolved",)},
        }[change]
        request = prepare(replace(plan, **altered))
    with pytest.raises(LedgerError):
        await tx.async_prepare(**request)
    assert not tx.store.records


@pytest.mark.parametrize(
    "kind",
    [
        "pump_service",
        "pump_storage",
        "late",
        "shot",
        "accounting",
        "cycle",
        "timer",
        "evaluation",
    ],
)
async def test_unresolved_work_blocks_intent(simulation, monkeypatch, kind):
    tx, plan = await setup(simulation, monkeypatch)
    held = asyncio.create_task(asyncio.Event().wait())
    try:
        if kind == "pump_service":
            simulation.pump._pending.add(held)
        elif kind == "pump_storage":
            simulation.pump._storage_pending.add(held)
        elif kind == "late":
            simulation.pump._timed_out[held] = "turn_on"
            simulation.pump._pending.add(held)
        elif kind == "shot":
            simulation.shots._task = held
        elif kind == "accounting":
            await simulation.shots._accounting_lock.acquire()
        elif kind == "cycle":
            await simulation.cycle._lock.acquire()
        elif kind == "timer":
            simulation.scheduler._timer = lambda: None
        else:
            simulation.scheduler._pending = lambda: None
        with pytest.raises(LedgerError):
            await tx.async_prepare(**prepare(plan))
        assert not tx.store.records
    finally:
        held.cancel()
        await asyncio.gather(held, return_exceptions=True)
        simulation.pump._pending.discard(held)
        simulation.pump._storage_pending.discard(held)
        simulation.pump._timed_out.pop(held, None)
        if kind == "shot":
            simulation.shots._task = None
        if simulation.shots._accounting_lock.locked():
            simulation.shots._accounting_lock.release()
        if simulation.cycle._lock.locked():
            simulation.cycle._lock.release()
        simulation.scheduler._timer = simulation.scheduler._pending = None


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize(
    "change",
    [
        "stop",
        "sensor",
        "ownership",
        "config",
        "legacy",
        "ledger",
        "journal",
        "day",
        "pump",
        "target",
    ],
)
async def test_changes_during_each_write_are_fail_closed(
    simulation, monkeypatch, boundary, change
):
    tx, plan = await setup(simulation, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.store.async_save
    count = 0

    async def held(data):
        nonlocal count
        count += 1
        if count == boundary:
            entered.set()
            await release.wait()
        await original(data)

    monkeypatch.setattr(tx.store, "async_save", held)

    async def protocol():
        result = await tx.async_prepare(**prepare(plan))
        return await tx.async_commit(**commit(result, plan))

    task = asyncio.create_task(protocol())
    await entered.wait()
    commands = list(simulation.actor.commands)
    if change == "stop":
        tx.revoke("stop")
    elif change == "sensor":
        simulation.vwc(40, "unavailable", 40)
    elif change == "ownership":
        simulation.shots.ownership = None
    elif change in {"config", "target", "legacy"}:
        simulation.hass.config_entries.async_update_entry(
            simulation.entry,
            data={
                **simulation.entry.data,
                **(
                    {"pump_switch": "switch.other"}
                    if change == "target"
                    else {"p1_shots_done": "counter.legacy"}
                    if change == "legacy"
                    else {"led_sunrise": "05:00:00"}
                ),
            },
        )
    elif change == "ledger":
        tx.ledger._blocked = "new_recovery_case"
    elif change == "journal":
        simulation.shots._journal["new_case"] = "unregistered_failure"
    elif change == "day":
        simulation.clock.move_to("2026-10-14T06:00:00+00:00")
    else:
        simulation.actor.report("on")
    await simulation.hass.async_block_till_done()
    release.set()
    with pytest.raises(LedgerError):
        await task
    assert tx.inspect()["blocked_reason"] is not None
    assert tx.inspect()["effective_state"] == "locked"
    assert simulation.actor.commands == commands
    assert all(
        simulation.entry.options[k] == 0
        for k in ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")
    )
    if change == "pump":
        simulation.actor.report("off")


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("failure", ["io", "timeout", "cancel"])
async def test_storage_uncertainty_cannot_be_overtaken(
    simulation, monkeypatch, boundary, failure
):
    tx, plan = await setup(simulation, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.store.async_save
    count = 0

    async def held(data):
        nonlocal count
        count += 1
        if count == boundary:
            entered.set()
            if failure == "io":
                raise OSError("storage failure")
            await release.wait()
        await original(data)

    monkeypatch.setattr(tx.store, "async_save", held)

    async def protocol():
        result = await tx.async_prepare(
            **prepare(plan, timeout=0 if failure == "timeout" and boundary == 1 else 5)
        )
        return await tx.async_commit(
            **commit(result, plan, timeout=0 if failure == "timeout" else 5)
        )

    task = asyncio.create_task(protocol())
    await entered.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises((OSError, LedgerError, asyncio.CancelledError)):
        await task
    tx.revoke("later_stop")
    assert tx.inspect()["blocked_reason"] == "storage_uncertain"
    with pytest.raises(LedgerError, match="transaction_unavailable"):
        await tx.async_prepare(**prepare(plan, request_id="younger"))
    with pytest.raises(LedgerError, match="transaction_unavailable"):
        await tx.async_record_revocation(request_id="audit")
    release.set()
    await asyncio.gather(tx._write, return_exceptions=True)
    assert len(tx.inspect()["events"]) == boundary - 1
    if failure != "io":
        assert len(tx.store.records["transactions"]["events"]) == boundary
    assert not tx.native_activation_allowed


@pytest.mark.parametrize("prefix", [0, 1, 2, 3])
async def test_restart_at_every_durable_prefix(
    simulation, monkeypatch, restarted_hass, prefix
):
    tx, plan = await setup(simulation, monkeypatch)
    result = await tx.async_prepare(**prepare(plan))
    await tx.async_commit(**commit(result, plan))
    records = deepcopy(tx.store.records)
    records["transactions"]["events"] = records["transactions"]["events"][:prefix]
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        ledger = await ledger_for(restored, deepcopy(tx.ledger.store.records))
        fresh = _RecoveryTransactionCoordinator(ledger)
        fresh.store = MemoryStore(records, "transactions")
        before = restored.checkpoint()
        await fresh.async_initialize()
        assert len(fresh.inspect()["events"]) == prefix
        assert fresh.inspect()["effective_state"] == "locked"
        assert fresh._session is None and not fresh.native_activation_allowed
        assert restored.checkpoint() == before and not restored.actor.commands
        assert not restored.scheduler.running
        if prefix:
            with pytest.raises(LedgerError, match="transaction_unavailable"):
                await fresh.async_commit(**commit(result, plan, request_id="new"))
            assert await fresh.async_prepare(**prepare(plan)) == result
    finally:
        await restored.close()


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "binding",
        "hash",
        "proof",
        "sequence",
        "intent",
        "commit",
        "id",
        "request",
    ],
)
async def test_corrupt_history_never_repaired(
    simulation, monkeypatch, restarted_hass, damage
):
    tx, plan = await setup(simulation, monkeypatch)
    result = await tx.async_prepare(**prepare(plan))
    await tx.async_commit(**commit(result, plan))
    data = deepcopy(tx.store.records["transactions"])
    if damage == "schema":
        data["schema_version"] = 2
    elif damage == "binding":
        data["binding"]["entity_id"] = "switch.other"
    elif damage == "proof":
        del data["events"][1]["payload"]["pump"]
    elif damage == "sequence":
        data["events"][0]["sequence"] = True
    elif damage == "intent":
        del data["events"][0]["payload"]["plan"]["source_revisions"]
    elif damage == "commit":
        data["events"][2]["payload"]["native_activation_allowed"] = True
    elif damage == "id":
        data["events"][1]["transaction_id"] = "invalid"
    elif damage == "request":
        data["events"][1]["request_id"] = "prepare"
    else:
        data["events"][0]["hash"] = "broken"
    # Recompute chain for structural corruption tests: schema validation must
    # reject this too, not just an accidental content-hash mismatch.
    if damage != "hash":
        from custom_components.growassistant_crop_steering.recovery_ledger import (
            _digest,
        )

        previous = None
        for event in data["events"]:
            event["previous_hash"] = previous
            event["hash"] = _digest({k: v for k, v in event.items() if k != "hash"})
            previous = event["hash"]
    ledger = tx.ledger
    # Separate runtime owner registry; no original owner is replaced.
    ledger.hass = restarted_hass
    fresh = _RecoveryTransactionCoordinator(ledger)
    fresh.store = MemoryStore({"transactions": data}, "transactions")
    before = deepcopy(fresh.store.records)
    with pytest.raises((LedgerError, ValueError, KeyError)):
        await fresh.async_initialize()
    assert fresh.store.records == before
    assert fresh.inspect()["blocked_reason"] == "transaction_load_failed"
    assert not fresh.inspect()["events"]


@pytest.mark.parametrize("committed", [False, True])
async def test_explicit_revocation_audit(simulation, monkeypatch, committed):
    tx, plan = await setup(simulation, monkeypatch)
    result = await tx.async_prepare(**prepare(plan))
    if committed:
        await tx.async_commit(**commit(result, plan))
    tx.revoke("ownership_lost")
    receipt = await tx.async_record_revocation(request_id="revoke")
    assert receipt["state"] == ("revoked" if committed else "aborted")
    assert await tx.async_record_revocation(request_id="revoke") == receipt
    assert tx.inspect()["effective_state"] == "locked"


async def test_event_aba_is_revoked(simulation, monkeypatch):
    tx, plan = await setup(simulation, monkeypatch)
    await tx.async_prepare(**prepare(plan))
    simulation.actor.report("on")
    simulation.actor.report("off")
    await simulation.hass.async_block_till_done()
    assert tx.inspect()["blocked_reason"] == "runtime_event"
    assert not tx.native_activation_allowed


async def test_missing_proof_blocks_commit_even_with_recomputed_hash(
    simulation, monkeypatch
):
    tx, plan = await setup(simulation, monkeypatch)
    result = await tx.async_prepare(**prepare(plan))
    # Validator must use the fixed contract, not accept arbitrary proof producers.
    # Independently remove proof data in the append boundary instead.
    original = tx._append

    async def incomplete(kind, tid, rid, request, payload, timeout):
        if kind == "participants_checked":
            payload = {}
        return await original(kind, tid, rid, request, payload, timeout)

    monkeypatch.setattr(tx, "_append", incomplete)
    with pytest.raises(LedgerError, match="participant_proof_missing"):
        await tx.async_commit(**commit(result, plan))
    assert len(tx.inspect()["events"]) == 1
    assert tx.inspect()["blocked_reason"] == "commit_failed"


@pytest.mark.parametrize("stage", ["before_intent", "after_intent", "after_proofs"])
async def test_cancel_during_participant_validation(simulation, monkeypatch, stage):
    tx, plan = await setup(simulation, monkeypatch)
    original = tx._revalidate
    entered = asyncio.Event()
    count = 0
    boundary = {"before_intent": 1, "after_intent": 3, "after_proofs": 4}[stage]

    async def held(*args):
        nonlocal count
        count += 1
        if count == boundary:
            entered.set()
            await asyncio.Event().wait()
        await original(*args)

    monkeypatch.setattr(tx, "_revalidate", held)

    async def protocol():
        result = await tx.async_prepare(**prepare(plan))
        await tx.async_commit(**commit(result, plan))

    task = asyncio.create_task(protocol())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        len(tx.inspect()["events"])
        == {"before_intent": 0, "after_intent": 1, "after_proofs": 2}[stage]
    )
    assert tx.inspect()["effective_state"] == "locked"
    assert not tx.native_activation_allowed
    if stage != "before_intent":
        await tx.async_record_revocation(request_id="audit")
        assert tx.inspect()["state"] == "aborted"


@pytest.mark.parametrize("kind", ["shot_storage", "cycle_storage", "safety_task"])
async def test_additional_pending_writes_are_diagnosed(simulation, monkeypatch, kind):
    tx, plan = await setup(simulation, monkeypatch)
    held = asyncio.create_task(asyncio.Event().wait())
    try:
        if kind == "shot_storage":
            simulation.shots._storage_task = held
        elif kind == "cycle_storage":
            simulation.cycle._write = held
        else:
            simulation.pump._safety_tasks.add(held)
        assert tx.inspect_drain()["observed_idle"] is False
        assert tx.inspect_drain()["quiesce_guaranteed"] is False
        with pytest.raises(LedgerError, match="drain_unresolved"):
            await tx.async_prepare(**prepare(plan))
    finally:
        held.cancel()
        await asyncio.gather(held, return_exceptions=True)
        simulation.shots._storage_task = None
        simulation.cycle._write = None
        simulation.pump._safety_tasks.discard(held)


@pytest.mark.parametrize("kind", ["sensor", "tray", "drain", "restored", "stale"])
async def test_initial_unsafe_sensor_rejects_before_intent(
    simulation, monkeypatch, kind
):
    tx, plan = await setup(simulation, monkeypatch)
    if kind == "sensor":
        simulation.vwc(40, "unavailable", 40)
    else:
        entity = simulation.entry.data[
            "drain_tray_sensor" if kind == "tray" else "drain_sensor"
        ]
        simulation.hass.states.async_set(
            entity,
            "on" if kind in {"tray", "drain"} else "off",
            attributes={"restored": kind == "restored"},
            force_update=True,
            timestamp=simulation.clock.at.timestamp() - (31 if kind == "stale" else 0),
        )
    await simulation.hass.async_block_till_done()
    with pytest.raises(LedgerError, match="sensor_unsafe|drain_unsafe"):
        await tx.async_prepare(**prepare(plan))
    assert not tx.store.records


async def test_real_ha_store_roundtrip_is_technical_history(
    simulation, monkeypatch, tmp_path
):
    import json
    from pathlib import Path

    from custom_components.growassistant_crop_steering.recovery_ledger import (
        _LedgerStore,
    )

    from .test_recovery_ledger import REAL_LOAD, REAL_WRITE

    with monkeypatch.context() as patch:
        tx, plan = await setup(simulation, monkeypatch)
        tx.hass.config.config_dir = str(tmp_path)
        tx.store = _LedgerStore(
            tx.hass,
            1,
            f"growassistant_crop_steering.recovery_transactions.{simulation.entry.entry_id}",
            atomic_writes=True,
            private=True,
        )
        tx.store._async_load = REAL_LOAD.__get__(tx.store)
        patch.setattr(
            "homeassistant.helpers.storage.Store._async_write_data", REAL_WRITE
        )
        before = simulation.checkpoint(), list(simulation.actor.commands)
        result = await tx.async_prepare(**prepare(plan))
        await tx.async_commit(**commit(result, plan))
        saved = json.loads(Path(tx.store.path).read_text())["data"]
        assert len(tx._validate(saved)) == 3
        assert await tx.store.async_load() == saved
        assert (simulation.checkpoint(), simulation.actor.commands) == before
        assert not tx.native_activation_allowed


async def test_real_executor_late_write_cannot_be_overtaken(
    simulation, monkeypatch, tmp_path
):
    import threading
    from pathlib import Path

    from custom_components.growassistant_crop_steering.recovery_ledger import (
        _LedgerStore,
    )

    from .test_recovery_ledger import REAL_WRITE

    with monkeypatch.context() as patch:
        tx, plan = await setup(simulation, monkeypatch)
        tx.hass.config.config_dir = str(tmp_path)
        tx.store = _LedgerStore(
            tx.hass,
            1,
            f"growassistant_crop_steering.recovery_transactions.{simulation.entry.entry_id}",
            atomic_writes=True,
            private=True,
        )
        original_write = tx.store._write_prepared_data
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def hold(*args):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
            return original_write(*args)

        patch.setattr(
            "homeassistant.helpers.storage.Store._async_write_data", REAL_WRITE
        )
        patch.setattr(tx.store, "_write_prepared_data", hold)
        task = asyncio.create_task(tx.async_prepare(**prepare(plan)))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert tx.inspect()["blocked_reason"] == "storage_uncertain"
            tx.revoke("stop")
            with pytest.raises(LedgerError, match="transaction_unavailable"):
                await tx.async_prepare(**prepare(plan, request_id="newer"))
        finally:
            release.set()
            await asyncio.gather(tx._write, return_exceptions=True)
        assert Path(tx.store.path).exists()
        assert not tx.inspect()["events"]
        assert not tx.native_activation_allowed


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("participant", ["pump", "shots", "cycle", "ledger"])
async def test_disk_only_changes_at_each_write_rejected(
    simulation, monkeypatch, boundary, participant
):
    tx, plan = await setup(simulation, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.store.async_save
    count = 0

    async def held(data):
        nonlocal count
        count += 1
        if count == boundary:
            entered.set()
            await release.wait()
        await original(data)

    monkeypatch.setattr(tx.store, "async_save", held)

    async def protocol():
        result = await tx.async_prepare(**prepare(plan))
        await tx.async_commit(**commit(result, plan))

    task = asyncio.create_task(protocol())
    await entered.wait()
    store = (
        tx.ledger.store
        if participant == "ledger"
        else getattr(simulation, participant).store
    )
    store.records[store.key] = {"external_conflicting_write": True}
    release.set()
    with pytest.raises(LedgerError):
        await task
    assert tx.inspect()["blocked_reason"] is not None
    assert not tx.native_activation_allowed


async def test_new_actual_recovery_case_invalidates_preparation(
    simulation, monkeypatch
):
    from .test_simulation import assert_recovery_locked, interrupted_p1

    tx, plan = await setup(simulation, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(tx.store, "async_save", held)
    task = asyncio.create_task(tx.async_prepare(**prepare(plan)))
    await entered.wait()
    await interrupted_p1(simulation)
    await simulation.hass.async_block_till_done()
    await tx.ledger.async_register(request_id="new_case", expected_revision=0)
    release.set()
    with pytest.raises(LedgerError):
        await task
    assert len(tx.ledger.inspect()["cases"]) == 1
    assert_recovery_locked(simulation)
    assert not tx.native_activation_allowed
