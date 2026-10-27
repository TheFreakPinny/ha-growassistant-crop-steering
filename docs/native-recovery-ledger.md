# Native recovery ledger, stage two

This implements only ledger, explicit case registration and internal incident
acknowledgment from [the release design](native-recovery-release-design.md), on
`main` after PR #93 (`cd50ecc0a2d956a649c97844aa894332a61727f6`).
[Stage-one snapshots](native-recovery-snapshots.md) remain diagnostic evidence.
The [existing recovery review](native-recovery-release-review.md) still applies.

**Acknowledgment is a recorded review, never a release or reconciliation.**
Every inspection reports `effective_state="locked"` and
`native_activation_allowed=False`, including empty and fully reviewed ledgers.
No integration setup imports or constructs the ledger. No service, dashboard,
ON call, scheduler start, permit, counter update, replay, replacement shot,
participant-journal write, gate mutator or reconciliation plan is added.

## Store and schema

`recovery_ledger.py` uses HA `Store`, version 1, with private, atomic writes, at
`growassistant_crop_steering.recovery_ledger.<entry_id>`.
The Store envelope contains a second, explicit payload schema:

```json
{
  "schema_version": 1,
  "binding": {
    "entry_id": "entry",
    "entity_id": "switch.native_harness_pump",
    "registry_id": null,
    "device_id": null
  },
  "revision": 2,
  "events": ["complete registered event", "complete acknowledged event"]
}
```

The strings above abbreviate event objects; they are not valid event records.
An event has exactly `event_id` (UUID), global `revision`, `previous_hash`,
`at` (aware ISO timestamp), `kind`, `request_id`, complete normalized `request`,
`result`, and SHA-256 `hash` of all its other fields. Allowed kinds:

- `registered`: stores the entire detached pump/shot/cycle evidence, its content
  digest, findings, expected revision and generated UUID `recovery_id`.
- `acknowledged`: stores recovery/operation identity, evidence digest, expected
  revision, actor/context, complete operation review and the historical result.

There is no stored mutable case projection. Initialization validates and replays
**every** event, including references, request/event uniqueness, content hashes,
sequence, schemas, bindings and review fields. `revision == len(events)` and
event revisions are consecutive from 1. A case's revision is the last global
revision that affected it; requests always compare the **global ledger revision**,
so concurrent writes to different cases cannot overwrite each other either.
Unknown schemas/event types, incomplete events, broken chains and invalid reviews
block the owner. A failed replay discards its partial in-memory projection and
never writes a replacement file. There is no migration or reset method.

Derived cases contain exact binding, `recovery_id`, affected operation records
(including reasons/outcomes), cycle IDs, stable failure keys, evidence, reviews,
`review_status`, and last revision. Review status is `review_required` until each
affected operation has its own acknowledgment, then `acknowledged`. Neither status
changes the effective lock. Events preserve original evidence and decisions;
there is no correction/delete API in this stage.

## Internal interfaces and evidence

These are development-only Python interfaces, not HA services:

1. `_RecoverySource(shots, cycle)` requires the actual cycle's coordinator and
   its pump/entry. `_RecoveryLedger(source)` reserves the only writer for that
   entry in `hass.data`, synchronously, for the entire HA lifetime.
2. `await ledger.async_initialize()` loads history without resuming any procedure.
3. `await ledger.async_register(request_id=..., expected_revision=..., timeout=5)`
   collects actual snapshots and compares their journals with persisted evidence.
   It creates a case for **all previously unregistered failed operations** in that
   capture. The result includes recovery ID and committed global revision.
4. `ledger.inspect()` returns detached, recursively immutable historical cases and
   events, the effective lock and any local failure reason.
5. `await ledger.async_acknowledge(recovery_id=..., operation_id=..., context=...,
   request_id=..., expected_revision=..., evidence_digest=..., review=...,
   timeout=5)` records exactly one operation's review after rechecking its evidence.

Collection rejects active/ambiguous work, unavailable durable records, unknown
snapshot/store schemas, mismatched entry/pump/operation/cycle identities,
nonterminal or incomplete operation records, and conflicting live/persisted
journals. An accepted fault must actually be a persisted `interrupted` or `fault`
shot with reservation and finish timestamps, failed outcome, no successful
accounting, and delivery/shutdown evidence. A healthy journal cannot create a case.
Callers cannot supply a fault label, select a subset to conceal another operation,
or turn validator assertions into fault evidence.

For HA participant Stores the collector reads their on-disk envelopes through
HA's executor **without** calling participant `Store.async_load`: that method can
rename corrupt journals or migrate data. Corrupt, missing or unfamiliar evidence
is rejected without changing those files. Injected memory stores are explicitly
for the isolated harness. Captures before/after the reads must match; this is
observed consistency, not a cross-store transaction or protection against external
writers. All existing adverse/unknown snapshot findings remain in the evidence.

The stable fault key binds exact resource, operation ID, cycle ID and original
reservation timestamp. Re-registering known faults is rejected without an event;
reusing an operation with another reservation/cycle is a conflict. A later capture
with new faults creates another case while older cases remain visible and locked.
There is no global acknowledgment that hides another case or another operation.

Legacy operation records lack per-record entry IDs; provenance is the owning
entry's Store and live coordinator. An explicit conflicting entry is rejected.
Registry/device IDs and durable participant revisions remain unknown where stage
one reports them as unknown. No revision is fabricated. The evidence digest binds
actual contents (including options, reasons, work and OFF evidence), omitting only
capture timestamps. It is **not** a participant revision or authenticity proof.
A changed digest rejects a new acknowledgment. A reboot usually changes runtime
proofs, so historical open reviews remain visible but cannot be continued against
stale evidence. This stage provides no evidence refresh/reconciliation bypass;
a future explicit, versioned recapture contract must preserve the old history.
An old, already committed identical request can still return its historical result.

## Actor/context and review contract

`TrustedReviewContext(actor_id, context_id, authority="isolated_harness")` is an
explicit **test/development-only trusted caller contract**. The caller asserts
that it resolved identity out of band; nonempty actor/context IDs and the exact
contract type/authority are required. A username string, dictionary, missing
identity or other authority is rejected. Creating this Python object is not real
authentication, nor proof of HA permissions. It must never be deserialized from
public input. There is no public API in this stage. A later HA boundary must resolve
HA's authenticated context/user and authorization itself, without trusting a
user-supplied actor name or exposing this harness assertion mechanism.

A review must include exactly:

| Field | Required content |
| --- | --- |
| `partial_delivery` | `possible`, `confirmed`, `unknown`, or `none_evidenced`; cannot deny persisted uncertainty/activation evidence |
| `volume` | `null`; no measured-volume provenance exists in this stage |
| `shutdown` | `method`, `source`, aware `checked_at`, `result`, and explicit `limitations` |
| `shutdown.result` | `confirmed_off`, `unknown`, or `failed`; the check time must be between shot finish and the audit event |
| `journal_review` | Nonempty documented journal/accounting/cycle assessment |
| `disposition` | `keep_locked` or `abandon_cycle_requires_reconciliation` |
| `rationale` | Nonempty professional reasoning |

The latter disposition is only a documented recommendation: it does not end or
modify a cycle. Negative/unknown shutdown checks can be recorded honestly, still
locked; no checkbox or asserted physical proof grants authority. FakePump inspection
proves only the simulated state, never physical safety. Actor, context, request,
operation/evidence reference and server event timestamp are retained in the audit.

## Idempotency, writes and crash guarantees

A single `asyncio.Lock` serializes initialization, captures, revision checks and
writes. The HA owner registry rejects a second writer, including after a failed
or timed-out writer. There is intentionally no unregister/reopen escape during
that HA lifetime. This is process-local serialization, not a filesystem or
multi-process lock.

An identical request ID and canonical JSON content returns the original committed
result/revision with no extra event, even after restart. Different content under
that ID is `request_conflict`, including boolean versus numeric revisions.
Registration retries bind their original server-collected evidence, not a new
capture. Another method cannot reuse their request ID. A new request with an old
revision is rejected; invalid revisions never reach storage. Each operation can
be acknowledged once in this schema. A retry is a historical receipt, not a
statement of current evidence, current revision or permission.

Candidate events/projections are validated on detached copies before writing.
The new in-memory result is installed only after the awaited save succeeds, with
no intervening await. HA normally logs `WriteError`/serialization errors rather
than raising them; `_LedgerStore` wraps these as `LedgerError("storage_failed")`.
Read-only Stores and HA shutdown's deferred-write path are rejected. Audit failure
therefore cannot produce a successful acknowledgment.

The save runs in a retained background task. Timeout/caller cancellation does
**not** cancel it, does not assume its executor stopped, and does not install a
late result in memory. The owner latches `storage_uncertain` for that boot and
rejects every later mutation; no younger write can overtake the old one. Late
exceptions are consumed. A second owner remains prohibited even when the late
write finishes. Inspecting history remains possible; no gate is touched.

Crash/restart can observe the previous full file, a new full file including the
acknowledgment, or unreadable/incomplete data if the underlying storage fails.
Only valid replayed events are recognized; corrupt data remains locked. A late
successful write can therefore be found after restart even though its caller got
a timeout. An identical request then returns its historical receipt. There is no
automatic write, replayed shot, next recovery step or recreated capability at boot.
A new runtime must not overlap a still-writing old process; external writers and
multiple HA processes are outside the local-owner guarantee.

### Explicit limits of append-only and durability

The application only appends events logically. HA atomically **replaces one JSON
file**; this is not a filesystem append log, WORM store, signed audit service,
database transaction or cross-store commit. Hash chaining detects many accidental
changes but privileged writers can recompute hashes, replace/truncate a consistent
history, delete the file, or roll back a whole backup. There is no external trusted
head, rollback counter, signature or independent replication. A missing file is
indistinguishable from first use and yields an empty **locked** ledger, never
clearance. HA `.corrupt*` remnants prevent treating its corruption fallback as a
new empty ledger. Preflight rejects malformed envelopes before HA can rename them.

A successful awaited atomic HA save provides that helper's single-file completion
semantics, not an additional guarantee about power-loss durability, filesystem or
disk caches, backups, global fsync ordering, or other journals. Resource binding
cannot prove physical identity or exclusive external ownership when registry/device
identity is absent. The collector and ledger cannot authenticate externally edited
participant files. Existing pump/shot/cycle gates remain the independent safety
barrier under every ledger result, failure and storage-loss scenario.

## Reason codes and errors

| Codes | Treatment |
| --- | --- |
| `binding_unknown`, `binding_mismatch`, `cycle_mismatch`, `reference_invalid` | Reject missing or conflicting resource/operation/cycle references |
| `evidence_incomplete`, `evidence_invalid`, `evidence_schema_unknown`, `evidence_not_durable`, `evidence_busy`, `evidence_changed` | Reject absent, ambiguous, changed or unsupported evidence |
| `no_failure`, `failure_already_registered`, `failure_identity_conflict` | Reject healthy, duplicate or rebound fault registration |
| `actor_invalid`, `review_incomplete`, `review_contradiction`, `disposition_invalid`, `time_invalid`, `already_reviewed` | Reject invalid actor, review, timestamp or repeated new decision |
| `request_invalid`, `request_conflict`, `revision_conflict`, `timeout_invalid` | Reject malformed request, conflicting idempotency key, stale revision or invalid write deadline |
| `writer_exists`, `ledger_unavailable` | Refuse another writer or mutations of an uninitialized/failed owner |
| `ledger_corrupt`, `ledger_schema_unknown` | Refuse invalid stored envelopes/history; loader latches `ledger_load_failed` |
| `storage_failed`, `storage_unavailable`, `storage_timeout` | No successful receipt; owner latches `storage_uncertain`; injected backend IO failures/cancellation also latch |

Snapshot reason codes remain documented in stage one and are not waived by a
review. Store/source IO exceptions propagate without an acknowledgment; no fault
handler deletes evidence or tries to repair another participant.

## Remaining work

Technical reconciliation, explicit evidence refresh after changed runtime proofs,
authenticated HA authorization, measured/physical shutdown proof, scheduler drain,
participant revisions, ownership epochs, accounting consistency and any future
cycle-release decision require separate reviewed changes. No plan/intent,
participant mutation, gate setter or release capability is implemented here.
Production activation remains a separate, prohibited capability.

## Validation

Validation results are recorded after the final local runs below. Tests use only
the fixed FakePump, injected memory IO and selected real HA Store files in temporary
directories. The underlying HA switching dispatcher is forbidden. Multiple-fault
fixtures assemble two actually simulated failures for the same entry/cycle at the
read-only source boundary; no live journal or gate is repaired or cleared.
Concurrent writes use event barriers. Crash images are captured at the held write;
timeout/cancellation tests also hold an actual HA executor write with a thread
barrier. No new fixed sleeps are used. Harness checkpoints now preserve `entry_id`
so restart tests exercise the exact resource binding.

Final environment: Python 3.13.16, Home Assistant 2025.12.5, pytest 9.0.0,
`pytest-homeassistant-custom-component==0.13.301`, Ruff 0.17.0. Dependencies were
installed from `.github/requirements-ci.txt`; the missing fixture was installed
with `--no-deps`, retaining the repository's HA pin. Missing transitive helpers
were installed too (`boolean.py==5.0`, `termcolor==3.3.0`, `execnet==2.1.2`,
`pip==26.2.2`). `uv pip check` reports only the documented fixture metadata
exception: the fixture requests HA 2025.12.4 while this repository tests 2025.12.5.
No requirements or production integration files were changed.

Commands used `/workspace/growassistant-venv/bin/python`; pytest used
`LITELLM_LOCAL_MODEL_COST_MAP=True` and the environment's network permission.
Without that permission the initial HA fixture setup timed out in the sandbox;
with it the isolated tests ran normally. Git fetch and dependency installation
also worked with that permission. `origin/main` was fetched and the final branch
starts directly at merged #93, not at a reconstructed local baseline.

| Final check | Result |
| --- | --- |
| `python -m pytest -q tests/native_harness/test_recovery_ledger.py` | **71 passed** (1.89 s) |
| Native harness in the final full run | **176 passed**; also confirmed by collection |
| `python -m pytest -q` | **972 passed**, no failures/skips (114.49 s): 901 baseline + 71 new |
| `python -m ruff check custom_components tests scripts` | Passed |
| `python -m ruff format --check custom_components tests scripts` | Passed, 51 files |
| `python -m compileall -q custom_components tests scripts` | Passed |
| `git diff --cached --check` | Passed, including all new files |

An earlier dedicated native-harness run, with the first 52 ledger cases, reported
156 passes and one failure in the pre-existing `test_normal_18_6_day` (one completed
shot instead of three). Its isolated rerun passed in 4.49 s. No scheduler/scenario
or production timeout code was changed. The subsequent full run passed all 971
then-current tests; after the final binding check was added, the final full run
passed all 972, including that scenario. The initial discrepancy is retained here
rather than omitted; the final suites have no unresolved failures.
