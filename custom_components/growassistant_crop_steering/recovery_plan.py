"""Internal read-only reconciliation diagnostics. No execution authority or IO writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import uuid4

from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery_ledger import (
    SCHEMA_VERSION as LEDGER_SCHEMA,
)
from .recovery_ledger import (
    LedgerError,
    _digest,
    _json,
    _RecoveryLedger,
    _require,
)
from .recovery_snapshot import COUNTER_KEYS, freeze, task_state

PLAN_SCHEMA = 1
POLICY_VERSION = "read_only_reconciliation_v1"
PlanStatus = Literal["blocked", "review_required", "preparation_eligible"]


@dataclass(frozen=True)
class PlanFinding:
    code: str
    source: str
    detail: str
    action: str
    severity: Literal["blocked", "review_required"] = "blocked"
    recovery_id: str | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class ReconciliationPlan:
    plan_id: str
    created_at: datetime
    status: PlanStatus
    binding: object
    recovery_ids: tuple[str, ...]
    cycle_ids: tuple[str, ...]
    operation_ids: tuple[str, ...]
    source_revisions: object
    evidence_digests: object
    evidence: object
    cases: object
    operations: object
    findings: tuple[PlanFinding, ...]
    open_checks: tuple[str, ...]
    schema_version: int = field(default=PLAN_SCHEMA, init=False)
    policy_version: str = field(default=POLICY_VERSION, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)

    def __post_init__(self):
        for name in (
            "binding",
            "source_revisions",
            "evidence_digests",
            "evidence",
            "cases",
            "operations",
        ):
            object.__setattr__(self, name, freeze(getattr(self, name)))


class _LedgerProjection(_RecoveryLedger):
    """Reuse stage-two event validation on detached data, without creating an owner.

    No Store, HA reference, initialization, registration or acknowledgment is used.
    Only _apply and its pure replay helpers are called on this private projection.
    """

    def __init__(self, data, binding):
        _require(
            isinstance(data, dict)
            and set(data) == {"schema_version", "binding", "revision", "events"},
            "ledger_corrupt",
        )
        _require(
            type(data["schema_version"]) is int
            and data["schema_version"] == LEDGER_SCHEMA,
            "ledger_schema_unknown",
        )
        _require(
            data["binding"] == binding
            and isinstance(data["events"], list)
            and type(data["revision"]) is int
            and data["revision"] == len(data["events"]),
            "ledger_corrupt",
        )
        self.binding = binding
        self._events, self._cases, self._requests = [], {}, {}
        for event in data["events"]:
            self._apply(event)


def _status(findings) -> PlanStatus:
    if any(f.severity == "blocked" for f in findings):
        return "blocked"
    return "review_required" if findings else "preparation_eligible"


def _runtime(ledger):
    """Observe actual runtime contracts without invoking readiness/gate mutators."""
    source = ledger.source
    scheduler = source.hass.data.get(f"{DOMAIN}.development_schedulers", {}).get(
        source.binding.entry_id
    )
    return _json(
        {
            "pump_ownership": source.pump.ownership,
            "shot_ownership": source.shots.ownership,
            "ownership_reasons": source.shots._ownership_reasons(
                source.binding.entity_id
            ),
            "entry_data": dict(source.shots.entry.data),
            "scheduler": None
            if scheduler is None
            else {
                "epochs": scheduler.recovery_gate_snapshot().runtime,
                "running": scheduler.running,
                "task": task_state(scheduler._task),
                "generation": scheduler._generation,
                "timer_revision": scheduler._timer_revision,
                "timer_pending": scheduler._timer is not None,
                "evaluation_pending": scheduler._pending is not None,
            },
        }
    )


async def async_create_reconciliation_plan(
    ledger: _RecoveryLedger,
) -> ReconciliationPlan:
    """Inspect every case; reject inconsistent observations without changing any store.

    Double reads detect observed changes, not a transaction or protection from
    external writers/ABA. No result, including preparation_eligible, is a permit.
    """
    source = ledger.source
    binding = _json(source.binding)
    findings = []
    evidence, cases, dispositions, revisions, digests = {}, {}, {}, {}, {}
    verified_cases = {}
    checks = []

    def add(code, origin, detail, action, severity="blocked", rid=None, op=None):
        findings.append(PlanFinding(code, origin, detail, action, severity, rid, op))

    try:
        ledger._ready()
        _require(not ledger._lock.locked(), "ledger_busy")
        before = _json(ledger.inspect())
        runtime = _runtime(ledger)
        saved = await source._read(ledger.store, "recovery_ledger")
        projection = _LedgerProjection(saved, binding)
        _require(
            _json(projection._cases) == before["cases"]
            and projection._events == before["events"]
            and projection.revision == before["revision"],
            "ledger_not_durable",
        )
        verified_cases = _json(projection._cases)
        captured = await source.capture()
        again = await source.capture()
        saved_again = await source._read(ledger.store, "recovery_ledger")
        _require(
            captured["digest"] == again["digest"]
            and saved == saved_again
            and before == _json(ledger.inspect())
            and runtime == _runtime(ledger)
            and captured["content"] == source._content(source._snapshots())
            and not ledger._lock.locked(),
            "evidence_changed",
        )
        ledger._ready()
        cases = _json(projection._cases)
        evidence = {"participants": captured, "ledger": saved, "runtime": runtime}
        pump, shots, cycle = captured["content"]
        revisions = {
            "ledger": projection.revision,
            **{
                name: {**participant["version"], "runtime": participant.get("runtime")}
                for name, participant in zip(
                    ("pump", "shots", "cycle"), captured["content"], strict=True
                )
            },
        }
        digests = {
            "participants": captured["digest"],
            "ledger": _digest(saved),
            "runtime": _digest(runtime),
            "cases": {rid: c["evidence"]["digest"] for rid, c in cases.items()},
        }
        for finding in captured["findings"]:
            code = finding["code"]
            severity = (
                "review_required"
                if code
                in {
                    "recovery_open",
                    "revision_unknown",
                    "actor_identity_unknown",
                    "required_check_unknown",
                }
                else "blocked"
            )
            add(
                code,
                finding["source"],
                finding["detail"],
                "Resolve and recapture source evidence; retain all recovery gates",
                severity,
            )
        checks.extend(
            (
                "scheduler_drained",
                "sensors_and_drain",
                "ownership_epoch",
                "external_writers",
            )
        )
        if any(
            f["code"] in {"revision_unknown", "revision_conflict", "schema_unknown"}
            for f in captured["findings"]
        ):
            checks.append("durable_participant_revisions")
        for reason in runtime["ownership_reasons"]:
            add(
                "ownership_conflict",
                "runtime",
                reason,
                "Establish exact exclusive ownership and review legacy writers",
            )
        if runtime["pump_ownership"] != runtime["shot_ownership"]:
            add(
                "ownership_conflict",
                "runtime",
                "Participant ownership differs",
                "Review both participant ownership contracts",
            )
        legacy_keys = (
            *COUNTER_KEYS,
            "p1_active",
            "p1_done",
            "p1_window_opened_today",
            "p2_ref_vwc",
        )
        if any("legacy" in str(f["detail"]) for f in captured["findings"]) or any(
            isinstance(runtime["entry_data"].get(key), str)
            and "." in runtime["entry_data"][key]
            for key in legacy_keys
        ):
            add(
                "legacy_conflict",
                "participants",
                "Legacy recovery marker or helper assignment present",
                "Review legacy cycle/helper migration without changing journals",
            )
        scheduler = runtime["scheduler"]
        if scheduler and (
            scheduler["running"]
            or scheduler["timer_pending"]
            or scheduler["evaluation_pending"]
            or scheduler["task"] not in {"absent", "completed"}
        ):
            add(
                "scheduler_unresolved",
                "runtime",
                "Scheduler work is not drained",
                "Require independent quiesce/drain evidence",
            )
        # A local idle scheduler and harness assertions cannot establish epochs.
        add(
            "ownership_epoch_unknown",
            "runtime",
            "No versioned ownership/legacy epoch",
            "Define fresh ownership and external-writer evidence",
            "review_required",
        )
        operations = shots["journal"]["operations"]
        owners = {}
        for rid, case in cases.items():
            if case["evidence"]["digest"] != captured["digest"]:
                add(
                    "case_evidence_changed",
                    "ledger",
                    "Historical evidence differs",
                    "Versioned evidence refresh requires a separate decision",
                    rid=rid,
                )
            for op, historical in case["operations"].items():
                if op in owners:
                    add(
                        "operation_case_conflict",
                        "ledger",
                        "Operation in multiple cases",
                        "Review case identity conflict",
                        rid=rid,
                        op=op,
                    )
                owners[op] = rid
                if operations.get(op) != historical:
                    add(
                        "operation_evidence_changed",
                        "ledger",
                        "Operation changed or missing",
                        "Review historical and current operation evidence",
                        rid=rid,
                        op=op,
                    )
                receipt = case["reviews"].get(op)
                if receipt is None:
                    add(
                        "acknowledgment_missing",
                        "ledger",
                        "Operation not acknowledged",
                        "Obtain explicit operation review",
                        "review_required",
                        rid,
                        op,
                    )
                else:
                    review = receipt["review"]
                    if review["shutdown"]["result"] != "confirmed_off":
                        add(
                            "review_shutdown_unresolved",
                            "ledger",
                            "Shutdown review negative/unknown",
                            "Obtain independent shutdown evidence",
                            rid=rid,
                            op=op,
                        )
                    if review["disposition"] == "abandon_cycle_requires_reconciliation":
                        add(
                            "cycle_disposition_required",
                            "ledger",
                            "Cycle abandonment only recommended",
                            "Decide cycle effects and new baseline separately",
                            rid=rid,
                            op=op,
                        )
                partial = (
                    historical["delivery_uncertain"]
                    or historical.get("delivery") != "not_requested"
                    or bool(historical.get("activation_requested_at"))
                    or bool(historical.get("activation_confirmed_at"))
                    or (
                        receipt is not None
                        and receipt["review"]["partial_delivery"] != "none_evidenced"
                    )
                )
                if partial:
                    add(
                        "partial_delivery_unresolved",
                        "ledger",
                        "No model resolves agronomic/accounting effects of possible delivery",
                        "Determine water-budget and cycle effects; never infer a volume",
                        rid=rid,
                        op=op,
                    )
        for op, raw in operations.items():
            rid = owners.get(op)
            if raw["state"] in {"fault", "interrupted"} and rid is None:
                add(
                    "recovery_case_missing",
                    "shots",
                    "Persisted failure has no case",
                    "Register and review this operation explicitly",
                    op=op,
                )
            dispositions[op] = {
                "recovery_id": rid,
                "operation_id": op,
                "cycle_id": raw["cycle_id"],
                "state": raw["state"],
                "accounted": raw["accounted"],
                "delivery": raw.get("delivery"),
                "volume": None,
                "disposition": "retain_locked"
                if raw["state"] != "completed"
                else "retain_accounted",
                "acknowledged": rid is not None and op in cases[rid]["reviews"],
            }
        record, journal = cycle["record"], shots["journal"]
        if any(
            journal.get(k)
            for k in ("missed_cycle_reset", "pump_restart_requires_review")
        ):
            add(
                "cycle_boundary_unresolved",
                "shots",
                "Missed reset/restart marker",
                "Review missed boundaries and affected cycle budget",
            )
        _require(isinstance(record.get("counters"), dict), "accounting_inconsistent")
        # Cycle counters are a baseline at the last phase transition, not a
        # live tally. Later successfully accounted shots may increase options.
        if any(
            type(record.get("counters", {}).get(k)) is not int
            or type(shots["options"].get(k)) is not int
            or record["counters"][k] > shots["options"][k]
            for k in COUNTER_KEYS
        ):
            add(
                "accounting_inconsistent",
                "cycle",
                "Cycle counter baseline exceeds current counters or has invalid types",
                "Reconcile checkpoints and counters without rewriting them",
            )
        checkpoint = journal.get("last_accounting")
        if checkpoint:
            _require(
                isinstance(checkpoint, dict)
                and isinstance(checkpoint.get("values"), dict),
                "accounting_inconsistent",
            )
            values = checkpoint["values"]
            prefix = values.get("last_shot_type")
            phase = {
                "p1": "p1_morning",
                "p2": "p2_midday",
                "p3_emergency": "p3_dryback",
            }.get(prefix)
            counter = f"{prefix}_shots_done"
            if (
                phase is None
                or set(values) != {counter, "last_shot", "last_shot_type"}
                or type(values.get(counter)) is not int
                or values.get(counter, 0) < 1
                or not any(
                    raw["state"] == "completed"
                    and raw["accounted"] is True
                    and raw.get("finished_at") == values.get("last_shot")
                    and raw.get("cycle_id") == checkpoint.get("cycle_id")
                    and raw.get("phase") == phase
                    for raw in operations.values()
                )
            ):
                add(
                    "accounting_checkpoint_unbound",
                    "shots",
                    "Checkpoint has no exact completed-operation evidence",
                    "Review checkpoint provenance, phase, timestamp and counter; retain accounting",
                )
        if not cases:
            add(
                "recovery_case_missing",
                "ledger",
                "No registered recovery case",
                "Establish explicit recovery scope",
                "review_required",
            )
    except (
        LedgerError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
    ) as err:
        add(
            getattr(err, "code", "evidence_invalid"),
            "capture",
            "Validated durable evidence unavailable or changed",
            "Resolve source evidence and create a new diagnostic plan",
        )
        # Keep only fully replayed historical scope; no current evidence is valid.
        cases = verified_cases
        evidence, dispositions, revisions, digests = {}, {}, {}, {}

    return ReconciliationPlan(
        plan_id=str(uuid4()),
        created_at=dt_util.utcnow(),
        status=_status(findings),
        binding=binding,
        recovery_ids=tuple(sorted(cases)),
        cycle_ids=tuple(
            sorted({c for case in cases.values() for c in case["cycle_ids"]})
        ),
        operation_ids=tuple(
            sorted(
                set(dispositions)
                | {op for case in cases.values() for op in case["operations"]}
            )
        ),
        source_revisions=revisions,
        evidence_digests=digests,
        evidence=evidence,
        cases=cases,
        operations=dispositions,
        findings=tuple(findings),
        open_checks=tuple(dict.fromkeys(checks + [f.code for f in findings])),
    )
