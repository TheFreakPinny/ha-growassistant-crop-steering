# Native irrigation decisions (Phase 2A)

`irrigation.py` is intentionally unused by integration setup, platforms, services,
the Blueprint and PumpController. It creates no timers, tasks, listeners or
subscriptions and does not write HA state, options, counters or timestamps.
All tests use synthetic states or the existing local HA fixtures. No production
Home Assistant or physical device is involved.

## API

```python
from homeassistant.util import dt as dt_util
from custom_components.growassistant_crop_steering.irrigation import (
    capture_inputs, evaluate, phase_windows,
)

inputs = capture_inputs(hass, entry)  # synchronous reads only
now = dt_util.as_local(dt_util.utcnow())  # caller supplies HA local time
result = evaluate(inputs, now)
payload = result.as_dict()  # JSON-compatible; timestamps use ISO offsets
```

`Inputs(config, vwc)` also supports pure callers/tests. Config contains existing
resolved keys (managed values or legacy helper states) plus `pump_state` and
explicit boolean `drain_sensor_configured` / `drain_tray_sensor_configured`.
VWC maps configured entity IDs to their captured readings. Snapshot mappings are
copied and read-only. Unsupported non-scalar readings fail closed. The capture
adapter honors options membership, including cleared optional sensors, uses the
existing managed defaults when a key was never configured, and reads legacy time
helper attributes and timestamps. A configured helper that is missing does not
receive a default. Snapshot creation must run on HA's event loop so synchronous
reads describe one state/config snapshot; callers must capture again to refresh.

`phase_windows(inputs, now)` returns local timezone-aware boundaries or raises
`ValueError` for invalid/ambiguous schedules. `evaluate` instead reports
`invalid_schedule`. Supply HA local timezone time, not UTC when HA is configured
for another timezone. Naive evaluation times fail closed.

`Decision` is frozen and contains:

| Field | Meaning |
| --- | --- |
| `phase` | Existing `p0_transpiration`, `p1_morning`, `p2_midday`, `p3_dryback`, or `off` |
| `eligible` | Current shot passes every applicable gate |
| `blocking_reasons` | Stable ordered machine-readable reason codes; all observed failures |
| `pump_entity`, `duration_s` | Selected valid pump and validated requested runtime; invalid values become null |
| `cycle_id` | Local sunrise ISO timestamp for the current sunrise-to-sunrise cycle, including overnight dryback |
| `shots_done`, `shot_limit`, `remaining_shots` | Current phase's counters; invalid/irrelevant values become null |
| `soak_until` | Earliest time permitted by previous-shot soak, even when other gates block |
| `next_eligible_time` | Now if eligible, or soak expiry if soak alone blocks and expiry remains inside the current phase window; otherwise null |
| `windows` | Cycle start/end, sunset, P1 start/end and P2 end cutoff |
| `vwc_average`, `vwc_valid_count` | Captured arithmetic average and finite parsed reading count |
| `p1_start_eligible`, `p1_start_blocking_reasons` | Separate sensor-mode P1 start readiness; never shot authorization or a state transition |

A future `next_eligible_time` is conditional on all other snapshot inputs staying
valid. It is not a reservation or guaranteed future authorization. Start readiness
is conservative and checks shot safety too; it never calls `start_p1`. In manual
mode start readiness is false, but already active manual P1 can receive shots.
The start VWC threshold applies only to starting P1, per the approved design;
active shots stop at field capacity. `p1_window_opened_today` prevents repeat
sensor starts. There is no completion action: field capacity, P1 done and limits
block shots without changing completion state or the P2 reference.

## Preserved steering semantics

- P0 blocks every shot. Lighting helpers, not the optional LED day sensor, drive
  phase selection. Sensor-mode active P1 may continue past its start window,
  through the light period, until sunset. Manual P1 uses its fixed window.
- Sensor-mode P2 requires completed P1. Manual P1 phase progression can select
  P2 after its window, but native P2 shots still require P1 completion and a
  positive reference. P2 manual mode does not authorize automatic shots.
- P2 requires VWC <= reference minus configured drop, respects the VWC cap
  (including the existing cap=0 behavior), remaining shots and the strictly open
  sunset-minus-offset cutoff. The cap has no new hysteresis behavior.
- P2 deliberately ignores the normal runoff sensor; the tray remains a safety
  gate. P1 checks both configured drains. Absent drains are optional for P1/P2;
  configured unavailable, unrecognized or wet drains block. P3 requires a
  configured available clear tray and ignores normal runoff.
- P3 includes nighttime and daytime dryback. Emergency shots require explicit
  enablement, VWC <= threshold, positive remaining limit, valid runtime, finished
  soak, clear required tray and an observed-off pump.
- Previous shot type selects soak across phase transitions. Missing historical
  type falls back to the candidate phase's soak. Interval helpers remain
  diagnostic/future settings; they do not introduce new gates.
- No missed-window catch-up, automatic resets, inferred completion, queued shots
  or automatic callback is implemented. Decisions evaluate exactly one instant.

## Explicit differences from the Blueprint / diagnostics

1. Native decisions validate finite values and existing numeric setting bounds;
   booleans are not numbers, counters must be nonnegative integers, and runtime
   must be 1..300 seconds, matching the current configured bound and controller
   hard maximum. Invalid required settings block instead of using template
   coercions/default counters. Invalid modes/flags also fail closed.
2. P1 shot safety explicitly checks capacity, remaining shots and configured
   runoff/tray availability. The Blueprint P1 branch mostly relies on its
   preceding completion branch and directly rejects only tray `on`; native
   decisions fail closed on unavailable or unrecognized configured tray states.
   Native P2 explicitly requires P1 completion, including manual P1 progression.
3. P1 start threshold is a start-only gate, as requested. Existing P1 diagnostic
   readiness can still report above-start-threshold during active P1; native
   active-shot eligibility can be true until field capacity. Neither diagnostics
   nor Blueprint is modified. Start readiness also checks shot safety gates;
   Blueprint auto-start itself checks fewer operational inputs.
4. A malformed, future or unavailable configured last-shot timestamp blocks.
   With no last timestamp and any positive known phase counter, native shots
   block instead of assuming soak expired. A genuinely fresh cycle with zero
   counters and no configured timestamp is allowed. Legacy naive last-shot
   timestamps use the supplied HA local timezone, except ambiguous/gap timestamps
   which block. Explicit-offset last-shot timestamps remain unambiguous.
5. Wall-clock light helpers and phase offsets preserve ordinary existing phase
   behavior (tested against `_calculate_phase`). Soak uses UTC elapsed time,
   avoiding same-zone datetime subtraction errors at DST. Ambiguous or nonexistent
   cycle/phase boundaries block the entire snapshot until a future explicit DST
   policy is selected. Equal sunrise/sunset also blocks: the existing calculation
   treats it as always-lit with zero seconds since sunrise, which can stall P0.
6. A valid pump must be a `switch` or `input_boolean` with state exactly `off`.
   Eligibility is not permission to bypass controller ownership, fault, service
   availability or durable recovery checks; those remain Phase 2B execution gates.

### Multiple VWC sensors

Existing `calculate_vwc_state` uses the arithmetic mean of readings that parse as
floats, keyed by entity ID. Missing/unavailable/unparseable peers are skipped; no
all-sensors quorum exists. Native decisions retain the arithmetic mean and allow
partial **missing/unavailable** peers when at least one valid reading remains.
They explicitly reject corrupt readings (unparseable, boolean, out of 0..100,
NaN or infinity), including corrupt peers beside a good sensor, rather than
silently shrinking the average. This is a documented fail-closed validation
change, not a new aggregation method. Recommend separately evaluating a quorum
or per-sensor minimum policy in Phase 2B; neither is implemented here.

## Phase 2B questions and execution requirements

- Establish exclusive Blueprint/native ownership, reconcile controller recovery,
  re-read safety inputs immediately before activation, and serialize eligibility,
  shot accounting and execution to prevent stale snapshot races.
- Define durable completion accounting and how partial/failed shots affect
  counters and last-shot timestamps. Cycle IDs identify current light cycles;
  this engine does not verify counter ownership or reset stale counters.
- Choose whether phase boundaries must constrain the entire runtime. This phase
  preserves the Blueprint's start-before-boundary policy: a shot starting just
  before sunset/cutoff may finish afterward. Phase 2B should explicitly decide.
- Choose DST repeated/gap helper behavior and equal-time lighting semantics before
  relaxing the conservative schedule rejection.
- Decide whether malformed/missing historical timestamps need a separate recovery
  latch and whether disabling VWC cap should get an explicit future option.
- Define freshness limits and a VWC sensor quorum separately from averaging.
- Keep interval catch-up disabled unless a later scheduling policy explicitly
  defines how to avoid accumulated irrigation after downtime.
