"""Crash-visible journal revisions and runtime evidence using fixed FakePump.

Barriers exercise the actual persistence boundary; no private safety gate is
cleared and no positive recovery model is injected.
"""

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.growassistant_crop_steering.recovery_ledger import (
    _json,
)
from custom_components.growassistant_crop_steering.recovery_participant import (
    PumpGateEvidence,
    aggregate_evidence,
    request_current,
)
from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_revision import (
    BootEpoch,
    ControllerEpoch,
    JournalRevision,
    PumpResourceEpoch,
    RevisionError,
    _ParticipantStore,
    content_digest,
    encode_receipt,
)
from custom_components.growassistant_crop_steering.recovery_snapshot import (
    ReasonCode,
    VersionEvidence,
    validate_pump,
)
from custom_components.growassistant_crop_steering.shot_engine import ShotRejected

from .harness import PUMP, MemoryStore, NativeHarness
from .test_recovery_ledger import REAL_WRITE, acknowledgment
from .test_recovery_participant_gates import setup
from .test_simulation import (
    assert_no_replay_or_accounting,
    assert_recovery_locked,
    interrupted_p1,
    start_p1,
)

pytestmark = pytest.mark.asyncio


async def journal(records=None):
    store = MemoryStore({} if records is None else records, "journal")
    state = JournalRevision("entry", PUMP, "store-key")
    await state.load(store)
    return state, store


async def save(state, store, data, timeout=1):
    await state.save(store, data, timeout=timeout, create_task=asyncio.create_task)


async def test_confirmed_changes_aba_and_noop_are_distinct():
    state, store = await journal()
    first = {"operations": {}, "phase": "A"}
    assert state.evidence(first, PUMP) is None
    await save(state, store, first)
    a = state.evidence(first, PUMP)
    assert a.revision == 1 and a.digest == content_digest(first)
    generation = state.generation
    await save(state, store, deepcopy(first))
    assert state.receipt == a and state.generation == generation
    await save(state, store, {**first, "phase": "B"})
    assert state.receipt.revision == 2
    await save(state, store, first)
    assert state.receipt.revision == 3 and state.receipt.digest == a.digest
    assert state.receipt.identity == a.identity
    assert (
        encode_receipt(state.receipt)
        == store.records["_participant_envelopes"]["journal"]
    )


@pytest.mark.parametrize(
    "initial", [{"operations": {"old": {"state": "interrupted"}}}, None]
)
async def test_legacy_payload_is_never_silently_certified(initial):
    records = {"journal": deepcopy(initial)}
    state, store = await journal(records)
    assert state.status == "legacy" and state.receipt is None
    await save(state, store, initial)
    assert state.receipt is None
    assert records["journal"] == initial
    restored, _ = await journal(records)
    assert restored.receipt is None and restored.status == "legacy"


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "old_schema",
        "bool",
        "negative",
        "float",
        "digest",
        "entry",
        "target",
        "key",
        "identity",
        "missing",
    ],
)
async def test_unknown_or_damaged_revision_cannot_load(damage):
    state, store = await journal()
    await save(state, store, {"operations": {}})
    metadata = store.records["_participant_envelopes"]["journal"]
    if damage == "missing":
        metadata.pop("digest")
    else:
        key, value = {
            "schema": ("schema", 99),
            "old_schema": ("schema", 0),
            "bool": ("revision", True),
            "negative": ("revision", -1),
            "float": ("revision", 1.0),
            "digest": ("digest", "bad"),
            "entry": ("entry_id", "other"),
            "target": ("pump_entity", "switch.other"),
            "key": ("store_key", "other"),
            "identity": ("journal_id", ""),
        }[damage]
        metadata[key] = value
    with pytest.raises(RevisionError):
        await journal(store.records)
    assert store.records["journal"] == {"operations": {}}


@pytest.mark.parametrize("outcome", ["error", "timeout", "cancel", "writer_cancel"])
async def test_uncertain_write_never_installs_late_confirmation(monkeypatch, outcome):
    state, store = await journal()
    await save(state, store, {"phase": "before"})
    receipt = state.receipt
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    original = store.async_save

    async def held(data):
        writes.append(asyncio.current_task())
        entered.set()
        await release.wait()
        if outcome == "error":
            raise OSError("failed write")
        await original(data)

    monkeypatch.setattr(store, "async_save", held)
    task = asyncio.create_task(
        save(state, store, {"phase": "after"}, timeout=0 if outcome == "timeout" else 1)
    )
    await entered.wait()
    assert state.evidence({"phase": "before"}, PUMP) is None
    if outcome == "cancel":
        task.cancel()
    elif outcome == "writer_cancel":
        writes[0].cancel()
    elif outcome == "error":
        release.set()
    try:
        with pytest.raises((RevisionError, OSError, asyncio.CancelledError)):
            await task
    finally:
        release.set()
        await asyncio.gather(*writes, return_exceptions=True)
    assert state.status == "uncertain" and state.receipt == receipt
    assert state.evidence({"phase": "after"}, PUMP) is None
    assert state.evidence({"phase": "before"}, PUMP) is None


async def test_concurrent_writes_reject_instead_of_queue(monkeypatch):
    state, store = await journal()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.async_save

    async def held(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(store, "async_save", held)
    task = asyncio.create_task(save(state, store, {"phase": "first"}))
    await entered.wait()
    try:
        with pytest.raises(RevisionError, match="busy"):
            await save(state, store, {"phase": "second"})
        assert "journal" not in store.records
    finally:
        release.set()
        await task
    assert state.receipt.revision == 1 and store.records["journal"]["phase"] == "first"


@pytest.mark.parametrize("crash_point", ["before_io", "after_io", "after_ack"])
async def test_crash_reloads_only_completed_journal_prefix(monkeypatch, crash_point):
    state, store = await journal()
    await save(state, store, {"phase": "A"})
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.async_save

    async def held(data):
        if crash_point == "after_io":
            await original(data)
        entered.set()
        await release.wait()
        if crash_point != "after_io":
            await original(data)

    monkeypatch.setattr(store, "async_save", held)
    task = asyncio.create_task(save(state, store, {"phase": "B"}))
    await entered.wait()
    if crash_point == "after_ack":
        release.set()
        await task
    checkpoint = deepcopy(store.records)
    release.set()
    await task
    restored, restored_store = await journal(checkpoint)
    assert restored.receipt.identity == state.receipt.identity
    assert restored.receipt.revision == (1 if crash_point == "before_io" else 2)
    assert restored.evidence(restored_store.records["journal"], PUMP)


async def test_participant_revisions_follow_actual_paths_and_preserve_accounting(
    simulation,
):
    await start_p1(simulation)
    initial = simulation.shots.safety_snapshot().version.receipt
    assert initial is not None
    before = simulation.checkpoint()
    await simulation.shots._save()
    assert simulation.checkpoint() == before
    assert simulation.shots.safety_snapshot().version.receipt == initial
    # Actual execution completes once; diagnostics add no accounting or commands.
    assert (await simulation.shots.async_execute("once")).accounted
    await simulation.hass.async_block_till_done()
    shot = simulation.shots.safety_snapshot()
    pump = simulation.pump.safety_snapshot()
    cycle = simulation.cycle.safety_snapshot()
    assert shot.version.revision > initial.revision
    assert pump.version.revision >= 3 and pump.record is None
    assert cycle.version.revision >= 2
    assert shot.journal["operations"]["once"]["accounted"] is True
    before, commands = simulation.checkpoint(), list(simulation.actor.commands)
    for _ in range(3):
        for participant in (simulation.pump, simulation.shots, simulation.cycle):
            snapshot = participant.safety_snapshot()
            assert snapshot.version.status == "confirmed"
            assert isinstance(snapshot.runtime.boot, BootEpoch)
            assert isinstance(snapshot.runtime.controller, ControllerEpoch)
            assert isinstance(snapshot.runtime.resource, PumpResourceEpoch)
            with pytest.raises(FrozenInstanceError):
                snapshot.runtime.safety_generation = 0
    with pytest.raises(ShotRejected, match="duplicate_operation"):
        await simulation.shots.async_execute("once")
    assert simulation.checkpoint() == before and simulation.actor.commands == commands
    assert simulation.entry.options["p1_shots_done"] == 1
    assert not simulation.scheduler.running


async def test_restart_preserves_revision_history_but_invalidates_runtime(
    simulation, monkeypatch, restarted_hass
):
    coordinator, _ = await setup(simulation)
    old = await coordinator.async_check()
    snapshots = coordinator.source._snapshots()
    checkpoint = simulation.checkpoint()
    coordinator.close()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        current = (
            restored.pump.safety_snapshot(),
            restored.shots.safety_snapshot(),
            restored.cycle.safety_snapshot(),
        )
        for previous, new in zip(snapshots, current, strict=True):
            assert new.version.receipt.identity == previous.version.receipt.identity
            assert new.version.revision >= previous.version.revision
            assert new.runtime.boot != previous.runtime.boot
            assert new.runtime.controller != previous.runtime.controller
        assert not request_current(restarted_hass, old.request)
        assert not restored.actor.commands and not restored.scheduler.running
        assert_recovery_locked(restored)
        await assert_no_replay_or_accounting(restored)
    finally:
        await restored.close()


@pytest.mark.parametrize(
    "change",
    [
        "ownership",
        "ownership_aba",
        "target",
        "target_aba",
        "journal",
        "scheduler_timer",
    ],
)
async def test_old_requests_are_revoked_by_owner_target_or_journal_change(
    simulation, change
):
    coordinator, _ = await setup(simulation)
    old = await coordinator.async_check()
    commands = list(simulation.actor.commands)
    if change.startswith("ownership"):
        evidence = simulation.pump.ownership
        simulation.pump.ownership = None
        if change.endswith("aba"):
            simulation.pump.ownership = evidence
    elif change.startswith("target"):
        data = dict(simulation.entry.data)
        simulation.hass.config_entries.async_update_entry(
            simulation.entry, data={**data, "pump_switch": "switch.changed"}
        )
        if change.endswith("aba"):
            simulation.hass.config_entries.async_update_entry(
                simulation.entry, data=data
            )
    elif change == "journal":
        # A conservative restart marker is a real journal change, not a gate edit.
        await simulation.shots.async_close()
    else:
        # Timer cancellation is a real lifecycle operation even when already idle.
        await simulation.scheduler.async_stop()
    await simulation.hass.async_block_till_done()
    assert not coordinator.confirmation_current(old)
    assert not request_current(simulation.hass, old.request)
    current = await coordinator.async_check()
    assert not current.coordination_ready and not current.native_activation_allowed
    assert simulation.actor.commands == commands
    assert_recovery_locked(simulation)
    coordinator.close()


@pytest.mark.parametrize(
    "source", ["pump", "shots", "accounting", "cycle", "scheduler"]
)
async def test_cross_participant_revision_or_epoch_conflict_is_not_hidden(
    simulation, source
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    revisions = tuple(
        replace(r, durable_revision=999) if r.source == source else r
        for r in result.request.revisions
    )
    altered = replace(result.request, revisions=revisions)
    aggregate = aggregate_evidence(altered, result.evidence)
    assert "revision_conflict" in aggregate.blockers
    assert "external_writers_unproven" in aggregate.blockers
    assert "coordinated_resolution_unimplemented" in aggregate.blockers
    assert not aggregate.coordination_ready and not aggregate.native_activation_allowed
    assert not coordinator.confirmation_current(aggregate)
    coordinator.close()


async def test_disk_only_revision_change_is_detected_despite_identical_payload(
    simulation,
):
    coordinator, _ = await setup(simulation)
    original = simulation.checkpoint()
    # Model a privileged file-only writer; no local runtime can exclude it.
    metadata = simulation.records["_participant_envelopes"]["shots"]
    metadata["revision"] += 2
    plan = await async_create_reconciliation_plan(coordinator.ledger)
    assert "revision_conflict" in {f.code for f in plan.findings}
    result = await coordinator.async_check()
    assert not result.coordination_ready
    assert "revision_conflict" in result.blockers
    assert simulation.records["shots"] == original["records"]["shots"]
    assert_recovery_locked(simulation)
    coordinator.close()


@pytest.mark.parametrize(
    "failure", ["write_error", "read_only", "shutdown", "unconfirmed_mock"]
)
async def test_strict_ha_adapter_requires_actual_writer_completion(
    simulation, monkeypatch, failure
):
    store = _ParticipantStore(simulation.hass, "strict-adapter")
    state = JournalRevision("entry", PUMP, store.key)
    await state.load(store)
    with monkeypatch.context() as patch:
        if failure == "write_error":

            async def broken(*args):
                raise WriteError("medium unavailable")

            patch.setattr(Store, "_async_write_data", broken)
        elif failure == "read_only":
            store._read_only = (
                True  # Storage fixture only; no participant safety flags.
            )
        elif failure == "shutdown":
            patch.setattr(simulation.hass, "state", CoreState.stopping)
        else:

            async def unconfirmed(*args):
                return None

            patch.setattr(store, "async_save", unconfirmed)
        with pytest.raises(RevisionError):
            await save(state, store, {"phase": "pending"})
        assert (
            state.receipt is None and state.evidence({"phase": "pending"}, PUMP) is None
        )


async def test_real_ha_envelope_atomic_completion_reload_and_corruption(
    simulation, monkeypatch, tmp_path
):
    store = _ParticipantStore(simulation.hass, "real-participant-journal")
    state = JournalRevision("entry", PUMP, store.key)
    with monkeypatch.context() as real_io:
        real_io.setattr(Store, "_async_write_data", REAL_WRITE)
        real_io.setattr(
            _ParticipantStore, "path", property(lambda self: str(tmp_path / self.key))
        )
        path = Path(store.path)
        await state.load(store)
        await save(
            state, store, {"operations": {"historical": {"state": "interrupted"}}}
        )
        envelope = json.loads(path.read_text())
        assert envelope["data"]["operations"]["historical"]["state"] == "interrupted"
        assert envelope["participant_revision"] == encode_receipt(state.receipt)
        assert store._atomic_writes is True
        restored = JournalRevision("entry", PUMP, store.key)
        await restored.load(_ParticipantStore(simulation.hass, store.key))
        assert restored.receipt == state.receipt
        path.write_text("{broken")
        with pytest.raises(RevisionError, match="journal_unreadable"):
            await JournalRevision("entry", PUMP, store.key).load(store)
        assert path.read_text() == "{broken"
        assert not list(path.parent.glob(path.name + ".corrupt*"))


async def test_full_plan_and_technical_receipts_bind_epoch_and_revision(simulation):
    from custom_components.growassistant_crop_steering.recovery_transaction import (
        _RecoveryTransactionCoordinator,
    )

    coordinator, _ = await setup(simulation)
    plan = await async_create_reconciliation_plan(coordinator.ledger)
    for name in ("pump", "shots", "cycle"):
        version = plan.source_revisions[name]
        assert version["receipt"]["revision"] == version["revision"]
        assert version["runtime"]["boot"]["value"]
    intent = {
        "payload": {
            "plan": _json(plan),
            "plan_digest": "test-only",
            "cycle_id": simulation.cycle.cycle_id,
        }
    }
    receipts = _RecoveryTransactionCoordinator._receipts(intent)
    assert all(
        r["source_revisions"] == _json(plan.source_revisions) for r in receipts.values()
    )
    assert not plan.native_activation_allowed and plan.status == "blocked"
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_boolean_ledger_revision_is_not_equal_integer_evidence(simulation):
    coordinator, rid = await setup(simulation, review=False)
    result = await coordinator.async_check()
    assert coordinator.ledger.revision == 1
    revisions = tuple(
        replace(r, durable_revision=True) if r.source == "ledger" else r
        for r in result.request.revisions
    )
    altered = replace(result.request, revisions=revisions)
    aggregate = aggregate_evidence(altered, result.evidence)
    assert "revision_conflict" in aggregate.blockers
    assert not aggregate.coordination_ready
    await coordinator.ledger.async_acknowledge(
        **acknowledgment(coordinator.ledger, rid, simulation)
    )
    assert not request_current(simulation.hass, result.request)
    coordinator.close()


async def test_invalid_live_payload_has_unknown_evidence_without_mutation():
    state, store = await journal()
    await save(state, store, {"phase": "valid"})
    before = deepcopy(store.records), state.receipt, state.generation
    assert state.evidence({"phase": float("nan")}, PUMP) is None
    assert (store.records, state.receipt, state.generation) == before


async def test_missing_proof_does_not_remove_durable_revision_blocker(simulation):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    assert "durable_participant_revisions_unproven" not in result.blockers
    missing = tuple(p for p in result.evidence if not isinstance(p, PumpGateEvidence))
    aggregate = aggregate_evidence(result.request, missing)
    assert "durable_participant_revisions_unproven" in aggregate.blockers
    assert not aggregate.coordination_ready
    coordinator.close()


async def test_schema_one_integer_never_becomes_new_revision_proof(simulation):
    await interrupted_p1(simulation)
    snapshot = replace(
        simulation.pump.safety_snapshot(), version=VersionEvidence(revision=99)
    )
    result = validate_pump(
        snapshot, snapshot.binding, now=simulation.clock.at, max_age=30
    )
    assert ReasonCode.REVISION_UNKNOWN in {f.code for f in result.findings}
    assert not result.native_activation_allowed


async def test_controller_recreation_changes_epoch_inside_same_boot(simulation):
    from custom_components.growassistant_crop_steering.pump import PumpController

    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    before = simulation.pump.safety_snapshot()
    checkpoint = simulation.checkpoint()
    await simulation.pump.async_close()
    pump = PumpController(simulation.hass, simulation.entry)
    pump.store = MemoryStore(deepcopy(checkpoint["records"]), "pump")
    try:
        await pump.async_initialize()
        current = pump.safety_snapshot()
        assert current.runtime.boot == before.runtime.boot
        assert current.runtime.controller != before.runtime.controller
        assert current.runtime.resource != before.runtime.resource
        assert current.version.receipt == before.version.receipt
        assert not coordinator.confirmation_current(result)
    finally:
        await pump.async_close()
        coordinator.close()


@pytest.mark.parametrize(
    "damage", ["empty", "major", "minor", "key", "missing_data", "revision_number"]
)
async def test_real_file_unknown_schema_or_corrupt_revision_is_preserved(
    simulation, monkeypatch, tmp_path, damage
):
    store = _ParticipantStore(simulation.hass, "schema-journal")
    state = JournalRevision("entry", PUMP, store.key)
    with monkeypatch.context() as real_io:
        real_io.setattr(Store, "_async_write_data", REAL_WRITE)
        real_io.setattr(
            _ParticipantStore, "path", property(lambda self: str(tmp_path / self.key))
        )
        await state.load(store)
        await save(state, store, {"phase": "original"})
        path = Path(store.path)
        envelope = json.loads(path.read_text())
        if damage == "empty":
            envelope = {}
        elif damage == "major":
            envelope["version"] = 2
        elif damage == "minor":
            envelope["minor_version"] = 99
        elif damage == "key":
            envelope["key"] = "other"
        elif damage == "missing_data":
            envelope.pop("data")
        else:
            envelope["participant_revision"]["revision"] += 1
        path.write_text(json.dumps(envelope))
        before = path.read_bytes()
        with pytest.raises(RevisionError):
            await JournalRevision("entry", PUMP, store.key).load(store)
        assert path.read_bytes() == before
        assert not list(path.parent.glob(path.name + ".corrupt*"))


async def test_real_executor_late_completion_never_acknowledges_timed_out_write(
    simulation, monkeypatch, tmp_path
):
    store = _ParticipantStore(simulation.hass, "late-executor-journal")
    state = JournalRevision("entry", PUMP, store.key)
    entered, release = asyncio.Event(), threading.Event()
    original = store._write_prepared_data
    task_holder = []

    def held(*args):
        simulation.hass.loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise AssertionError("executor barrier not released")
        original(*args)

    def create_task(work):
        task = asyncio.create_task(work)
        task_holder.append(task)
        return task

    with monkeypatch.context() as real_io:
        real_io.setattr(Store, "_async_write_data", REAL_WRITE)
        real_io.setattr(
            _ParticipantStore, "path", property(lambda self: str(tmp_path / self.key))
        )
        real_io.setattr(store, "_write_prepared_data", held)
        await state.load(store)
        task = asyncio.create_task(
            state.save(store, {"phase": "late"}, timeout=0.01, create_task=create_task)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(RevisionError, match="journal_write_timeout"):
                await task
            assert not Path(store.path).exists()
        finally:
            release.set()
            await asyncio.gather(task, *task_holder, return_exceptions=True)
        assert (
            json.loads(Path(store.path).read_text())["participant_revision"]["revision"]
            == 1
        )
        assert state.receipt is None and state.status == "uncertain"
        assert state.evidence({"phase": "late"}, PUMP) is None
