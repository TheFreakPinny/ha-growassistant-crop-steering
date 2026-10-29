"""Internal development-only recovery audit. Never grants execution authority.

No integration setup imports this module. TrustedReviewContext is a harness
contract, NOT authentication suitable for a service or other public boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery_revision import LEGACY_METADATA, decode_receipt
from .recovery_snapshot import Finding, ReasonCode, freeze, validate_safety

SCHEMA_VERSION = 1
_KEY = f"{DOMAIN}.development_recovery_ledgers"


class LedgerError(Exception):
    """Stable machine-readable rejection; no successful acknowledgment implied."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise LedgerError(code)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        _require(
            parsed.tzinfo is not None and parsed.utcoffset() is not None, "time_invalid"
        )
        return parsed
    except (ValueError, TypeError):
        raise LedgerError("time_invalid") from None


def _json(value):
    """Detach only known JSON evidence, preserving unknowns as a rejection."""
    if is_dataclass(value):
        return {f.name: _json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        _require(all(isinstance(k, str) for k in value), "evidence_invalid")
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise LedgerError("evidence_invalid")


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class TrustedReviewContext:
    """TEST/DEVELOPMENT ONLY: caller has verified identity out of band.

    Possession/creation of this dataclass is not real authentication. A future
    HA adapter must resolve HA's authenticated user and authorization itself;
    accepting user-supplied fields of this type at a public boundary is forbidden.
    """

    actor_id: str
    context_id: str
    authority: str = "isolated_harness"

    def validate(self) -> None:
        _require(
            _text(self.actor_id)
            and _text(self.context_id)
            and self.authority == "isolated_harness",
            "actor_invalid",
        )


class _LedgerStore(Store):
    """Preflight prevents HA's corrupt-file fallback becoming an empty ledger."""

    async def async_save(self, data):
        # HA normally defers writes during shutdown and logs some errors instead
        # of raising them. Neither behavior is a durable audit acknowledgment.
        _require(
            self.hass.state is not CoreState.stopping and not self._read_only,
            "storage_unavailable",
        )
        await super().async_save(data)

    async def _async_write_data(self, data):
        try:
            await super()._async_write_data(data)
        except Exception as err:
            # Escape Store's SerializationError/WriteError log-and-return path.
            raise LedgerError("storage_failed") from err

    async def async_load(self):
        def preflight():
            path = Path(self.path)
            _require(
                not list(path.parent.glob(path.name + ".corrupt*")), "ledger_corrupt"
            )
            if not path.exists():
                return False
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                _require(
                    isinstance(envelope, dict)
                    and type(envelope.get("version")) is int
                    and envelope["version"] == 1
                    and type(envelope.get("minor_version", 1)) is int
                    and envelope.get("minor_version", 1) == 1
                    and isinstance(envelope.get("data"), dict),
                    "ledger_schema_unknown",
                )
            except (OSError, ValueError):
                raise LedgerError("ledger_corrupt") from None
            return True

        exists = await self.hass.async_add_executor_job(preflight)
        data = await super().async_load()
        _require(not exists or data is not None, "ledger_corrupt")
        return data


class _RecoverySource:
    """Bind snapshots to actual participant stores, never caller-written faults."""

    def __init__(self, shots, cycle):
        _require(cycle.coordinator is shots, "binding_mismatch")
        self.shots, self.cycle = shots, cycle
        self.pump = shots.controller
        self.hass = shots.hass
        self.binding = self.pump.safety_snapshot().binding
        _require(
            _text(self.binding.entry_id) and _text(self.binding.entity_id),
            "binding_unknown",
        )

    def _snapshots(self):
        return (
            self.pump.safety_snapshot(),
            self.shots.safety_snapshot(),
            self.cycle.safety_snapshot(),
        )

    async def _read(self, store, suffix):
        if not isinstance(store, Store):
            # Explicit test-only memory-store injection, as in the native harness.
            return await store.async_load()
        expected_key = f"{DOMAIN}.{suffix}.{self.binding.entry_id}"
        _require(store.key == expected_key, "binding_mismatch")

        def read_only():
            path = Path(store.path)
            _require(
                not list(path.parent.glob(path.name + ".corrupt*")), "evidence_invalid"
            )
            if not path.exists():
                return None
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                _require(
                    isinstance(envelope, dict)
                    and envelope.get("key") == expected_key
                    and type(envelope.get("version")) is int
                    and envelope["version"] == 1
                    and type(envelope.get("minor_version", 1)) is int
                    and envelope.get("minor_version", 1) == 1
                    and "data" in envelope,
                    "evidence_schema_unknown",
                )
                return envelope["data"]
            except (ValueError, OSError):
                raise LedgerError("evidence_invalid") from None

        # Store.async_load may rename corrupt participant journals. Inspect only
        # their on-disk envelope instead; never trigger migration or repair.
        return await self.hass.async_add_executor_job(read_only)

    async def capture(self):
        snapshots = self._snapshots()
        for snapshot in snapshots:
            _require(snapshot.binding == self.binding, "binding_mismatch")
            _require(
                snapshot.version.snapshot_schema in {1, 2}
                and snapshot.version.store_schema == 1,
                "evidence_schema_unknown",
            )
        pump, shots, cycle = snapshots
        _require(shots.initialized and cycle.initialized, "evidence_incomplete")
        _require(
            not pump.work
            and not pump.lock_held
            and not pump.deadline_pending
            and not pump.stop_requested
            and not shots.accounting_locked
            and not cycle.lock_held
            and all(
                w.state in {"absent", "completed"} and not w.timed_out
                for s in (shots, cycle)
                for w in s.work
            ),
            "evidence_busy",
        )
        saved = await asyncio.gather(
            self._read(self.pump.store, "pump_recovery"),
            self._read(self.shots.store, "shot_journal"),
            self._read(self.cycle.store, "native_cycle"),
        )
        _require(
            [_json(pump.record), _json(shots.journal), _json(cycle.record)]
            == _json(saved),
            "evidence_not_durable",
        )
        # Compare the revision in the very same journal envelope, independently
        # of its payload. A matching end value alone cannot hide journal ABA.
        revision_findings = []
        for store, snapshot, payload in zip(
            (self.pump.store, self.shots.store, self.cycle.store),
            snapshots,
            saved,
            strict=True,
        ):
            if hasattr(store, "async_read_versioned"):
                data, metadata = await store.async_read_versioned()
                _require(data == payload, "evidence_changed")
                if metadata is not None and metadata != LEGACY_METADATA:
                    try:
                        receipt = decode_receipt(
                            metadata,
                            data,
                            self.binding.entry_id,
                            self.binding.entity_id,
                            snapshot.version.receipt.identity.store_key
                            if snapshot.version.receipt is not None
                            else "unknown",
                        )
                    except (ValueError, RuntimeError):
                        receipt = None
                    if receipt is None or receipt != snapshot.version.receipt:
                        revision_findings.append(
                            Finding(
                                ReasonCode.REVISION_CONFLICT,
                                "participants",
                                "Stored revision metadata does not validate against live evidence",
                            )
                        )
                else:
                    if snapshot.version.receipt is not None:
                        revision_findings.append(
                            Finding(
                                ReasonCode.REVISION_CONFLICT,
                                "participants",
                                "Stored revision metadata is missing",
                            )
                        )
            elif snapshot.version.receipt is not None:
                revision_findings.append(
                    Finding(
                        ReasonCode.REVISION_CONFLICT,
                        "participants",
                        "Store adapter cannot provide revision metadata",
                    )
                )
        evidence = self._content(snapshots)
        _require(evidence == self._content(self._snapshots()), "evidence_changed")
        self._validate_content(evidence)
        result = validate_safety(
            *snapshots, self.binding, now=dt_util.utcnow(), max_age=30
        )
        return {
            "content": evidence,
            "digest": _digest(evidence),
            "findings": _json((*result.findings, *revision_findings)),
        }

    @staticmethod
    def _content(snapshots):
        result = [_json(s) for s in snapshots]
        for snapshot in result:
            snapshot.pop("captured_at")
        return result

    @staticmethod
    def _validate_content(content):
        pump, shots, cycle = content
        for participant in content:
            version = participant.get("version", {})
            _require(
                type(version.get("snapshot_schema")) is int
                and version["snapshot_schema"] in {1, 2}
                and type(version.get("store_schema")) is int
                and version["store_schema"] == 1,
                "evidence_schema_unknown",
            )
        binding = shots["binding"]
        _require(pump["binding"] == binding == cycle["binding"], "binding_mismatch")
        _require(pump["off"]["binding"] == binding, "binding_mismatch")
        if pump["record"] is not None:
            _require(
                isinstance(pump["record"], dict)
                and pump["record"].get("entity_id") == binding["entity_id"],
                "binding_mismatch",
            )
        journal = shots["journal"]
        _require(
            isinstance(journal, dict) and isinstance(journal.get("operations"), dict),
            "evidence_incomplete",
        )
        record = cycle["record"]
        if pump["record"] is not None:
            operation = pump["record"].get("operation_id")
            _require(
                _text(operation) and operation in journal["operations"],
                "binding_mismatch",
            )
        _require(
            isinstance(record, dict) and _text(record.get("cycle_id")),
            "evidence_incomplete",
        )
        _require(
            record["cycle_id"] == cycle["current_cycle_id"] == shots["cycle_id"],
            "cycle_mismatch",
        )
        _require(shots["options"] == cycle["options"], "evidence_changed")
        _time(record["cycle_id"])
        for operation, raw in journal["operations"].items():
            _require(_text(operation) and isinstance(raw, dict), "evidence_invalid")
            _require(
                raw.get("operation_id", operation) == operation
                and raw.get("entry_id", binding["entry_id"]) == binding["entry_id"]
                and raw.get("pump_entity") == binding["entity_id"],
                "binding_mismatch",
            )
            _require(raw.get("cycle_id") == record["cycle_id"], "cycle_mismatch")
            _time(raw.get("reserved_at"))
            _require(
                raw.get("state") in {"completed", "interrupted", "fault"},
                "evidence_incomplete",
            )
            _require(
                type(raw.get("accounted")) is bool
                and type(raw.get("delivery_uncertain")) is bool
                and type(raw.get("shutdown_confirmed")) is bool,
                "evidence_incomplete",
            )
            if raw["state"] in {"interrupted", "fault"}:
                _require(
                    _time(raw.get("finished_at")) >= _time(raw["reserved_at"]),
                    "time_invalid",
                )
                _require(
                    _text(raw.get("outcome"))
                    and raw["outcome"] != "completed"
                    and raw["accounted"] is False,
                    "evidence_invalid",
                )


def _failures(evidence):
    content = evidence["content"]
    return {
        op: raw
        for op, raw in content[1]["journal"]["operations"].items()
        if raw["state"] in {"interrupted", "fault"}
    }


def _failure_key(binding, operation, raw):
    return _digest([binding, operation, raw["cycle_id"], raw["reserved_at"]])


def _review_valid(review, raw, instant):
    _require(
        isinstance(review, dict)
        and set(review)
        == {
            "partial_delivery",
            "volume",
            "shutdown",
            "journal_review",
            "disposition",
            "rationale",
        },
        "review_incomplete",
    )
    _require(
        review["partial_delivery"]
        in ("possible", "confirmed", "unknown", "none_evidenced"),
        "review_incomplete",
    )
    _require(
        not raw["delivery_uncertain"] or review["partial_delivery"] != "none_evidenced",
        "review_contradiction",
    )
    if review["partial_delivery"] == "none_evidenced":
        _require(
            raw.get("delivery") == "not_requested"
            and not raw.get("activation_requested_at")
            and not raw.get("activation_confirmed_at"),
            "review_contradiction",
        )
    # No invented volume: current journal has no measured-volume evidence.
    _require(review["volume"] is None, "review_contradiction")
    _require(
        _text(review["journal_review"]) and _text(review["rationale"]),
        "review_incomplete",
    )
    _require(
        review["disposition"]
        in ("keep_locked", "abandon_cycle_requires_reconciliation"),
        "disposition_invalid",
    )
    off = review["shutdown"]
    _require(
        isinstance(off, dict)
        and set(off) == {"method", "source", "checked_at", "result", "limitations"},
        "review_incomplete",
    )
    _require(
        all(_text(off[k]) for k in ("method", "source", "limitations"))
        and off["result"] in ("confirmed_off", "unknown", "failed"),
        "review_incomplete",
    )
    checked = _time(off["checked_at"])
    _require(_time(raw["finished_at"]) <= checked <= _time(instant), "time_invalid")


class _RecoveryLedger:
    """One event-loop writer per entry; no release, repair or gate references."""

    def __init__(self, source: _RecoverySource):
        self.source = source
        self.hass = source.hass
        self.binding = _json(source.binding)
        owners = self.hass.data.setdefault(_KEY, {})
        _require(source.binding.entry_id not in owners, "writer_exists")
        owners[source.binding.entry_id] = self
        self.store = _LedgerStore(
            self.hass,
            1,
            f"{DOMAIN}.recovery_ledger.{source.binding.entry_id}",
            atomic_writes=True,
            private=True,
        )
        self._lock = asyncio.Lock()
        self._events = []
        self._cases = {}
        self._requests = {}
        self._initialized = False
        self._blocked = None
        self._write = None

    @property
    def revision(self):
        return len(self._events)

    @property
    def native_activation_allowed(self):
        return False

    def inspect(self):
        """Historical reviews only. Effective state is always locked, even empty."""
        return freeze(
            {
                "revision": self.revision,
                "effective_state": "locked",
                "blocked_reason": self._blocked,
                "cases": self._cases,
                "events": self._events,
                "native_activation_allowed": False,
            }
        )

    async def async_initialize(self):
        async with self._lock:
            _require(
                not self._initialized and self._blocked is None, "ledger_unavailable"
            )
            try:
                data = await self.store.async_load()
                if data is not None:
                    _require(
                        isinstance(data, dict)
                        and set(data)
                        == {"schema_version", "binding", "revision", "events"},
                        "ledger_corrupt",
                    )
                    _require(
                        type(data["schema_version"]) is int
                        and data["schema_version"] == SCHEMA_VERSION,
                        "ledger_schema_unknown",
                    )
                    _require(
                        data["binding"] == self.binding
                        and isinstance(data["events"], list)
                        and type(data["revision"]) is int
                        and data["revision"] == len(data["events"]),
                        "ledger_corrupt",
                    )
                    for event in data["events"]:
                        self._apply(event)
                self._initialized = True
            except BaseException:
                self._blocked = "ledger_load_failed"
                self._events, self._cases, self._requests = [], {}, {}
                raise

    def _fresh_failures(self, evidence):
        failures = _failures(evidence)
        known = set()
        for case in self._cases.values():
            known.update(case["failure_keys"])
            for operation, raw in case["operations"].items():
                if operation in failures:
                    _require(
                        _failure_key(self.binding, operation, raw)
                        == _failure_key(self.binding, operation, failures[operation]),
                        "failure_identity_conflict",
                    )
        return {
            op: raw
            for op, raw in failures.items()
            if _failure_key(self.binding, op, raw) not in known
        }

    def _apply(self, event):
        """Validate and replay every event; no trusted persisted projection."""
        _require(
            isinstance(event, dict)
            and set(event)
            == {
                "event_id",
                "revision",
                "previous_hash",
                "at",
                "kind",
                "request_id",
                "request",
                "result",
                "hash",
            },
            "ledger_corrupt",
        )
        _require(
            _text(event["event_id"])
            and event["event_id"] not in {e["event_id"] for e in self._events},
            "ledger_corrupt",
        )
        _require(
            type(event["revision"]) is int
            and event["revision"] == self.revision + 1
            and event["previous_hash"]
            == (self._events[-1]["hash"] if self._events else None),
            "ledger_corrupt",
        )
        _require(
            event["hash"] == _digest({k: v for k, v in event.items() if k != "hash"}),
            "ledger_corrupt",
        )
        _time(event["at"])
        request = event["request"]
        _require(
            _text(event["request_id"])
            and event["request_id"] not in self._requests
            and isinstance(request, dict)
            and type(request.get("expected_revision")) is int
            and request["expected_revision"] == self.revision,
            "ledger_corrupt",
        )
        if event["kind"] == "registered":
            _require(
                set(request) == {"expected_revision", "evidence"}, "ledger_corrupt"
            )
            evidence = request["evidence"]
            _require(
                set(evidence) == {"content", "digest", "findings"}
                and evidence["digest"] == _digest(evidence["content"]),
                "ledger_corrupt",
            )
            _RecoverySource._validate_content(evidence["content"])
            _require(
                evidence["content"][0]["binding"] == self.binding, "binding_mismatch"
            )
            failures = _failures(evidence)
            _require(bool(failures), "no_failure")
            fresh = self._fresh_failures(evidence)
            result = event["result"]
            _require(
                set(result) == {"recovery_id"}
                and _text(result["recovery_id"])
                and result["recovery_id"] not in self._cases
                and bool(fresh),
                "ledger_corrupt",
            )
            self._cases[result["recovery_id"]] = {
                "recovery_id": result["recovery_id"],
                "binding": self.binding,
                "operations": fresh,
                "cycle_ids": sorted({raw["cycle_id"] for raw in fresh.values()}),
                "failure_keys": [
                    _failure_key(self.binding, op, raw) for op, raw in fresh.items()
                ],
                "evidence": evidence,
                "reviews": {},
                "review_status": "review_required",
                "revision": event["revision"],
            }
        elif event["kind"] == "acknowledged":
            _require(
                set(request)
                == {
                    "expected_revision",
                    "recovery_id",
                    "operation_id",
                    "evidence_digest",
                    "actor",
                    "review",
                },
                "ledger_corrupt",
            )
            case = self._cases.get(request["recovery_id"])
            _require(
                case is not None and request["operation_id"] in case["operations"],
                "reference_invalid",
            )
            _require(
                request["evidence_digest"] == case["evidence"]["digest"],
                "evidence_changed",
            )
            actor = request["actor"]
            _require(
                isinstance(actor, dict)
                and set(actor) == {"actor_id", "context_id", "authority"},
                "actor_invalid",
            )
            TrustedReviewContext(**actor).validate()
            _review_valid(
                request["review"],
                case["operations"][request["operation_id"]],
                event["at"],
            )
            _require(request["operation_id"] not in case["reviews"], "already_reviewed")
            _require(
                event["result"]
                == {
                    "recovery_id": request["recovery_id"],
                    "operation_id": request["operation_id"],
                    "acknowledged": True,
                },
                "ledger_corrupt",
            )
            case["reviews"][request["operation_id"]] = {
                "request_id": event["request_id"],
                "actor": actor,
                "review": request["review"],
                "at": event["at"],
            }
            case["review_status"] = (
                "acknowledged"
                if set(case["reviews"]) == set(case["operations"])
                else "review_required"
            )
            case["revision"] = event["revision"]
        else:
            raise LedgerError("ledger_schema_unknown")
        self._events.append(deepcopy(event))
        self._requests[event["request_id"]] = event

    def _ready(self):
        _require(self._initialized and self._blocked is None, "ledger_unavailable")

    def _retry(self, request_id, request):
        _require(_text(request_id), "request_invalid")
        previous = self._requests.get(request_id)
        if previous is not None:
            _require(
                _digest(previous["request"]) == _digest(request), "request_conflict"
            )
            return freeze(
                {
                    **previous["result"],
                    "revision": previous["revision"],
                    "native_activation_allowed": False,
                }
            )
        _require(
            type(request.get("expected_revision")) is int
            and request["expected_revision"] == self.revision,
            "revision_conflict",
        )
        return None

    async def _append(self, kind, request_id, request, result, timeout):
        _require(
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 0,
            "timeout_invalid",
        )
        event = {
            "event_id": str(uuid4()),
            "revision": self.revision + 1,
            "previous_hash": self._events[-1]["hash"] if self._events else None,
            "at": dt_util.utcnow().isoformat(),
            "kind": kind,
            "request_id": request_id,
            "request": request,
            "result": result,
        }
        event["hash"] = _digest(event)
        # Validate on a detached projection before starting any disk work.
        old = self._events, self._cases, self._requests
        self._events, self._cases, self._requests = deepcopy(old)
        try:
            self._apply(event)
            candidate = self._events, self._cases, self._requests
        finally:
            self._events, self._cases, self._requests = old
        data = {
            "schema_version": SCHEMA_VERSION,
            "binding": self.binding,
            "revision": event["revision"],
            "events": candidate[0],
        }
        self._write = self.hass.async_create_background_task(
            self.store.async_save(deepcopy(data)), "recovery ledger persistence"
        )
        # Consume late exceptions, but NEVER install a late result in memory.
        self._write.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        try:
            done, _ = await asyncio.wait({self._write}, timeout=timeout)
            _require(bool(done), "storage_timeout")
            self._write.result()
        except BaseException:
            # Cancellation/timeout cannot prove termination of executor IO. This
            # owner remains latched and registered for the whole HA lifetime.
            self._blocked = "storage_uncertain"
            raise
        self._events, self._cases, self._requests = candidate
        return freeze(
            {
                **result,
                "revision": event["revision"],
                "native_activation_allowed": False,
            }
        )

    async def async_register(self, *, request_id, expected_revision, timeout=5):
        _require(_text(request_id), "request_invalid")
        async with self._lock:
            self._ready()
            # A retry uses its original immutable evidence rather than fresh time.
            prior = self._requests.get(request_id)
            if prior is not None:
                _require(prior["kind"] == "registered", "request_conflict")
                return self._retry(
                    request_id,
                    {
                        "expected_revision": expected_revision,
                        "evidence": prior["request"]["evidence"],
                    },
                )
            _require(
                type(expected_revision) is int and expected_revision == self.revision,
                "revision_conflict",
            )
            evidence = await self.source.capture()
            failures = _failures(evidence)
            _require(bool(failures), "no_failure")
            _require(
                bool(self._fresh_failures(evidence)),
                "failure_already_registered",
            )
            request = {"expected_revision": expected_revision, "evidence": evidence}
            self._retry(request_id, request)
            return await self._append(
                "registered",
                request_id,
                request,
                {"recovery_id": str(uuid4())},
                timeout,
            )

    async def async_acknowledge(
        self,
        *,
        recovery_id,
        operation_id,
        context,
        request_id,
        expected_revision,
        evidence_digest,
        review,
        timeout=5,
    ):
        _require(_text(request_id), "request_invalid")
        _require(
            _text(recovery_id) and _text(operation_id) and _text(evidence_digest),
            "reference_invalid",
        )
        _require(type(context) is TrustedReviewContext, "actor_invalid")
        context.validate()
        request = _json(
            {
                "expected_revision": expected_revision,
                "recovery_id": recovery_id,
                "operation_id": operation_id,
                "evidence_digest": evidence_digest,
                "actor": context,
                "review": review,
            }
        )
        async with self._lock:
            self._ready()
            previous = self._retry(request_id, request)
            if previous is not None:
                return previous
            case = self._cases.get(recovery_id)
            _require(
                case is not None and operation_id in case["operations"],
                "reference_invalid",
            )
            # Ack reviews historical evidence; changed live content requires a new
            # explicitly reviewed case/version in a later stage, never a bypass.
            current = await self.source.capture()
            _require(
                current["digest"] == case["evidence"]["digest"] == evidence_digest,
                "evidence_changed",
            )
            return await self._append(
                "acknowledged",
                request_id,
                request,
                {
                    "recovery_id": recovery_id,
                    "operation_id": operation_id,
                    "acknowledged": True,
                },
                timeout,
            )
