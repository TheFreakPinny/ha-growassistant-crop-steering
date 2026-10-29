"""Contract examples never issue commands or establish real isolation."""

from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
    CommandIntent,
    CommandReceipt,
    FencingToken,
    IsolationEvidence,
    OwnershipEpoch,
    OwnershipLease,
    validate_command,
    validate_gate_contract,
    validate_lease,
    validate_receipt,
)
from custom_components.growassistant_crop_steering.recovery_participant import (
    ParticipantAggregation,
)
from custom_components.growassistant_crop_steering.recovery_revision import (
    BootEpoch,
    JournalIdentity,
    JournalReceipt,
    OwnershipGeneration,
)
from custom_components.growassistant_crop_steering.recovery_snapshot import (
    ResourceBinding,
)


@pytest.fixture
def contract():
    actuator = ActuatorIdentity(
        ResourceBinding("entry", "switch.pump"), "serial", "1", "acl"
    )
    epoch = OwnershipEpoch("gateway-boot", OwnershipGeneration(7))
    boot = BootEpoch("ha-boot")
    lease = OwnershipLease(actuator, epoch, "writer", boot, "lease", 10, 20)
    intent = CommandIntent(
        actuator, "cmd", "on", FencingToken("lease", epoch, "writer"), boot, 2, 19
    )
    context = dict(actuator=actuator, epoch=epoch, ha_boot=boot, now_tick=15)
    return lease, intent, context


def assert_no_authority(result):
    assert result.native_activation_allowed is False
    assert result.coordination_ready is False
    assert result.hardware_exclusivity_proven is False


def test_consistent_values_are_not_authority(contract):
    lease, intent, context = contract
    for result in (
        validate_lease(lease, **context),
        validate_command(intent, lease, **context, last_sequence=1),
    ):
        assert result.blockers == ()
        assert_no_authority(result)
    evidence = IsolationEvidence(lease, "unverified-reference", ("ha", "device"), 15)
    assert evidence.exclusive_isolation_confirmed is False
    with pytest.raises(FrozenInstanceError):
        evidence.exclusive_isolation_confirmed = True


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_id": ""},
        {"owner_id": " "},
        {"issued_tick": 16},
        {"issued_tick": -1},
        {"expires_tick": 15},
        {"expires_tick": 9},
        {"expires_tick": float("nan")},
        {"issued_tick": float("inf")},
        {"issued_tick": True},
        {"ha_boot": BootEpoch("old")},
        {"epoch": OwnershipEpoch("old", OwnershipGeneration(7))},
        {"epoch": OwnershipEpoch("gateway-boot", OwnershipGeneration(6))},
    ],
)
def test_invalid_lease(contract, changes):
    lease, _, context = contract
    result = validate_lease(replace(lease, **changes), **context)
    assert result.blockers
    assert_no_authority(result)


@pytest.mark.parametrize("generation", [0, -1, True])
def test_invalid_generation(contract, generation):
    lease, _, context = contract
    epoch = OwnershipEpoch("gateway-boot", OwnershipGeneration(generation))
    context["epoch"] = epoch
    assert validate_lease(replace(lease, epoch=epoch), **context).blockers


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"sequence": 1}, "command_sequence_conflict"),
        ({"sequence": True}, "command_sequence_conflict"),
        ({"deadline_tick": 15}, "command_deadline_invalid"),
        ({"deadline_tick": 21}, "command_deadline_invalid"),
        ({"command_id": ""}, "command_invalid"),
        ({"action": "toggle"}, "command_invalid"),
        ({"ha_boot": BootEpoch("old")}, "command_boot_conflict"),
        (
            {
                "token": FencingToken(
                    "other", OwnershipEpoch("g", OwnershipGeneration(7)), "writer"
                )
            },
            "fencing_conflict",
        ),
    ],
)
def test_invalid_command(contract, changes, code):
    lease, intent, context = contract
    result = validate_command(
        replace(intent, **changes), lease, **context, last_sequence=1
    )
    assert code in result.blockers
    assert_no_authority(result)


def test_wrong_physical_target(contract):
    lease, intent, context = contract
    other = replace(lease.actuator, channel="2")
    result = validate_command(
        replace(intent, actuator=other), lease, **context, last_sequence=1
    )
    assert "command_target_conflict" in result.blockers


@pytest.mark.parametrize(
    "change", ["ha_restart", "gateway_restart", "handover", "lease_expired"]
)
def test_old_writer_after_crash_or_handover(contract, change):
    lease, intent, context = contract
    if change == "ha_restart":
        context["ha_boot"] = BootEpoch("new-ha")
    elif change == "gateway_restart":
        context["epoch"] = replace(lease.epoch, gateway_boot="new-gateway")
    elif change == "handover":
        context["epoch"] = replace(lease.epoch, generation=OwnershipGeneration(8))
    else:
        context["now_tick"] = 20
    for action in ("on", "off"):
        result = validate_command(
            replace(intent, action=action), lease, **context, last_sequence=1
        )
        assert result.blockers
        assert_no_authority(result)
    # Normal OFF rejection says nothing about the independent emergency channel.


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"gateway_boot": "old"}, "receipt_binding_conflict"),
        ({"sequence": 1}, "receipt_binding_conflict"),
        ({"sequence": True}, "receipt_binding_conflict"),
        ({"result": "rejected"}, "command_rejected"),
        ({"result": "physical_off"}, "receipt_result_invalid"),
        ({"result": "software_off"}, "receipt_off_conflict"),
        ({"observed_state": "unknown"}, "actuator_state_unknown"),
        ({"observed_state": "unavailable"}, "actuator_state_unknown"),
        ({"observed_tick": 16}, "receipt_time_invalid"),
        ({"observed_tick": 1}, "receipt_time_invalid"),
        ({"observed_tick": float("nan")}, "receipt_time_invalid"),
    ],
)
def test_invalid_receipt(contract, changes, code):
    _, intent, _ = contract
    receipt = CommandReceipt(intent, "gateway-boot", 2, "accepted", "on", 14)
    result = validate_receipt(
        replace(receipt, **changes),
        intent,
        gateway_boot="gateway-boot",
        now_tick=15,
        max_age=2,
    )
    assert code in result.blockers
    assert_no_authority(result)


def test_off_receipt_is_software_only(contract):
    _, intent, _ = contract
    intent = replace(intent, action="off")
    receipt = CommandReceipt(intent, "gateway-boot", 2, "software_off", "off", 14)
    result = validate_receipt(
        receipt, intent, gateway_boot="gateway-boot", now_tick=15, max_age=2
    )
    assert not result.blockers
    assert_no_authority(result)


@pytest.mark.parametrize(
    "prefix",
    [
        "before_prepare",
        "partial_commit",
        "technical_commit",
        "release_audit",
        "restart",
    ],
)
def test_durable_prefix_never_releases(prefix):
    # History labels cannot supply missing live participant evidence.
    aggregation = ParticipantAggregation(None, (), (prefix,), False, False)
    receipt = JournalReceipt(
        JournalIdentity("entry", "switch.pump", "pump", "journal"), 1, "digest"
    )
    result = validate_gate_contract(
        aggregation, expected_revisions=(receipt,), observed_revisions=(receipt,)
    )
    assert prefix in result.blockers
    assert "gate_evidence_incomplete" in result.blockers
    assert_no_authority(result)


@pytest.mark.parametrize(
    "change", ["revision", "identity", "digest", "missing", "duplicate"]
)
def test_revision_conflict(contract, change):
    aggregation = ParticipantAggregation(None, (), (), False, False)
    receipt = JournalReceipt(
        JournalIdentity("entry", "switch.pump", "pump", "journal"), 1, "digest"
    )
    observed = (receipt,)
    if change == "revision":
        observed = (replace(receipt, revision=2),)
    elif change == "identity":
        observed = (
            replace(receipt, identity=replace(receipt.identity, journal_id="other")),
        )
    elif change == "digest":
        observed = (replace(receipt, digest="other"),)
    elif change == "missing":
        observed = ()
    else:
        observed = (receipt, receipt)
    result = validate_gate_contract(
        aggregation, expected_revisions=(receipt,), observed_revisions=observed
    )
    assert {"revision_invalid", "revision_conflict"} & set(result.blockers)
    assert_no_authority(result)


@pytest.mark.parametrize("field", ["lease_id", "owner_id", "epoch"])
def test_stale_token_with_current_lease(contract, field):
    lease, intent, context = contract
    value = {
        "lease_id": "old-lease",
        "owner_id": "old-writer",
        "epoch": replace(lease.epoch, generation=OwnershipGeneration(6)),
    }[field]
    intent = replace(intent, token=replace(intent.token, **{field: value}))
    result = validate_command(intent, lease, **context, last_sequence=1)
    assert result.blockers == ("fencing_conflict",)
    assert_no_authority(result)


@pytest.mark.parametrize("missing", [False, True])
async def test_actual_participant_evidence_remains_locked(simulation, missing):
    from .test_recovery_participant_gates import setup

    coordinator, _ = await setup(simulation)
    try:
        aggregation = await coordinator.async_check()
        before = simulation.checkpoint()
        commands = list(simulation.actor.commands)
        revisions = tuple(
            dict.fromkeys(
                r.receipt
                for r in aggregation.request.revisions
                if r.receipt is not None
            )
        )
        if missing:
            aggregation = replace(aggregation, evidence=aggregation.evidence[:-1])
        result = validate_gate_contract(
            aggregation,
            expected_revisions=revisions,
            observed_revisions=revisions,
        )
        assert "external_writers_unproven" in result.blockers
        if missing:
            assert "gate_evidence_incomplete" in result.blockers
        assert_no_authority(result)
        assert simulation.checkpoint() == before
        assert simulation.actor.commands == commands
        assert not simulation.scheduler.running
    finally:
        coordinator.close()
