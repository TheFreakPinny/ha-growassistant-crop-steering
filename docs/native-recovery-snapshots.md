# Native recovery evidence, stage one

This implements only step 1 of section 10 of
[native-recovery-release-design.md](native-recovery-release-design.md).
There is no RecoveryCoordinator, acknowledgment, reconciliation write, release
service, button, permit, scheduler start or new activation path. Existing gates,
loader behavior, journal formats, counters and accounting rules are unchanged.
`ValidationResult.native_activation_allowed` is an immutable, non-constructor
field fixed to `False`, including for `diagnostic_ready`.

## Internal contracts

`recovery_snapshot.py` defines frozen dataclasses for resource binding, schema /
revision evidence, task/service/write evidence, OFF evidence and the three
participant snapshots. `safety_snapshot()` is synchronous on the HA event loop;
it does not acquire a lock or await. It only reads controller fields and existing
HA state/configuration. The cycle snapshot evaluates the existing pure decision
function; it deliberately does not call `permits()` or `execution_available`,
which can latch recovery faults. Calling snapshots never loads/saves a Store,
dispatches a service, installs listeners or changes a journal.

Journal and options values are detached recursively into read-only mappings and
tuples. Existing shot/accounting/cycle records remain the domain model rather
than gaining a second parallel mutable representation. Missing fields remain
missing; unsupported non-JSON objects become `UnknownEvidence`, never `{}` or a
clean journal. `None` denotes unavailable evidence, not an affirmative proof.

- `ResourceBinding`: exact entry and entity IDs, optional registry/device IDs.
  Equality includes every binding field. Current participants supply the entry
  and configured entity only; stable registry/device binding remains unknown.
  Pump journal targets and shot `pump_entity` remain separately visible, so
  assignment changes cannot erase the old target. Shot operation identity comes
  from the existing operation-map key; an explicit conflicting `operation_id`
  is rejected. Older records do not persist their entry ID: this is reported as
  unknown even though the enclosing live controller is bound to an entry.
- `VersionEvidence`: snapshot format 1 and known Store adapter schema 1 are
  distinct from a durable participant revision. Current journals have **no
  monotone revision**, so snapshots report `revision=None`. The schema number
  describes the adapter, not a claim that arbitrary journal contents are valid.
  No revision is manufactured from a timestamp, generation or record count.
- `WorkEvidence`: kind, task status, service target/operation where observable,
  and whether a service still belongs to the timeout registry. Task states are
  `absent`, `pending`, `completed`, `cancelled`, `failed`, `unknown`.
  A completed timed-out service is still unresolved until reconciliation.
- `OffEvidence`: cached HA state, receipt/report time, restored marker, last
  successfully verified OFF-exchange report time, ordering evidence and separate
  independent physical confirmation. `after_commands=True` describes only the
  controller's observed work; it cannot exclude external writers.
- `Finding` / `ValidationResult`: explicit reason code, participant, explanation,
  unknown flag and required checks. `status` is `negative` if any adverse finding
  exists, `unknown` if all findings are unknown, otherwise `diagnostic_ready`.
  `diagnostic_ready` is not a capability or permission and is not used by gates.

`validate_pump`, `validate_shots`, `validate_cycle` and `validate_safety` are pure:
callers supply the expected binding, aware `now`, and a finite positive `max_age`
in seconds. They perform no clock reads or IO. The age is caller policy, not a
new production freshness default. They check bindings, task/write ambiguity,
OFF freshness/order, journal intents, partial delivery, operation identity,
accounting/checkpoint/counter conflicts and cycle phase/flags/reference/schedule.
The aggregate additionally compares participant options, cycle and counters.
Cycle baseline counters can precede later accounted shots; the aggregate uses
`native_counters` where present rather than treating the baseline as a live tally.
Known booked operations may not exceed the current counter; this does not claim
an atomic or fully reconstructed accounting proof.

## Available evidence and deliberate gaps

Pump work created through `_task` is observed from creation through its completion
callback, including activation, initialization, stop, close, persistence and late
service reconciliation. Service metadata is retained across timeout; failures
and cancellations of background work leave conservative diagnostic summaries.
This observation changes no existing gate and has no clear/reset API. Pump
pending writes and shot/cycle write handles are visible, including completed
failed/cancelled handles still retained by the existing controller. Shot task,
accounting lock, cycle lock and pending transition intent are copied as well.
Locks indicate observed work, not externally enforceable ownership.

The existing successful pump shutdown path records its already-observed fresh
OFF report **in memory only** after durable journal clear. Each subsequent
service dispatch invalidates it. An empty journal, initial HA OFF state,
`IDLE`, or a successful `async_stop()` with no record does not create this proof.
A late unsolicited OFF report after failed confirmation does not create it either.
Changing the configured entity makes old OFF evidence unusable. A newer report
must not silently inherit an older exchange's verification. Restart restores no
OFF verification, task history, registry identity, ownership epoch or permission.

Still unavailable: physical switch/pump/flow feedback; trustworthy source
freshness beyond HA receipt time; independent shutdown verification; authoritative
external-writer isolation; durable participant/options revisions; a shared
transaction/commit; scheduler quiescence/drain proof; a new recovery-specific
sensor/drain policy; authenticated operation dispositions. Underlying executor
writes can outlive their Python await. A completed Python task proves only that
observable await's outcome, not fsync, global atomicity or termination of remote
hardware work. Nothing in this API upgrades those guarantees.

Therefore current live snapshots cannot pass all prerequisites. The aggregate
always includes `required_check_unknown` for participants outside this stage.
Synthetic unit evidence can demonstrate an individual validator's diagnostic
success, but no live state or test result grants recovery or production access.
FakePump reports are a simulation and prove no safety of real equipment. No
new hardware adapter or real HA service dispatch is used by the added tests.

## Reason codes

| Code | Meaning |
| --- | --- |
| `binding_unknown` | Required entry/target or persisted per-operation binding absent. |
| `binding_mismatch` | Exact expected resource, OFF binding or shot target differs. |
| `actor_identity_unknown` | Stable registry/device identity unavailable. |
| `schema_unknown` | Missing/unsupported snapshot or Store schema. |
| `revision_unknown` | Missing, invalid or non-monotone-compatible participant revision. |
| `snapshot_time_invalid` | Invalid age policy, naive, future or expired capture time. |
| `off_missing` | OFF state or verified OFF-exchange report absent. |
| `off_stale` | OFF timestamp naive, future or older than caller's maximum age. |
| `off_restored` | Restored or unknown-origin report cannot establish fresh OFF. |
| `off_order_unknown` | OFF after commands unproven, or current report differs from verified exchange. |
| `physical_off_unknown` | Independent physical shutdown proof absent. |
| `command_unresolved` | Pending, failed, cancelled, unknown or timed-out pump command. |
| `task_unresolved` | Other task, lock, stop request or deadline remains unresolved. |
| `storage_unresolved` | Observed write pending, failed, cancelled or unknown. |
| `pump_unresolved` | Non-IDLE lifecycle, retained record or restart review. |
| `accounting_inconsistent` | Duplicate/conflicting operation identity, pending/missing booking, malformed or mismatched checkpoint/counters, or participant disagreement. |
| `shot_unresolved` | Unknown or nonterminal operation state. |
| `partial_delivery` | Possible or unknown delivery/uncertainty remains. No amount is inferred. |
| `cycle_inconsistent` | Missing/invalid cycle, phase, flags, reference provenance, counters, schedule or pending transition. |
| `recovery_open` | Existing participant reason, recovery marker, interrupted/faulted operation or uninitialized/blocked participant remains. Details retain the original reason. |
| `required_check_unknown` | Required scheduler, sensor/drain, ownership/legacy participant proof is not supplied by stage one. |

Unknown findings block diagnostic readiness just like negative findings. No code
means "ignore this gate", and a successful OFF exchange clears no shot/cycle
review. There is no repair, compensation or counter mutation in a validator.

## Later RecoveryCoordinator connection

A later coordinator may collect these snapshots for `inspect_case`, present
findings and required checks, and bind subsequent plans to actual durable
revisions and resource/boot/ownership epochs once those contracts exist. Snapshot
collection is not a cross-store transaction or a TOCTOU guarantee. It must then
quiesce/drain all participants, validate again under the designed serialization,
and preserve existing gates. These diagnostic results must never become a cached
permit. Ledger, acknowledgment, disposition, commit protocol and release are
separate future changes described in the design.

## Validation

Environment: Python 3.13.16, installed HA 2025.12.5. The documented fixture package
`pytest-homeassistant-custom-component==0.13.301` and timeout plugin are absent.
Installing the fixture with `uv pip install --no-deps` failed because the managed
network/proxy cannot reach PyPI; shell Git fetch also failed at the proxy. Current
main after #92 was read through the GitHub connector; only its four changed files
since the local checkout were synchronized for validation.

Added: **49 pure contract cases and 11 isolated FakePump harness cases**, giving
an expected **901 tests** from the documented 841 baseline. This is an expected
count, not a successful full collection/run.

- Pure contract suite: **49 passed**.
- Available tests excluding the four HA-fixture directories: **397 passed**
  (includes the 49 new contract tests).
- Targeted FakePump snapshots and complete native harness: blocked during
  conftest import by the missing fixture package; not executed.
- Full pytest: stopped with four collection errors for `lifecycle`,
  `native_harness`, `pump`, `shot_engine`, all due to the missing fixture package.
- Ruff lint and format check, Python compilation, Git whitespace: passed.

The reduced run reports the missing timeout-plugin configuration warning and an
existing HA aiohttp inheritance deprecation. Before merge, CI must run all 901
expected tests with the documented dependency pins. Event barriers and explicit
zero timeouts drive the new IO races; no new fixed sleeps were introduced.
