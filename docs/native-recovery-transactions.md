# Native recovery transactions, stage four

This implements the conservative preparatory protocol allowed by sections 6–8
and step 4 of [the release design](native-recovery-release-design.md), against
`main` after merged PR #95 (`a0602cb`). It builds on the
[ledger](native-recovery-ledger.md) and [plans](native-recovery-plans.md).

**A technical commit is a historical receipt for diagnostic preparation in one
Store. It never authorizes irrigation.** `native_activation_allowed` is always
`False`, and effective state is always `locked`, including after commit/restart.
No production setup imports or constructs this component. No service, dashboard,
permit, scheduler start, ON/OFF command, replay, replacement, accounting write,
participant-journal mutation, repair or gate setter is added.

## Conservative boundary and missing guarantees

The existing planner cannot produce `preparation_eligible`: participant revisions,
physical shutdown, ownership epochs, external-writer isolation, full drain and
policy proofs remain incomplete. The coordinator requires a fresh eligible plan
with **no findings or open checks**, so existing actual participant sets remain
rejected. A caller cannot waive checks or supply participant proofs. A future
planner policy/proof implementation requires separate review.

Positive tests replace only the internal planner boundary with an explicitly
isolated **protocol model** over captured FakePump evidence. They demonstrate
serialization, state transitions and persistence failure behavior, **not** safe
recovery, resolved findings, or eligibility of today's participants. Separate
unmodified-planner tests verify rejection of healthy incomplete evidence and real
interrupted-shot cases, including every existing recovery gate.

There are no participant-owned recovery receipts yet. `read_only_observation`
receipts are recorded in the transaction Store and bind the complete source
revision/digest map, exact cycle and plan digest. They are not participant writes,
participant votes, external ownership proofs, or global commit acknowledgments.
No HA multi-file atomicity, global compare-and-swap, physical exclusion or
power-loss durability is claimed. This is intentionally a preparatory protocol
contract, not a transactionally committed reconciliation across existing journals.

## Internal API and states

`_RecoveryTransactionCoordinator(ledger)` reserves the pump entity for the entire
HA lifetime, even when initialization/preparation/storage fails. Different entries
cannot obtain another owner for the same entity. This registry and synchronous
non-queuing `_busy` reservation serialize only this process/event loop; they do
not exclude HA automations, scripts, direct service calls or another process.
There is no owner release/reset/supersession API in schema 1. At most one intent
is accepted for this resource in a journal; even committed/aborted history cannot
be silently replaced by another transaction. Explicit supersession is future work.

1. `async_initialize()` validates all stored events before installing history.
   It never resumes, commits, repairs or issues executable authority. Nonempty
   valid history latches `restart_requires_new_review`; corrupt history latches
   `transaction_load_failed`, discards partial projections and preserves the file.
2. `async_prepare(plan, expected_plan_digest, expected_revision, request_id)` binds
   every plan field, including identity, timestamp, target, all cases, operations,
   cycles, revisions, configuration/evidence and diagnostic checks. Expected
   revision means **global recovery-ledger revision**, never a guessed participant
   revision. It validates sources, durably writes the `prepared` intent, then
   validates sources again. Only then is an in-memory session installed.
3. `async_commit(transaction_id, expected_plan_digest, request_id)` requires that
   same live session and exact intent. It revalidates, saves `participants_checked`,
   revalidates, saves `committed`, and revalidates once more before returning a
   technical receipt. No participant or gate changes at any point.
4. `revoke(reason)` synchronously increments generation and latches a block,
   without acquiring a lock or awaiting storage. A later stop cannot replace the
   stronger `storage_uncertain` latch. Existing pump stop remains independent.
5. `async_record_revocation(request_id)` may audit an already effective block:
   `aborted` for an unfinished preparation, `revoked` after historical commit.
   It refuses uncertain writes and never retries/finalizes an incomplete commit.

`participants_checked` is an internal intermediate state, not permission.
Rejected/cancelled work latches `preparation_failed` or `commit_failed` unless a
more specific synchronous revocation/storage block already exists. There is no
automatic compensating participant write. A final committed file can coexist
with a failed post-save validation: it remains historical, effectively locked,
and may subsequently receive an explicit revocation event. Crashing before that
negative audit is safe because boot always locks. This API does not promise a
durable rejection record if the audit medium is unavailable.

Identical durable request IDs/content return immutable **historical** receipts,
including after restart/revocation. A receipt explicitly says `historical_only`;
it is not current readiness. Different content or another method under the same
ID is `request_conflict`, including boolean/numeric revision differences.
In-flight duplicates are rejected as busy rather than creating another intent;
retry after completion returns the original receipt. Different concurrent
requests are not queued. Commit uses a separate caller ID; its internal participant
event reserves `<commit_request_id>:participants`, rejecting collisions.

## Lock audit and mandatory order

The source audit found:

- `cycle.py`: sunrise, transition, reference and manual-completion paths acquire
  Cycle before `_ShotCoordinator._accounting_lock`.
- `shot_engine.py`: final accounting acquires Accounting. Shot lifecycle work is
  tracked by its task/generation; the recovery code never waits for it under a
  participant lock.
- `pump.py`: activation/stop/reconciliation serialize with the pump's own lock.
  Service completions schedule reconciliation; no callback enters Recovery.
- The recovery ledger has its own serialization lock; the planner observes it
  and rejects a busy ledger instead of acquiring it.

The mandatory future hierarchy is **Recovery serialization → Cycle → Accounting**.
Pump stop remains outside that hierarchy and must not wait for Recovery to
invalidate a generation. Pump callbacks must not await a return into the hierarchy.
This PR introduces **no nested locks**: the coordinator's event-loop reservation
only excludes competing Recovery requests, and participant/ledger locks are
inspected, never acquired. No Cycle/Accounting/Pump ordering changes are made.
Any later live stop integration must call synchronous `revoke` before awaiting
stop; this development component is not installed into current pump control.

## Quiesce/drain and TOCTOU contract

`inspect_drain()` is non-mutating diagnosis: `observed_idle`, optional reason/
guard digest, `quiesce_guaranteed=False`, `native_activation_allowed=False`.
It does not cancel tasks, stop scheduling, wait for IO, clear locks or quiesce a
running system. Unknown/unresolved work rejects preparation. True quiesce needs
separate participant admission/generation contracts; no current behavior is changed.

The guard checks service pending/timed-out sets, snapshot work (including safety,
late reconciliation and storage tasks), pump deadline/stop/lock state, active shot
and accounting lock, cycle transition lock/write, scheduler task/timer/pending
and running state. Cancelled/failed/unknown shot or cycle tasks do not count as
completed. It applies existing VWC freshness/quorum/spread policy, additionally
requires no rejected configured sensors, and requires configured fresh,
non-restored dry drain/tray states. The 30-second drain limit is development-only,
not an approved installation policy.

Guards bind boot UUID, generation, exact live snapshots, all options, entry data,
ownership, ledger projection, sensor policy and current cycle decision (including
schedule/day boundary). An aggregate digest of all HA states detects observed
state/configured-helper/automation changes; unrelated state payloads are not
persisted. A conservative state-change/HA-stop listener invalidates the generation
while preparing or holding a session, including observed transient state ABA.
No automatic action follows that listener.

Before and after preparation, participant-check and commit writes, the protocol
checks the synchronous guard and re-reads durable participants and the ledger,
then regenerates the plan. All fields except the newly generated diagnostic
plan ID/time must agree. Disk-only changes during the last commit write are
therefore rejected too. Immediately after each relevant awaited operation the
live generation/guard is checked before progressing or returning success.

These are observed-consistency checks, not atomic isolation. A Python listener
cannot exclude an external service call. Unobserved filesystem/configuration ABA,
privileged editing, concurrent processes, clock rollback and physical changes
remain outside the guarantee. Current unknown ownership epochs/revisions prevent
actual positive preparation. No production sensor/external-writer guarantee is
inferred from the isolated model or a local idle observation.

## Persistence format and crash behavior

HA `_LedgerStore`, version 1, atomic/private writes, key
`growassistant_crop_steering.recovery_transactions.<entry_id>`, contains:

```json
{
  "schema_version": 1,
  "protocol": "diagnostic_preparation_v1",
  "binding": {"entry_id": "entry", "entity_id": "switch.native_harness_pump", "registry_id": null, "device_id": null},
  "events": []
}
```

Each event has exact fields: UUID event/transaction IDs, consecutive integer
`sequence`, `previous_hash`, aware server `at`, `kind`, unique `request_id`,
normalized complete `request`, `payload`, and SHA-256 `hash` over all other fields.
`prepared` stores the entire plan, plan digest, exact current `cycle_id` and guard.
`participants_checked` stores exactly pump/shot/cycle/ledger/runtime observation
receipts. `committed` stores the plan digest and literal technical-only/false
activation flags. Negative events store a nonempty reason. Replay validates
schemas, hashes, sequence, references, request uniqueness, complete plan shape,
request/intent binding, exact current cycle and required receipts/state ordering.
No mutable projection or executable permit is saved.

| Crash/failure boundary | Effective result |
| --- | --- |
| Before intent save | No transaction, still locked; existing source gates unchanged |
| During/after intent | Old or prepared history; boot locks, no automatic continuation |
| During participant validation/save | Prepared or checked prefix; boot locks, missing proof never commits |
| Before technical commit | Checked history only; boot does not finalize |
| After commit/before or after final validation | Historical commit only; no session/permit restored |
| Storage exception, timeout, caller cancellation during save | `storage_uncertain` for entire boot; no younger write or negative audit may overtake it |
| Late successful write | May appear after restart; never installed late in live memory; historical retry only |
| Missing, malformed, unsupported or contradictory history | No automatic authority; malformed history preserved and owner blocked |

Writes run in retained background tasks. Caller timeout/cancellation never assumes
that executor IO stopped. Late exceptions are consumed; the owner registry and
uncertainty latch persist for that boot. Cancellation during a read/validation
latches a failure and leaves the last durable prefix, with no background write
unless one had already started. Missing files remain distinguishable only as
empty history, never as a clean/released system. HA corrupt remnants prevent
silent initialization; preflight avoids corrupt-file rename/migration.

HA atomically replaces **one JSON file**. Logical append-only/hash chaining is
neither WORM storage nor a signature/rollback defense: an external writer can
recompute hashes, truncate/restore a coherent backup or delete a file. There is
no external trusted head, multi-file transaction, fsync-order or additional
power-loss guarantee. Old and new complete files or corrupt data may be observed
after storage failures. An old process with outstanding IO must not overlap a new
HA process. Participant journals are read without their mutating HA loaders.

## Errors and later release prerequisites

`LedgerError.code` provides structured rejection reasons. These include
`transaction_busy`, `transaction_unavailable`, `resource_reserved`, `writer_exists`,
`request_invalid`, `request_conflict`, `revision_conflict`, `plan_invalid`,
`plan_digest_conflict`, `plan_blocked`, `plan_stale`, `source_changed`,
`drain_unresolved`, `sensor_unsafe`, `drain_unsafe`, `transaction_revoked`,
`transaction_incomplete`, `transaction_conflict`, `participant_proof_missing`,
`transaction_corrupt`, `transaction_schema_unknown`, `timeout_invalid` and storage
errors inherited from the strict ledger Store. Cancellation propagates; read IO
failures may propagate while latching the preparation/commit failure.

Later positive Cycle release still needs participant-owned durable recovery
receipts/revisions, admission/quiesce and ownership epochs, authenticated identity,
external-writer isolation, independent physical shutdown and sensor provenance,
resolved partial-delivery/budget dispositions, explicit cycle baseline decisions,
reviewed migration/supersession rules and a separate runtime release decision.
Only that separate work could define a boot-bound volatile permit and coordinated
gate evaluation. Nothing in this journal is such a permit.

## Validation

Results are recorded below after the final runs. New tests use only the existing
NativeHarness/FakePump with real HA dispatch forbidden. Barriers control concurrent
requests, every save boundary, validation cancellation, genuine newly registered
faults, disk-only changes, IO failures and late real HA executor writes. Restart
coverage reloads every complete persisted prefix into a fresh HA runtime. The
positive model fixture does not weaken any existing test/assertion or production
validator. Source checkpoints, counters, command lists and recovery gates remain
covered independently of the model's technical receipt.

Environment: Python 3.13.16, Home Assistant 2025.12.5, pytest 9.0.0,
Ruff 0.17.0; interpreter `/workspace/growassistant-cloud/venv/bin/python`.
The existing environment needed the repository CI requirements and HA fixture
installed. As in the prior phase, the fixture was installed with `--no-deps` to
retain the repository's HA pin. `pip check` reports only the documented fixture
metadata mismatch: fixture 0.13.301 requests HA 2025.12.4, whereas this repository
pins/tests 2025.12.5. No dependency files changed. Pytest used
`LITELLM_LOCAL_MODEL_COST_MAP=True` and the supported command network permission;
the initial restricted fetch could not reach the proxy, and the initial test
attempt identified the missing fixture before collection. Current main was
fetched again before PR creation and remained at `a0602cb`.

| Final check | Result |
| --- | --- |
| `python -m pytest -q tests/native_harness/test_recovery_transactions.py --tb=short -x` | **107 passed**, 5.96 s |
| `python -m pytest -q tests/native_harness --tb=short` | **326 passed**, 65.94 s |
| `python -m pytest -q --tb=short` | **1122 passed**, no failures/skips, 119.61 s; 1015 baseline + 107 new |
| `python -m ruff check custom_components tests scripts` | Passed |
| `python -m ruff format --check custom_components tests scripts` | Passed, 55 files |
| `python -m compileall -q custom_components tests scripts` | Passed |
| `git diff --cached --check` | Passed, including all three new files |

Final suites ran sequentially, without another pytest process. Existing test
files/assertions, production controllers and dependency pins are unchanged.
