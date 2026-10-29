# Native recovery reconciliation plans, stage three

Fortschreibung nach PR #98: [Dauerhafte Teilnehmerrevisionen und Laufzeitepochen](native-recovery-revisions.md). Die dort beschriebenen Revisionsbelege und Schema-2-Laufzeitbindungen ersetzen die unten dokumentierte frühere Revisionslücke; Freigabegrenzen bleiben bestehen.

This implements section 10, step 3 of
[native-recovery-release-design.md](native-recovery-release-design.md) on `main`
after merged PR #94 (`67dddacb815c9d85b8ebd51a61bf462d772cbcba`). PR #94
implemented [the ledger](native-recovery-ledger.md), not reconciliation planning.
The [snapshot contracts](native-recovery-snapshots.md) remain the evidence model.

## Internal API and collection

`await async_create_reconciliation_plan(ledger)` in `recovery_plan.py` returns a
frozen `ReconciliationPlan`. It is an internal diagnostic API over the actual
ledger's participant source. No integration setup, service or dashboard imports
it. There is no caller-supplied evidence, fault label, selected case subset,
water quantity, acceptance override, permit or repair command.

The planner reads the ledger's durable envelope with the existing read-only
collector, then replays every event on a detached `_LedgerProjection`. This
projection reuses the ledger's event/hash/reference/review validation without
constructing a registered writer, Store or HA owner. The replayed cases, events
and revision must match the initialized live ledger. Missing, corrupt, unsupported
or mismatching durable history blocks planning; a missing file never supplies
positive evidence of an empty/reconciled history.

Two participant captures compare live snapshots with the actually stored pump,
shot and cycle records. They validate the existing schemas, bindings, operation
identities and timestamps. OFF, task, accounting and cycle validators run again
against current observations, rather than trusting historical findings. The
ledger is read again. All captured content, live ledger state, participant state
and runtime ownership/scheduler observations must still agree. Active participant
or ledger work is rejected; planning neither waits for nor drains it.

Real HA Store files are read through the HA executor, not `Store.async_load`;
there is no corrupt-file rename, migration, save, initialization, reset or repair.
Only isolated harness memory stores use their injected read-only load method.
IO errors and invalid evidence produce a blocked diagnostic. Cancellation
propagates and does not launch a background write or pump command.

## Data model

| Field | Meaning |
| --- | --- |
| `plan_id`, `created_at` | New UUID and aware observation time for this diagnostic attempt; no persisted intent |
| `schema_version`, `policy_version` | Plan schema `1`, policy `read_only_reconciliation_v1`; independent of Store/snapshot schemas |
| `binding` | Exact entry, pump entity, registry and device fields from the actual source; unknown IDs remain null |
| `recovery_ids`, `cycle_ids`, `operation_ids` | All validated historical cases/cycles and all observed operations; operation entries retain their own cycle and recovery association |
| `source_revisions` | Global ledger revision and each participant's snapshot/store schema and actual revision; absent durable participant revisions remain null |
| `evidence_digests` | SHA-256 digests of current participants, complete durable ledger, runtime observations and each case's original evidence |
| `evidence` | Detached validated participant snapshots/findings, stored ledger and runtime evidence used by this plan |
| `cases` | Complete replayed historical cases, affected operations and operation-specific review receipts; no case is hidden by another case's acknowledgment |
| `operations` | Per-operation identity, cycle, associated recovery case, stored lifecycle/delivery/accounting state, acknowledgment and proposed diagnostic disposition |
| `findings` | Immutable `PlanFinding` records: reason code, source, detail, required action, severity and optional recovery/operation IDs |
| `open_checks` | Outstanding reason codes plus explicitly unproven scheduler, sensor/drain, ownership-epoch, external-writer and durable-revision checks |
| `native_activation_allowed` | Non-constructor frozen field, always `False` |

Mappings and sequences are recursively detached and frozen. UUIDs and digests
identify observations; they are not authenticity proofs, monotone participant
revisions or authority tokens. The complete evidence retains original journals,
checkpoint values, counters, cycle flags, phase, schedule and OFF/work evidence.
If collection fails, current evidence, dispositions, revisions and digests are
withheld. Only cases already fully replayed and compared before that failure may
remain as explicitly historical scope; findings block their use as current proof.

## Reconciliation and diagnostic dispositions

All failed operations need their own acknowledgment. Receipts are validated by
replaying the ledger; they remain historical review records. Changed current
content produces `case_evidence_changed`, and changed/missing operation content
also produces `operation_evidence_changed`. This version cannot refresh evidence
or silently carry a review onto a new runtime. Multiple cases are assessed
together, including previously registered cases whose evidence predates newer
failures. New unregistered failed operations are reported explicitly.

Every operation retains its exact existing accounting state. A successful stored
operation is proposed as `retain_accounted`; a failed/interrupted operation as
`retain_locked`. These are descriptions, not instructions to perform writes.
`volume` is always null. No duration, pump command or review statement is converted
to a measured amount. Possible, confirmed or unknown partial delivery remains
`partial_delivery_unresolved`, including after a complete acknowledgment and a
recommendation to abandon a cycle. The current ledger has no structured model
that resolves the agronomic, water-budget and accounting effects. That requires
a separate data-model decision; prose rationale cannot satisfy it.

Existing validators check terminal states, accounting intent, shutdown/delivery
consistency, checkpoint/options agreement, native counters, known booked shots,
cycle IDs, phase, P1 flags, P2 reference and reference-cycle association, schedules,
and P0/P1/P2/P3 lifecycle consistency. The planner additionally checks that the
cycle counter baseline does not exceed current counters. A baseline can precede
later successful shots; inequality in that direction is not itself a conflict.
A present accounting checkpoint must have exactly its phase counter, last-shot
timestamp and shot type, with a matching completed/accounted operation in that
cycle. This establishes a provenance check, not a reconstructed volume ledger.
Missed reset/restart markers require explicit cycle-boundary review.

Fresh OFF reports, ordering, restored-state exclusion, independent shutdown
confirmation, unresolved commands, writes and tasks remain subject to the existing
validators. Review shutdown results cannot replace live OFF evidence. Actual pump
and shot ownership contracts must agree and have no ownership rejection reasons.
Configured legacy cycle/counter helpers are reported even when managed options
mask them. Scheduler running/task/timer/pending observations are inspected without
starting or stopping it. Idle local state does not prove a fresh drain/ownership
epoch or exclude external writers.

## Status policy

| Status | Definition |
| --- | --- |
| `blocked` | At least one blocking conflict, unavailable/changed evidence, unresolved shutdown/task/ownership state, or possible partial-delivery effect |
| `review_required` | No blocking finding, but at least one missing review or outstanding diagnostic proof/contract |
| `preparation_eligible` | No findings under the policy; diagnostic eligibility for separately designed preparation only |

Blocking findings take precedence over missing reviews. **No status grants any
release.** The current participant contracts have unknown revisions/physical
identity and no full scheduler/sensor/ownership epoch proof. The planner explicitly
retains these gaps; current stage-one/two evidence cannot reach
`preparation_eligible`. Tests exercise the three-way status rule without inventing
production evidence or adding a caller override. New proof contracts would require
an explicit policy change and review.

## Reason codes and required actions

Snapshot reason codes retain their existing meanings from
[native-recovery-snapshots.md](native-recovery-snapshots.md). Structural identity,
schema/time, OFF, unresolved work, partial-delivery, accounting and cycle findings
block. `recovery_open`, `revision_unknown`, `actor_identity_unknown` and
`required_check_unknown` request review without waiving any other finding.
Collector/replay errors such as `binding_mismatch`, `cycle_mismatch`,
`evidence_busy`, `evidence_not_durable`, `evidence_changed`, `ledger_corrupt` and
`ledger_schema_unknown` block. Unexpected malformed JSON shapes or IO failures
are `evidence_invalid`; no clean fallback evidence is manufactured.

| Additional code | Action; blocking unless stated otherwise |
| --- | --- |
| `ledger_busy`, `ledger_unavailable`, `ledger_not_durable` | Obtain an initialized, idle ledger matching durable validated history |
| `acknowledgment_missing` | Obtain the affected operation's explicit review; review severity |
| `review_shutdown_unresolved` | Obtain independent shutdown evidence; negative/unknown review remains visible |
| `partial_delivery_unresolved` | Decide agronomic, water-budget and cycle effects under a separately defined model; retain lock and unknown volume |
| `cycle_disposition_required` | Decide cycle abandonment/new baseline separately; a recommendation performs nothing |
| `case_evidence_changed`, `operation_evidence_changed` | Review historical/current evidence and define an explicit versioned refresh contract |
| `operation_case_conflict` | Resolve operation identity appearing in multiple cases |
| `recovery_case_missing` | Register/review a persisted failure; an otherwise empty scope requests review |
| `cycle_boundary_unresolved` | Assess missed resets, restarts and affected cycle budgets |
| `accounting_checkpoint_unbound` | Reconcile checkpoint provenance, exact cycle/phase/time and counter evidence |
| `accounting_inconsistent` | Review conflicting accounting, malformed counters or cycle baseline exceeding current counters |
| `ownership_conflict`, `legacy_conflict` | Establish exact exclusive ownership and explicitly review legacy assignments/writers |
| `scheduler_unresolved` | Obtain independent quiesce/drain proof; the planner never drains work |
| `ownership_epoch_unknown` | Define fresh ownership/external-writer evidence; review severity |

## Safety and concurrency limits

Planning creates an in-memory diagnostic only. It does not write a recovery,
pump, shot or cycle journal; acknowledge/register a case; mutate accounting,
counters, gates or ownership; create a permit; replay/replace a shot; dispatch
ON/OFF; install listeners; or start a scheduler. Every existing recovery lock
remains authoritative. There are no new HA services or dashboard controls.

Double reads detect observed concurrent changes, including disk-only ledger
changes. They do not provide a cross-store transaction, filesystem lock, protection
against external privileged writers, transient ABA changes or rollback detection.
A plan becomes historical immediately after capture. It must never be consumed
as commit/release authority. Serialization, generations, revocation and commit
protocols remain step 4; explicit Fake cycle release remains step 5.

## Validation

Tests use only the fixed FakePump and forbidden real HA service dispatch. They
compare full participant checkpoints, ledger records/projections and command
lists before and after planning. Real Store tests additionally compare exact
file bytes and forbid Store load/save calls. Cases cover acknowledgments,
partial-delivery uncertainty, accounting/checkpoints/counters, cycle flags and
reference, missing OFF evidence, ownership/legacy/scheduler conflicts, multiple
cases, restart, corrupt durable history, cancellation, busy participants and
barrier-controlled concurrent ledger/runtime/disk/options changes.

Final environment: Python 3.13.16, Home Assistant 2025.12.5, pytest 9.0.0,
Ruff 0.17.0. The existing collector's 30-second freshness limit is a diagnostic
harness limit, not a production freshness policy. The missing HA fixture was
installed with `--no-deps`; CI requirements were installed in the existing isolated
venv. `uv pip check` reports the already documented fixture metadata exception:
`pytest-homeassistant-custom-component==0.13.301` requests HA 2025.12.4 while the
repository pins/tests 2025.12.5. No dependency files were changed.

The complete existing checkout matched current `main` and was confirmed with
`git fetch origin main` at `67dddacb815c9d85b8ebd51a61bf462d772cbcba`.
Initial sandbox network attempts could not reach the proxy; the supported command
network permission allowed fetch, package installation and HA test execution.
No source reconstruction from selected remote files was used.

Commands used `/workspace/growassistant-cloud/venv/bin/python`; pytest used
`LITELLM_LOCAL_MODEL_COST_MAP=True` and command network permission.

| Final check | Result |
| --- | --- |
| `python -m pytest -q tests/native_harness/test_recovery_plans.py` | **43 passed**, 2.44 s |
| `python -m pytest -q` | **1015 passed**, no failures/skips, 114.38 s; 972 baseline + 43 new |
| `python -m ruff check custom_components tests scripts` | Passed |
| `python -m ruff format --check custom_components tests scripts` | Passed, 53 files |
| `python -m compileall -q custom_components tests scripts` | Passed |
| `git diff --cached --check` | Passed, including new files |

The existing two multi-failure/case ledger tests additionally verify planner
aggregation, an unregistered newer fault, case-specific missing acknowledgment and
unchanged journals/commands. An earlier full run (before the last four new test
cases) had 1010 passes and one existing timing-sensitive failure in
`test_p1_at_capacity_counts_only_confirmed_shutdown[True]`, which reported
`deadline_exceeded`. Targeted tests were running concurrently at that time. The
final full run above ran without another pytest process and passed every test;
no unrelated test assertion or production deadline was changed.
