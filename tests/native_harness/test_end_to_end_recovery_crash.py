"""End-to-end deterministic failures; real findings and safety gates retained."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.common import async_test_home_assistant

from custom_components.growassistant_crop_steering.recovery_ledger import LedgerError
from custom_components.growassistant_crop_steering.recovery_revision import (
    content_digest,
)

from .fake_gateway_transactions import NativeGatewayTransactionDiagnostics
from .recovery_crash_harness import (
    CRASH_POINTS,
    EventBarrier,
    RecoveryCrashHarness,
    StoreFault,
)
from .test_fake_gateway_recovery import assert_never_release, intent_for
from .test_simulation import assert_no_replay_or_accounting


@asynccontextmanager
async def new_ha():
    async with async_test_home_assistant() as hass:
        try:
            yield hass
        finally:
            await hass.async_stop(force=True)


@pytest.fixture
async def system(simulation, monkeypatch):
    system = RecoveryCrashHarness(simulation, monkeypatch)
    await system.initialize()
    yield system
    await system.close()


async def parked(system, point, work):
    system.barrier = EventBarrier(point)
    task = asyncio.create_task(work())
    # If work fails before the selected boundary, propagate it instead of hanging.
    entered = asyncio.create_task(system.barrier.entered.wait())
    done, _ = await asyncio.wait((task, entered), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        entered.cancel()
        await asyncio.gather(entered, return_exceptions=True)
        await task
        raise AssertionError(f"unreached boundary: {point}")
    return task


async def cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_full_chain_is_historical_and_read_only(system, monkeypatch):
    before = system.persisted()
    protocol = system.transaction.inspect()

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic attempted release/output")

    monkeypatch.setattr(system.runtime.scheduler, "async_start", forbidden)
    for name in ("async_prepare", "async_commit"):
        monkeypatch.setattr(system.transaction, name, forbidden)
    result = await system.attempt()
    assert result.evidence_consistent and result.audit_persisted
    names = [event.name for event in system.events]
    assert names.index("after_gateway_snapshot") < names.index("participant_collection")
    report = await system.adapter.async_recover_history()
    assert report.historical_only and not report.current_authorization
    assert not report.atomic_multi_store_commit and not report.authenticity_proven
    assert_never_release(report)
    after = system.persisted()
    for name in before.keys() - {"audit", "audit_witness"}:
        assert before[name] == after[name]
    assert system.transaction.inspect() == protocol
    await assert_no_replay_or_accounting(system.runtime)
    system.record("replay_denied")


@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_at_each_real_boundary(system, monkeypatch, point):
    work = system.attempt
    if point == "gateway_restarting":
        work = system.restart_gateway
    elif point == "ha_restarting":
        async with new_ha() as hass:
            task = await parked(system, point, lambda: system.restart_ha(hass))
            await cancel(task)
        system.record("ha_crash_after_shutdown")
        return
    elif point in {"ledger_writing", "participant_writing", "audit_writing"}:
        store = system.audit_store
        if point == "ledger_writing":
            store, work = system.ledger.store, system.review
        elif point == "participant_writing":
            store, work = (
                system.runtime.shots.store,
                lambda: system.persist_participant("shots"),
            )
        fault = StoreFault(system, store, "delay_before", point)
        fault.install(monkeypatch)
    task = await parked(system, point, work)
    old = system.adapter
    old.restart()
    await cancel(task)
    # Protected ledger/participant writes outlive caller cancellation. Complete
    # them explicitly, retaining the owner's uncertainty latch.
    system.barrier.release.set()
    for pending in (system.ledger._write, system.runtime.shots._storage_task):
        if pending and not pending.done():
            await pending
    system.record("crashed")
    with pytest.raises(ValueError, match="unavailable"):
        await old.async_commit(None)
    fresh = NativeGatewayTransactionDiagnostics(
        system.integration, system.transaction, system.audit_store, system.witness
    )
    if point == "commit_saved":
        with pytest.raises(ValueError, match="witness_conflict"):
            await fresh.async_initialize()
    else:
        await fresh.async_initialize()
        report = await fresh.async_recover_history()
        assert_never_release(report)
        with pytest.raises(ValueError, match="prepare_identity"):
            await fresh.async_commit(None)


RESTART_ORDERS = (
    ("gateway",),
    ("coordinator",),
    ("adapter",),
    ("ha",),
    ("gateway", "ha"),
    ("ha", "gateway"),
    ("gateway", "gateway", "gateway"),
    ("ha", "ha", "ha"),
    ("adapter", "coordinator", "gateway", "ha"),
)


@pytest.mark.parametrize("order", RESTART_ORDERS)
async def test_restart_orders_with_real_reconstruction(system, order):
    prepared = await system.prepare()
    old = system.adapter
    old_gateway = system.gateway
    intent = intent_for(old_gateway)
    async with AsyncExitStack() as stack:
        for component in order:
            if component == "ha":
                hass = await stack.enter_async_context(new_ha())
                await system.restart_ha(hass)
            else:
                await getattr(system, "restart_" + component)()
        try:
            result = await old.async_commit(prepared)
        except ValueError as err:
            assert "unavailable" in str(err)
        else:
            assert not result.evidence_consistent
            assert_never_release(result)
        if "gateway" in order:
            assert old_gateway.accept(intent).status == "rejected"
            assert system.gateway.accept(intent).status == "rejected"
            assert (await old_gateway.execute(intent)).status == "rejected"
            assert (await system.gateway.execute(intent)).status == "rejected"
            assert system.gateway.state == "off"
        system.record("old_authority_denied")
        # Close while the reconstructed HA fixtures still exist.
        await system.close()


@pytest.mark.parametrize("restart", ["gateway", "adapter", "coordinator", "ha"])
@pytest.mark.parametrize("persist_first", [False, True])
async def test_restart_with_pending_commit_callback(
    system, monkeypatch, restart, persist_first
):
    prepared = await system.prepare()
    old = system.adapter
    fault = StoreFault(
        system,
        system.audit_store,
        "delay_after" if persist_first else "delay_before",
        "audit_writing",
    )
    fault.install(monkeypatch)
    task = await parked(system, "audit_writing", lambda: system.commit(prepared))
    async with AsyncExitStack() as stack:
        if restart == "ha":
            hass = await stack.enter_async_context(new_ha())
            # Retired callback owns the old HA serialization; new HA can inspect
            # storage only, never inherit its prepare. Persisted unwitnessed data
            # is expected to prevent initialization.
            if persist_first:
                with pytest.raises(ValueError, match="witness_conflict"):
                    await system.restart_ha(hass)
            else:
                await system.restart_ha(hass)
        elif restart in {"adapter", "coordinator"}:
            with pytest.raises(ValueError, match="unavailable"):
                await getattr(system, "restart_" + restart)()
        else:
            await system.restart_gateway()
        system.barrier.release.set()
        result = await task
        assert result.audit_persisted and not result.evidence_consistent
        assert_never_release(result)
        if restart in {"adapter", "ha"}:
            assert old.witness.expected is None
        system.record("late_callback_completed")
        await system.close()


@pytest.mark.parametrize(
    "store_name", ["ledger", "pump", "shots", "cycle", "protocol", "audit"]
)
@pytest.mark.parametrize("mode", ["before", "after", "partial"])
async def test_multi_store_write_errors(system, monkeypatch, store_name, mode):
    store = system.stores()[store_name]
    before = await store.async_load()
    fault = StoreFault(
        system,
        store,
        mode,
        "ledger_writing" if store_name == "ledger" else "participant_writing",
    )
    fault.install(monkeypatch)
    prepared = await system.prepare()
    writers = {
        "ledger": system.review,
        "pump": lambda: system.persist_participant("pump"),
        "shots": lambda: system.persist_participant("shots"),
        "cycle": lambda: system.persist_participant("cycle"),
        # Protocol transitions are unreachable with real blocked plans. This
        # faults its storage boundary only; no synthetic protocol is created.
        "protocol": lambda: system.protocol_store.async_save(
            {"injected_storage_probe": True}
        ),
        "audit": lambda: system.commit(prepared),
    }
    with pytest.raises((OSError, LedgerError, RuntimeError)):
        await writers[store_name]()
    saved = await store.async_load()
    assert fault.submitted
    if mode == "before":
        assert saved == before
    elif mode == "partial":
        assert saved == {"partial": True}
    else:
        assert fault.persisted
    if store_name != "audit":
        result = await system.adapter.async_commit(prepared)
        if store_name == "protocol" and mode == "before":
            # A failed external proposal that changed nothing is invisible to
            # the read-only protocol observer. Integrity is not failure telemetry.
            assert result.evidence_consistent and result.audit_persisted
            assert_never_release(result)
        else:
            assert not result.evidence_consistent and not result.audit_persisted
    else:
        assert system.adapter._uncertain
    system.record(f"{store_name}:{mode}:uncertain")


@pytest.mark.parametrize(
    "store_name", ["pump", "shots", "cycle", "ledger", "protocol", "audit"]
)
@pytest.mark.parametrize("damage", ["schema", "revision", "rollback"])
async def test_stored_evidence_damage_is_detected(system, store_name, damage):
    prepared = await system.prepare()
    store = system.stores()[store_name]
    if store_name == "audit":
        await system.commit(prepared)
        await system.restart_adapter()
        prepared = await system.prepare()
    data = await store.async_load()
    if damage == "rollback":
        store.records.pop(store.key, None)
        store.records.get("_participant_envelopes", {}).pop(store.key, None)
    elif store_name in {"pump", "shots", "cycle"} and damage == "revision":
        metadata = store.records["_participant_envelopes"][store.key]
        # A separately persisted receipt is part of the store, not live evidence.
        assert metadata["revision"] > 1
        stale = {**metadata, "revision": metadata["revision"] - 1}
        stale["metadata_digest"] = content_digest(
            {key: value for key, value in stale.items() if key != "metadata_digest"}
        )
        store.records["_participant_envelopes"][store.key] = stale
    else:
        await store.async_save(
            {"schema_version": 999, "revision": -1}
            if data is None
            else {**data, "schema_version": 999, "revision": -1}
        )
    if store_name == "audit":
        with pytest.raises(ValueError, match="audit_rollback"):
            await system.adapter.async_commit(prepared)
    else:
        result = await system.adapter.async_commit(prepared)
        if store_name == "protocol" and damage == "rollback":
            # The diagnostic protocol has no events to roll back. Absence is its
            # legitimate baseline, not proof of an externally retained revision.
            assert result.evidence_consistent and result.audit_persisted
        else:
            assert not result.evidence_consistent and not result.audit_persisted
    system.record("stored_damage_detected")


@pytest.mark.parametrize(
    "mode", ["before", "after", "delay", "schema", "rollback", "witness", "partial"]
)
async def test_gateway_journal_faults(system, mode):
    prepared = await system.prepare()
    store = system.gateway.store
    before = store.snapshot()
    if mode in {"before", "after", "delay"}:
        store.fail = "probe:" + mode
        with pytest.raises(OSError):
            store.write("probe", lambda: None)
        assert (store.revision == before.revision) is (mode == "before")
        if mode == "delay":
            store.acknowledge()
            assert store.witness.blocked
    elif mode == "schema":
        store.schema = 99
    elif mode == "rollback":
        store.write("probe", lambda: None)
        store.restore(before)
    elif mode == "witness":
        store.witness = None
    else:
        store.records["truncated"] = None
    result = await system.adapter.async_commit(prepared)
    assert not result.evidence_consistent and not result.audit_persisted
    system.record("gateway_error_detected")


RACES = (
    ("prepare_captured", "safety_off"),
    ("commit_checked", "safety_off"),
    ("participant_collection", "lease"),
    ("after_gateway_snapshot", "gateway"),
    ("prepare_captured", "ha_boot"),
    ("after_plan", "ledger"),
    ("after_prepare", "participant"),
    ("commit_saved", "gateway"),
)


@pytest.mark.parametrize("point,change", RACES)
async def test_combined_races(system, point, change):
    task = await parked(system, point, system.attempt)
    async with AsyncExitStack() as stack:
        try:
            if change == "safety_off":
                system.gateway.safety_off("race")
            elif change == "lease":
                system.gateway.revoke()
            elif change == "gateway":
                await system.restart_gateway()
            elif change == "ha_boot":
                hass = await stack.enter_async_context(new_ha())
                await system.restart_ha(hass)
            elif change == "ledger":
                await system.review()
            else:
                await system.persist_participant("shots")
            system.record("race_injected")
        finally:
            system.barrier.release.set()
        result = await task
        assert not result.evidence_consistent
        assert result.audit_persisted is (point == "commit_saved")
        assert_never_release(result)
        if change == "ha_boot":
            await system.close()


async def test_two_attempts_same_resource_and_late_receipt(system):
    other = NativeGatewayTransactionDiagnostics(
        system.integration, system.transaction, system.audit_store, system.witness
    )
    await other.async_initialize()
    task = await parked(system, "commit_checked", system.attempt)
    plan = await system.plan()
    try:
        with pytest.raises(ValueError, match="unavailable"):
            await other.async_prepare(
                plan=plan,
                request_id="second",
                expected_plan_id=plan.plan_id,
                expected_plan_digest="unused",
                expected_ledger_revision=system.ledger.revision,
            )
    finally:
        system.barrier.release.set()
    assert (await task).audit_persisted
    # Late gateway evidence is historical. It cannot repair missing provenance
    # or re-open a committed diagnostic attempt.
    system.gateway.store.fail = "accept:delay"
    system.gateway.accept(intent_for(system.gateway))
    system.gateway.store.acknowledge()
    report = await system.adapter.async_recover_history()
    assert_never_release(report)
    assert system.gateway.store.witness.blocked
    system.record("late_gateway_receipt")


@pytest.mark.parametrize("joint_witness", [False, True])
async def test_simultaneous_rollback_and_trust_limit(system, joint_witness):
    saved = system.persisted()
    await system.attempt()
    await system.review()
    for name in ("pump", "shots", "cycle"):
        await system.persist_participant(name)
    await system.protocol_store.async_save({"injected_storage_probe": True})
    system.gateway.store.write("advance", lambda: None)
    for name, store in system.stores().items():
        records, _ = saved[name]
        store.records.clear()
        store.records.update(deepcopy(records))
    system.gateway.store.restore(saved["gateway"])
    if joint_witness:
        system.witness.expected = deepcopy(saved["audit_witness"].expected)
        system.gateway.store.witness = deepcopy(saved["gateway_witness"])
        report = await system.adapter.async_recover_history()
        assert report.transaction_ids == ()
        assert not report.authenticity_proven and not report.current_authorization
        assert_never_release(report)
    else:
        with pytest.raises(ValueError, match="witness_conflict"):
            await system.adapter.async_recover_history()
    system.record("rollback_trust_limit")


@pytest.mark.parametrize("store_name", ["ledger", "pump", "shots", "cycle", "protocol"])
@pytest.mark.parametrize("persist_first", [False, True])
async def test_late_multi_store_confirmation(
    system, monkeypatch, store_name, persist_first
):
    prepared = await system.prepare()
    store = system.stores()[store_name]
    fault = StoreFault(
        system,
        store,
        "delay_after" if persist_first else "delay_before",
        "ledger_writing" if store_name == "ledger" else "participant_writing",
    )
    fault.install(monkeypatch)
    if store_name == "ledger":
        work = system.review
    elif store_name == "protocol":

        async def work():
            await store.async_save({"injected_storage_probe": True})
    else:

        async def work():
            await system.persist_participant(store_name)

    task = await parked(system, fault.point, work)
    assert fault.submitted and fault.persisted is persist_first
    system.clock.advance(1)
    await system.restart_gateway()
    # Complete the exact original IO task. Never retry a write or reconstruct
    # a prepare from its completion, even if all bytes eventually persist.
    system.barrier.release.set()
    await task
    result = await system.adapter.async_commit(prepared)
    assert not result.evidence_consistent and not result.audit_persisted
    system.record("multi_store_late_confirmation")


async def test_real_protocol_stays_blocked(system):
    from custom_components.growassistant_crop_steering.recovery_transaction import (
        plan_digest,
    )

    plan = await system.plan()
    before = system.persisted()
    with pytest.raises(LedgerError, match="plan_blocked"):
        await system.transaction.async_prepare(
            plan=plan,
            expected_plan_digest=plan_digest(plan),
            expected_revision=system.ledger.revision,
            request_id="real-prepare",
        )
    assert system.transaction._session is None
    assert not system.transaction.inspect()["events"]
    assert system.persisted() == before
    system.record("real_protocol_prepare_denied")


async def test_release_controller_refuses_real_plan(system):
    plan = await system.plan()
    receipt = await system.release.async_request(
        actor=system.authority.issue("harness", "explicit-refusal-check"),
        plan=plan,
        request_id="explicit-release-request",
        transaction_id="no-commit",
        binding=system.transaction.binding,
        cycle_id=system.runtime.cycle.cycle_id,
        expected_ledger_revision=system.ledger.revision,
        expected_transaction_revision=0,
        boot_id=system.authority.boot_id,
        ownership_epoch="unproven",
        decision="release",
        rationale="Blocked end-to-end plan must refuse",
    )
    assert receipt["released"] is False
    assert system.release.permit is None
    system.record("release_denied")


async def test_missing_audit_witness_cannot_recover_history(system):
    await system.attempt()
    system.witness.expected = None
    with pytest.raises(ValueError, match="witness_conflict"):
        await system.adapter.async_recover_history()
    system.record("audit_witness_lost")
