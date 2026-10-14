# Phase 3B: native safety and migration preparation

Development only. Normal integration setup constructs none of the native pump,
shot, scheduler or cycle controllers. There is no production activation setting,
service, button, migration UI, rollback UI or fault-clear API. The Blueprint and
its existing sensor averaging and automation behavior are unchanged. All execution
validation uses local HA fixtures and simulated pumps. This is preparation for a
separately reviewed activation project, not permission to migrate a live system.

## Safety changes and scope

- Both coordinator and PumpController reject missing, uncertain, wrong-target or
  legacy-conflicting ownership evidence. The private evidence object admits only
  explicitly simulated pumps in this phase, with all review flags exactly true.
  It is never persisted, discovered from automations, inferred from pump off or
  provided by integration setup. Reinitialization cannot restore authorization.
- Pump initialization still attempts off against a retained recovery target and
  clears the actuator record only after verified shutdown and durable persistence.
  It retains a restart observation; coordinator review is latched in the journal
  even after pump reconciliation. Direct controller activation also rejects this
  restart observation. Confirming off resolves shutdown, not accounting/delivery.
- Completed, committed, matching shot checkpoints can yield `clean_restart`.
  A matching committed cycle still has `cycle_restart_requires_review`: this is a
  clean restart with mandatory review, not permission to resume. Other safety
  causes produce `unsafe_recovery`. No scheduler starts on initialization; no
  missed or interrupted shot is replayed, and no accounting is replayed.
- Historical interrupted/faulted records require review even if an older journal
  omitted its recovery flag. Nonterminal records retain `restart_original_state`
  and their original configuration/timestamps before being marked interrupted.
  Completed records without affirmative accounting block as incomplete accounting.
- An accounting checkpoint from a different sunrise blocks even if all counters
  happen to be zero. Missed sunrise/reset is not inferred as a harmless new cycle.
  Existing UTC cycle identities, civil-time/DST validation and no catch-up policy
  remain. Ambiguous/nonexistent local boundaries fail closed; no DST guessing.

## Private sensor policy

`SensorPolicy` in `native_safety.py` is a private harness dependency, not a config
entry option or user-facing setting. Defaults are **300 seconds maximum age**, at
least **one** valid sensor, **100%** of distinct configured sensors valid, and at
most **10 VWC percentage points** between the highest and lowest valid reading.
These deliberately conservative provisional defaults need agronomic and source
validation before a future activation project. A harness may explicitly configure
stricter/looser quorum or age/spread; malformed, non-finite or nonsensical policy
values fail closed. Changing policy or ownership during a reservation interrupts.

Availability, age and timestamps are evaluated per sensor. Missing, unknown,
unavailable, restored, future-dated, naive-timestamp or stale readings are excluded.
Non-numeric, non-finite or outside 0–100 readings block even if other peers satisfy
quorum. Duplicate entity IDs cannot manufacture quorum. Count **and** percentage
must pass. Arithmetic averaging applies only to validated peers; there is no
median, weighting or automatic replacement of a failed sensor. An explicit lower
quorum permits partial availability, but never corrupted readings or excessive
spread. The complete rejection map is diagnostic even when quorum passes.

Reservation, final pre-dispatch validation, periodic active-shot supervision and
state-event fault latching use the same policy. Event assessment uses the actual
reported state, so a transient disagreement or invalid reading followed rapidly
by a valid reading still interrupts. Cycle reference capture also uses validated
peers and rechecks safety after its persistence await. An active shot reaching
field capacity still finishes its bounded duration; independent sensor faults
continue to interrupt.

Age uses HA `last_updated`, never coordinator startup time or `last_reported`.
An unchanged value reported later does not make an old value fresh. A restored
value with a new HA timestamp is still excluded. This may conservatively mark a
stable, repeatedly reported reading stale; do not circumvent it by relabeling
historical samples. **HA timestamps establish receipt, not physical sample time.**
HA cannot detect an integration that republishes historical data as a new live
reading without source metadata. Source acquisition timestamps, device heartbeat
and restart semantics must be independently verified before production. No code
here silently substitutes startup time for missing/historical timestamps.

## Delivery and attempted-water budgets

Each operation retains its reservation, selected phase/cycle, duration, validated
readings, configured values, timestamps, software accounting and shutdown status.
New records add `delivery`, `delivery_uncertain`, `delivered_volume`, `outcome`,
`activation_requested_at`, `activation_confirmed_at`, `shutdown_confirmed` and
`sensor_validation`. Policy thresholds and ownership review evidence are also
retained for diagnostics, but persisted ownership evidence never authorizes a restart. History and operation ID replay protection are never pruned,
including across sunrise reset.

| Observation | Success counter | Delivery classification | Next attempt |
| --- | --- | --- | --- |
| Reservation rejected before activation intent | No | `not_requested` | Fresh full authorization required |
| Failed activation after durable intent | No | `possible_partial`; no measured volume | Blocked for review |
| Confirmed on, interrupted, confirmed off | No | `possible_partial` | Blocked for review |
| Shutdown unconfirmed | No | `possible_partial`, `unconfirmed_shutdown` | Blocked for review and verified off |
| Bounded execution, confirmed off, committed accounting | Yes | `software_completed`; no measured volume | Existing soak, phase and shot limits still apply |
| Accounting/storage failure or compensation | No confirmed successful result | `possible_partial`, accounting/storage fault | Blocked for review |

A durable activation intent is conservative: failure before actual service dispatch
may still be marked uncertain. This avoids assuming zero delivery when an on call
may have reached the device. There is no automatic replacement, retry, replay,
last-shot backdating or automatic expiry of the review block. Failed attempts never
increment successful-shot counters. Successful accounting retains its existing
reset lock, generation fencing, intent-before-options and compensation checks.

`delivery_assessment` reports reservations, successful shots, uncertain attempts
and replacement blocking separately; `delivered_volume` is always null. The water
budget for an uncertain attempt is **all further native attempts blocked**, rather
than a guessed partial-shot allowance. Existing success-count phase budgets remain;
this is not a calibrated liters-per-day ledger. Flow, pressure, pump power or an
independent delivery meter is needed to quantify water. On/off states cannot prove
flow, delivery, pump failure, siphoning or valve closure. A confirmed off software
state cannot establish physical pump-off. History reconciliation must retain this
uncertainty rather than assigning invented volume.

## Machine-readable recovery and review

The private coordinator exposes `recovery_assessment.diagnostic()`,
`sensor_assessment().diagnostic()` and `delivery_assessment`. Pump faults additionally
have `recovery_reasons`. These diagnostics do not clear faults, mutate ownership or
authorize activation. `migration_eligibility()` is a read-only checklist for both
future migration and rollback review. Even when `review_eligible` is true,
`native_activation_allowed` is always false in Phase 3B.

Example after an interrupted shot with possible delivery:

```json
{
  "restart": "unsafe_recovery",
  "reasons": ["interrupted_shot", "possible_partial_delivery", "shot_review_required"],
  "review_required": true,
  "native_activation_allowed": false
}
```

| Reason | Required investigation/evidence before any future repair |
| --- | --- |
| `interrupted_shot`, `possible_partial_delivery`, `shot_review_required` | Retain the full journal and operation ID; identify interruption, inspect actual delivery independently, review remaining water budget; never replay it. |
| `unconfirmed_pump_shutdown` | Isolate execution authority, inspect original target and pending service calls, confirm off independently, resolve every late on/off call and durable write. |
| `pump_shutdown_reconciled_after_restart` | Off was reconciled, but the interrupted attempt and accounting still need review; do not erase this history. |
| `incomplete_accounting`, `storage_inconsistency` | Compare Store records, pending intents, checkpoint and restored config-entry options; wait for unresolved writes; preserve backups and diagnose the underlying storage failure. |
| `incomplete_cycle_transition`, `cycle_persistence_inconsistent` | Compare flags/reference/counters/schedule and reference cycle; identify whether an intent or options write committed; never synthesize field capacity. |
| `cycle_restart_requires_review` | Clean persisted cycle still requires schedule/state review and future explicit authorization; never auto-start. |
| `missed_sunrise_or_cycle_reset` | Inspect UTC sunrise identity, timezone/DST and reset marker; reconcile counters without catch-up watering or last-shot backdating. |
| `sensor_unavailability`, `sensor_quorum_failed`, `sensor_disagreement`, `invalid_vwc`, `vwc_unavailable`, `invalid_sensor_policy` | Inspect per-sensor rejection map, acquisition time, calibration and configuration; verify genuinely fresh samples/quorum; investigate disagreement rather than averaging it away. |
| `pump_ownership_unknown`, `pump_ownership_unverified`, `blueprint_ownership_unresolved`, `pump_ownership_target_changed` | Inspect all known writers, require explicit relinquishment and independent exclusive-control evidence; reject unresolved ownership. |

Cycle reasons such as options/schedule/reference drift remain machine-readable
alongside these categories. No automatic acknowledgment, success counter rewrite,
record deletion or fault-clear endpoint is provided. Review must identify and verify
the cause, not merely obtain acknowledgment that someone wants watering resumed.

## Pump ownership limitation

Inventory known Blueprint instances, automations, scripts, scenes, schedules,
manual controls, remote clients, MQTT/device writers and other HA instances. Entity
registry and automation inspection cannot prove completeness: dynamic templates,
external systems and future edits can introduce writers that HA does not discover.
Do not automatically disable any user automation. Do not infer ownership from
absence of a discovered Blueprint, a Boolean acknowledgment or observed pump off.

The in-memory evidence object is a development checklist, not cryptographic or
physical fencing. A harness explicitly asserting `simulated_pump` is responsible
for ensuring services are actually simulated; this flag cannot authenticate hardware.
No production path supplies it. HA cannot enforce exclusive external ownership.
For future production use, require an **independent hardware or gateway control**
that exclusively accepts the selected owner's commands, uses expiring ownership
leases/fencing tokens, fails off on lease loss and prevents old legacy credentials
from issuing commands. Add an independent maximum-runtime watchdog and physical
shutdown verification. If ownership cannot be demonstrated and continuously
maintained, native migration is ineligible.

## Future migration procedure (not implemented or executed)

1. **Inspect legacy execution.** Export/back up HA configuration, Blueprint inputs,
   automation/script definitions, pump assignment, schedules and current journal/
   options. Inventory related writers and unresolved service calls. Record unknown
   writers as a blocker; do not assume discovery is exhaustive.
2. **Safely stop existing execution.** Keep native inactive. In a separately
   authorized migration window, have the operator stop legacy scheduling and its
   current execution through the known owner's stop mechanism. No automatic
   disabling of user automations; no native activation while legacy might restart.
3. **Verify pump off.** Confirm the original pump target independently, including
   physical/gateway evidence and fresh HA off. Drain pending/late activation calls.
   Do not proceed with uncertain shutdown or stale state acknowledgments.
4. **Reconcile state.** Review counters, last-shot timestamp/type, sunrise/reset
   marker, P1 active/done/opened, P2 reference and reference cycle, schedule/timezone,
   shot/cycle intents and storage checkpoints. Retain attempted/partial-shot history
   separately from successes. Do not import legacy values as unquestioned native
   authority or backdate last-shot to bypass soak. Resolve storage errors first.
5. **Establish exclusive native ownership.** Obtain explicit acknowledgment naming
   pump target, relinquished Blueprint/other known writers and verification evidence.
   Enforce exclusivity independently at hardware/gateway level and revoke old paths.
   Unknown or unenforceable ownership blocks this step. Do not change the Blueprint
   in Phase 3B or silently modify user automations in a future migration.
6. **Validate sensors and recovery.** Confirm physical acquisition timestamps,
   calibrated VWC, quorum/spread, drains/tray, valid unambiguous DST schedule and
   a resolved review assessment. Retain all diagnostics and repair audit evidence.
7. **Explicitly authorize activation.** A future separately reviewed project must
   provide an explicit operator authorization tied to verified target, ownership,
   recovery evidence and bounded test plan. Phase 3B exposes no such UI/API; its
   checklist always prohibits native activation. Restart cannot restore permission.
8. **Monitor controlled test shots.** Begin with supervised, bounded shots under
   the independent watchdog, verify on/off, measured delivery, sensor responses,
   counters and committed journal. Verify the approved P1 capacity rule: capacity
   during a shot does not shorten it; additional P1 shots are blocked. Stop on any
   uncertainty; do not replace an uncertain shot automatically.
9. **Rollback and recover.** Stop native requests/scheduling immediately, await
   execution shutdown, independently verify off and resolve pending writes/calls.
   Snapshot all native records; reconcile partial delivery, counters, timestamps,
   phase/reference and reset marker before transferring ownership. Revoke native
   gateway lease/credentials before explicitly restoring legacy ownership. Legacy
   must also respect existing soak/budget and uncertain delivery; never replay a
   missed/interrupted shot. Post-rollback verify native remains inactive, legacy is
   the sole allowed owner, physical off/watchdog work, sensors are fresh, journal
   discrepancies resolved and sunrise/DST/reset behavior is valid. A failed off or
   unresolved delivery/accounting means **remain stopped**, not restore watering.

The migration and rollback checklist cannot carry out these operations or authorize
physical hardware. Review eligibility is a prerequisite for future human-controlled
work, not evidence that that work has already occurred.

## P1 transition safety review

The normal **wet drain** remains the documented Blueprint-compatible automatic
P1 completion trigger, separate from field capacity and successful P1 shot-budget
exhaustion. Persisted `completion_reasons` distinguishes `drain_sensor_wet`,
`field_capacity_reached`, `shot_limit_reached` and private manual completion.
A wet normal drain does not prove field capacity, and the reference is the current
validated arithmetic VWC mean, not a fabricated capacity reading.

A **wet tray**, unavailable/invalid required drain, invalid/stale/quorum-failed VWC,
pump safety fault or blocked accounting prevents completion/reference capture even
when capacity or a budget trigger is also present. This explicitly hardens the
native development transition: the previous valid-mean/invalid-config check alone
could combine a completion trigger with a concurrent tray fault. Production and
Blueprint behavior are untouched. Active wet drain/tray and sensor faults still
interrupt; an interrupted shot never transitions as successful completion.

The approved running-shot rule is unchanged: valid field capacity during P1 does
not shorten that shot; after verified shutdown/accounting, further P1 shots are
blocked and normal completion may capture the reference. Capacity cannot override
an independent safety fault. Manual completion also rechecks safety before and
after persistence; it cannot be used to bypass a fault.

## Remaining production blockers

- Independent enforceable pump ownership and hardware/gateway runtime watchdog;
  software acknowledgment and HA off state alone are insufficient.
- An explicitly authorized activation/migration/rollback workflow, with repair
  audit trails and cause verification. None is implemented in Phase 3B.
- Independent delivered-water measurement and an approved quantitative attempted-
  water ledger/budget, including uncertain/partial deliveries and rollback.
- Transactional persistence or a reviewed recovery/reconciliation protocol across
  config-entry options, shot Store and cycle Store. Exactly-once durability across
  these files is not claimed; unresolved writes/inconsistencies stay blocked.
- Source-level sensor freshness, calibration, conservative site-specific quorum/
  disagreement settings, device restart semantics and boundary/DST verification.
- Production supervision, physical off verification and protection against HA
  event-loop stalls, late commands, credential drift or externally forged states.
- An approved history-retention/archive scheme that retains operation replay
  protection and unresolved safety evidence. This phase never prunes history.
- Explicit policies for non-P1 phase boundaries/manual controls/legacy mirroring
  in a future activation project. Existing bounded-shot and fail-closed rules remain.

## Validation

Baseline `a76fbde` (merged PR #73) passed **608 tests** after restoring the unchanged
repository setup script, Python **3.13.16**, HA **2025.12.5**, fixture **0.13.301**
with the documented HA metadata exception, and the pinned pytest plugins.
The setup retained inherited proxy, TLS verification and CA trust; supported
execution network permission was applied to setup and fixture commands.

Existing clock-based native tests now explicitly align HA state timestamps with
their simulated civil clock. Moving that test clock also simulates new physical
sensor reports; freshness tests advance without those reports. Real event-loop
monotonic shot timers remain unmodified. Existing partial-availability arithmetic
coverage now explicitly selects 50% private quorum; the default requires 100%.
These fixture changes do not relax production safety or weaken assertions.

New tests cover freshness boundaries, unchanged/restored/historical readings,
non-finite/out-of-range corruption, sensor count/percentage, duplicate peers,
validated arithmetic averages, partial availability, disagreement, transient and
mid-shot faults, unsafe restart and original-state preservation, durable review
following pump reconciliation, missed reset with zero counters, activation failure,
unconfirmed shutdown, uncertain delivery, clean restart without restored ownership,
unknown/legacy/external/production ownership, pre-dispatch ownership revocation,
P1 completion with concurrent faults and migration/rollback review without activation.
Existing cycle tests retain missed-sunrise, both Berlin DST transitions, incomplete
transition/accounting persistence, drain-triggered completion, running-shot capacity
and inactive normal setup coverage. All pumps/services are simulated.

Final local validation passed **694 tests** (**86 new cases**) on the pinned
environment. Ruff lint and format checks, Python compilation and Git whitespace
checks passed. Blueprint, integration setup and CI configuration are unchanged.
GitHub Actions results are recorded in the draft pull request.
