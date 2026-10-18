# Crop Steering Scenario Runner (test only)

The runner composes the unchanged native pump, shot coordinator, cycle controller
and scheduler from PR #77. It simulates exactly 86,400 elapsed seconds, records
native decisions and durable outcomes, and writes a standalone HTML report, CSV
timeline and JSON evidence. It is outside the HACS integration and can run only
through pytest's isolated Home Assistant fixture with the fixed `FakePump`.

## Run an example

Use the Python 3.13.16 / HA 2025.12.5 environment described in
[native-test-harness.md](native-test-harness.md#reproduce-and-extend), or prepare it
with `scripts/setup-cloud.sh`. The existing fixture metadata exception remains:
install `pytest-homeassistant-custom-component==0.13.301` with `--no-deps` rather
than changing HA to its metadata pin of 2025.12.4.

From the repository root, with that environment activated:

```bash
python scripts/run-scenario.py tests/native_harness/scenarios/normal-18-6.json \
  --output work/scenario-normal
```

Open `work/scenario-normal/report.html` locally. No server, CDN or network access
is needed by the report. Every run produces:

* `timeline.csv`: local offset-aware time, elapsed seconds, phase, three raw sensor
  states, validated average, reported pump state, coordinator state, all three
  managed shot counters, P2 reference, eligibility and blocking reasons. Samples
  also include every observed decision, pump report and shot result.
* `events.json`: ordered native decision snapshots (including phase windows),
  sensor/recovery diagnostics, execution availability, scheduler retry latch,
  phase changes, shot requests/rejections/results, reserved authorizations,
  active supervision checks, fake pump commands and state reports, controller
  shutdown checks, coordinator-confirmed shutdowns and durable records. It
  includes the resolved input scenario, summary and final memory checkpoint.
* `report.html`: VWC, native phase and pump charts, outcome counts and a detailed
  event table. Decisions and safety diagnostics are fully retained in JSON;
  blocked evaluations are counted separately from actually rejected requests.

The committed [example report](examples/scenario-normal/report.html) is generated
by this command, not a manually invented trace.

The test command can also be invoked directly:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q \
  tests/native_harness/test_scenario_file.py --timeout=0 \
  --crop-scenario=tests/native_harness/scenarios/normal-18-6.json \
  --crop-output=work/scenario-normal
```

The CLI removes pytest's whole-test 30-second ceiling because configured real
shot durations and real failure deadlines can legitimately take longer. It does
not alter any pump, shot, supervision, confirmation, watchdog or journal timeout.
Execution time is approximately the real active-shot/fault time plus simulation
overhead; a 24-hour civil day does not require 24 hours of wall time.

## Input format

JSON is validated by `Scenario.load` before scheduling. Unknown fields, entity
assignments, activation keys, managed cycle state, non-finite values and out-of-
range settings are rejected. The example is the complete executable template.

| Field | Meaning |
| --- | --- |
| `name` | Report title. |
| `start` | ISO datetime with explicit offset, e.g. `2026-10-13T06:00:00+02:00`. |
| `timezone` | IANA zone, e.g. `Europe/Berlin`; start offset must agree with it. |
| `lighting` | Optional `sunrise` and `sunset` in local wall time. Default `06:00:00` to `00:00:00` gives 18:6, including midnight crossing. |
| `config` | Existing scalar numeric/boolean steering settings, plus `p1_mode` and `p2_mode`. Ranges come directly from `NUMERIC_SETTING_DESCRIPTIONS`. Units are unchanged: VWC percentages / percentage-point drops, minutes for windows/soak, seconds for shot duration, integer shot budgets. |
| `sample_seconds` | Idle sensor-report cadence, 1–240 seconds; default 60. All three peers normally publish fresh reports at each sample. |
| `vwc` | One of the two explicit test input models below. |
| `events` | Optional sorted fault/input events with `at_seconds` relative to start. |

The runner starts with the existing harness's fresh, empty managed cycle state
and scalar defaults, then applies `config` before the scheduler starts. Existing
harness defaults include 1-second shot durations, Field Capacity 60%, P1 start
50%, 3 P1 shots, 3 P2 shots and 2 emergency shots. Explicitly set parameters whose
values matter to a scenario. Unspecified settings resolve through the existing
engine/harness defaults; the runner adds no watering policy. Managed flags,
counters and `p2_ref_vwc` cannot be seeded or edited through the scenario.

### Scripted VWC

```json
{
  "mode": "scripted",
  "points": [
    {"at_seconds": 0, "values": [39, 40, 41]},
    {"at_seconds": 3600.3, "values": [39, "unavailable", 41]},
    {"at_seconds": 4000, "values": [39, 40, 41]}
  ]
}
```

Points start at zero and have strictly increasing elapsed seconds. They are
piecewise constant, with no interpolation or pump-dependent response. Each sensor
can independently be a finite value in [0,100], `null` (missing), `unknown` or
`unavailable`. An abrupt independent change during a shot can trip the existing
spread policy. That interruption is retained; the runner never smooths scripted
data to conceal it. Use small explicit steps if a healthy gradual rise is wanted.

### Synthetic VWC — exclusively a test assumption

```json
{
  "mode": "synthetic",
  "initial": [39, 40, 41],
  "rise_per_shot": [12, 12, 12],
  "dryback_per_hour": [0.5, 0.5, 0.5]
}
```

This is **not a real plant, substrate, hydraulic or agronomic model**. Each sensor
has an independent initial VWC, percentage-point rise after a successfully
accounted shot, and percentage-point decline per elapsed idle hour. Values are
clamped to [0,100]. There is no drying while the fake pump reports on. Failed or
interrupted shots produce no assumed rise. A completed shot's response is emitted
through the harness's existing gradual report helper before the scheduler's next
cycle observation; Field Capacity/P2 capture is still decided by the native
cycle controller. No new Field Capacity target, phase transition, emergency rule,
shot authorization, counter increment or retry rule is implemented by the model.

### Fault events

```json
[
  {"at_seconds": 0, "pump": {"fail_on": true}},
  {"at_seconds": 4000, "pump": {"fail_on": false}},
  {"at_seconds": 5000, "sensor_reports": [true, false, true]},
  {"at_seconds": 6000, "tray": true}
]
```

Supported fake pump keys are `fail_on`, `on_service_delay_s`,
`off_service_delay_s`, `on_confirmation_delay_s` and
`off_confirmation_delay_s`. Delay values are real seconds (0–30); `null` on a
confirmation key suppresses the state report. `sensor_reports` pauses/resumes
reports independently, allowing the native freshness policy to detect stale
peers. `drain` and `tray` report wet/clear; `lose_ownership: true` removes the
test ownership evidence. No event can grant ownership, clear recovery, change a
target, edit journals, clear a retry latch or enable native production execution.

## Time and reproducibility

The existing `SimulationClock` controls native civil time. The scheduler's civil
wakeup registration is connected to a small cancellable test queue: its original
callback is invoked at the engine's requested instant. This prevents HA's host
wall-clock timer adapter from repeatedly firing historical scenario deadlines.
This injection is confined to the scheduler's civil timer boundary; every
eligibility evaluation and scheduling choice still runs in `_NativeScheduler`.

Idle time advances to the earliest report, scripted input, fault event or native
evaluation boundary. During active work the runner advances in 100-ms civil
steps, waiting that duration on the real event loop. Faults during a shot are
applied on the next active step (up to 100 ms after their requested civil time).
Input knots are never skipped while idle. UTC elapsed arithmetic gives exactly
24 elapsed hours across DST; local lighting interpretation belongs to the engine.
The interval is `[start, start + 24h)`: the endpoint is recorded and the scheduler
is stopped safely, without starting/resetting the next day. A boundary shot is
drained via the existing interruption mechanism. The final safety snapshot can
therefore include `scheduler_stopped_requires_review`; this is recorded cleanup.

Real monotonic time is never frozen, accelerated or replaced. Original service,
confirmation, maximum-runtime, active-supervision and journal deadlines all
remain in force. JSON includes real monotonic observation offsets as well as
simulated civil timestamps; the fake actor also retains original monotonic
command times. Identical input data reproduces policy outcomes; real callback
timing and resulting subsecond timestamps are observations, not a promise of
byte-identical artifacts. Synthetic responses are explicit fixed increments.
There is no random input and no fabricated successful result.

## Automated validation and safety boundary

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q
python -m ruff check custom_components tests scripts
python -m ruff format --check custom_components tests scripts
python -m compileall -q custom_components tests scripts
git diff --check
```

`test_scenarios.py` checks complete 18:6 days, P1 start/capacity, P1→P2,
P2 drop/repeated shots, P3/night dryback, P3 emergencies, an in-shot sensor
failure, pump failure with blocked follow-ups and the explicit synthetic model.
Assertions inspect actual decisions, reports, managed counters and durable shot
records, including shutdown confirmation and real minimum runtime. Additional
cases check silent loss of an individual sensor, wet-tray interruption, missing
off confirmation under the unchanged 5-second production timeout, interruption
at the 24-hour endpoint, and elapsed-time behavior across the autumn DST change.

The original HA dispatcher is guarded by a sentinel and must never be awaited.
Only `switch.native_harness_pump` is admissible. All persistence uses the existing
memory stores. There are no changes to `async_setup_entry`, production services,
activation options, recovery/ownership/journal mechanisms or Blueprints. Normal
HA installations keep native execution disabled. No productive safety logic
needed changing to implement these simulations.
