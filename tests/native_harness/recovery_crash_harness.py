"""Event-driven composition of real recovery owners; no recovery implementation.

Only isolated NativeHarness runtimes may be supplied. Stores and witnesses live
outside disposable instances. Boundaries suspend callers, never production IO.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
)
from custom_components.growassistant_crop_steering.const import DOMAIN
from custom_components.growassistant_crop_steering.recovery_participant import (
    _RecoveryGateCoordinator,
)
from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_release import (
    _CycleReleaseCoordinator,
    _HarnessReleaseAuthority,
)
from custom_components.growassistant_crop_steering.recovery_transaction import (
    _RecoveryTransactionCoordinator,
    plan_digest,
)

from .fake_actuator_gateway import FakeClock, FakeGateway, FakePersistence
from .fake_gateway_recovery import DiagnosticBinding, NativeGatewayRecoveryCoordinator
from .fake_gateway_transactions import AuditWitness, NativeGatewayTransactionDiagnostics
from .harness import MemoryStore, NativeHarness
from .test_recovery_ledger import acknowledgment, ledger_for
from .test_simulation import assert_recovery_locked, start_p1

CRASH_POINTS = (
    "before_gateway_snapshot",
    "after_gateway_snapshot",
    "participant_collection",
    "after_plan",
    "prepare_captured",
    "after_prepare",
    "commit_checked",
    "audit_writing",
    "commit_saved",
    "after_commit",
    "gateway_restarting",
    "ha_restarting",
    "ledger_writing",
    "participant_writing",
)


class EventBarrier:
    """Exactly one selected event; no timer, polling or scheduler assumptions."""

    def __init__(self, point):
        if point not in CRASH_POINTS:
            raise ValueError(point)
        self.point = point
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fired = False

    async def arrive(self, point):
        if point == self.point and not self.fired:
            self.fired = True
            self.entered.set()
            await self.release.wait()


@dataclass(frozen=True)
class EventState:
    name: str
    tick: float
    gateway_boot: str
    gateway_runtime: int
    ha_boot: object
    coordinator_boot: str
    adapter_epoch: str
    participant_revisions: tuple
    ledger_revision: int
    protocol: object
    persisted: object
    runtime: object


async def interrupted_case(runtime, monkeypatch):
    """Generate actual partial-delivery uncertainty using an activation barrier.

    The controller completes fake activation before the supervisor is told of
    sensor loss. No nominal shot duration or polling interval is awaited.
    """
    await start_p1(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    activate = runtime.pump.async_activate

    async def activated(*args, **kwargs):
        await activate(*args, **kwargs)
        entered.set()
        await release.wait()

    with monkeypatch.context() as patch:
        patch.setattr(runtime.pump, "async_activate", activated)
        task = asyncio.create_task(runtime.shots.async_execute("interrupted"))
        try:
            await entered.wait()
            runtime.vwc(40, "unavailable", 40)
            await runtime.shots._abort.wait()
        finally:
            release.set()
        result = await task
    assert result.state.value == "interrupted" and not result.accounted
    ledger = await ledger_for(runtime)
    registered = await ledger.async_register(request_id="register", expected_revision=0)
    return ledger, registered["recovery_id"]


class RecoveryCrashHarness:
    """Current components, retained stores, retired owners and safety transcript."""

    def __init__(self, runtime, monkeypatch):
        self.runtime, self.patch = runtime, monkeypatch
        self.clock = FakeClock()
        self.audit_store = MemoryStore({}, "audit")
        self.protocol_store = MemoryStore({}, "protocol")
        self.release_store = MemoryStore({}, "release")
        self.witness = AuditWitness()
        self.events = []
        self.retired = []
        self.transactions = []
        self.recoveries = []
        self.runtimes = [runtime]
        self.closed_runtimes = set()
        self.barrier = None
        self.results = []

    async def initialize(self):
        self.ledger, self.recovery_id = await interrupted_case(self.runtime, self.patch)
        self.history = self.runtime.shots.safety_snapshot().journal["operations"]
        self.commands = {id(self.runtime): tuple(self.runtime.actor.commands)}
        self.recovery = _RecoveryGateCoordinator(self.ledger, self.runtime.scheduler)
        self.recoveries.append(self.recovery)
        initial = await self.recovery.async_check()
        pump = self.runtime.pump.safety_snapshot()
        self.gateway = FakeGateway(
            ActuatorIdentity(initial.request.binding, "fake", "1", "test-only"),
            self.clock,
            FakePersistence(),
        )
        assert self.gateway.recheck()
        self.gateway.grant(pump.runtime.controller.value, pump.runtime.boot, 8)
        self.binding = DiagnosticBinding.capture(self.gateway, initial)
        await self._wire()
        self.record("initialized")

    async def _wire(self):
        self.integration = NativeGatewayRecoveryCoordinator(
            self.gateway, self.recovery, self.binding
        )
        check = self.integration.async_check

        async def instrumented(**kwargs):
            await self.hit("before_gateway_snapshot")

            async def boundary(name):
                if name == "gateway_captured":
                    await self.hit("after_gateway_snapshot")

            return await check(boundary=boundary, **kwargs)

        self.integration.async_check = instrumented
        capture = self.ledger.source.capture

        async def captured():
            evidence = await capture()
            if self.integration._busy:
                await self.hit("participant_collection")
            return evidence

        # Install once per ledger; fresh runtimes get a fresh source.
        if not getattr(self.ledger.source, "_crash_instrumented", False):
            self.ledger.source.capture = captured
            self.ledger.source._crash_instrumented = True
        self.transaction = _RecoveryTransactionCoordinator(self.ledger)
        self.transaction.store = self.protocol_store
        self.transactions.append(self.transaction)
        await self.transaction.async_initialize()
        owners = self.runtime.hass.data.setdefault(f"{DOMAIN}.cycle_release", {})
        previous = owners.pop(self.transaction.binding["entity_id"], None)
        if previous is not None:
            assert previous is self.release
            self.retired.append(previous)
        self.authority = _HarnessReleaseAuthority()
        self.release = _CycleReleaseCoordinator(self.transaction, self.authority)
        self.release.store = self.release_store
        await self.release.async_initialize()
        await self._adapter()

    async def _adapter(self):
        self.adapter = NativeGatewayTransactionDiagnostics(
            self.integration, self.transaction, self.audit_store, self.witness
        )
        await self.adapter.async_initialize()

    def stores(self):
        return {
            "ledger": self.ledger.store,
            "pump": self.runtime.pump.store,
            "shots": self.runtime.shots.store,
            "cycle": self.runtime.cycle.store,
            "protocol": self.protocol_store,
            "audit": self.audit_store,
            "release": self.release_store,
        }

    def persisted(self):
        return deepcopy(
            {
                **{k: (s.records, s.key) for k, s in self.stores().items()},
                "gateway": self.gateway.store.snapshot(),
                "gateway_witness": self.gateway.store.witness,
                "audit_witness": self.witness,
            }
        )

    def record(self, name):
        from .test_fake_gateway_recovery import assert_never_release

        runtime = self.runtime
        assert_recovery_locked(runtime)
        assert not runtime.scheduler.running
        assert runtime.shots.safety_snapshot().journal["operations"] == self.history
        assert self.ledger.native_activation_allowed is False
        assert self.recovery.native_activation_allowed is False
        assert self.transaction.native_activation_allowed is False
        assert self.release.native_activation_allowed is False
        assert self.release.permit is None
        assert self.release.inspect()["effective_state"] == "locked"
        assert self.transaction._session is None
        for result in self.results:
            assert_never_release(result)
        for old in self.runtimes:
            assert tuple(old.actor.commands) == self.commands[id(old)]
            assert not old.scheduler.running
        snapshots = (
            runtime.pump.safety_snapshot(),
            runtime.shots.safety_snapshot(),
            runtime.cycle.safety_snapshot(),
            runtime.scheduler.recovery_gate_snapshot(),
        )
        self.events.append(
            EventState(
                name,
                self.clock.tick,
                self.gateway.boot,
                self.gateway._runtime,
                snapshots[0].runtime.boot,
                self.transaction._boot_id,
                self.adapter.instance_id,
                tuple(getattr(s, "version", None) for s in snapshots),
                self.ledger.revision,
                self.transaction.inspect(),
                self.persisted(),
                snapshots,
            )
        )

    async def hit(self, name):
        self.record(name)
        if self.barrier:
            await self.barrier.arrive(name)
        self.record(name + ":resumed")

    async def plan(self):
        plan = await async_create_reconciliation_plan(self.ledger)
        assert plan.findings or plan.open_checks
        await self.hit("after_plan")
        return plan

    async def prepare(self, plan=None):
        plan = plan or await self.plan()
        adapter = self.adapter  # Deliberately retain the old caller across restart.
        result = await adapter.async_prepare(
            plan=plan,
            request_id="end-to-end",
            expected_plan_id=plan.plan_id,
            expected_plan_digest=plan_digest(plan),
            expected_ledger_revision=plan.source_revisions["ledger"],
            boundary=self.hit,
        )
        self.results.append(result)
        await self.hit("after_prepare")
        return result

    async def commit(self, prepared):
        result = await self.adapter.async_commit(prepared, boundary=self.hit)
        self.results.append(result)
        await self.hit("after_commit")
        return result

    async def attempt(self):
        prepared = await self.prepare()
        if not prepared.evidence_consistent:
            return prepared
        return await self.commit(prepared)

    async def persist_participant(self, name):
        owner = getattr(self.runtime, name)
        payload = await owner.store.async_load()
        # Detached storage fault proposal. Historical operations and accounting
        # in the actual controller are never edited to manufacture a write.
        candidate = {**(payload or {}), "harness_fault_probe": True}
        create_task = owner._revision_task
        await owner._revision.save(
            owner.store, candidate, timeout=5, create_task=create_task
        )

    async def review(self):
        return await self.ledger.async_acknowledge(
            **acknowledgment(self.ledger, self.recovery_id, self.runtime)
        )

    async def restart_gateway(self):
        old = self.gateway
        self.gateway = FakeGateway(old.actuator, self.clock, old.store)
        self.retired.append(old)
        await self.hit("gateway_restarting")
        self.integration.gateway = self.gateway
        self.record("gateway_restarted")

    async def restart_adapter(self):
        old = self.adapter
        old.restart()
        self.retired.append(old)
        await self._adapter()
        self.record("adapter_restarted")

    async def restart_coordinator(self):
        old = self.transaction
        old.revoke("coordinator_restart")
        for unsub in old._unsub:
            unsub()
        old._unsub.clear()
        owners = old.hass.data[f"{DOMAIN}.recovery_transactions"]
        assert owners.pop(old.binding["entity_id"]) is old
        self.retired.append(old)
        await self._wire()
        self.record("coordinator_restarted")

    async def restart_ha(self, hass):
        old = self.runtime
        checkpoint = old.checkpoint()
        ledger_records = deepcopy(self.ledger.store.records)
        self.adapter.restart()
        self.recovery.close()
        old.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await old.hass.async_block_till_done()
        await old.close()
        self.closed_runtimes.add(id(old))
        # Normal close may append OFF; these are explicit shutdown operations,
        # outside the diagnostic path, and must never append ON.
        prior = self.commands[id(old)]
        assert all(c.service == "turn_off" for c in old.actor.commands[len(prior) :])
        self.commands[id(old)] = tuple(old.actor.commands)
        self.retired.extend((old, self.recovery, self.transaction, self.adapter))
        await self.hit("ha_restarting")
        self.runtime = NativeHarness(hass, self.patch, old.clock, checkpoint=checkpoint)
        await self.runtime.initialize(fresh=False)
        assert not self.runtime.actor.commands
        self.commands[id(self.runtime)] = ()
        self.runtimes.append(self.runtime)
        self.ledger = await ledger_for(self.runtime, ledger_records)
        self.recovery = _RecoveryGateCoordinator(self.ledger, self.runtime.scheduler)
        self.recoveries.append(self.recovery)
        await self._wire()
        self.record("ha_restarted")

    async def close(self):
        for recovery in self.recoveries:
            recovery.close()
        for transaction in self.transactions:
            for unsub in transaction._unsub:
                unsub()
            transaction._unsub.clear()
        for runtime in self.runtimes[1:]:
            if id(runtime) not in self.closed_runtimes:
                await runtime.close()
                self.closed_runtimes.add(id(runtime))


class StoreFault:
    """Fault the existing storage boundary without replacing its codec/revisions."""

    def __init__(self, harness, store, mode, point):
        self.harness, self.store, self.mode, self.point = harness, store, mode, point
        self.submitted = self.persisted = False

    def install(self, patch):
        save = self.store.async_save

        async def faulty(data):
            self.submitted = True
            if self.mode == "before":
                raise OSError("before persistence")
            if self.mode == "partial":
                self.store.records[self.store.key] = {"partial": True}
                raise OSError("partial persistence")
            if self.mode in {"after", "delay_after"}:
                await save(data)
                self.persisted = True
            await self.harness.hit(self.point)
            if self.mode == "after":
                raise OSError("after persistence")
            if self.mode not in {"after", "delay_after"}:
                await save(data)
                self.persisted = True

        patch.setattr(self.store, "async_save", faulty)
