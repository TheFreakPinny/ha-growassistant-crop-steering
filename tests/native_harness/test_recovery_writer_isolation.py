"""Diagnostic isolation under fixed FakePump; real HA dispatch is forbidden."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from homeassistant.core import Context

from custom_components.growassistant_crop_steering.recovery_participant import (
    PumpGateEvidence,
    _RecoveryGateCoordinator,
    aggregate_evidence,
)
from custom_components.growassistant_crop_steering.recovery_revision import BootEpoch
from custom_components.growassistant_crop_steering.recovery_writer import WriterReason

from .harness import PUMP, NativeHarness
from .test_recovery_ledger import ledger_for
from .test_recovery_participant_gates import setup
from .test_simulation import assert_recovery_locked

pytestmark = pytest.mark.asyncio


def isolation(result):
    return next(
        p.writer_isolation for p in result.evidence if isinstance(p, PumpGateEvidence)
    )


def fire(harness, domain="switch", service="turn_on", data=None, context=None):
    # Inject only the HA event envelope; no service registration or dispatch.
    harness.hass.bus.async_fire(
        "call_service",
        {
            "domain": domain,
            "service": service,
            "service_data": data if data is not None else {"entity_id": PUMP},
        },
        context=context,
    )


async def test_known_owner_and_silence_never_prove_exclusivity(simulation):
    coordinator, _ = await setup(simulation)
    before = (
        simulation.checkpoint(),
        deepcopy(coordinator.ledger.store.records),
        list(simulation.actor.commands),
    )
    result = await coordinator.async_check()
    evidence = isolation(result)
    assert (
        evidence.internal_owner_known
        and evidence.owner_id == evidence.runtime.controller.value
    )
    assert evidence.no_foreign_writers_observed and not evidence.conflicts
    assert evidence.runtime.boot == result.request.boot_epoch
    assert evidence.revisions == result.request.revisions
    assert evidence.unknown_writers and evidence.monitoring_active
    assert WriterReason.VISIBILITY in result.blockers
    assert WriterReason.UNPROVEN in result.blockers
    assert WriterReason.EXCLUSIVE not in result.blockers
    assert not evidence.exclusive_isolation_confirmed
    assert not evidence.native_activation_allowed and not result.coordination_ready
    with pytest.raises(FrozenInstanceError):
        evidence.owner_id = "foreign"
    with pytest.raises(TypeError):
        replace(evidence, exclusive_isolation_confirmed=True)
    assert (
        simulation.checkpoint(),
        coordinator.ledger.store.records,
        simulation.actor.commands,
    ) == before
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_unknown_owner_is_explicit_and_blocks(simulation):
    coordinator, _ = await setup(simulation)
    simulation.pump.ownership = None
    result = await coordinator.async_check()
    evidence = isolation(result)
    assert not evidence.internal_owner_known and evidence.owner_id is None
    assert WriterReason.OWNER_UNKNOWN in result.blockers
    assert WriterReason.UNPROVEN in result.blockers
    assert not result.coordination_ready
    coordinator.close()


@pytest.mark.parametrize(
    "domain,service,data",
    [
        ("switch", "turn_on", {"entity_id": PUMP}),
        ("switch", "turn_off", {"entity_id": [PUMP]}),
        ("switch", "toggle", {"entity_id": "switch.other, " + PUMP}),
        ("homeassistant", "turn_on", {"entity_id": PUMP}),
        ("switch", "turn_off", {"entity_id": "all"}),
        ("vendor_gateway", "write", {"entity_id": PUMP}),
    ],
)
async def test_untrusted_targeted_service_immediately_revokes_without_dispatch(
    simulation, domain, service, data
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    before = simulation.checkpoint(), list(simulation.actor.commands)
    context = Context(user_id="external-user", parent_id="automation-parent")
    fire(simulation, domain, service, data, context)
    await simulation.hass.async_block_till_done()
    assert not coordinator.confirmation_current(result)
    new = await coordinator.async_check()
    evidence = isolation(new)
    assert WriterReason.CONFLICT in new.blockers
    assert evidence.conflicts[0].user_id == "external-user"
    assert evidence.conflicts[0].parent_id == "automation-parent"
    assert evidence.conflicts[0].context_id == context.id
    assert not evidence.no_foreign_writers_observed
    assert not new.coordination_ready
    assert (simulation.checkpoint(), simulation.actor.commands) == before
    coordinator.close()


@pytest.mark.parametrize(
    "domain,service,data",
    [
        ("script", "irrigate", {}),
        ("automation", "trigger", {"entity_id": "automation.irrigate"}),
        ("homeassistant", "turn_on", {"area_id": "growroom"}),
        ("switch", "toggle", {"device_id": "pump-device"}),
    ],
)
async def test_indirect_automation_script_or_target_remains_unknown(
    simulation, domain, service, data
):
    coordinator, _ = await setup(simulation)
    previous = await coordinator.async_check()
    fire(simulation, domain, service, data)
    await simulation.hass.async_block_till_done()
    assert not coordinator.confirmation_current(previous)
    result = await coordinator.async_check()
    evidence = isolation(result)
    assert evidence.writers and not evidence.conflicts
    assert WriterReason.VISIBILITY in result.blockers
    assert WriterReason.UNPROVEN in result.blockers
    assert not result.coordination_ready
    coordinator.close()


@pytest.mark.parametrize(
    "kind", ["state", "state_aba", "missing_event", "gap", "monitor_stop", "overflow"]
)
async def test_state_direct_access_or_monitoring_gap_is_fail_closed(simulation, kind):
    coordinator, _ = await setup(simulation)
    previous = await coordinator.async_check()
    commands = list(simulation.actor.commands)
    if kind in {"state", "state_aba"}:
        simulation.actor.report("on")
        if kind == "state_aba":
            simulation.actor.report("off")
    elif kind == "gap":
        coordinator._writers.visibility_lost()
    elif kind == "monitor_stop":
        coordinator._writers.close()
    elif kind == "overflow":
        for _ in range(130):
            fire(simulation)
    # A device/network command with no HA event is inherently unobservable.
    # Its absence from the log must still never certify external exclusion.
    await simulation.hass.async_block_till_done()
    if kind != "missing_event":
        assert not coordinator.confirmation_current(previous)
    result = await coordinator.async_check()
    evidence = isolation(result)
    assert WriterReason.VISIBILITY in result.blockers
    assert WriterReason.UNPROVEN in result.blockers
    assert not evidence.exclusive_isolation_confirmed and not result.coordination_ready
    if kind in {"state", "state_aba", "overflow"}:
        assert WriterReason.CONFLICT in result.blockers and evidence.conflicts
    if kind in {"gap", "monitor_stop", "overflow"}:
        assert "monitoring_gap" in evidence.unknown_writers
    assert len(evidence.writers) <= 128
    assert simulation.actor.commands == commands
    assert_recovery_locked(simulation)
    coordinator.close()


@pytest.mark.parametrize(
    "change",
    ["ownership", "ownership_aba", "target", "target_aba", "safety", "revision"],
)
async def test_live_changes_invalidate_bound_writer_evidence(simulation, change):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    old = isolation(result)
    if change.startswith("ownership"):
        owner = simulation.pump.ownership
        simulation.pump.ownership = None
        if change.endswith("aba"):
            simulation.pump.ownership = owner
    elif change.startswith("target"):
        options = dict(simulation.entry.options)
        simulation.hass.config_entries.async_update_entry(
            simulation.entry, options={**options, "pump_switch": "switch.other"}
        )
        if change.endswith("aba"):
            simulation.hass.config_entries.async_update_entry(
                simulation.entry, options=options
            )
    elif change == "safety":
        simulation.pump._runtime.invalidate()
    else:
        # Use actual versioned writes at the existing memory journal boundary.
        record = simulation.pump._record
        await simulation.pump._write({"diagnostic_revision_test": True})
        await simulation.pump._write(record)
    await simulation.hass.async_block_till_done()
    assert not coordinator.confirmation_current(result)
    fresh = coordinator._writers.capture(result.request)
    assert fresh != old
    assert WriterReason.EPOCH in fresh.reasons
    assert not fresh.exclusive_isolation_confirmed
    coordinator.close()


@pytest.mark.parametrize(
    "change", ["service", "competing", "gap", "ownership", "target"]
)
async def test_changes_during_recovery_await_are_not_publishable(
    simulation, monkeypatch, change
):
    coordinator, _ = await setup(simulation)
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.ledger.source._read

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(coordinator.ledger.source, "_read", held)
    task = asyncio.create_task(coordinator.async_check())
    await asyncio.wait_for(entered.wait(), 2)
    if change in {"service", "competing"}:
        fire(simulation)
        if change == "competing":
            fire(
                simulation, service="turn_off", context=Context(user_id="other-writer")
            )
    elif change == "gap":
        coordinator._writers.visibility_lost()
    elif change == "ownership":
        simulation.pump.ownership = None
    else:
        simulation.hass.config_entries.async_update_entry(
            simulation.entry,
            options={**simulation.entry.options, "pump_switch": "switch.other"},
        )
    await simulation.hass.async_block_till_done()
    release.set()
    result = await task
    assert not coordinator.confirmation_current(result)
    assert not result.coordination_ready and not result.native_activation_allowed
    assert "evidence_changed" in result.blockers
    assert WriterReason.UNPROVEN in result.blockers
    assert_recovery_locked(simulation)
    coordinator.close()


async def test_old_boot_revision_and_missing_isolation_are_never_accepted(simulation):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    evidence = isolation(result)
    altered = replace(result.request, boot_epoch=BootEpoch("old-boot"))
    assert WriterReason.EPOCH in coordinator._writers.capture(altered).reasons
    pump = next(p for p in result.evidence if isinstance(p, PumpGateEvidence))
    for changed in (
        replace(pump, writer_isolation=None),
        replace(pump, writer_isolation=replace(evidence, attempt_id="old-attempt")),
    ):
        aggregate = aggregate_evidence(
            result.request, tuple(changed if p is pump else p for p in result.evidence)
        )
        assert WriterReason.UNPROVEN in aggregate.blockers
        assert not aggregate.coordination_ready
    simulation.clock.move_to("2026-10-13T07:00:31+00:00")
    assert WriterReason.EXPIRED in coordinator._writers.capture(result.request).reasons
    assert not coordinator.confirmation_current(result)
    coordinator.close()


async def test_restart_does_not_restore_writer_proofs(
    simulation, monkeypatch, restarted_hass
):
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    old = isolation(result)
    checkpoint = simulation.checkpoint()
    records = deepcopy(coordinator.ledger.store.records)
    coordinator.close()
    await simulation.close()
    restored = NativeHarness(
        restarted_hass, monkeypatch, simulation.clock, checkpoint=checkpoint
    )
    try:
        await restored.initialize(fresh=False)
        ledger = await ledger_for(restored, records)
        fresh = _RecoveryGateCoordinator(ledger, restored.scheduler)
        try:
            current = await fresh.async_check()
            new = isolation(current)
            assert new.runtime.boot != old.runtime.boot
            assert new.runtime.controller != old.runtime.controller
            assert new.monitor_id != old.monitor_id
            assert not fresh.confirmation_current(result)
            assert WriterReason.UNPROVEN in current.blockers
            assert not restored.actor.commands and not restored.scheduler.running
            assert_recovery_locked(restored)
        finally:
            fresh.close()
    finally:
        await restored.close()


@pytest.mark.parametrize("damage", ["data", "domain", "service"])
async def test_malformed_event_is_visibility_loss(simulation, damage):
    coordinator, _ = await setup(simulation)
    previous = await coordinator.async_check()
    payload = {
        "domain": "switch",
        "service": "turn_on",
        "service_data": {"entity_id": PUMP},
    }
    payload[{"data": "service_data"}.get(damage, damage)] = []
    simulation.hass.bus.async_fire("call_service", payload)
    await simulation.hass.async_block_till_done()
    assert not coordinator.confirmation_current(previous)
    result = await coordinator.async_check()
    assert "monitoring_gap" in isolation(result).unknown_writers
    assert WriterReason.UNPROVEN in result.blockers
    assert not result.coordination_ready
    coordinator.close()


async def test_listener_close_is_local_and_does_not_touch_services(simulation):
    registry_before = simulation.hass.services.async_services()
    coordinator, _ = await setup(simulation)
    result = await coordinator.async_check()
    commands = list(simulation.actor.commands)
    records = simulation.checkpoint()
    coordinator.close()
    generation = coordinator._writers.generation
    fire(simulation)
    await simulation.hass.async_block_till_done()
    assert coordinator._writers.generation == generation
    assert simulation.hass.services.async_services() == registry_before
    assert not coordinator.confirmation_current(result)
    assert simulation.actor.commands == commands
    assert simulation.checkpoint() == records
