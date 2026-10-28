"""Internal diagnostic preparation protocol. Never a release or participant writer.

Lock contract: recovery serialization -> cycle -> accounting; pump stop separate.
This implementation takes ONLY recovery serialization, never participant locks.
"""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from dataclasses import fields
from uuid import UUID, uuid4

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery_ledger import (
    LedgerError,
    _digest,
    _json,
    _LedgerStore,
    _require,
    _text,
    _time,
)
from .recovery_plan import (
    PLAN_SCHEMA,
    POLICY_VERSION,
    ReconciliationPlan,
    _runtime,
    async_create_reconciliation_plan,
)
from .recovery_snapshot import freeze

TRANSACTION_SCHEMA = 1
PROTOCOL = "diagnostic_preparation_v1"
PARTICIPANTS = ("pump", "shots", "cycle", "ledger", "runtime")


def plan_digest(plan):
    """Bind every field, including plan identity, findings and unresolved checks."""
    _require(type(plan) is ReconciliationPlan, "plan_invalid")
    return _digest(_json(plan))


def _observations(plan):
    data = _json(plan)
    del data["plan_id"], data["created_at"]
    return data


def _uuid(value):
    _require(
        isinstance(value, str) and str(UUID(value)) == value, "transaction_corrupt"
    )


class _RecoveryTransactionCoordinator:
    """One lifetime owner per pump; no production construction or public service.

    Receipts attest read-only observations in this Store, NOT participant commits.
    Unknown physical ownership/revisions remain explicit in the original plan.
    """

    def __init__(self, ledger):
        self.ledger, self.source = ledger, ledger.source
        self.hass = ledger.hass
        self.binding = _json(self.source.binding)
        owners = self.hass.data.setdefault(f"{DOMAIN}.recovery_transactions", {})
        resource = self.binding["entity_id"]
        _require(resource not in owners, "writer_exists")
        owners[resource] = self
        self.store = _LedgerStore(
            self.hass,
            1,
            f"{DOMAIN}.recovery_transactions.{self.binding['entry_id']}",
            atomic_writes=True,
            private=True,
        )
        self._events = []
        self._initialized = False
        self._blocked = None
        self._busy = False
        self._generation = 0
        self._boot_id = str(uuid4())
        self._write = None
        self._session = None
        # Internal-only observer. Global state changes conservatively invalidate
        # even transient changes/ABA, including external automation state changes.
        self._unsub = [
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed),
            self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._changed),
        ]

    @callback
    def _changed(self, event):
        if self._busy or self._session is not None:
            self.revoke("runtime_event")

    @callback
    def revoke(self, reason="stop_requested"):
        """Synchronous invalidation has priority over every awaited write.

        No pump stop is issued here; the existing independent stop path retains
        responsibility. Explicit async_record_revocation may later audit this.
        """
        _require(_text(reason), "reason_invalid")
        self._generation += 1
        if self._blocked != "storage_uncertain":
            self._blocked = reason

    @property
    def native_activation_allowed(self):
        return False

    def inspect(self):
        return freeze(
            {
                "schema_version": TRANSACTION_SCHEMA,
                "protocol": PROTOCOL,
                "revision": len(self._events),
                "events": self._events,
                "state": self._events[-1]["kind"] if self._events else None,
                "effective_state": "locked",
                "blocked_reason": self._blocked,
                "native_activation_allowed": False,
            }
        )

    def _validate(self, data):
        try:
            return self._validate_history(data)
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            raise LedgerError("transaction_corrupt") from err

    def _validate_history(self, data):
        _require(
            isinstance(data, dict)
            and set(data) == {"schema_version", "protocol", "binding", "events"},
            "transaction_corrupt",
        )
        _require(
            type(data["schema_version"]) is int
            and data["schema_version"] == TRANSACTION_SCHEMA
            and data["protocol"] == PROTOCOL,
            "transaction_schema_unknown",
        )
        _require(
            data["binding"] == self.binding and isinstance(data["events"], list),
            "transaction_corrupt",
        )
        previous, state, intent = None, None, None
        seen, requests = set(), set()
        for index, event in enumerate(data["events"], 1):
            _require(
                isinstance(event, dict)
                and set(event)
                == {
                    "event_id",
                    "sequence",
                    "previous_hash",
                    "at",
                    "kind",
                    "transaction_id",
                    "request_id",
                    "request",
                    "payload",
                    "hash",
                },
                "transaction_corrupt",
            )
            _uuid(event["event_id"])
            _uuid(event["transaction_id"])
            _time(event["at"])
            _require(
                event["event_id"] not in seen
                and type(event["sequence"]) is int
                and event["sequence"] == index
                and event["previous_hash"] == previous
                and event["hash"]
                == _digest({k: v for k, v in event.items() if k != "hash"}),
                "transaction_corrupt",
            )
            _require(
                _text(event["request_id"])
                and isinstance(event["request"], dict)
                and event["request_id"] not in requests,
                "transaction_corrupt",
            )
            seen.add(event["event_id"])
            requests.add(event["request_id"])
            kind, payload = event["kind"], event["payload"]
            if state is None:
                _require(
                    kind == "prepared"
                    and isinstance(payload, dict)
                    and set(payload) == {"plan", "plan_digest", "guard", "cycle_id"},
                    "transaction_corrupt",
                )
                intent = event
                plan = payload["plan"]
                _require(
                    isinstance(plan, dict)
                    and set(plan) == {f.name for f in fields(ReconciliationPlan)}
                    and type(plan["schema_version"]) is int
                    and plan["schema_version"] == PLAN_SCHEMA
                    and plan["policy_version"] == POLICY_VERSION
                    and plan["status"] == "preparation_eligible"
                    and plan["findings"] == []
                    and plan["open_checks"] == []
                    and isinstance(plan["source_revisions"], dict)
                    and set(plan["source_revisions"])
                    == {"pump", "shots", "cycle", "ledger"}
                    and type(plan["source_revisions"]["ledger"]) is int
                    and isinstance(plan["evidence_digests"], dict)
                    and bool(plan["evidence"])
                    and plan.get("binding") == self.binding
                    and plan.get("native_activation_allowed") is False
                    and payload["plan_digest"] == _digest(plan)
                    and isinstance(payload["guard"], dict),
                    "transaction_corrupt",
                )
                _uuid(plan["plan_id"])
                _time(plan["created_at"])
                _require(
                    event["request"]
                    == {
                        "method": "prepare",
                        "plan": plan,
                        "expected_plan_digest": payload["plan_digest"],
                        "expected_revision": plan["source_revisions"]["ledger"],
                    },
                    "transaction_corrupt",
                )
                _require(
                    set(payload["guard"])
                    == {
                        "participants",
                        "runtime",
                        "ledger",
                        "sensor_policy",
                        "states",
                        "cycle_decision",
                        "generation",
                        "boot_id",
                    },
                    "transaction_corrupt",
                )
                _uuid(payload["guard"]["boot_id"])
                _require(
                    _text(payload["cycle_id"])
                    and payload["cycle_id"]
                    == payload["guard"]["participants"][2]["record"]["cycle_id"]
                    and payload["cycle_id"]
                    == payload["guard"]["cycle_decision"]["cycle_id"]
                    and type(event["request"]["expected_revision"]) is int,
                    "transaction_corrupt",
                )
            else:
                _require(
                    event["transaction_id"] == intent["transaction_id"],
                    "transaction_corrupt",
                )
                if kind == "participants_checked":
                    _require(
                        state == "prepared"
                        and _digest(payload) == _digest(self._receipts(intent))
                        and event["request"].get("method") == "participants"
                        and event["request"].get("commit_request")
                        == {
                            "method": "commit",
                            "transaction_id": intent["transaction_id"],
                            "expected_plan_digest": intent["payload"]["plan_digest"],
                        },
                        "participant_proof_missing",
                    )
                elif kind == "committed":
                    _require(
                        state == "participants_checked"
                        and event["request"]
                        == data["events"][index - 2]["request"]["commit_request"]
                        and event["request_id"] + ":participants"
                        == data["events"][index - 2]["request_id"]
                        and payload.get("technical_only") is True
                        and payload.get("native_activation_allowed") is False
                        and payload
                        == {
                            "technical_only": True,
                            "native_activation_allowed": False,
                            "plan_digest": intent["payload"]["plan_digest"],
                        },
                        "transaction_corrupt",
                    )
                else:
                    _require(
                        kind in {"aborted", "revoked"}
                        and state not in {"aborted", "revoked"}
                        and kind == ("revoked" if state == "committed" else "aborted")
                        and isinstance(payload, dict)
                        and set(payload) == {"reason"}
                        and _text(payload["reason"])
                        and event["request"]
                        == {"method": "revoke", "reason": payload["reason"]},
                        "transaction_corrupt",
                    )
            state, previous = kind, event["hash"]
        return deepcopy(data["events"])

    async def async_initialize(self):
        _require(
            not self._initialized and not self._busy and self._blocked is None,
            "transaction_unavailable",
        )
        self._busy = True
        try:
            data = await self.store.async_load()
            if data is not None:
                self._events = self._validate(data)
            self._initialized = True
            if self._events:
                self._blocked = "restart_requires_new_review"
        except BaseException:
            self._events = []
            self._blocked = "transaction_load_failed"
            raise
        finally:
            self._busy = False

    def _data(self, events):
        return {
            "schema_version": TRANSACTION_SCHEMA,
            "protocol": PROTOCOL,
            "binding": self.binding,
            "events": events,
        }

    def _retry(self, request_id, request):
        _require(self._initialized and _text(request_id), "request_invalid")
        for event in self._events:
            if event["request_id"] == request_id:
                _require(
                    _digest(event["request"]) == _digest(request), "request_conflict"
                )
                return self._receipt(event)
        return None

    @staticmethod
    def _receipt(event):
        return freeze(
            {
                "transaction_id": event["transaction_id"],
                "state": event["kind"],
                "revision": event["sequence"],
                "historical_only": True,
                "native_activation_allowed": False,
            }
        )

    def _enter(self):
        _require(self._initialized and self._blocked is None, "transaction_unavailable")
        _require(not self._busy, "transaction_busy")
        self._busy = True  # no await: reject competitors, never queue them

    def inspect_drain(self):
        """Non-mutating development contract, never a quiesce or exclusivity proof."""
        try:
            guard = self._guard()
        except LedgerError as err:
            return freeze(
                {
                    "observed_idle": False,
                    "reason": err.code,
                    "quiesce_guaranteed": False,
                    "native_activation_allowed": False,
                }
            )
        return freeze(
            {
                "observed_idle": True,
                "guard_digest": _digest(guard),
                "quiesce_guaranteed": False,
                "native_activation_allowed": False,
            }
        )

    def _guard(self):
        source = self.source
        snapshots = source._snapshots()
        pump, shots, cycle = snapshots
        _require(
            not source.pump._pending
            and not source.pump._timed_out
            and not pump.work
            and not pump.lock_held
            and not pump.deadline_pending
            and not pump.stop_requested
            and not shots.accounting_locked
            and not cycle.lock_held
            and not source.shots.execution_active
            and all(
                w.state in {"absent", "completed"} and not w.timed_out
                for s in (shots, cycle)
                for w in s.work
            ),
            "drain_unresolved",
        )
        runtime = _runtime(self.ledger)
        scheduler = runtime["scheduler"]
        _require(
            scheduler is not None
            and not scheduler["running"]
            and not scheduler["timer_pending"]
            and not scheduler["evaluation_pending"]
            and scheduler["task"] in {"absent", "completed"},
            "drain_unresolved",
        )
        assessment = source.shots.sensor_assessment()
        _require(not assessment.reasons and not assessment.rejected, "sensor_unsafe")
        raw = {**source.shots.entry.data, **source.shots.entry.options}
        for key in ("drain_sensor", "drain_tray_sensor"):
            state = self.hass.states.get(raw.get(key, ""))
            _require(
                state is not None
                and state.state == "off"
                and not state.attributes.get("restored")
                and 0 <= (dt_util.utcnow() - state.last_updated).total_seconds() <= 30,
                "drain_unsafe",
            )
        return _json(
            {
                "boot_id": self._boot_id,
                "generation": self._generation,
                "participants": source._content(snapshots),
                "runtime": runtime,
                "ledger": self.ledger.inspect(),
                "sensor_policy": source.shots.sensor_policy,
                "states": _digest(
                    _json(
                        sorted(
                            (s.as_dict() for s in self.hass.states.async_all()),
                            key=lambda s: s["entity_id"],
                        )
                    )
                ),
                "cycle_decision": source.cycle._decision(dt_util.now()),
            }
        )

    def _check(self, generation, guard):
        _require(
            self._generation == generation and self._blocked is None,
            "transaction_revoked",
        )
        _require(self._guard() == guard, "source_changed")

    async def _revalidate(self, plan, generation, guard):
        self._check(generation, guard)
        captured = await self.source.capture()
        self._check(generation, guard)
        _require(captured == _json(plan.evidence["participants"]), "source_changed")
        saved = await self.source._read(self.ledger.store, "recovery_ledger")
        self._check(generation, guard)
        _require(saved == _json(plan.evidence["ledger"]), "source_changed")
        current = await async_create_reconciliation_plan(self.ledger)
        self._check(generation, guard)
        _require(_observations(current) == _observations(plan), "plan_stale")

    async def _append(self, kind, tid, request_id, request, payload, timeout):
        event = {
            "event_id": str(uuid4()),
            "sequence": len(self._events) + 1,
            "previous_hash": self._events[-1]["hash"] if self._events else None,
            "at": dt_util.utcnow().isoformat(),
            "kind": kind,
            "transaction_id": tid,
            "request_id": request_id,
            "request": request,
            "payload": payload,
        }
        event["hash"] = _digest(event)
        data = self._data([*self._events, event])
        candidate = self._validate(data)
        self._write = self.hass.async_create_background_task(
            self.store.async_save(deepcopy(data)), "recovery transaction persistence"
        )
        self._write.add_done_callback(
            lambda t: None if t.cancelled() else t.exception()
        )
        try:
            done, _ = await asyncio.wait({self._write}, timeout=timeout)
            _require(bool(done), "storage_timeout")
            self._write.result()
        except BaseException as err:
            self._blocked = "storage_uncertain"
            if isinstance(err, Exception) and not isinstance(err, LedgerError):
                raise LedgerError("storage_failed") from err
            raise
        self._events = candidate  # historical only, including a concurrent revocation
        return self._receipt(event)

    @staticmethod
    def _timeout(timeout):
        _require(
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 0,
            "timeout_invalid",
        )

    @staticmethod
    def _receipts(intent):
        plan = intent["payload"]["plan"]
        return {
            name: {
                "kind": "read_only_observation",
                "cycle_id": intent["payload"]["cycle_id"],
                "plan_digest": intent["payload"]["plan_digest"],
                "source_revisions": plan["source_revisions"],
                "evidence_digests": plan["evidence_digests"],
            }
            for name in PARTICIPANTS
        }

    async def async_prepare(
        self, *, plan, expected_plan_digest, expected_revision, request_id, timeout=5
    ):
        """Persist a revalidated diagnostic intent, never execution authority."""
        self._timeout(timeout)
        digest = plan_digest(plan)
        request = {
            "method": "prepare",
            "plan": _json(plan),
            "expected_plan_digest": expected_plan_digest,
            "expected_revision": expected_revision,
        }
        retry = self._retry(request_id, request)
        if retry is not None:
            return retry
        self._enter()
        try:
            _require(not self._events, "resource_reserved")
            _require(digest == expected_plan_digest, "plan_digest_conflict")
            _require(
                type(expected_revision) is int
                and expected_revision == self.ledger.revision
                and plan.source_revisions.get("ledger") == expected_revision,
                "revision_conflict",
            )
            _require(
                _json(plan.binding) == self.binding
                and bool(plan.evidence)
                and plan.status == "preparation_eligible"
                and not plan.findings
                and not plan.open_checks,
                "plan_blocked",
            )
            generation, guard = self._generation, self._guard()
            await self._revalidate(plan, generation, guard)
            tid = str(uuid4())
            result = await self._append(
                "prepared",
                tid,
                request_id,
                request,
                {
                    "plan": _json(plan),
                    "plan_digest": digest,
                    "guard": guard,
                    "cycle_id": self.source.cycle.cycle_id,
                },
                timeout,
            )
            self._check(generation, guard)
            await self._revalidate(plan, generation, guard)
            self._session = (plan, generation, guard)
            return result
        except BaseException:
            self._blocked = self._blocked or "preparation_failed"
            raise
        finally:
            self._busy = False

    async def async_commit(
        self, *, transaction_id, request_id, expected_plan_digest, timeout=5
    ):
        """Technical commit in this Store only; no execution capability exists."""
        self._timeout(timeout)
        request = {
            "method": "commit",
            "transaction_id": transaction_id,
            "expected_plan_digest": expected_plan_digest,
        }
        retry = self._retry(request_id, request)
        if retry is not None:
            return retry
        self._enter()
        try:
            _require(
                self._session is not None and self._events[-1]["kind"] == "prepared",
                "transaction_incomplete",
            )
            intent = self._events[0]
            _require(
                transaction_id == intent["transaction_id"]
                and expected_plan_digest == intent["payload"]["plan_digest"],
                "transaction_conflict",
            )
            plan, generation, guard = self._session
            await self._revalidate(plan, generation, guard)
            # Internal IDs cannot collide with caller IDs: reject, never overwrite.
            proof_id = f"{request_id}:participants"
            _require(
                not any(
                    e["request_id"] in {proof_id, request_id} for e in self._events
                ),
                "request_conflict",
            )
            await self._append(
                "participants_checked",
                transaction_id,
                proof_id,
                {"method": "participants", "commit_request": request},
                self._receipts(intent),
                timeout,
            )
            self._check(generation, guard)
            await self._revalidate(plan, generation, guard)
            result = await self._append(
                "committed",
                transaction_id,
                request_id,
                request,
                {
                    "technical_only": True,
                    "native_activation_allowed": False,
                    "plan_digest": expected_plan_digest,
                },
                timeout,
            )
            self._check(generation, guard)
            await self._revalidate(plan, generation, guard)
            return result
        except BaseException:
            self._blocked = self._blocked or "commit_failed"
            raise
        finally:
            self._busy = False

    async def async_record_revocation(self, *, request_id, timeout=5):
        """Audit a synchronous block; never overtake an uncertain write."""
        self._timeout(timeout)
        _require(
            self._initialized
            and self._blocked is not None
            and not self._busy
            and (self._write is None or self._write.done())
            and self._blocked != "storage_uncertain",
            "transaction_unavailable",
        )
        request = {"method": "revoke", "reason": self._blocked}
        retry = self._retry(request_id, request)
        if retry is not None:
            return retry
        _require(bool(self._events), "transaction_incomplete")
        self._busy = True
        try:
            kind = "revoked" if self._events[-1]["kind"] == "committed" else "aborted"
            return await self._append(
                kind,
                self._events[0]["transaction_id"],
                request_id,
                request,
                {"reason": self._blocked},
                timeout,
            )
        finally:
            self._busy = False
