"""Deterministic tests of the isolated gateway, never real pump authority."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
    CommandIntent,
    FencingToken,
)
from custom_components.growassistant_crop_steering.recovery_revision import BootEpoch
from custom_components.growassistant_crop_steering.recovery_snapshot import (
    ResourceBinding,
)

from .fake_actuator_gateway import FakeClock, FakeGateway, FakePersistence


class Barrier:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self):
        self.entered.set()
        await self.release.wait()


@pytest.fixture
def model():
    clock = FakeClock()
    store = FakePersistence()
    gateway = FakeGateway(
        ActuatorIdentity(ResourceBinding("entry", "switch.fake"), "fake", "1", "test"),
        clock,
        store,
    )
    assert gateway.recheck()
    lease = gateway.grant("writer", BootEpoch("ha"), 8)
    intent = CommandIntent(
        gateway.actuator,
        "cmd",
        "on",
        FencingToken(lease.lease_id, lease.epoch, lease.owner_id),
        lease.ha_boot,
        1,
        7,
    )
    return gateway, clock, store, intent


async def test_accept_is_not_execution(model):
    gateway, _, _, intent = model
    receipt = gateway.accept(intent)
    assert receipt.status == "accepted"
    assert gateway.state == "off"
    assert not receipt.native_activation_allowed
    assert not receipt.coordination_ready
    assert (await gateway.execute(intent)).status == "executed"
    assert gateway.events == ("off", "on")


@pytest.mark.parametrize(
    "change", ["target", "token", "owner", "boot", "sequence", "deadline", "nan"]
)
async def test_reject_invalid(model, change):
    gateway, _, _, intent = model
    changes = {
        "target": {"actuator": replace(intent.actuator, channel="wrong")},
        "token": {"token": replace(intent.token, lease_id="forged")},
        "owner": {"token": replace(intent.token, owner_id="forged")},
        "boot": {"ha_boot": BootEpoch("old")},
        "sequence": {"sequence": 0},
        "deadline": {"deadline_tick": 0},
        "nan": {"deadline_tick": float("nan")},
    }
    bad = replace(intent, **changes[change])
    assert gateway.accept(bad).status == "rejected"
    assert (await gateway.execute(bad)).status == "rejected"
    assert "on" not in gateway.events


@pytest.mark.parametrize(
    "event",
    [
        "expiry",
        "revoke",
        "stop",
        "disconnect",
        "watchdog",
        "unknown",
        "restart",
        "handover",
        "deadline",
    ],
)
async def test_revalidate_across_await(model, event):
    gateway, clock, store, intent = model
    assert gateway.accept(intent).status == "accepted"
    barrier = Barrier()
    task = asyncio.create_task(gateway.execute(intent, before_apply=barrier.wait))
    await barrier.entered.wait()
    if event == "expiry":
        clock.advance(8)
    elif event == "deadline":
        clock.advance(7)
    elif event == "revoke":
        gateway.revoke()
    elif event == "stop":
        gateway.safety_off("test")
    elif event == "disconnect":
        gateway.connection_lost()
    elif event == "unknown":
        gateway.unknown_state()
    elif event == "watchdog":
        clock.advance(10)
        gateway.poll()
    elif event == "restart":
        restarted = FakeGateway(gateway.actuator, clock, store)
        assert restarted.boot != gateway.boot
        assert restarted.stopped and restarted.lease is None
    else:
        gateway.revoke()
        assert gateway.recheck()
        new_lease = gateway.grant("new-writer", BootEpoch("new-ha"), 8)
        assert new_lease.epoch.generation.value > intent.token.epoch.generation.value
    barrier.release.set()
    assert (await task).status in {"rejected", "uncertain"}
    assert "on" not in gateway.events


async def test_idempotency_conflict_and_duplicate_on(model):
    gateway, _, _, intent = model
    accepted = gateway.accept(intent)
    assert gateway.accept(intent) == accepted
    assert gateway.accept(replace(intent, sequence=2)).reason == "command_id_conflict"
    executed = await gateway.execute(intent)
    assert gateway.accept(intent) == executed
    assert await gateway.execute(intent) == executed
    second = replace(intent, command_id="second", sequence=2)
    gateway.accept(second)
    assert (await gateway.execute(second)).reason == "already_on"
    assert gateway.events.count("on") == 1


async def test_concurrent_execution_serialized_and_off_priority(model):
    gateway, _, _, intent = model
    second = replace(intent, command_id="second", sequence=2)
    gateway.accept(intent)
    barrier = Barrier()
    first = asyncio.create_task(gateway.execute(intent, before_apply=barrier.wait))
    await barrier.entered.wait()
    assert gateway.accept(second).status == "accepted"
    next_task = asyncio.create_task(gateway.execute(second))
    # Stop takes effect synchronously while the execution lock is occupied.
    assert gateway.safety_off("urgent")
    for _ in range(3):
        assert gateway.safety_off("again")
    barrier.release.set()
    results = await asyncio.gather(first, next_task)
    assert all(r.status == "rejected" for r in results)
    assert gateway.events == ("off",)


async def test_concurrent_valid_on_only_once(model):
    gateway, _, _, intent = model
    second = replace(intent, command_id="second", sequence=2)
    gateway.accept(intent)
    gateway.accept(second)
    results = await asyncio.gather(gateway.execute(intent), gateway.execute(second))
    assert [r.status for r in results] == ["executed", "rejected"]
    assert gateway.events.count("on") == 1


async def test_execution_order_cannot_go_backwards(model):
    gateway, _, _, intent = model
    second = replace(intent, command_id="second", sequence=2, action="off")
    gateway.accept(intent)
    gateway.accept(second)
    assert (await gateway.execute(second)).status == "executed"
    assert (await gateway.execute(intent)).status == "rejected"
    assert gateway.accept(replace(intent, command_id="old")).status == "rejected"
    assert "on" not in gateway.events


@pytest.mark.parametrize("phase", ["accept", "execute", "finish"])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_persistence_failures_fail_closed(model, phase, when):
    gateway, clock, store, intent = model
    if phase != "accept":
        gateway.accept(intent)
    store.fail = f"{phase}:{when}"
    result = (
        gateway.accept(intent) if phase == "accept" else await gateway.execute(intent)
    )
    assert result.status == "uncertain"
    assert gateway.stopped and gateway.fault and gateway.state == "off"
    assert not gateway.recheck()
    count = gateway.events.count("on")
    restarted = FakeGateway(gateway.actuator, clock, store)
    assert restarted.stopped and restarted.lease is None
    assert restarted.events.count("on") == count
    # A committed final receipt may exist after an acknowledgement failure;
    # neither that receipt nor an uncertain intent authorizes replay.
    await restarted.execute(intent)
    assert restarted.events.count("on") == count


@pytest.mark.parametrize("hook", ["before_apply", "after_apply"])
async def test_crash_pending_intent_no_replay(model, hook):
    gateway, clock, store, intent = model
    gateway.accept(intent)
    barrier = Barrier()
    task = asyncio.create_task(gateway.execute(intent, **{hook: barrier.wait}))
    await barrier.entered.wait()
    count = gateway.events.count("on")
    restarted = FakeGateway(gateway.actuator, clock, store)
    assert store.records[intent.command_id].status == "uncertain"
    assert restarted.state == "off"
    barrier.release.set()
    assert (await task).status == "uncertain"
    assert store.records[intent.command_id].reason == "interrupted_by_restart"
    assert restarted.recheck()
    restarted.grant("new", BootEpoch("new-ha"), 8)
    assert restarted.accept(intent).status == "uncertain"
    await restarted.execute(intent)
    assert restarted.events.count("on") == count


@pytest.mark.parametrize("event", ["expiry", "ownership", "stop", "restart"])
async def test_invalidation_after_apply_is_uncertain(model, event):
    gateway, clock, store, intent = model
    gateway.accept(intent)
    barrier = Barrier()
    task = asyncio.create_task(gateway.execute(intent, after_apply=barrier.wait))
    await barrier.entered.wait()
    assert gateway.state == "on"
    if event == "expiry":
        clock.advance(8)
    elif event == "ownership":
        gateway.revoke()
        gateway.recheck()
        gateway.grant("new", BootEpoch("new-ha"), 8)
    elif event == "restart":
        FakeGateway(gateway.actuator, clock, store)
    else:
        gateway.safety_off("stop")
    barrier.release.set()
    assert (await task).status == "uncertain"
    assert gateway.state == "off"


async def test_cancelled_execution_remains_uncertain(model):
    gateway, _, store, intent = model
    gateway.accept(intent)
    barrier = Barrier()
    task = asyncio.create_task(gateway.execute(intent, after_apply=barrier.wait))
    await barrier.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.records[intent.command_id].status == "uncertain"
    assert gateway.state == "off"


@pytest.mark.parametrize("event", ["lease", "watchdog", "connection", "unknown"])
async def test_running_output_stops_on_fault(model, event):
    gateway, clock, _, intent = model
    gateway.accept(intent)
    await gateway.execute(intent)
    if event == "lease":
        clock.advance(8)
    elif event == "watchdog":
        clock.advance(10)
    elif event == "connection":
        gateway.connection_lost()
    else:
        gateway.unknown_state()
    gateway.poll()
    assert gateway.stopped and gateway.lease is None and gateway.state == "off"


async def test_off_unconfirmed_remains_unknown(model):
    gateway, clock, store, intent = model
    gateway.accept(intent)
    await gateway.execute(intent)
    gateway.set_off_confirmation(False)
    assert not gateway.safety_off("test")
    assert gateway.state == "unknown"
    restarted = FakeGateway(gateway.actuator, clock, store)
    assert restarted.state == "unknown" and restarted.stopped
    assert not restarted.recheck()
    assert (
        restarted.accept(replace(intent, command_id="new", sequence=2)).status
        == "rejected"
    )


@pytest.mark.parametrize("phase", ["boot", "grant", "recover"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_lifecycle_persistence_failure(model, phase, when):
    gateway, clock, store, intent = model
    if phase == "recover":
        gateway.accept(intent)
    if phase == "grant":
        gateway.revoke()
        gateway.recheck()
    store.fail = f"{phase}:{when}"
    if phase == "grant":
        with pytest.raises(OSError):
            gateway.grant("new", BootEpoch("ha"), 8)
        target = gateway
    else:
        target = FakeGateway(gateway.actuator, clock, store)
        assert gateway.accept(intent).status in {"rejected", "uncertain", "accepted"}
        assert gateway.stopped
    assert target.fault and target.stopped and not target.recheck()


@pytest.mark.parametrize("tick", [-1, float("nan"), float("inf")])
def test_clock_fault_latches(model, tick):
    gateway, clock, _, intent = model
    clock.tick = tick
    assert gateway.accept(intent).status == "rejected"
    assert gateway.fault and not gateway.recheck()


async def test_gateway_cannot_mutate_native_gates_or_counters(model, simulation):
    gateway, _, _, intent = model
    before = simulation.checkpoint()
    commands = list(simulation.actor.commands)
    gateway.accept(intent)
    await gateway.execute(intent)
    gateway.safety_off("test")
    assert simulation.checkpoint() == before
    assert simulation.actor.commands == commands
    assert not gateway.native_activation_allowed and not gateway.coordination_ready


async def test_unexpected_executor_failure_is_uncertain(model):
    gateway, _, store, intent = model
    gateway.accept(intent)

    async def failure():
        raise RuntimeError("simulated acknowledgement failure")

    result = await gateway.execute(intent, after_apply=failure)
    assert result.status == "uncertain"
    assert store.records[intent.command_id].status == "uncertain"
    assert gateway.state == "off" and gateway.stopped


def test_safety_receipts_never_attest_physical_off(model):
    gateway, _, _, _ = model
    gateway.safety_off("emergency")
    receipt = gateway.off_receipts[-1]
    assert receipt.result == "software_off" and receipt.reason == "emergency"
    assert not receipt.native_activation_allowed and not receipt.coordination_ready
    gateway.set_off_confirmation(False)
    gateway.safety_off("retry")
    assert gateway.off_receipts[-1].result == "unknown"


def test_only_gateway_issues_lease_and_requires_explicit_handover(model):
    gateway, _, _, _ = model
    old = gateway.lease
    with pytest.raises(ValueError):
        gateway.grant("other", BootEpoch("other-ha"), 8)
    gateway.revoke()
    with pytest.raises(ValueError):
        gateway.grant("other", BootEpoch("other-ha"), 8)
    assert gateway.recheck()
    new = gateway.grant("other", BootEpoch("other-ha"), 8)
    assert new.epoch.generation.value == old.epoch.generation.value + 1


async def test_heartbeat_does_not_extend_lease(model):
    gateway, clock, _, intent = model
    gateway.accept(intent)
    await gateway.execute(intent)
    clock.advance(6)
    gateway.heartbeat()
    assert gateway.state == "on"
    clock.advance(2)
    gateway.poll()
    assert gateway.state == "off" and gateway.stopped


async def test_late_old_gateway_off_invalidates_new_gateway_pending_on(model):
    old, clock, store, intent = model
    current = FakeGateway(old.actuator, clock, store)
    assert current.recheck()
    lease = current.grant("new", BootEpoch("new-ha"), 8)
    command = replace(
        intent,
        command_id="new-command",
        ha_boot=lease.ha_boot,
        token=FencingToken(lease.lease_id, lease.epoch, lease.owner_id),
    )
    assert current.accept(command).status == "accepted"
    barrier = Barrier()
    task = asyncio.create_task(current.execute(command, before_apply=barrier.wait))
    await barrier.entered.wait()
    # Even a delayed safety request from the old instance is actuator-wide.
    old.safety_off("late_old_off")
    barrier.release.set()
    assert (await task).status == "rejected"
    assert current.stopped and current.lease is None
    assert "on" not in current.events
