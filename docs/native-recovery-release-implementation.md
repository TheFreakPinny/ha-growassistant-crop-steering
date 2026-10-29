# Explicit internal cycle-release decisions: conservative phase five

Basis: main after PR #96, `0a03841199ae8c6143710997b53712793df3b07a`.
See [design sections 4–10](native-recovery-release-design.md),
[ledger](native-recovery-ledger.md), [plans](native-recovery-plans.md) and
[transactions](native-recovery-transactions.md).

This implements the explicitly permitted **safe refusal contract**, not positive
cycle release. The actual participants cannot yet coordinate gate resolution.
There is no permit issuer. Every result has `released=False`,
`historical_only=True`, and `native_activation_allowed=False`. No production
setup imports this component. Existing controller behavior, public HA services,
participant journals, history, accounting and gates remain unchanged. Recovery
issues no pump command, scheduler start, replay, replacement or catch-up shot.

## Evidence audit before implementation

Actual controller/ledger data provide exact resource and operation identities,
operation delivery/activation history, persisted reviews, counter/checkpoint and
cycle observations, pending tasks/writes, current sensor assessment and local
ownership assertions. They support detecting contradictions and unknowns. A
review records a decision about evidence; it cannot manufacture missing evidence.

The actual planner always retains unresolved checks, including durable participant
revisions, ownership epochs and external writers. The transaction coordinator
stores `read_only_observation` receipts in its own Store; these are **not**
participant-owned reconciliation votes. Its committed event is technical only.
The PR #96 modeled eligible-plan tests are not evidence of safe recovery, and are
not reused by these new tests. New tests never replace findings, open checks,
private blocked/recovery flags or persisted operation records.

| Gate | Actual evidence | Missing coordinated resolution contract |
| --- | --- | --- |
| Pump reconciliation | Recovery record, OFF observation, outstanding services/tasks | Fresh isolated physical OFF proof bound to command generation and participant receipt |
| Shot review/disposition | Ledger review, immutable operation identity/delivery | Participant validation of safe disposition without erasing historical fault |
| Cycle | Phase/flags/schedule/reference and baseline snapshot | Cycle-owned reconciliation receipt and explicit baseline decision |
| Accounting | Counters, pending intents, checkpoints, operation records | Revisioned idempotent reconciliation agreement across options and journals |
| Scheduler retry | Local task/timer/drain observations; retry latch remains | Admission/quiesce generation and reviewed retry resolution that leaves scheduler stopped |
| Sensors, drain/tray | Existing assessment and live states | Installation policy/provenance plus final coordinated safety generation |
| Ownership/legacy | Simulated ownership assertion and local owner registries | Shared revocable epoch and enforced isolation of every writer |
| Persistence/tasks | Store reads and tracked tasks/writes | Durable participant acknowledgments, serialized admission and shared revisions |

A true positive end-to-end path requires **participant-owned gate resolution**:
versioned pump/shot/cycle/accounting/scheduler acknowledgments for the same plan,
shared command/admission and safety generations, a fresh isolated FakePump
ownership epoch and a final synchronous validator. The planner must derive all
proofs from these contracts. Only then could it clear its own derived findings
and issue eligibility. That work crosses current controller boundaries and is not
implemented by externally assigning private flags. Real hardware additionally
needs independent physical OFF/source freshness evidence, effective external-writer
exclusion and a separately reviewed activation/authentication policy.

## Actor and request contract

`_CycleReleaseCoordinator` is internal and reserves one owner per pump for the HA
lifetime. It shares `_RecoveryTransactionCoordinator`'s non-queuing reservation:
Recovery before Cycle before Accounting; this implementation never acquires the
participant locks. Revocation never waits for that reservation.

Trusted isolated harness setup constructs `_HarnessReleaseAuthority`, then issues
an in-memory identity capability with actor/context identity and a new boot UUID.
Authentication checks exact object identity against that issuer's issued objects.
Equal reconstructed values, another issuer's objects, strings, `None`, and
`TrustedReviewContext` are rejected. This is an internal process trust boundary,
**not public HA authentication**, not a credential and not a production role.
Nothing exposes the issuer or its capabilities through services or persistent data.
Privileged code inside this process is trusted and is outside this threat model.

`async_request` requires a separate request ID, transaction ID, full plan identity
and digest, exact entry/pump binding and cycle, expected ledger and transaction
revisions, boot binding, requested ownership epoch, `release` or `deny`, and a
nonempty rationale. Actor and revisions are checked, the actual planner is rerun,
and observations must equal the submitted plan. Plan ID/time remain identity
bindings but regenerated diagnostic ID/time are excluded from observation equality.
A requested ownership epoch is **not an authenticated participant epoch**. Its
absence blocks request validation; its presence cannot make a release possible.
For a blocked plan the transaction ID is only an audit claim; only an eligible
plan would reach the committed-transaction identity check. No claimed ID creates
an actual transaction or resolves another case.

Invalid/untrusted/stale requests raise `LedgerError` and do not write an audit
success. Valid negative decisions persist one chained refusal receipt with reason
`operator_denied`, `plan_blocked`, `transaction_incomplete`, or
`participant_owned_gate_resolution_missing`. The final two branches specify future
refusal behavior; actual current planner evidence reaches `plan_blocked`.
There is intentionally no `released` branch. A second explicit decision never
substitutes for reconciliation.

## State and permit lifetime

The effective state is always `locked`; decision audit history is distinct from
permission. No automatic state transition leaves locked. Duplicate completed IDs
with identical content return the original historical refusal without another
write; conflicting content raises `request_conflict`. In-flight requests share the
transaction busy reservation and are rejected rather than queued.

`_CycleReleasePermit` declares the future volatile value shape: boot ID, ownership
epoch, entry, unchanged target, cycle, plan and commit IDs, ledger/transaction and
journal revisions, safety generation. **No code constructs, persists, restores or
consumes it.** `permit` is a read-only property returning `None`. Thus sensor,
ownership, target, commands, open writes, cycle boundaries and revocation cannot
leave stale authority in this implementation. No production pump/scheduler
permission can be inferred from this type or a technical commit.

A future issuer needs synchronous revocation on every relevant change, final
live validation after the last await and before publication, and validation before
every later use. Those are prerequisites, not guarantees demonstrated here. Tests
of sensor/ownership/target/stop changes during refusal audit prove continued
refusal only; they do not prove positive permit revocation or final-commit safety.

## Persistence and crashes

The private atomic Store `growassistant_crop_steering.cycle_release.<entry_id>`
contains schema 1, exact binding and a list of refusal events. Each event binds its
sequence, previous hash, normalized full request and negative reason with a digest.
Replay validates the schema, resource, request shape, unique request IDs, chain
and allowed negative reasons. Hashes are consistency checks, not signatures,
rollback resistance or a multi-store transaction. Actor data in history is audit
identity, never a restored capability. The Store contains no permit.

Initialization takes the same reservation, validates history, and loads only
negative audit data. A fresh HA runtime has a fresh authority; old actor objects
are rejected. Even a historical diagnostic transaction commit never restores a
session/permit. A nonempty restarted transaction remains independently locked by
its existing loader. There is no automatic finalization or supersession.

Cancellation, exception or timeout during an audit save latches this coordinator
unavailable and synchronously revokes the transaction. The retained background
write may finish late; no younger request can overtake it and no late completion
installs history or authority. Invalid loads similarly latch unavailable. A
runtime change during a completed refusal write may still return its durable
**negative** receipt: it never reports release. A crash before/during/after that
write leaves absent or historical refusal data and no permit. HA's single-file
atomic replacement does not establish multi-file/power-loss guarantees. There
is no durable rejection guarantee when the audit medium itself fails.

## Partial delivery and test scope

Tests induce a real interrupted P1 shot through NativeHarness/FakePump, register
its actual ledger case, and acknowledge it using the existing review contract.
The actual planner still reports `partial_delivery_unresolved`. Unknown volume
stays unknown; the affected cycle remains locked, operations remain consumed and
no success is booked again. A later cycle requires separate agronomic effects
review, documented disposition and validated new baseline; no such path is added.

The new suite covers absent second decision, actual blocked evidence, untrusted/
cloned/foreign actors, invalid bindings/revisions/decisions, actual changed plan
sources, identical/conflicting retries, explicit denial, concurrent requests,
changes at deterministic audit-write barriers, cancellation, failure, timeout and
late writes, negative-history corruption, and fresh-runtime restarts before/after
a decision. Real HA service dispatch is forbidden by the existing fixture.
All existing assertions remain unchanged. Source checkpoints, commands, scheduler
state and replay/accounting invariants are checked independently.

Positive permit publication, simultaneous positive winners, sensor failure during
a positive final commit, positive permit revocation and restart after positive
release cannot be exercised: no positive issuer exists. PR #96's transaction race
and crash-prefix tests remain intact but establish only diagnostic protocol safety.
These explicit limits avoid mistaking refusal tests for full recovery validation.

## Validation

Final sequential runs on Python 3.13.16, Home Assistant 2025.12.5, pytest
9.0.0 and Ruff 0.17.0, using `/workspace/growassistant-cloud/venv/bin/python`
and `LITELLM_LOCAL_MODEL_COST_MAP=True`:

| Check | Result |
| --- | --- |
| `python -m pytest -q tests/native_harness/test_recovery_release.py --tb=short` | **33 passed**, 1.55 s |
| `python -m pytest -q tests/native_harness --tb=short` | **359 passed**, 65.70 s |
| `python -m pytest -q --tb=short` | **1155 passed**, no failures/skips, 119.01 s |
| `python -m ruff check custom_components tests scripts` | Passed |
| `python -m ruff format --check custom_components tests scripts` | Passed, 57 files |
| `python -m compileall -q custom_components tests scripts` | Passed |
| `git diff --cached --check` | Passed, all three new files included |

The new total is **1122 baseline + 33 new = 1155**. Existing tests/assertions and
requirements are unchanged. CI requirements and the fixture were installed into
the existing isolated venv, retaining the repository HA pin. `pip check` reports
only the previously documented metadata mismatch: fixture 0.13.301 requests HA
2025.12.4; this repository pins/tests 2025.12.5.

The initial restricted-network test attempt timed out in HA fixture setup, and
one intermediate test used forbidden direct ConfigEntry assignment. Tests now
change entry data through HA's supported `async_update_entry` contract. Final
suites ran sequentially with supported command network permission, without another
pytest process, and all passed. The final `git fetch origin main` confirmed the
base remained `0a03841199ae8c6143710997b53712793df3b07a`.
