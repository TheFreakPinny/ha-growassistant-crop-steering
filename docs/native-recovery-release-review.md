# Native recovery and manual release review

Reviewed against main `7fb057385d14bd9c5ea2aa72dc5c5942f364eff6`.
Only isolated tests and this report are changed. No production irrigation logic,
activation gates or existing assertions are changed.

## Current mechanisms and their boundaries

| Mechanism | Existing behavior | What it does not authorize |
| --- | --- | --- |
| Pump reconciliation (`pump.py`, `async_initialize`, `async_stop`) | Loads the persisted target, dispatches stop, requires a fresh observed off and durable journal clear; unresolved or invalid targets remain faults. `restart_had_recovery` survives reconciliation in that controller and prevents activation. | Clearing the pump journal does not clear shot uncertainty, cycle review or ownership. A software off report is not physical verification. |
| Shot recovery (`shot_engine.py`, `async_initialize`, `async_execute`) | Persists operation identity, activation/delivery uncertainty, shutdown confirmation and accounting intent. Restored nonterminal/pending operations become interrupted and require review. Interrupted/fault records retain review requirements; inconsistent accounting also blocks. | There is no supported review/acknowledgment method that resolves these records and releases `_blocked`. No replay, replacement shot or inferred delivered volume is authorized. |
| Recovery assessment (`native_safety.py`, `assess_recovery`) | Combines pump, shot, accounting, storage, cycle and sensor reasons into read-only diagnostics. | `review_required=False` is not permission: `native_activation_allowed` defaults to and remains `False`. |
| Native cycle (`cycle.py`, `async_initialize`, `permits`, `async_observe`) | Loads phase, flags, counters, reference and pending transitions. Even consistent stored cycles get `cycle_restart_requires_review`; mismatches, missed sunrise and partial transitions block. Observation of an interrupted shot blocks the cycle. | No supported cycle recovery acknowledgment clears this reason. Sunrise/reset cannot erase unresolved shot evidence. |
| Ownership (`native_safety.py`, `OwnershipEvidence`) | Requires the exact pump, relinquished legacy control, reviewed writers, verified exclusivity and a simulated pump. Harness startup supplies local evidence only for its fixed fake; restart does not restore it. | Persisted acknowledgment cannot establish ownership. Production ownership cannot be proved by these development fixtures. Fake ownership alone cannot clear recovery. |
| Migration/rollback checklist (`migration_eligibility`) | Reports missing recovery, sensor, off, state and ownership prerequisites. `review_eligible` may be true for a clean simulated fixture. | Pure read-only checklist, always returns `native_activation_allowed=False`, including rollback. Not a release API or acknowledgment. |
| Development scheduler (`scheduler.py`) | Interrupted/failed attempts latch `retry_blocked`. Stop/start preserves the latch and cycle/shot blocks; new healthy reports do not retry. | Restarting a scheduler does not approve recovery. A fresh scheduler still encounters restored shot/cycle gates. |

`execution_available` is an internal simulated-execution readiness hint, distinct
from production `native_activation_allowed`. Clean harness shots can execute with
explicit fake ownership while production activation remains prohibited. Normal
integration setup does not construct/start these development controllers. Legacy
helper/reset and stop services are not a native recovery release procedure.

## Added isolated safety tests

All new cases are in `tests/native_harness/test_simulation.py`, using the existing
`simulation` and `restarted_hass` fixtures. The fixture forbids the underlying HA
service dispatcher; `FakePump` accepts only the fixed virtual pump. Persistence
uses deep-copy memory stores; no test edits a saved journal to obtain clearance.

| Test | Evidence |
| --- | --- |
| `test_interrupted_shot_stays_locked_after_healthy_sensors_and_off` | A running P1 shot is interrupted by a sensor fault and remains locked immediately, after healthy sensor reports, and after confirmed stop with an empty pump journal. Fake ownership remains valid, isolating recovery as the blocker. |
| `test_terminal_interruption_survives_fresh_runtime_and_fake_ownership` | Restores an actual terminal interruption after pump reconciliation into a separate HA runtime. Checks retained operation evidence, cycle review, absent restored ownership and continued blocking even after explicit fake ownership is supplied. |
| `test_scheduler_stop_start_cannot_acknowledge_interruption` | Real scheduler interruption, healthy readings and stop/start cannot remove the retry latch, alter shot records, count the failed shot or dispatch another activation. |
| `test_review_checklist_cannot_release_interrupted_shot` (8 variants) | Missing off verification, state reconciliation or ownership, wrong target, unresolved legacy control/writers, production target, and even complete asserted prerequisites all remain ineligible with interrupted evidence. Checks both migration and rollback, no persistence mutation, rejected duplicate operation and rejected replacement. |
| `test_clean_review_eligibility_is_not_native_activation` | Honest clean fixture with full simulated checklist can be review-eligible but still cannot obtain native activation permission; no pump command or journal mutation occurs. |

The shared assertions verify zero successful accounting, exactly one uncertain
reservation, unchanged persisted operations on rejected replay/replacement, and
continued recovery reasons. Existing crash tests additionally cover restart from
RUNNING with both successful and missing off confirmation. This adds coverage for
restart *after* successful pump shutdown, when only shot/cycle evidence remains.

## Why there is no positive release test yet

No fully supported manual release mechanism exists. The tests therefore exercise
the existing diagnostic checklist and rejected execution paths; they do not
invent a release service or fabricate a cleared journal. A later positive test
needs an implemented and reviewed procedure providing:

1. Explicit operator review of the exact interrupted operation and uncertain
   delivery, with an auditable durable disposition that preserves history and
   prevents replay, replacement and double accounting.
2. Consistent reconciliation of pump, shot accounting, phase/counters, P2
   reference, pending transitions and missed cycle resets, with crash-safe writes
   and handling of failed/partial acknowledgment.
3. Fresh sensor and drain/tray safety checks, verified pump off with no unresolved
   service tasks or storage writes, and verified exclusive ownership/legacy
   handover for the actual target; all revalidated at release time.
4. An explicit supported acknowledgment/release operation defining how the pump
   restart block, shot block, cycle review and scheduler latch are coordinated,
   authorized and persisted across restart without deleting safety evidence.
5. A deliberately implemented production activation decision; today the diagnostic
   field is unconditionally false, even for an otherwise complete checklist.

These are missing procedure/implementation requirements, not instructions to
manually modify journals or private controller flags. The current software tests
prove retention of the modeled gates, not physical hardware safety or a working
positive recovery path.

## Validation

Environment: Python 3.13.16, Home Assistant 2025.12.5,
`pytest-homeassistant-custom-component==0.13.301`, dependencies from
`.github/requirements-ci.txt`. The prepared virtual environment initially lacked
the HA fixture package and supporting dependencies; these were installed using
the repository's documented pins. `pip check` reports only the documented fixture
metadata exception (fixture pins HA 2025.12.4, repository uses HA 2025.12.5).
The restricted sandbox stalled fixture setup; rerunning with the execution
environment's network permission enabled succeeded. The original HA dispatcher
remained forbidden by the fixture throughout; no real service or pump was used.

Commands run with `LITELLM_LOCAL_MODEL_COST_MAP=True` for pytest:

| Check | Result |
| --- | --- |
| New tests selected with `pytest -q tests/native_harness/test_simulation.py -k 'interrupted_shot_stays or terminal_interruption or scheduler_stop_start or review_checklist or clean_review'` | 12 passed, 60 deselected |
| `python -m pytest -q tests/native_harness` | 94 passed |
| `python -m pytest -q` | 841 passed, no failures or skips (106.98 s) |
| `python -m ruff check custom_components tests scripts` | Passed |
| `python -m ruff format --check custom_components tests scripts` | Passed (46 files) |
| `python -m compileall -q custom_components tests scripts` | Passed |
| `git diff --check` | Passed |
