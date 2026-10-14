# Phase 3C read-only diagnostics design

Design recorded before implementation, against `bf7199f` (merged PR #74).
Baseline: 694 tests passed using `scripts/setup-cloud.sh`, Python 3.13.16,
Home Assistant 2025.12.5 and the documented fixture metadata exception.

## Architecture review

Config entry entity selectors resolve options before entry data, including empty
selectors. Numeric/boolean managed options and legacy helpers remain unchanged.
The existing button, number, sensor and switch platforms expose Blueprint helpers
and status. Dispatcher signals update managed helper entities; the cycle-reset
coordinator manages the existing daily reset independently of native execution.
Options do not automatically reload the entry.

PumpController initialization can reconcile shutdown and change its recovery
Store. ShotCoordinator initialization can rewrite recovery/accounting evidence.
Scheduler installs execution timing and listeners; CycleController owns native
phase transitions and reconciliation. None may be constructed or initialized by
diagnostics. Their existing explicit development APIs and unload paths remain.

Phase 3B provides pure sensor/recovery assessments and a migration checklist which
always returns native activation false. HA diagnostic entities belong to the
existing integration device, use stable entry-based unique IDs, and expose state
and attributes through sensor/binary_sensor platforms. They provide no actions.

## Data and integrity contract

A separate shared diagnostics coordinator reads the three existing version-1
Store files (pump recovery, shot journal, native cycle) once per setup. It never saves,
removes or reconciles them. Historical evidence is explicitly a setup-time
snapshot with a load timestamp; reload obtains another snapshot. Invalid records
and load failures remain visible, and do not fail normal Blueprint setup.

Implementation review found that HA's Store loader can rename corrupt files and
save migrations even with `read_only=True`. Consequently, diagnostic reads use
raw JSON envelopes in HA's executor, validate supported versions, and bypass
Store loading entirely. Pending in-memory writes are not durable evidence.

Live VWC uses `assess_sensors` with the provisional Phase 3B default SensorPolicy.
Counts, rejection map, quorum, disagreement, freshness thresholds, machine reasons
and human explanations are exposed. Existing VWC and Blueprint averaging stay
unchanged. HA off is a live observation only, never physical pump-off verification.

Historical recovery is assessed on a private copy of validated records. Older
interrupted/fault records and incomplete accounting are treated conservatively.
No history means `not_evaluated`, never clean restart/idle. Valid historical
records can show `historical_evidence_present` or `historical_review_required`;
they cannot establish current recovery readiness or reconciled cycle state.
The last outcome is shown only for an explicitly timestamped, uniquely latest
persisted operation with an outcome. It never substitutes Blueprint last-shot
options or invents a result for an incomplete record.

Migration diagnostics reuse the checklist, add missing-evaluation reasons and
separately expose sensors, recovery, physical pump-off, Blueprint relinquishment,
exclusive ownership and cycle reconciliation. Unknown ownership is `unverified`.
Native activation availability/eligibility is always false, including healthy
sensors and completed historical records. Normal native engine status is
`disabled`; this is a production-policy statement, not a fabricated controller
lifecycle state. No manual acknowledgments or activation pathways are added.

## Lifecycle and performance

Sensor and binary_sensor entities share one coordinator. Relevant state events
and entry-option changes refresh the inexpensive live assessment; selectors are
rebound on options updates. One 60-second timer bounds freshness display delay
without per-entity polling or repeated journal I/O. Listeners are installed only
after platform forwarding succeeds and removed on successful unload; failed
unload retains diagnostics. Failed setup removes the coordinator for retry.
Reload preserves unique IDs and obtains new history. No production HA/hardware
is contacted during development; tests use HA fixtures and simulated state.

## Validation plan

Cover fresh/legacy entries, absent/corrupt/failed Stores, completed/interrupted
history, freshness/quorum/disagreement, unknown ownership, recovery reasons,
migration prohibition, selector changes, entity categories and IDs, setup retry,
reload/unload and unchanged journals. Patch execution constructors/initializers
to fail and record pump service calls. Run full pytest, Ruff lint/format,
compilation, whitespace checks and GitHub Actions on the draft PR.
