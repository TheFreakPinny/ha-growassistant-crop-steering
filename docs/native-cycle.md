# Native phase and cycle management (Phase 3A)

**Phase 3B update:** [Safety and migration preparation](native-safety-migration.md)
supersedes the earlier open questions below about freshness/quorum, restart
classification, partial-delivery review and pump ownership. Native remains
development-only and normal setup remains inactive.

Development only. Normal setup does not construct, initialize or start the native
cycle controller. The Blueprint and production reset coordinator are unchanged.
Only internal test harnesses may attach a cycle controller to the native scheduler
and shot coordinator. There is no activation service, button or migration.

## Existing behavior reviewed before implementation

- The Blueprint requires the pump off before choosing an action. It starts P1
  when the phase is P1 and debug readiness is true, with active/done/opened flags
  false. Readiness includes the sensor-mode start threshold, window, soak,
  capacity, shot budget and configured drain gates. Manual mode waits for explicit
  start; its phase window ends at `p1_start + p1_duration_min`.
- Active P1 completes on capacity, its shot limit, or wet drain sensor. Wet tray
  and unavailable drain are blocking/fault conditions, not completion triggers.
  `complete_p1` captures the current available-sensor VWC mean, then writes the
  P2 reference, done, and inactive flags. Without valid VWC it preserves state.
  Manual window expiry alone does not call completion or capture a reference.
- `start_p1` sets active/opened, clears done/reference and backdates last_shot to
  bypass soak. The new harness preserves genuine last-shot history and never
  backdates it: Phase 2A soak remains applicable. This is an explicit development
  safety difference, not a new watering rule.
- Phase 2A selects P0 before P1, sensor P1 while active (potentially beyond the
  start window), completed P2 while budget and sunset-offset permit, otherwise
  P3. P2 requires done, a positive reference, VWC drop and cap. P3 emergency
  requires enablement, dry tray, threshold, soak and budget. Manual P2 cannot
  irrigate automatically. These decisions remain in `irrigation.py`.
- The production `_CycleResetCoordinator` runs at setup and sunrise with catch-up
  and a stored marker. Reset clears three flags, three counters and reference,
  including legacy mirrors. Native accounting already shares a lock with that
  reset and invalidates shot generation immediately. Native execution uses
  managed options; legacy helpers remain Blueprint owned.
- Phase 2B alone executes through PumpController and owns success accounting and
  recovery records. Phase 2C supplies one coalesced evaluation and one UTC timer;
  it originally consumed external phase flags and had no phase ownership.

## Proposed conservative lifecycle policies

Native ownership requires explicitly clean managed flags/reference/counters; it
never imports or resets legacy helper values. A new cycle is identified by its
Phase 2A sunrise ISO timestamp. Initialization installs no listeners and cannot
change phases or execute. Restart restores identity for diagnostics but requires
review, even when persisted files agree: it never resumes an interrupted cycle.
No recovery-clear API is introduced.

| Event | Owned transition | Shot authorization |
| --- | --- | --- |
| Observed sunrise | Previous cycle ended; new identity and P0 reset intent/commit | P0 blocks shots |
| P0 window expires | P1 waiting | Sensor readiness or private manual start required |
| P1 start readiness | Active/opened true, done false, reference zero | Fresh Phase 2A decision and Phase 2B reservation |
| Capacity, P1 budget or wet drain | After execution settles: current mean captured, done true, active false | New P1 shots blocked |
| Completed P1, P2 budget and time remain | P2 with reference identity | Existing VWC drop, cap, soak and budget gates |
| P2 cutoff/budget, sunset or unstarted sensor P1 window expiry | P3 | Existing emergency gates |
| Restart, incomplete intent, fault or ownership drift | Review required | Native execution blocked |

Live sunrise observation accepts at most one second of timer latency; a missed
sunrise blocks for review instead of catching up. P0 initializes at sunrise,
sensor P1 starts only on Phase 2A `p1_start_eligible`, and repeated starts are
blocked by owned opened state. Manual P1 requires an explicit private harness
start, within its time window; it bypasses the sensor start threshold, but never
bypasses shot authorization. Automatic completion uses the existing three
Blueprint triggers only while the decision is P1, with valid VWC. Private manual
completion mirrors the existing explicit completion service. Manual window expiry
does not synthesize completion. Phase 2A continues to decide P2/P3 eligibility.

Transitions wait until active execution has finished, so capacity never aborts
an authorized P1 shot. Drain, sensor and other safety faults still interrupt via
Phase 2B. A failed/interrupted shot latches cycle recovery; a sunrise/reset cannot
clear that latch. Reference identity is stored with its cycle and checked before
every native request. Options drift, schedule drift, ambiguous ownership, storage
failure, incomplete transition, stop/reload and external legacy reset block native
execution. No pending request or missed shot is replayed.

Transition/reset intent is stored before a single synchronous managed-options
update, followed by a committed record. Both use Phase 2B's accounting lock.
Reset invalidates old-shot generation before waiting, retains every shot journal
record, and updates the accounting checkpoint for the new cycle. It only clears
owned counters and flags; last-shot history/soak is retained across sunrise.
An active-shot reset invalidates generation immediately and blocks instead of
resetting ambiguous counters. An old completion in flight is compensated under
the existing accounting lock. The previous cycle's end identity remains in the
new record for diagnostics. No operation or unresolved recovery record is pruned.

## Persistence limits and Phase 3B questions

Config-entry options, cycle Store and shot Store are independent files. There is
no transaction across them; power loss may leave old options or a pending intent.
Restart always blocks, and inconsistencies remain review-required. Unresolved
writes are retained and cannot be overtaken by another cycle write. Exactly-once
durable phase/reset accounting is not claimed.

Phase 3B must define explicit migration/activation/rollback, external pump and
phase ownership fencing, recovery review and repair, manual controls, legacy
mirroring, sensor age/quorum, attempted-water budgets after partial shots, history
retention, transactional persistence, DST ambiguity handling and a hardware
watchdog. Software fixture results do not prove delivery or production safety.

## Validation

Latest fetched main `f97a358` contains merged PR #72. The unchanged baseline
passed **539 tests** after the documented `scripts/setup-cloud.sh` installed
Python **3.13.16**, Home Assistant **2025.12.5** and the fixture package with its
documented metadata exception. The final suite passed **608 tests**, including
**69 new cycle cases**, using local HA fixtures and simulated pump services.
Coverage includes phase transitions, both P1 modes, threshold/readiness changes,
capacity during execution, reference ownership, reset/accounting races, duplicate
sunrise, overnight cycles, Berlin DST, restart/reload, storage failures, sensor and
drain faults, legacy-state rejection and inactive production setup.

Separate Ruff lint, Ruff formatting, compilation, Git whitespace and unchanged
Blueprint checks passed. See [cloud setup](cloud-development.md) for commands;
the draft PR records GitHub Actions results. No production Home Assistant or
physical hardware was accessed, and no native production activation was added.
