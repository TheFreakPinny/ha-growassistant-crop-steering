"""Harness-only correlation of real blocked plans; never a recovery state machine."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field, fields
from typing import Literal
from uuid import UUID, uuid4

from custom_components.growassistant_crop_steering.recovery_ledger import (
    SCHEMA_VERSION,
    _digest,
    _json,
)
from custom_components.growassistant_crop_steering.recovery_plan import (
    async_create_reconciliation_plan,
)
from custom_components.growassistant_crop_steering.recovery_transaction import (
    PROTOCOL,
    _observations,
    plan_digest,
)

from .fake_gateway_recovery import RETAINED_BLOCKERS, recovery_scope


def canonical(value):
    return json.dumps(_json(value), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TransactionContext:
    transaction_id: str
    request_id: str
    instance_id: str
    binding: object
    gateway_boot: str
    gateway_runtime: int
    ownership_epoch: int
    safety_generation: int
    journal_revision: int
    witness_status: tuple[bool, bool, bool]
    ledger_revision: int
    plan_id: str
    plan_digest: str
    participant_revisions: str
    cycle_id: str
    operations: str
    # Full evidence, including independent HA ownership/safety/runtime epochs.
    gateway_evidence: str
    recovery_evidence: str
    participant_request: str
    transaction_protocol: str


@dataclass(frozen=True)
class TransactionDiagnostic:
    context: TransactionContext
    evidence_consistent: bool
    audit_persisted: bool
    blockers: tuple[str, ...]
    prepare_blocked: Literal[True] = field(default=True, init=False)
    recovery_release_allowed: Literal[False] = field(default=False, init=False)
    cycle_release_allowed: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)
    hardware_exclusivity_proven: Literal[False] = field(default=False, init=False)
    release_permit: None = field(default=None, init=False)


@dataclass
class AuditWitness:
    """Independent retained model witness; joint rollback cannot be detected."""

    expected: dict | None = None


@dataclass(frozen=True)
class HistoricalAudit:
    """Read-only integrity diagnosis; equality never restores a capability."""

    request_ids: tuple[str, ...]
    transaction_ids: tuple[str, ...]
    context_differences: tuple[tuple[str, ...], ...]
    blockers: tuple[str, ...] = field(default=RETAINED_BLOCKERS, init=False)
    historical_only: Literal[True] = field(default=True, init=False)
    current_authorization: Literal[False] = field(default=False, init=False)
    authenticity_proven: Literal[False] = field(default=False, init=False)
    atomic_multi_store_commit: Literal[False] = field(default=False, init=False)
    prepare_blocked: Literal[True] = field(default=True, init=False)
    recovery_release_allowed: Literal[False] = field(default=False, init=False)
    cycle_release_allowed: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)
    hardware_exclusivity_proven: Literal[False] = field(default=False, init=False)
    release_permit: None = field(default=None, init=False)


def inspect_audit_history(data, *, current_context=None):
    """Validate structure and chain only, without executing or adopting receipts.

    The caller must separately check a retained witness. Joint rollback of that
    witness and this journal is indistinguishable from the original history.
    """
    if data is None:
        return HistoricalAudit((), (), ())
    if (
        type(data) is not dict
        or set(data) != {"schema", "receipts"}
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or type(data["receipts"]) is not list
    ):
        raise ValueError("audit_schema_invalid")
    previous, transactions, requests, differences = None, [], [], []
    context_fields = {f.name for f in fields(TransactionContext)}
    receipt_fields = {
        "context",
        "previous",
        "evidence_consistent_at_check",
        "historical_only",
        "recovery_release_allowed",
        "blockers",
        "hash",
    }
    for receipt in data["receipts"]:
        if type(receipt) is not dict or set(receipt) != receipt_fields:
            raise ValueError("audit_receipt_invalid")
        context = receipt["context"]
        if type(context) is not dict or set(context) != context_fields:
            raise ValueError("audit_context_invalid")
        for key in context_fields - {
            "binding",
            "witness_status",
            "gateway_runtime",
            "ownership_epoch",
            "safety_generation",
            "journal_revision",
            "ledger_revision",
        }:
            if type(context[key]) is not str or not context[key].strip():
                raise ValueError("audit_context_invalid")
        try:
            for key in ("transaction_id", "instance_id"):
                if str(UUID(context[key])) != context[key]:
                    raise ValueError("identity")
            for key in (
                "participant_revisions",
                "operations",
                "gateway_evidence",
                "recovery_evidence",
                "participant_request",
                "transaction_protocol",
            ):
                decoded = json.loads(context[key])
                if (
                    type(decoded) not in (dict, list)
                    or canonical(decoded) != context[key]
                ):
                    raise ValueError("evidence")
        except (ValueError, TypeError):
            raise ValueError("audit_context_invalid") from None
        for key in (
            "gateway_runtime",
            "ownership_epoch",
            "safety_generation",
            "journal_revision",
            "ledger_revision",
        ):
            if type(context[key]) is not int or context[key] < 0:
                raise ValueError("audit_context_invalid")
        if (
            type(context["binding"]) is not dict
            or not context["binding"]
            or type(context["witness_status"]) is not list
            or len(context["witness_status"]) != 3
            or any(type(v) is not bool for v in context["witness_status"])
            or type(receipt["blockers"]) is not list
            or any(not isinstance(v, str) for v in receipt["blockers"])
            or receipt["evidence_consistent_at_check"] is not True
            or receipt["historical_only"] is not True
            or receipt["recovery_release_allowed"] is not False
        ):
            raise ValueError("audit_receipt_invalid")
        payload = {k: v for k, v in receipt.items() if k != "hash"}
        if receipt["hash"] != _digest(payload) or receipt["previous"] != previous:
            raise ValueError("audit_corrupt")
        if context["transaction_id"] in transactions:
            raise ValueError("audit_transaction_duplicate")
        transactions.append(context["transaction_id"])
        requests.append(context["request_id"])
        current = _json(current_context) if current_context is not None else {}
        differences.append(
            tuple(sorted(k for k in context_fields if context[k] != current.get(k)))
        )
        previous = receipt["hash"]
    return HistoricalAudit(tuple(requests), tuple(transactions), tuple(differences))


class NativeGatewayTransactionDiagnostics:
    """Single-use diagnostic attempt. Existing protocol is observed, never advanced.

    The separate audit store contains historical observations, not prepared or
    committed recovery states. No participant, gateway or protocol store writes.
    """

    def __init__(self, integration, transaction, store, witness):
        if transaction.ledger is not integration.recovery.ledger:
            raise ValueError("ledger_identity_conflict")
        self.integration, self.transaction = integration, transaction
        self.store, self.witness = store, witness
        self.instance_id = str(uuid4())
        self._busy = False
        self._prepared = None
        self._used = False
        self._history = None
        self._uncertain = False
        owners = transaction.hass.data.setdefault(
            "native_gateway_audit_serialization", {}
        )
        resource = canonical(integration.binding.gateway.binding)
        self._serialization = owners.setdefault(resource, {"busy": False})
        self._serialization["coordinator"] = transaction

    async def async_initialize(self):
        if self._history is not None or self._busy or self._serialization["busy"]:
            raise ValueError("audit_unavailable")
        self._busy = self._serialization["busy"] = True
        try:
            data = await self.store.async_load()
            if data != self.witness.expected:
                raise ValueError("audit_witness_conflict")
            inspect_audit_history(data)
            self._history = [] if data is None else deepcopy(data["receipts"])
        except BaseException:
            self._uncertain = True
            raise
        finally:
            self._busy = self._serialization["busy"] = False

    def restart(self):
        """Retire this adapter even if a collector/storage callback is pending.

        A replacement must initialize from storage; this object cannot regain
        authority. Do not clear shared busy state while an old write is pending.
        """
        self.instance_id = str(uuid4())
        self._prepared = None
        self._used = True
        self._uncertain = True

    async def async_recover_history(self, *, current_context=None):
        """Read-only, separately witness-checked historical diagnosis."""
        data = await self.store.async_load()
        report = inspect_audit_history(data, current_context=current_context)
        if data != self.witness.expected:
            raise ValueError("audit_witness_conflict")
        return report

    def _enter(self):
        if (
            self._busy
            or self._serialization["busy"]
            or self._uncertain
            or self._history is None
            or self._used
        ):
            raise ValueError("diagnostic_unavailable")
        self._busy = self._serialization["busy"] = True

    def _protocol(self):
        return canonical(
            {
                "protocol": PROTOCOL,
                "observation": self.transaction.inspect(),
                "boot": self.transaction._boot_id,
                "generation": self.transaction._generation,
                "initialized": self.transaction._initialized,
                "busy": self.transaction._busy,
            }
        )

    async def _plan_current(self, plan):
        tx = self.transaction
        if self._serialization["coordinator"] is not tx:
            return False
        ledger = tx.ledger
        ledger_saved = await ledger.store.async_load()
        ledger_expected = {
            "schema_version": SCHEMA_VERSION,
            "binding": ledger.binding,
            "revision": ledger.revision,
            "events": ledger.inspect()["events"],
        }
        if (
            not ledger._initialized
            or ledger._blocked is not None
            or (ledger._write is not None and not ledger._write.done())
            or (ledger_saved is None and ledger.revision != 0)
            or (
                ledger_saved is not None
                and canonical(ledger_saved) != canonical(ledger_expected)
            )
        ):
            return False
        saved = await tx.store.async_load()
        observed = tx.inspect()
        if (
            not tx._initialized
            or tx._busy
            or observed["blocked_reason"] is not None
            or (saved is None and observed["events"])
            or (
                saved is not None
                and canonical(saved) != canonical(tx._data(tx._events))
            )
        ):
            return False
        fresh = await async_create_reconciliation_plan(self.transaction.ledger)
        return (
            bool(plan.plan_id)
            and _observations(plan) == _observations(fresh)
            and bool(plan.evidence)
            and plan.source_revisions.get("ledger") == self.transaction.ledger.revision
        )

    async def async_prepare(
        self,
        *,
        plan,
        request_id,
        expected_plan_id,
        expected_plan_digest,
        expected_ledger_revision,
        boundary=None,
    ):
        self._enter()
        self._prepared = None
        epoch = self.instance_id
        try:
            if not isinstance(request_id, str) or not request_id.strip():
                raise ValueError("request_identity_missing")
            protocol = self._protocol()
            diagnostic = await self.integration.async_check()
            current = (
                expected_plan_id == plan.plan_id
                and expected_plan_digest == plan_digest(plan)
                and type(expected_ledger_revision) is int
                and expected_ledger_revision == self.transaction.ledger.revision
                and await self._plan_current(plan)
            )
            if boundary:
                await boundary("prepare_captured")
            recovery, gateway = diagnostic.recovery, diagnostic.gateway
            if recovery is None or recovery.request is None or gateway is None:
                raise ValueError("evidence_incomplete")
            request = recovery.request
            context = TransactionContext(
                str(uuid4()),
                request_id,
                self.instance_id,
                request.binding,
                gateway.gateway_boot,
                gateway.runtime,
                gateway.ownership_generation,
                gateway.stop_serial,
                gateway.journal_revision,
                (
                    gateway.witness_present,
                    gateway.witness_blocked,
                    gateway.witness_matches,
                ),
                plan.source_revisions.get("ledger"),
                plan.plan_id,
                plan_digest(plan),
                canonical(request.revisions),
                request.cycle_id,
                canonical(request.operations),
                canonical(gateway.state_key()),
                canonical(recovery_scope(recovery)),
                canonical(request),
                protocol,
            )
            # Revalidate after every awaited collector and deterministic boundary.
            current = current and await self._plan_current(plan)
            consistent = (
                current
                and epoch == self.instance_id
                and not self._used
                and self.integration.diagnostic_current(diagnostic)
                and protocol == self._protocol()
                and bool(request.cycle_id)
                and bool(request.revisions)
                and all(r is not None for r in request.revisions)
                and canonical(plan.binding) == canonical(request.binding)
            )
            reasons = (
                *diagnostic.blockers,
                *(f.code for f in plan.findings),
                *plan.open_checks,
                "recovery_prepare_blocked",
            )
            if not consistent:
                reasons += ("transaction_evidence_conflict",)
            result = TransactionDiagnostic(
                context, bool(consistent), False, tuple(dict.fromkeys(reasons))
            )
            if consistent:
                self._prepared = (result, diagnostic, plan)
            return result
        except BaseException:
            self._used = True
            self._prepared = None
            raise
        finally:
            self._busy = self._serialization["busy"] = False

    async def async_commit(self, prepared, *, boundary=None):
        self._enter()
        self._used = True  # cancellation/failure/late ack can never retry this attempt
        try:
            if self._prepared is None or prepared is not self._prepared[0]:
                raise ValueError("prepare_identity_conflict")
            _, diagnostic, plan = self._prepared
            context = prepared.context

            async def current():
                valid = await self._plan_current(plan)
                return (
                    valid
                    and context.instance_id == self.instance_id
                    and context.plan_id == plan.plan_id
                    and context.plan_digest == plan_digest(plan)
                    and context.transaction_protocol == self._protocol()
                    and self.integration.diagnostic_current(diagnostic)
                )

            valid = await current()
            if boundary:
                await boundary("commit_checked")
            valid = valid and await current()
            if not valid:
                return TransactionDiagnostic(
                    context,
                    False,
                    False,
                    (*prepared.blockers, "commit_evidence_changed"),
                )
            data = await self.store.async_load()
            if (
                data != self.witness.expected
                or (data or {}).get("receipts", []) != self._history
            ):
                raise ValueError("audit_rollback")
            if not await current():
                return TransactionDiagnostic(
                    context,
                    False,
                    False,
                    (*prepared.blockers, "commit_evidence_changed"),
                )
            receipt = {
                "context": _json(context),
                "previous": self._history[-1]["hash"] if self._history else None,
                "evidence_consistent_at_check": True,
                "historical_only": True,
                "recovery_release_allowed": False,
                "blockers": list(prepared.blockers),
            }
            receipt["hash"] = _digest(receipt)
            candidate = {"schema": 1, "receipts": [*self._history, receipt]}
            # Any failure/cancellation after submission is uncertain, including a
            # write that persisted before raising. No automatic retry or recovery.
            self._uncertain = True
            await self.store.async_save(candidate)
            if boundary:
                await boundary("commit_saved")
            saved = await self.store.async_load()
            persisted = saved == candidate
            if persisted and context.instance_id == self.instance_id:
                self.witness.expected = deepcopy(candidate)
                self._history = candidate["receipts"]
            valid = persisted and await current()
            reasons = (
                prepared.blockers
                if valid
                else (*prepared.blockers, "commit_confirmation_obsolete")
            )
            return TransactionDiagnostic(context, bool(valid), persisted, reasons)
        finally:
            self._prepared = None
            self._busy = self._serialization["busy"] = False
