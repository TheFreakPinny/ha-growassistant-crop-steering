"""Internal phase-five refusal contract; no gate resolver or permit issuer exists.

Only a future participant-owned, isolated FakePump gate protocol can make a
release possible. Diagnostic transaction receipts never satisfy that protocol.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from .const import DOMAIN
from .recovery_ledger import LedgerError, _digest, _json, _LedgerStore, _require, _text
from .recovery_plan import async_create_reconciliation_plan
from .recovery_snapshot import freeze
from .recovery_transaction import _observations, plan_digest


@dataclass(frozen=True)
class _ReleaseActor:
    actor_id: str
    context_id: str
    boot_id: str


class _HarnessReleaseAuthority:
    """In-process identity capability, NOT HA authentication.

    Construction belongs exclusively to trusted isolated harness setup. Possession
    of an equal dataclass, a username or TrustedReviewContext confers no authority.
    No public service may accept or construct this authority.
    """

    def __init__(self):
        self.boot_id = str(uuid4())
        self._actors = []

    def issue(self, actor_id, context_id):
        _require(_text(actor_id) and _text(context_id), "actor_invalid")
        actor = _ReleaseActor(actor_id, context_id, self.boot_id)
        self._actors.append(actor)
        return actor

    def authenticate(self, actor):
        _require(
            type(actor) is _ReleaseActor
            and actor.boot_id == self.boot_id
            and any(actor is issued for issued in self._actors),
            "actor_unauthenticated",
        )


@dataclass(frozen=True)
class _CycleReleasePermit:
    """Future volatile value contract; deliberately no mint/load/use API.

    None of these fields is a scheduler or pump capability. A future issuer must
    verify all participant-owned receipts and revoke synchronously on change.
    This stage never constructs a permit, even for a diagnostic commit.
    """

    boot_id: str
    ownership_epoch: str
    entry_id: str
    pump_target: str
    cycle_id: str
    plan_id: str
    commit_id: str
    ledger_revision: int
    transaction_revision: int
    journal_revisions: object
    safety_generation: int


class _CycleReleaseCoordinator:
    """Serialized explicit decisions with durable negative receipts only.

    Shares the transaction reservation; never takes Cycle/Accounting locks or
    modifies participant journals. No production construction or service exists.
    """

    def __init__(self, transaction, authority):
        _require(type(authority) is _HarnessReleaseAuthority, "authority_invalid")
        owners = transaction.hass.data.setdefault(f"{DOMAIN}.cycle_release", {})
        resource = transaction.binding["entity_id"]
        _require(resource not in owners, "writer_exists")
        owners[resource] = self
        self.transaction, self.authority = transaction, authority
        self.store = _LedgerStore(
            transaction.hass,
            1,
            f"{DOMAIN}.cycle_release.{transaction.binding['entry_id']}",
            atomic_writes=True,
            private=True,
        )
        self._events = []
        self._initialized = False
        self._blocked = False
        self._write = None

    @property
    def native_activation_allowed(self):
        return False

    @property
    def permit(self):
        # Intentionally cannot be installed, restored or derived from a receipt.
        return None

    def inspect(self):
        return freeze(
            {
                "effective_state": "locked",
                "permit": None,
                "revision": len(self._events),
                "events": self._events,
                "native_activation_allowed": False,
                "missing_contract": "participant_owned_gate_resolution_v1",
            }
        )

    def _validate(self, data):
        _require(
            isinstance(data, dict)
            and set(data) == {"schema_version", "binding", "events"}
            and type(data["schema_version"]) is int
            and data["schema_version"] == 1
            and data["binding"] == self.transaction.binding
            and isinstance(data["events"], list),
            "release_corrupt",
        )
        previous, seen = None, set()
        for index, event in enumerate(data["events"], 1):
            _require(
                isinstance(event, dict)
                and set(event)
                == {"sequence", "previous_hash", "request", "reason", "hash"}
                and type(event["sequence"]) is int
                and event["sequence"] == index
                and event["previous_hash"] == previous
                and event["hash"]
                == _digest({k: v for k, v in event.items() if k != "hash"})
                and event["reason"]
                in {
                    "operator_denied",
                    "plan_blocked",
                    "transaction_incomplete",
                    "participant_owned_gate_resolution_missing",
                },
                "release_corrupt",
            )
            request = event["request"]
            _require(isinstance(request, dict), "release_corrupt")
            self._validate_request(request)
            _require(request["request_id"] not in seen, "release_corrupt")
            seen.add(request["request_id"])
            previous = event["hash"]
        return deepcopy(data["events"])

    async def async_initialize(self):
        _require(not self._initialized and not self._blocked, "release_unavailable")
        tx = self.transaction
        tx._enter()
        try:
            data = await self.store.async_load()
            if data is not None:
                self._events = self._validate(data)
            self._initialized = True
        except BaseException:
            self._blocked = True
            raise
        finally:
            tx._busy = False

    def _validate_request(self, request):
        _require(
            set(request)
            == {
                "request_id",
                "transaction_id",
                "plan_id",
                "plan_digest",
                "binding",
                "cycle_id",
                "actor",
                "expected_ledger_revision",
                "expected_transaction_revision",
                "boot_id",
                "ownership_epoch",
                "decision",
                "rationale",
            },
            "request_invalid",
        )
        _require(
            all(
                _text(request[k])
                for k in (
                    "request_id",
                    "transaction_id",
                    "plan_id",
                    "plan_digest",
                    "cycle_id",
                    "boot_id",
                    "ownership_epoch",
                    "rationale",
                )
            )
            and request["decision"] in {"release", "deny"}
            and request["binding"] == self.transaction.binding
            and all(
                type(request[k]) is int and request[k] >= 0
                for k in (
                    "expected_ledger_revision",
                    "expected_transaction_revision",
                )
            )
            and isinstance(request["actor"], dict)
            and set(request["actor"]) == {"actor_id", "context_id", "boot_id"}
            and all(_text(v) for v in request["actor"].values())
            and request["actor"]["boot_id"] == request["boot_id"],
            "request_invalid",
        )

    @staticmethod
    def _receipt(event):
        return freeze(
            {
                "request_id": event["request"]["request_id"],
                "revision": event["sequence"],
                "reason": event["reason"],
                "released": False,
                "historical_only": True,
                "native_activation_allowed": False,
            }
        )

    async def async_request(self, *, actor, plan, timeout=5, **values):
        """Record an explicit refusal, never grant authority from supplied claims.

        ownership_epoch is a requested binding only: today's participants cannot
        authenticate it. Consequently even an otherwise matching commit is denied.
        """
        tx = self.transaction
        tx._timeout(timeout)
        self.authority.authenticate(actor)
        _require(self._initialized and not self._blocked, "release_unavailable")
        request = _json(
            {
                **values,
                "actor": actor,
                "plan_id": plan.plan_id,
                "plan_digest": plan_digest(plan),
            }
        )
        self._validate_request(request)
        for event in self._events:
            if event["request"]["request_id"] == request["request_id"]:
                _require(event["request"] == request, "request_conflict")
                return self._receipt(event)
        tx._enter()
        try:
            _require(request["boot_id"] == self.authority.boot_id, "boot_conflict")
            _require(
                request["expected_ledger_revision"] == tx.ledger.revision
                and request["expected_transaction_revision"]
                == tx.inspect()["revision"],
                "revision_conflict",
            )
            _require(
                request["cycle_id"] == tx.source.cycle.cycle_id
                and _json(plan.binding) == tx.binding,
                "binding_conflict",
            )
            generation = tx._generation
            current = await async_create_reconciliation_plan(tx.ledger)
            _require(
                tx._generation == generation and tx._blocked is None, "release_revoked"
            )
            # Reject caller edits and stale plans using the actual planner.
            _require(_observations(current) == _observations(plan), "plan_stale")
            history = tx.inspect()["events"]
            if request["decision"] == "deny":
                reason = "operator_denied"
            elif (
                plan.findings
                or plan.open_checks
                or plan.status != "preparation_eligible"
            ):
                reason = "plan_blocked"
            elif not history or history[-1]["kind"] != "committed":
                reason = "transaction_incomplete"
            else:
                _require(
                    history[-1]["transaction_id"] == request["transaction_id"]
                    and history[0]["payload"]["plan"]["plan_id"] == plan.plan_id,
                    "transaction_conflict",
                )
                # Diagnostic observations are not participant gate acknowledgments.
                reason = "participant_owned_gate_resolution_missing"
            event = {
                "sequence": len(self._events) + 1,
                "previous_hash": self._events[-1]["hash"] if self._events else None,
                "request": request,
                "reason": reason,
            }
            event["hash"] = _digest(event)
            data = {
                "schema_version": 1,
                "binding": tx.binding,
                "events": [*self._events, event],
            }
            candidate = self._validate(data)
            self._write = tx.hass.async_create_background_task(
                self.store.async_save(deepcopy(data)), "cycle release refusal audit"
            )
            self._write.add_done_callback(
                lambda t: None if t.cancelled() else t.exception()
            )
            try:
                done, _ = await asyncio.wait({self._write}, timeout=timeout)
                _require(bool(done), "storage_timeout")
                self._write.result()
            except BaseException as err:
                self._blocked = True
                tx.revoke("release_storage_uncertain")
                if isinstance(err, Exception) and not isinstance(err, LedgerError):
                    raise LedgerError("storage_failed") from err
                raise
            self._events = candidate
            # Even a changed runtime during the audit can only receive a refusal.
            return self._receipt(event)
        finally:
            tx._busy = False
