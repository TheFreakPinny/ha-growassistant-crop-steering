# Native Crop Steering simulation harness

## Architecture and activation boundaries

Reviewed against `main` commit `4476f468083f730ba4847956d3f24a0f15f193d8`
(2026-10-14). All integration and Blueprint files remain unchanged in this work.

| Component | Existing isolation and responsibility |
| --- | --- |
| `irrigation.py` | Immutable input snapshot and pure `evaluate(inputs, now)`; no services or timers. P1 start readiness is separate from active-shot eligibility. |
| `pump.py` | Explicitly initialized owner; serialized dispatch, bounded runtime, fresh on/off observation, persisted recovery target, late-dispatch reconciliation. |
| `shot_engine.py` | Reservation, final revalidation, active supervision, durable intent and accounting only after confirmed shutdown. Uncertain delivery prevents replacement shots. |
| `cycle.py` | Native phase flags, counters and P2 reference under the accounting lock; live sunrise only, no catch-up reset. Restored state always requires review. |
| `scheduler.py` | Explicit initialize/start, event coalescing, fresh eligibility and latched retry blocking. Never queues active-shot triggers. |
| `native_safety.py` | Shared freshness/quorum/spread policy, simulated-only ownership evidence, recovery diagnostics. `native_activation_allowed` remains false. |

Normal `async_setup_entry` creates read-only native diagnostics and the existing
Blueprint-compatible cycle reset coordinator. It never constructs/initializes a
native pump, shot coordinator, native cycle controller or scheduler. Existing
public services and entity/option surfaces are unchanged. The diagnostics and
migration checklist cannot grant activation. Native unload hooks remain cleanup
only. There is no simulation option, switch, service, production mode or repair
API added to the integration.

The previous native tests already isolate sensor policy and phase decisions, and
use real HA fixtures with synthetic registered switching services. This harness
adds reusable composition with a stronger IO boundary: **no HA switching service
is invoked at all**. It reuses the full existing controller implementations.

## Test-only composition

`tests/native_harness/harness.py` is imported only by pytest. Use it exclusively
with the isolated HA test fixture, never with an installed/production HA object.
It is outside the HACS integration directory and normal setup never imports it.

The injected boundaries are:

- The isolated fixture's service registry is replaced by `FakePump`, whose
  `async_call` and `has_service` methods implement only the fake hardware boundary. There is no delegation to the original dispatcher or registered
  switching handler. A suite-wide sentinel checks the original HA dispatcher is
  never awaited. All accepted commands are recorded as `PumpCommand` values.
- The actor admits exactly `switch.native_harness_pump` and exactly `turn_on` or
  `turn_off`. Arbitrary switches, input booleans, target lists and corrupt recovery
  targets are rejected. An invalid recovery target remains faulted/persisted.
- Three fixed virtual VWC sensor IDs accept independent readings, unavailable or
  corrupt reports and missing state. Scalar steering configuration cannot assign
  HA entities or provide an activation flag.
- Pump, shot and cycle stores are injected `MemoryStore` instances. Deep copies
  preserve intent and evidence across snapshot/restoration; there is no disk
  journal mutation or deletion to clear recovery gates.
- The clock injects timezone-aware civil time for decision, phase, freshness and
  accounting. `move_to(..., refresh=False)` models silent communication loss;
  ordinary advances explicitly generate fresh virtual VWC reports. Light phase
  comes from the virtual sunrise/sunset schedule and simulated civil time.
  `lighting(sunrise, sunset)` accepts wall-clock times only, including overnight
  schedules; changing an owned schedule still invokes existing safety blocks.

`FakePump.service_delay` models communication latency. `confirmation_delay`
models delayed state observations independently; `None` suppresses a report.
`fail_on` models a start communication exception. The real PumpController still
owns service timeouts, pending tasks, monotonic deadlines and late-service
reconciliation. The real shot coordinator owns authorization and accounting.

Civil time is virtual; asyncio monotonic timers deliberately remain real. Normal
shots use the minimum valid one-second duration, and fixture service/confirmation
timeouts are shortened to 150 ms. No hard-runtime limit, sensor policy, ownership,
recovery or journal check is disabled. This is bounded software simulation, not
physical pump verification or accelerated arbitrary-duration virtual time.

## P1 contract and scenarios

P1 starts only at/below its start threshold. Once active, readings above that
threshold may still authorize fresh shots below Field Capacity, subject to all
other gates. Field Capacity reached during an already authorized shot does not
truncate that timed shot. After confirmed off, it counts once and the native cycle
completes P1/captures the P2 reference. No further P1 shot is permitted. Independent
sensor, tray or ownership faults still interrupt that same shot and never count
it. These behaviors already exist on the reviewed main; no P1/P2/P3 code changes
were necessary.

| Test group | Assertions |
| --- | --- |
| Full scheduler cycle | P0 blocks, P1 activates/completes, P2 respects reference/drop, P3 emergency uses its own threshold/budget; light-off remains P3. |
| P1 boundaries | Three-sensor mean, equality at start threshold, continuation above start, capacity before/during shot, independent safety fault at capacity. |
| Spacing/accounting | Soak boundary, counted shots, budget limit, no follow-up dispatch; zero count while off acknowledgement is pending. |
| Sensors | Each of three peers missing/unavailable/unknown/corrupt/disagreeing, each peer individually stale after communication loss. |
| Pump IO | Start exception, missing on, late on, delayed off, absent/late off, hard runtime rejection and automatic watchdog shutdown. |
| Lifecycle/recovery | HA stop during a running shot; real crash-visible intent snapshot restored into fresh controller instances, with successful or missing off reconciliation; no replay, no counter increment, no review clearance. |
| Isolation | No original HA dispatcher calls; reject arbitrary targets, entity configuration and activation keys; retain corrupt recovery evidence. |

A crash test snapshots **persisted** records/options and the virtual pump state
while a real shot is running. It then drains the old objects for test cleanup and
creates a fresh isolated Home Assistant runtime and every controller from the
captured pre-cleanup records. Cleanup never
modifies the captured image. This models a process crash at the IO/persistence
boundary in separate isolated HA runtimes; it is not a subprocess kill or real HA
installation restart. A separate test fires HA's actual stop event during a shot.
Restoration intentionally grants no ownership evidence and never resumes native
irrigation. Reconciled off does not erase uncertain delivery or cycle review.

## Reproduce and extend

Use Python 3.13.16, `.github/requirements-ci.txt` and
`pytest-homeassistant-custom-component==0.13.301` installed with `--no-deps`, as in
CI or `scripts/setup-cloud.sh`. The fixture package's metadata pins HA 2025.12.4;
this repository intentionally tests HA 2025.12.5 (the existing documented metadata
exception). Do not replace HA to satisfy that metadata pin.

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q tests/native_harness
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q
python -m ruff check custom_components tests
python -m ruff format --check custom_components tests
python -m compileall -q custom_components tests
git diff --check
```

Tests run in the existing CI suite automatically. Add new deterministic scenarios
using the `simulation` fixture, `vwc(a, b, c)`, `move_to`, `lighting`, and
`configure` before cycle ownership, and fake confirmation controls. Change only scalar configuration;
external managed-state drift must continue to fail existing ownership checks.
`ramp_vwc` emits gradual independent reports for a healthy rise to capacity;
`vwc` can intentionally produce transient disagreement. Never clear `_blocked`,
retry latches, ownership requirements or persisted recovery evidence to make a scenario run. Production activation, enforceable
hardware ownership, physical off verification and transactional storage remain
separate future work.

## Validation for this change

For the separate review of manual recovery/release boundaries and its additional
safety tests, see [Native recovery and manual release review](native-recovery-release-review.md).

On the reviewed main baseline with Python 3.13.16 / HA 2025.12.5:
**807 tests passed** (747 existing tests and 60 new harness scenarios).
Ruff lint, Ruff formatting, compilation and Git whitespace checks passed.
No file under `custom_components/` or `blueprints/` was changed. Normal Home
Assistant installations therefore retain the same native activation prohibition
and the complete existing Blueprint behavior.
