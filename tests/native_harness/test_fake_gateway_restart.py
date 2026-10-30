"""Restart fences and read-only historical audit integrity, never release proof."""

# ruff: noqa: F811 -- imported shared pytest fixtures

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from custom_components.growassistant_crop_steering.recovery_ledger import _digest

from .fake_gateway_transactions import (
    NativeGatewayTransactionDiagnostics,
    inspect_audit_history,
)
from .test_fake_actuator_gateway import Barrier
from .test_fake_gateway_recovery import assert_never_release, unchanged_state
from .test_fake_gateway_transactions import (
    coupling,  # noqa: F401
    integration,  # noqa: F401
    mutate,
    prepare,
)


async def restart_coordinator(adapter):
    """Simulate coordinator teardown/reconstruction without restarting HA."""
    from custom_components.growassistant_crop_steering.const import DOMAIN
    from custom_components.growassistant_crop_steering.recovery_transaction import (
        _RecoveryTransactionCoordinator,
    )

    old = adapter.transaction
    old.revoke("coordinator_restart")
    for unsub in old._unsub:
        unsub()
    owners = old.hass.data[f"{DOMAIN}.recovery_transactions"]
    assert owners.pop(old.binding["entity_id"]) is old
    fresh = _RecoveryTransactionCoordinator(old.ledger)
    fresh.store = old.store
    await fresh.async_initialize()
    return fresh


def rehash(data):
    previous = None
    for receipt in data["receipts"]:
        receipt["previous"] = previous
        receipt["hash"] = _digest({k: v for k, v in receipt.items() if k != "hash"})
        previous = receipt["hash"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_context",
        "extra",
        "list_receipt",
        "missing_hash",
        "empty_request",
        "boolean_revision",
        "missing_binding",
        "witness_shape",
        "historical_false",
        "blockers_type",
        "duplicate",
        "predecessor",
        "schema_bool",
        "schema_unknown",
        "truncated",
        "release",
        "context_extra",
        "revision_negative",
        "truncated_evidence",
        "bad_identity",
    ],
)
async def test_strict_history_validation(coupling, damage):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    data = await adapter.store.async_load()
    receipt = data["receipts"][0]
    context = receipt["context"]
    if damage == "missing_context":
        del receipt["context"]
    elif damage == "extra":
        receipt["unknown"] = True
    elif damage == "list_receipt":
        data["receipts"][0] = []
    elif damage == "missing_hash":
        del receipt["hash"]
    elif damage == "empty_request":
        context["request_id"] = ""
    elif damage == "boolean_revision":
        context["ledger_revision"] = True
    elif damage == "missing_binding":
        context["binding"] = None
    elif damage == "witness_shape":
        context["witness_status"] = [True]
    elif damage == "historical_false":
        receipt["historical_only"] = False
    elif damage == "blockers_type":
        receipt["blockers"] = "blocked"
    elif damage == "duplicate":
        data["receipts"].append(deepcopy(receipt))
    elif damage == "predecessor":
        receipt["previous"] = "missing"
    elif damage == "schema_bool":
        data["schema"] = True
    elif damage == "schema_unknown":
        data["schema"] = 99
    elif damage == "truncated":
        data["receipts"] = [{}]
    elif damage == "release":
        receipt["recovery_release_allowed"] = True
    elif damage == "context_extra":
        context["unknown"] = "x"
    elif damage == "truncated_evidence":
        context["gateway_evidence"] = "{broken"
    elif damage == "bad_identity":
        context["transaction_id"] = "arbitrary"
    else:
        context["journal_revision"] = -1
    if damage not in {"list_receipt", "missing_hash", "predecessor", "truncated"}:
        rehash(data)
    # An agreeing witness and recomputed hashes cannot fix invalid structure.
    await adapter.store.async_save(data)
    adapter.witness.expected = deepcopy(data)
    with pytest.raises(ValueError, match="audit_"):
        await adapter.async_recover_history()


@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
@pytest.mark.parametrize("restarts", [1, 2, 4])
async def test_adapter_restart_retires_pending_attempt(
    coupling, simulation, stage, restarts
):
    adapter, plan = coupling
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        result = await prepare(adapter, plan)
        task = asyncio.create_task(adapter.async_commit(result, boundary=boundary))
    await barrier.entered.wait()
    before = unchanged_state(simulation, adapter.integration)
    for _ in range(restarts):
        adapter.restart()
    barrier.release.set()
    result = await task
    assert not result.evidence_consistent
    assert_never_release(result)
    assert unchanged_state(simulation, adapter.integration) == before
    assert adapter._prepared is None
    assert adapter.witness.expected is None
    with pytest.raises(ValueError, match="unavailable"):
        await prepare(adapter, plan)


@pytest.mark.parametrize(
    "change",
    [
        "boot",
        "ha_restart",
        "protocol",
        "stop",
        "owner",
        "participant",
        "journal",
        "rollback",
        "witness",
    ],
)
async def test_history_remains_read_only_after_component_change(
    coupling, simulation, change
):
    adapter, plan = coupling
    committed = await adapter.async_commit(await prepare(adapter, plan))
    await mutate(adapter, simulation, change)
    before = unchanged_state(simulation, adapter.integration)
    report = await adapter.async_recover_history(current_context=committed.context)
    assert report.request_ids == ("diagnostic-request",)
    assert report.historical_only and not report.current_authorization
    assert not report.authenticity_proven and not report.atomic_multi_store_commit
    assert_never_release(report)
    assert unchanged_state(simulation, adapter.integration) == before


@pytest.mark.parametrize(
    "field",
    [
        "instance_id",
        "gateway_boot",
        "gateway_runtime",
        "ownership_epoch",
        "safety_generation",
        "journal_revision",
        "witness_status",
        "ledger_revision",
        "participant_revisions",
        "binding",
        "cycle_id",
        "operations",
        "recovery_evidence",
        "transaction_protocol",
    ],
)
async def test_historical_context_comparison_never_authorizes(coupling, field):
    adapter, plan = coupling
    result = await adapter.async_commit(await prepare(adapter, plan))
    value = getattr(result.context, field)
    changed = value + 1 if type(value) is int else "different"
    report = await adapter.async_recover_history(
        current_context=replace(result.context, **{field: changed})
    )
    assert field in report.context_differences[0]
    assert not report.current_authorization
    # Identical values also confer no capability.
    identical = await adapter.async_recover_history(current_context=result.context)
    assert identical.context_differences == ((),)
    assert_never_release(identical)


async def test_repeated_request_ids_preserved_but_transactions_unique(coupling):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await fresh.async_initialize()
    await fresh.async_commit(await prepare(fresh, plan))
    report = await fresh.async_recover_history()
    assert report.request_ids == ("diagnostic-request", "diagnostic-request")
    assert len(set(report.transaction_ids)) == 2
    assert_never_release(report)


@pytest.mark.parametrize("joint", [False, True])
async def test_old_history_rollback_trust_limit(coupling, joint):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    old = await adapter.store.async_load()
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await fresh.async_initialize()
    await fresh.async_commit(await prepare(fresh, plan))
    await adapter.store.async_save(old)
    if joint:
        adapter.witness.expected = deepcopy(old)
        report = await adapter.async_recover_history()
        # Joint rollback is not detectable without an independent trust root.
        assert len(report.transaction_ids) == 1
        assert not report.authenticity_proven
        assert_never_release(report)
    else:
        with pytest.raises(ValueError, match="witness_conflict"):
            await adapter.async_recover_history()


async def test_history_reader_does_not_mutate_input(coupling):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    data = await adapter.store.async_load()
    before = deepcopy(data)
    inspect_audit_history(data)
    assert data == before


@pytest.mark.parametrize(
    "stores",
    [
        ("ledger",),
        ("protocol",),
        ("ledger", "protocol"),
        ("protocol", "ledger"),
        ("audit", "ledger"),
        ("ledger", "audit"),
    ],
)
@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
async def test_partial_multi_store_persistence(coupling, simulation, stores, stage):
    adapter, plan = coupling

    async def boundary(name):
        if name != stage:
            return
        for store in stores:
            target = {
                "ledger": adapter.transaction.ledger.store,
                "protocol": adapter.transaction.store,
                "audit": adapter.store,
            }[store]
            await target.async_save({"partial": "older-or-incomplete"})

    before = unchanged_state(simulation, adapter.integration)
    if stage == "prepare_captured":
        result = await prepare(adapter, plan, boundary=boundary)
    else:
        prepared = await prepare(adapter, plan)
        assert prepared.evidence_consistent
        result = await adapter.async_commit(prepared, boundary=boundary)
    assert not result.evidence_consistent
    assert_never_release(result)
    # Only explicit injected store writes are permitted; no actuator/scheduler change.
    after = unchanged_state(simulation, adapter.integration)
    assert before[0] == after[0]


@pytest.mark.parametrize(
    "order",
    [
        ("adapter", "gateway"),
        ("gateway", "adapter"),
        ("coordinator", "adapter", "gateway"),
        ("gateway", "coordinator", "adapter"),
    ],
)
async def test_restart_order_discards_prepare(coupling, simulation, order):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    replacements = []
    try:
        for component in order:
            if component == "adapter":
                adapter.restart()
            elif component == "gateway":
                await mutate(adapter, simulation, "boot")
            else:
                adapter.restart()
                tx = await restart_coordinator(adapter)
                replacements.append(tx)
        with pytest.raises(ValueError, match="unavailable"):
            await adapter.async_commit(prepared)
        assert adapter._prepared is None
        assert_never_release(await adapter.async_recover_history())
    finally:
        for tx in replacements:
            for unsub in tx._unsub:
                unsub()


@pytest.mark.parametrize("persist_first", [False, True])
@pytest.mark.parametrize("restarts", [1, 3])
async def test_late_storage_ack_after_restart(
    coupling, monkeypatch, persist_first, restarts
):
    adapter, plan = coupling
    prepared = await prepare(adapter, plan)
    barrier = Barrier()
    save = adapter.store.async_save

    async def delayed(data):
        if persist_first:
            await save(data)
        await barrier.wait()
        if not persist_first:
            await save(data)

    monkeypatch.setattr(adapter.store, "async_save", delayed)
    task = asyncio.create_task(adapter.async_commit(prepared))
    await barrier.entered.wait()
    for _ in range(restarts):
        adapter.restart()
    replacement = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    # A retired writer still owns the barrier until its delayed callback finishes.
    with pytest.raises(ValueError, match="unavailable"):
        await replacement.async_initialize()
    barrier.release.set()
    result = await task
    assert result.audit_persisted and not result.evidence_consistent
    assert adapter.witness.expected is None
    assert_never_release(result)
    with pytest.raises(ValueError, match="witness_conflict"):
        await replacement.async_initialize()


@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
async def test_two_request_ids_cannot_overlap(coupling, stage):
    adapter, plan = coupling
    other = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await other.async_initialize()
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(
            prepare(adapter, plan, request_id="first", boundary=boundary)
        )
    else:
        prepared = await prepare(adapter, plan, request_id="first")
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    try:
        with pytest.raises(ValueError, match="unavailable"):
            await prepare(other, plan, request_id="second")
    finally:
        barrier.release.set()
    assert_never_release(await task)


@pytest.mark.parametrize("stage", ["before_prepare", "after_prepare", "after_commit"])
async def test_restart_outside_await_boundaries(coupling, stage):
    adapter, plan = coupling
    prepared = None
    if stage != "before_prepare":
        prepared = await prepare(adapter, plan)
    if stage == "after_commit":
        assert (await adapter.async_commit(prepared)).audit_persisted
    adapter.restart()
    with pytest.raises(ValueError, match="unavailable"):
        await prepare(adapter, plan)
    if prepared:
        with pytest.raises(ValueError, match="unavailable"):
            await adapter.async_commit(prepared)
    report = await adapter.async_recover_history()
    assert len(report.transaction_ids) == int(stage == "after_commit")
    assert_never_release(report)


@pytest.mark.parametrize(
    "stage", ["prepare_captured", "commit_checked", "commit_saved"]
)
async def test_replaced_coordinator_fences_old_inflight_diagnosis(coupling, stage):
    adapter, plan = coupling
    barrier = Barrier()

    async def boundary(name):
        if name == stage:
            await barrier.wait()

    if stage == "prepare_captured":
        task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
    else:
        prepared = await prepare(adapter, plan)
        task = asyncio.create_task(adapter.async_commit(prepared, boundary=boundary))
    await barrier.entered.wait()
    tx = await restart_coordinator(adapter)
    try:
        NativeGatewayTransactionDiagnostics(
            adapter.integration, tx, adapter.store, adapter.witness
        )
        barrier.release.set()
        result = await task
        assert not result.evidence_consistent
        assert_never_release(result)
    finally:
        barrier.release.set()
        if not task.done():
            await task
        for unsub in tx._unsub:
            unsub()


@pytest.mark.parametrize("stage", ["prepare_captured", "commit_checked"])
async def test_repeated_crash_before_confirmation(coupling, stage):
    adapter, plan = coupling
    for _ in range(3):
        barrier = Barrier()

        async def boundary(name):
            if name == stage:
                await barrier.wait()

        if stage == "prepare_captured":
            task = asyncio.create_task(prepare(adapter, plan, boundary=boundary))
        else:
            prepared = await prepare(adapter, plan)
            task = asyncio.create_task(
                adapter.async_commit(prepared, boundary=boundary)
            )
        await barrier.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError, match="unavailable"):
            await prepare(adapter, plan)
        adapter = NativeGatewayTransactionDiagnostics(
            adapter.integration, adapter.transaction, adapter.store, adapter.witness
        )
        await adapter.async_initialize()
        assert_never_release(await adapter.async_recover_history())
    assert (await adapter.async_recover_history()).transaction_ids == ()


@pytest.mark.parametrize(
    "stores",
    [
        ("ledger",),
        ("gateway", "ledger"),
        ("audit", "ledger"),
        ("ledger", "audit", "gateway"),
    ],
)
async def test_rollback_multiple_stores_never_certifies_atomic_commit(
    coupling, simulation, stores
):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await fresh.async_initialize()
    prepared = await prepare(fresh, plan)
    assert prepared.evidence_consistent
    for store in stores:
        if store == "ledger":
            data = await fresh.transaction.ledger.store.async_load()
            assert data["revision"] > 0
            data["events"] = data["events"][:-1]
            data["revision"] -= 1
            await fresh.transaction.ledger.store.async_save(data)
        elif store == "gateway":
            await mutate(fresh, simulation, "rollback")
        else:
            fresh.store.records.clear()
    result = await fresh.async_commit(prepared)
    assert not result.evidence_consistent and not result.audit_persisted
    assert_never_release(result)


@pytest.mark.parametrize("damage", ["reorder", "truncate"])
async def test_history_order_and_retained_witness(coupling, damage):
    adapter, plan = coupling
    await adapter.async_commit(await prepare(adapter, plan))
    fresh = NativeGatewayTransactionDiagnostics(
        adapter.integration, adapter.transaction, adapter.store, adapter.witness
    )
    await fresh.async_initialize()
    await fresh.async_commit(await prepare(fresh, plan))
    data = await fresh.store.async_load()
    if damage == "reorder":
        data["receipts"].reverse()
        with pytest.raises(ValueError, match="audit_corrupt"):
            inspect_audit_history(data)
    else:
        data["receipts"].pop()
        # A valid prefix still fails against an independent retained witness.
        assert len(inspect_audit_history(data).transaction_ids) == 1
        await fresh.store.async_save(data)
        with pytest.raises(ValueError, match="witness_conflict"):
            await fresh.async_recover_history()
