"""Stage two uses real simulated faults, fixed FakePump and deterministic IO barriers."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.growassistant_crop_steering.recovery_ledger import (
    LedgerError,
    TrustedReviewContext,
    _digest,
    _RecoveryLedger,
    _RecoverySource,
)

from .harness import MemoryStore, NativeHarness
from .test_simulation import (
    assert_no_replay_or_accounting,
    assert_recovery_locked,
    interrupted_p1,
    running,
    start_p1,
)

pytestmark = pytest.mark.asyncio
ACTOR = TrustedReviewContext("verified-test-user", "trusted-test-context")
REAL_LOAD = Store._async_load
REAL_WRITE = Store._async_write_data


async def ledger_for(harness, records=None, *, real_store=False):
    ledger = _RecoveryLedger(_RecoverySource(harness.shots, harness.cycle))
    if not real_store:
        ledger.store = MemoryStore(records if records is not None else {}, "ledger")
    else:
        # Bypass only the HA fixture's storage mock, keeping actual Store IO.
        ledger.store._async_load = REAL_LOAD.__get__(ledger.store)
    await ledger.async_initialize()
    return ledger


async def case_for(harness, records=None, *, real_store=False):
    await interrupted_p1(harness)
    await harness.hass.async_block_till_done()
    ledger = await ledger_for(harness, records, real_store=real_store)
    result = await ledger.async_register(request_id="register", expected_revision=0)
    return ledger, result["recovery_id"]


def review(harness):
    return {
        "partial_delivery": "possible",
        "volume": None,
        "shutdown": {
            "method": "independent fixed FakePump state inspection",
            "source": "isolated FakePump",
            "checked_at": harness.clock.at.isoformat(),
            "result": "confirmed_off",
            "limitations": "Simulation only; no physical hardware proof",
        },
        "journal_review": "Compared shot intent, accounting and cycle; uncertainty retained",
        "disposition": "keep_locked",
        "rationale": "Possible partial delivery; technical reconciliation still required",
    }


def acknowledgment(ledger, recovery_id, harness, **updates):
    request = {
        "recovery_id": recovery_id,
        "operation_id": "interrupted",
        "context": ACTOR,
        "request_id": "ack",
        "expected_revision": ledger.revision,
        "evidence_digest": ledger.inspect()["cases"][recovery_id]["evidence"]["digest"],
        "review": review(harness),
    }
    request.update(updates)
    return request


async def test_register_real_failure_and_complete_review_preserve_all_gates(simulation):
    ledger, recovery_id = await case_for(simulation)
    before, commands = simulation.checkpoint(), list(simulation.actor.commands)
    case = ledger.inspect()["cases"][recovery_id]
    assert case["binding"]["entry_id"] == simulation.entry.entry_id
    assert (
        case["binding"]["entity_id"]
        == simulation.pump.safety_snapshot().binding.entity_id
    )
    assert set(case["operations"]) == {"interrupted"}
    assert case["cycle_ids"] == (simulation.cycle.cycle_id,)
    assert case["evidence"]["findings"]
    request = acknowledgment(ledger, recovery_id, simulation)
    result = await ledger.async_acknowledge(**request)
    assert result["acknowledged"] and result["revision"] == 2
    state = ledger.inspect()
    assert state["cases"][recovery_id]["review_status"] == "acknowledged"
    assert state["effective_state"] == "locked"
    assert (
        state["native_activation_allowed"] is ledger.native_activation_allowed is False
    )
    assert state["events"][1]["request"]["actor"]["actor_id"] == ACTOR.actor_id
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert_recovery_locked(simulation)
    await assert_no_replay_or_accounting(simulation)
    assert (await ledger.async_acknowledge(**request)) == result
    assert ledger.revision == 2
    with pytest.raises(TypeError):
        state["cases"][recovery_id]["operations"]["interrupted"]["accounted"] = True


async def test_registration_retry_and_duplicate_fault(simulation):
    ledger, recovery_id = await case_for(simulation)
    assert (await ledger.async_register(request_id="register", expected_revision=0))[
        "recovery_id"
    ] == recovery_id
    with pytest.raises(LedgerError, match="request_conflict"):
        await ledger.async_register(request_id="register", expected_revision=1)
    with pytest.raises(LedgerError, match="failure_already_registered"):
        await ledger.async_register(request_id="different", expected_revision=1)
    assert ledger.revision == 1
    with pytest.raises(LedgerError, match="writer_exists"):
        _RecoveryLedger(_RecoverySource(simulation.shots, simulation.cycle))


@pytest.mark.parametrize(
    "context",
    [
        None,
        "a username",
        {},
        TrustedReviewContext("", "ctx"),
        TrustedReviewContext("actor", ""),
        TrustedReviewContext("actor", "ctx", "public"),
    ],
)
async def test_missing_or_invalid_actor(simulation, context):
    ledger, rid = await case_for(simulation)
    with pytest.raises(LedgerError, match="actor_invalid"):
        await ledger.async_acknowledge(
            **acknowledgment(ledger, rid, simulation, context=context)
        )
    assert ledger.revision == 1


@pytest.mark.parametrize(
    "change",
    [
        {"partial_delivery": "none_evidenced"},
        {"volume": 42},
        {"rationale": ""},
        {"shutdown": {}},
        {"disposition": "release"},
        {"journal_review": ""},
        {"partial_delivery": ""},
    ],
)
async def test_incomplete_or_contradictory_review(simulation, change):
    ledger, rid = await case_for(simulation)
    data = review(simulation)
    data.update(change)
    with pytest.raises(LedgerError):
        await ledger.async_acknowledge(
            **acknowledgment(ledger, rid, simulation, review=data)
        )
    assert ledger.revision == 1
    assert ledger.inspect()["cases"][rid]["review_status"] == "review_required"


async def test_request_conflict_revision_and_references(simulation):
    ledger, rid = await case_for(simulation)
    request = acknowledgment(ledger, rid, simulation)
    for changes, code in [
        ({"expected_revision": 0}, "revision_conflict"),
        ({"operation_id": "other"}, "reference_invalid"),
        ({"recovery_id": "other"}, "reference_invalid"),
        ({"evidence_digest": "different"}, "evidence_changed"),
    ]:
        with pytest.raises(LedgerError, match=code):
            await ledger.async_acknowledge(**{**request, **changes})
    result = await ledger.async_acknowledge(**request)
    with pytest.raises(LedgerError, match="request_conflict"):
        await ledger.async_acknowledge(
            **{**request, "context": TrustedReviewContext("other", "ctx")}
        )
    assert await ledger.async_acknowledge(**request) == result
    with pytest.raises(LedgerError, match="request_conflict"):
        await ledger.async_acknowledge(**{**request, "request_id": "register"})
    with pytest.raises(LedgerError, match="already_reviewed"):
        await ledger.async_acknowledge(
            **{**request, "request_id": "again", "expected_revision": 2}
        )


async def test_no_fault_cannot_be_registered(simulation):
    await simulation.observe()
    ledger = await ledger_for(simulation)
    with pytest.raises(LedgerError, match="no_failure"):
        await ledger.async_register(request_id="healthy", expected_revision=0)
    assert ledger.revision == 0 and not ledger.inspect()["cases"]


@pytest.mark.parametrize(
    "corruption",
    ["target", "entry", "cycle", "identity", "missing", "schema", "durability"],
)
async def test_reject_missing_or_invalid_actual_evidence(
    simulation, monkeypatch, corruption
):
    await interrupted_p1(simulation)
    await simulation.hass.async_block_till_done()
    ledger = await ledger_for(simulation)
    original = simulation.shots.safety_snapshot
    snapshot = original()
    raw = deepcopy(simulation.records["shots"])
    if corruption == "schema":
        snapshot = replace(
            snapshot, version=replace(snapshot.version, snapshot_schema=99)
        )
    elif corruption == "durability":
        monkeypatch.setattr(
            simulation.shots.store,
            "async_load",
            AsyncMock(return_value={"operations": {}}),
        )
    else:
        operation = raw["operations"]["interrupted"]
        if corruption == "missing":
            operation.pop("finished_at")
        else:
            operation[
                {
                    "target": "pump_entity",
                    "entry": "entry_id",
                    "cycle": "cycle_id",
                    "identity": "operation_id",
                }[corruption]
            ] = "wrong"
        # Fault injection only: neither live nor saved production journals change.
        snapshot = replace(snapshot, journal=raw)
        monkeypatch.setattr(
            simulation.shots.store, "async_load", AsyncMock(return_value=raw)
        )
    monkeypatch.setattr(simulation.shots, "safety_snapshot", lambda: snapshot)
    with pytest.raises(LedgerError):
        await ledger.async_register(request_id="invalid", expected_revision=0)
    assert ledger.revision == 0
    assert_recovery_locked(simulation)


async def test_evidence_change_rejects_review(simulation):
    ledger, rid = await case_for(simulation)
    request = acknowledgment(ledger, rid, simulation)
    simulation.configure(p1_shots=4)
    with pytest.raises(LedgerError, match="evidence_changed"):
        await ledger.async_acknowledge(**request)
    assert ledger.revision == 1


async def test_concurrent_reviews_have_no_lost_updates(simulation, monkeypatch):
    ledger, rid = await case_for(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ledger.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(ledger.store, "async_save", held)
    request = acknowledgment(ledger, rid, simulation)
    first = asyncio.create_task(ledger.async_acknowledge(**request))
    await entered.wait()
    second = asyncio.create_task(
        ledger.async_acknowledge(**{**request, "request_id": "competing"})
    )
    identical = asyncio.create_task(ledger.async_acknowledge(**request))
    release.set()
    results = await asyncio.gather(first, second, identical, return_exceptions=True)
    assert results[0] == results[2]
    assert (
        isinstance(results[1], LedgerError) and results[1].code == "revision_conflict"
    )
    assert ledger.revision == 2
    assert len(ledger.store.records["ledger"]["events"]) == 2


@pytest.mark.parametrize("when", ["register", "acknowledge"])
async def test_storage_failure_never_confirms(simulation, monkeypatch, when):
    if when == "register":
        await interrupted_p1(simulation)
        await simulation.hass.async_block_till_done()
        ledger = await ledger_for(simulation)

        async def action():
            return await ledger.async_register(
                request_id="register", expected_revision=0
            )
    else:
        ledger, rid = await case_for(simulation)
        request = acknowledgment(ledger, rid, simulation)

        async def action():
            return await ledger.async_acknowledge(**request)

    before = ledger.revision
    monkeypatch.setattr(
        ledger.store, "async_save", AsyncMock(side_effect=OSError("disk failed"))
    )
    with pytest.raises(OSError):
        await action()
    assert ledger.revision == before
    assert ledger.inspect()["blocked_reason"] == "storage_uncertain"
    with pytest.raises(LedgerError, match="ledger_unavailable"):
        await action()
    assert_recovery_locked(simulation)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("late_failure", [False, True])
@pytest.mark.parametrize("crash_before_write", [False, True])
async def test_late_write_after_timeout_or_cancellation_stays_latched(
    simulation, monkeypatch, restarted_hass, cancel, late_failure, crash_before_write
):
    records = {}
    ledger, rid = await case_for(simulation, records)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ledger.store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        try:
            if late_failure:
                raise OSError("late disk failure")
            await original(data)
        finally:
            finished.set()

    monkeypatch.setattr(ledger.store, "async_save", held)
    request = acknowledgment(ledger, rid, simulation, timeout=5 if cancel else 0)
    task = asyncio.create_task(ledger.async_acknowledge(**request))
    await entered.wait()
    before_write = deepcopy(records)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else LedgerError):
        await task
    assert not ledger._write.done()
    with pytest.raises(LedgerError, match="ledger_unavailable"):
        await ledger.async_acknowledge(**request)
    with pytest.raises(LedgerError, match="writer_exists"):
        _RecoveryLedger(ledger.source)
    release.set()
    await finished.wait()
    await asyncio.gather(ledger._write, return_exceptions=True)
    assert ledger.revision == 1  # Late success cannot become success in this boot.
    assert ledger.inspect()["blocked_reason"] == "storage_uncertain"
    assert records["ledger"]["revision"] == (1 if late_failure else 2)
    assert before_write["ledger"]["revision"] == 1
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        crash_image = before_write if crash_before_write else records
        fresh = await ledger_for(restored, deepcopy(crash_image))
        assert fresh.revision == (1 if crash_before_write or late_failure else 2)
        assert fresh.inspect()["effective_state"] == "locked"
        assert not fresh.native_activation_allowed
        assert not restored.actor.commands
        assert not restored.shots.execution_available
        if not late_failure and not crash_before_write:
            assert (await fresh.async_acknowledge(**request))["acknowledged"]
    finally:
        await restored.close()


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_restart_retains_open_and_reviewed_cases(
    simulation, monkeypatch, restarted_hass, acknowledged
):
    records = {}
    ledger, rid = await case_for(simulation, records)
    request = acknowledgment(ledger, rid, simulation)
    if acknowledged:
        result = await ledger.async_acknowledge(**request)
    checkpoint = simulation.checkpoint()
    events = deepcopy(records["ledger"]["events"])
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        new = await ledger_for(restored, records)
        assert records["ledger"]["events"] == events
        assert new.inspect()["cases"][rid]["review_status"] == (
            "acknowledged" if acknowledged else "review_required"
        )
        assert new.inspect()["effective_state"] == "locked"
        if acknowledged:
            assert await new.async_acknowledge(**request) == result
        assert not restored.actor.commands
        assert not restored.shots.execution_available
        assert not new.native_activation_allowed
    finally:
        await restored.close()


@pytest.mark.parametrize(
    "damage", ["schema", "revision", "truncated", "hash", "request", "review"]
)
async def test_corrupt_or_unknown_ledger_never_loads_as_clean(
    simulation, monkeypatch, restarted_hass, damage
):
    records = {}
    ledger, rid = await case_for(simulation, records)
    await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
    broken = deepcopy(records)
    data = broken["ledger"]
    if damage == "schema":
        data["schema_version"] = 99
    elif damage == "revision":
        data["revision"] += 1
    elif damage == "truncated":
        data["events"].pop()
    elif damage == "hash":
        data["events"][0]["hash"] = "bad"
    else:
        event = data["events"][1]
        if damage == "request":
            event["request_id"] = data["events"][0]["request_id"]
        else:
            event["request"]["review"].pop("shutdown")
        event["hash"] = _digest({k: v for k, v in event.items() if k != "hash"})
    checkpoint = simulation.checkpoint()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        new = _RecoveryLedger(_RecoverySource(restored.shots, restored.cycle))
        new.store = MemoryStore(broken, "ledger")
        with pytest.raises(LedgerError):
            await new.async_initialize()
        assert new.inspect()["blocked_reason"] == "ledger_load_failed"
        with pytest.raises(LedgerError, match="ledger_unavailable"):
            await new.async_register(
                request_id="cannot_reset", expected_revision=new.revision
            )
        assert broken["ledger"] == data
        assert not restored.actor.commands and not new.native_activation_allowed
    finally:
        await restored.close()


async def test_real_ha_store_writes_versioned_ledger(simulation, tmp_path, monkeypatch):
    with monkeypatch.context() as store_patch:
        store_patch.setattr(Store, "_async_write_data", REAL_WRITE)
        simulation.hass.config.config_dir = str(tmp_path)
        ledger, rid = await case_for(simulation, real_store=True)
        await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
        saved = json.loads(Path(ledger.store.path).read_text())
        assert saved["version"] == saved["data"]["schema_version"] == 1
        assert saved["data"]["revision"] == 2
        assert len(saved["data"]["events"]) == 2


@pytest.mark.parametrize(
    "content",
    ["{broken", "{}", '{"version":99,"data":{}}', '{"version":1,"data":null}'],
)
async def test_real_store_corruption_is_preserved_and_blocked(simulation, content):
    ledger = _RecoveryLedger(_RecoverySource(simulation.shots, simulation.cycle))
    path = Path(ledger.store.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(LedgerError):
        await ledger.async_initialize()
    assert path.read_text() == content
    assert ledger.inspect()["blocked_reason"] == "ledger_load_failed"


@pytest.mark.parametrize("separate_cases", [False, True])
async def test_multiple_real_failure_records_and_cases_never_mask_each_other(
    simulation, monkeypatch, restarted_hass, separate_cases
):
    # Two independently observed real fault records are assembled as a crash
    # fixture. No state is edited into success, removed or used to clear a gate.
    initial = simulation.checkpoint()
    await interrupted_p1(simulation)
    await simulation.hass.async_block_till_done()
    first = simulation.checkpoint()
    await simulation.close()
    simulation.clock.move_to("2026-10-13T06:00:00+00:00")
    second = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=initial
    )
    try:
        await second.initialize()
        await start_p1(second)
        task = await running(second, "second")
        second.vwc(40, "unavailable", 40)
        await task
        await second.hass.async_block_till_done()
        raw = deepcopy(second.records["shots"]["operations"]["second"])
        # Existing format scopes entry by its enclosing store, not per operation.
        assert "entry_id" not in raw
        source = _RecoverySource(second.shots, second.cycle)
        snapshot = second.shots.safety_snapshot()
        aggregate = deepcopy(second.records["shots"])
        aggregate["operations"]["interrupted"] = deepcopy(
            first["records"]["shots"]["operations"]["interrupted"]
        )
        aggregate["operations"].pop("second")
        if not separate_cases:
            aggregate["operations"]["second"] = raw
        # Inject only the aggregate source evidence; live and persisted journals,
        # gates and counters remain unchanged throughout this fixture.
        monkeypatch.setattr(
            second.shots,
            "safety_snapshot",
            lambda: replace(snapshot, journal=aggregate),
        )
        monkeypatch.setattr(
            second.shots.store,
            "async_load",
            AsyncMock(side_effect=lambda: deepcopy(aggregate)),
        )
        ledger = _RecoveryLedger(source)
        ledger.store = MemoryStore({}, "ledger")
        await ledger.async_initialize()
        result = await ledger.async_register(request_id="multi", expected_revision=0)
        rid = result["recovery_id"]
        if separate_cases:
            first_rid = rid
            aggregate["operations"]["second"] = raw
            result = await ledger.async_register(
                request_id="new-failure", expected_revision=1
            )
            rid = result["recovery_id"]
            assert len(ledger.inspect()["cases"]) == 2
            assert set(ledger.inspect()["cases"][rid]["operations"]) == {"second"}
        else:
            assert set(ledger.inspect()["cases"][rid]["operations"]) == {
                "interrupted",
                "second",
            }
            await ledger.async_acknowledge(**acknowledgment(ledger, rid, second))
            assert ledger.inspect()["cases"][rid]["review_status"] == "review_required"
        await ledger.async_acknowledge(
            **acknowledgment(
                ledger, rid, second, request_id="second-review", operation_id="second"
            )
        )
        assert ledger.inspect()["cases"][rid]["review_status"] == "acknowledged"
        if separate_cases:
            assert (
                ledger.inspect()["cases"][first_rid]["review_status"]
                == "review_required"
            )
        assert ledger.inspect()["effective_state"] == "locked"
        assert not second.shots.execution_available
    finally:
        await second.close()


@pytest.mark.parametrize(
    "failure", ["write_error", "serialization", "shutdown", "read_only"]
)
async def test_ha_store_cannot_silently_confirm_failed_or_deferred_audit(
    simulation, tmp_path, monkeypatch, failure
):
    from homeassistant.core import CoreState
    from homeassistant.util.file import WriteError
    from homeassistant.util.json import SerializationError

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_async_write_data", REAL_WRITE)
        simulation.hass.config.config_dir = str(tmp_path)
        ledger, rid = await case_for(simulation, real_store=True)
        before = Path(ledger.store.path).read_text()
        if failure in {"write_error", "serialization"}:

            def fail(*args):
                if failure == "write_error":
                    raise WriteError("injected disk error")
                raise SerializationError("injected serialization error")

            patch.setattr(ledger.store, "_write_prepared_data", fail)
        elif failure == "shutdown":
            patch.setattr(simulation.hass, "state", CoreState.stopping)
        else:
            patch.setattr(ledger.store, "_read_only", True)
        with pytest.raises(LedgerError, match="storage_failed|storage_unavailable"):
            await ledger.async_acknowledge(**acknowledgment(ledger, rid, simulation))
        assert ledger.revision == 1
        assert ledger.inspect()["blocked_reason"] == "storage_uncertain"
        assert Path(ledger.store.path).read_text() == before
        assert_recovery_locked(simulation)


async def test_real_store_corrupt_tombstone_cannot_become_empty(simulation, tmp_path):
    simulation.hass.config.config_dir = str(tmp_path)
    ledger = _RecoveryLedger(_RecoverySource(simulation.shots, simulation.cycle))
    path = Path(ledger.store.path + ".corrupt.saved")
    path.parent.mkdir(parents=True)
    path.write_text("broken historical audit")
    with pytest.raises(LedgerError, match="ledger_corrupt"):
        await ledger.async_initialize()
    assert path.read_text() == "broken historical audit"
    assert ledger.inspect()["effective_state"] == "locked"


@pytest.mark.parametrize("damage", [None, "corrupt", "schema", "missing", "binding"])
async def test_participant_disk_evidence_is_read_only(
    simulation, tmp_path, monkeypatch, damage
):
    await interrupted_p1(simulation)
    await simulation.hass.async_block_till_done()
    simulation.hass.config.config_dir = str(tmp_path)
    source = _RecoverySource(simulation.shots, simulation.cycle)
    before = simulation.checkpoint()
    with monkeypatch.context() as patch:
        paths = []
        for participant, suffix, key in [
            (simulation.pump, "pump_recovery", "pump"),
            (simulation.shots, "shot_journal", "shots"),
            (simulation.cycle, "native_cycle", "cycle"),
        ]:
            store = Store(
                simulation.hass,
                1,
                f"growassistant_crop_steering.{suffix}.{simulation.entry.entry_id}",
            )
            path = Path(store.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            envelope = {
                "version": 1,
                "minor_version": 1,
                "key": store.key,
                "data": before["records"][key],
            }
            if key == "shots":
                if damage == "schema":
                    envelope["version"] = 99
                elif damage == "binding":
                    envelope["key"] = "another-entry"
                if damage == "missing":
                    continue
            path.write_text(
                "{broken"
                if key == "shots" and damage == "corrupt"
                else json.dumps(envelope)
            )
            patch.setattr(participant, "store", store)
            patch.setattr(
                store,
                "async_load",
                AsyncMock(
                    side_effect=AssertionError("No participant migration/repair")
                ),
            )
            paths.append((path, path.read_bytes()))
        if damage is None:
            assert (await source.capture())["digest"]
        else:
            # Missing disk evidence also rejects; do not fall back to the live journal.
            if damage == "missing":
                store = Store(
                    simulation.hass,
                    1,
                    f"growassistant_crop_steering.shot_journal.{simulation.entry.entry_id}",
                )
                patch.setattr(simulation.shots, "store", store)
            with pytest.raises(LedgerError):
                await source.capture()
        assert all(path.read_bytes() == content for path, content in paths)
        assert not list(tmp_path.rglob("*.corrupt*"))
    assert simulation.checkpoint() == before


async def test_idempotency_distinguishes_json_boolean_from_revision(simulation):
    ledger, rid = await case_for(simulation)
    request = acknowledgment(ledger, rid, simulation)
    await ledger.async_acknowledge(**request)
    with pytest.raises(LedgerError, match="request_conflict"):
        await ledger.async_acknowledge(**{**request, "expected_revision": True})


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), None, True])
async def test_invalid_write_timeout_never_starts_a_write(simulation, timeout):
    ledger, rid = await case_for(simulation)
    with pytest.raises(LedgerError, match="timeout_invalid"):
        await ledger.async_acknowledge(
            **acknowledgment(ledger, rid, simulation, timeout=timeout)
        )
    assert ledger.revision == 1 and ledger._write.done()


async def test_existing_operation_cannot_be_rebound_as_a_new_failure(
    simulation, monkeypatch
):
    ledger, _ = await case_for(simulation)
    original = simulation.shots.safety_snapshot()
    changed = deepcopy(simulation.records["shots"])
    changed["operations"]["interrupted"]["reserved_at"] = "2026-10-13T06:59:00+00:00"
    monkeypatch.setattr(
        simulation.shots, "safety_snapshot", lambda: replace(original, journal=changed)
    )
    monkeypatch.setattr(
        simulation.shots.store, "async_load", AsyncMock(return_value=changed)
    )
    with pytest.raises(LedgerError, match="failure_identity_conflict"):
        await ledger.async_register(request_id="rebind", expected_revision=1)
    assert ledger.revision == 1 and len(ledger.inspect()["cases"]) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_executor_write_finishes_after_uncertain_ack(
    simulation, tmp_path, monkeypatch, cancel
):
    from threading import Event

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_async_write_data", REAL_WRITE)
        simulation.hass.config.config_dir = str(tmp_path)
        ledger, rid = await case_for(simulation, real_store=True)
        entered, release = asyncio.Event(), Event()
        original = ledger.store._write_prepared_data

        def held(*args):
            simulation.hass.loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=10):
                raise OSError("test barrier not released")
            return original(*args)

        patch.setattr(ledger.store, "_write_prepared_data", held)
        request = acknowledgment(ledger, rid, simulation, timeout=5 if cancel else 0)
        task = asyncio.create_task(ledger.async_acknowledge(**request))
        try:
            await entered.wait()
            assert (
                json.loads(Path(ledger.store.path).read_text())["data"]["revision"] == 1
            )
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else LedgerError):
                await task
            assert not ledger._write.done()
            with pytest.raises(LedgerError, match="ledger_unavailable"):
                await ledger.async_acknowledge(**request)
        finally:
            release.set()
            await asyncio.gather(task, ledger._write, return_exceptions=True)
        assert json.loads(Path(ledger.store.path).read_text())["data"]["revision"] == 2
        assert (
            ledger.revision == 1
            and ledger.inspect()["blocked_reason"] == "storage_uncertain"
        )
        assert_recovery_locked(simulation)


async def test_retained_pump_operation_must_match_shot_evidence(
    simulation, monkeypatch
):
    ledger, _ = await case_for(simulation)
    original = simulation.pump.safety_snapshot()
    record = {"entity_id": original.binding.entity_id, "operation_id": "unrelated-shot"}
    monkeypatch.setattr(
        simulation.pump, "safety_snapshot", lambda: replace(original, record=record)
    )
    monkeypatch.setattr(
        simulation.pump.store, "async_load", AsyncMock(return_value=record)
    )
    with pytest.raises(LedgerError, match="binding_mismatch"):
        await ledger.async_register(
            request_id="wrong-pump-operation", expected_revision=1
        )
    assert ledger.revision == 1
