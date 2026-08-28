# Changelog

## v0.1.16 - 2026-08-28

### Changed

- Added persistent Last Shot Type tracking for P1, P2 and P3 Emergency shots.
- Soak duration is now selected from the shot that created the Last Shot timestamp rather than from the current phase.
- A P2 soak can therefore continue correctly into P3 without being extended to the configured P3 Emergency soak.
- After the first P3 Emergency shot, the configured P3 Emergency soak applies.
- Added diagnostics for Last Shot Type and effective soak selection.
- Updated the optional Shot Engine Blueprint to label P1, P2 and P3 Emergency shots.

### Compatibility

- Existing installations without a stored Last Shot Type keep the previous caller-specific soak behavior until the next typed shot.
- Existing `set_last_shot_now` calls without `shot_type` remain valid.
- Pump control, drain safety and phase behavior are unchanged.

## v0.1.15 - 2026-08-28

### Added

- Added standard and high-resolution logo branding files for Home Assistant integration and device presentation.

### Compatibility

- Existing icon branding remains unchanged.
- No crop-steering logic, Blueprint logic, pump control, entity IDs, unique IDs, config flow behavior, number settings, safety behavior, or irrigation behavior changed in this release.

## v0.1.14 - 2026-08-27

### Changed

- Removed the helper integration classification so GrowAssistant is presented as a normal Home Assistant integration in the Integrations UI.
- Corrected the repository documentation and issue tracker URLs for the current `TheFreakPinny/ha-growassistant-crop-steering` repository.
- Reworked the README to document the current v0.1 feature set, including P3 Emergency, automatic cycle reset, diagnostics, and safety behavior.

### Added

- Added integration branding icons for delivery with future HACS installs and updates.
- Added a GrowAssistant repository banner.

### Compatibility

- No crop-steering logic, P1/P2/P3 behavior, Blueprint irrigation behavior, pump control, entity IDs, unique IDs, config flow behavior, number ranges, steps, units, or existing safety behavior changed in this release.

## v0.1.13 - 2026-08-27

### Improved

- All GrowAssistant integration-managed number entities now render as direct numeric input fields by using `NumberMode.BOX` instead of sliders.
- BOX mode applies globally to current and future integration-managed number entities.

### Compatibility

- No entity IDs, unique IDs, values, minimum or maximum values, steps, units, crop steering logic, Blueprint logic, or pump control were changed.

## v0.1.12 - 2026-08-27

### Changed

- The normal Drain Sensor is now diagnostic-only during P2 and no longer blocks P2 irrigation when wet or unavailable. Existing P1 Drain Sensor behavior is unchanged.
- The Drain Tray remains a fail-closed safety gate and blocks irrigation when wet or unavailable.

### Added

- Added optional P3 Emergency Dryback Shots while remaining in `p3_dryback`, with configurable enablement, VWC threshold, shot duration, soak time, maximum shots per light cycle, and an Emergency Shots Done counter.
- P3 Emergency readiness requires the feature to be enabled in `p3_dryback`, valid VWC at or below the configured threshold, completed soak, an available shot, a valid positive duration, the pump to be off, and an explicitly configured, available, dry Drain Tray.
- The normal Drain Sensor does not block P3 Emergency; the Drain Tray remains fail-closed.
- P3 Emergency Shots Done resets once at the start of each new light cycle with the existing cycle reset. Last Shot remains preserved.
- Added a P3 Emergency branch and inputs to the optional Shot Engine Blueprint. Pump control remains in the Blueprint; no native Python pump-on control was introduced, and existing P1/P2 pump behavior is unchanged.
- Existing Blueprint automations created from an older version must have the new P3 inputs assigned after the Blueprint is updated.

### Live validation

- Manually validated in Home Assistant that P3 Emergency readiness became true below the configured test threshold and the Blueprint executed one real 30-second emergency pump shot.
- Confirmed that P3 Emergency Shots Done incremented from 0 to 1, Last Shot was updated, and the soak countdown started after the shot.
- After soak expired with Max Shots set to 1, readiness remained false with status `p3_emergency_shot_limit_reached`.

## v0.1.11 - 2026-08-22

### Improved

- General GrowAssistant Debug sensor now reports phase-aware `blocking_reasons` and `passed_conditions`.
- Added machine-readable `phase_reason` to explain why the current phase was selected.
- P1 diagnostics now distinguish between start-readiness conditions and active-P1 shot conditions.
- P2 sensor-mode diagnostics expose VWC/drop, soak, shot-limit, time-window, VWC-cap, and drain conditions without introducing interval gating.
- P3 diagnostics now distinguish the actual phase-selection reason from operational P2 shot blockers.
- Light-off P3 reports `light_cycle_ended` without obsolete P1/P2 readiness noise.

### Compatibility

- Existing P1 Debug sensor remains unchanged.
- Existing Block Reason sensor remains unchanged.
- No irrigation behavior changes.
- No Blueprint changes.
- No pump-control changes.

## v0.1.10 - 2026-08-22

### Added

- New phase-independent GrowAssistant Debug diagnostic sensor.
- Aggregated diagnostics for current phase, light timing, VWC, P1, P2, soak timers, shot counters, last-shot state, and drain safety sensors.
- Human-readable `block_reason` plus detailed machine-readable `blocking_reasons` and `passed_conditions`.
- English and German translations for the new Debug entity.

### Compatibility

- Existing P1 Debug sensor remains available.
- Existing Block Reason sensor remains available.
- No irrigation behavior changes.
- No pump-control changes.

## v0.1.9 - 2026-08-20

Bugfix release for reliably resetting cycle state at the start of each grow day.

### Fixed

- Added an automatic cycle reset when a new LED light cycle starts at sunrise.
- The reset runs exactly once per grow day and clears P1 Active, P1 Done, P1 Window Opened Today, P1 Shots Done, P2 Shots Done, and P2 Reference VWC while preserving Last Shot.
- The automatic reset does not control or activate pumps.
- A persistent cycle marker prevents duplicate resets after Home Assistant restarts.
- If Home Assistant starts during an already active light cycle, a missed reset is performed once.
- Overnight light cycles such as 19:00–07:00 are supported.
- Changing sunrise or sunset helpers during an active cycle does not trigger another reset; the updated sunrise applies to the next grow day.

## v0.1.8 - 2026-08-18

Release focused on editable configuration, explicit P1 completion, and fail-closed P2 pump gating in the optional Shot Engine blueprint.

### Configuration and usability

- Configured external entities can now be changed from the Home Assistant **Configure** options flow without removing and re-adding the integration.
- Options override initial config-entry data, including the current multi-sensor VWC selection.
- Optional drain and drain-tray assignments can be cleared without falling back to stale initial configuration.

### P1

- Added automatic P1 preparation to the optional Shot Engine blueprint. `start_p1` prepares state without starting the pump; the first P1 shot waits for a later trigger.
- Added `growassistant_crop_steering.complete_p1`, which captures the current averaged VWC as P2 Reference VWC before setting P1 Done and disabling P1 Active. The service fails closed when no valid VWC is available.
- P1 completes only when its maximum shot count or Field Capacity is reached, or its configured Drain Sensor is wet. The Drain Tray remains a safety blocker and does not complete P1.

### P2 and pump safety

- Made P2 shot gating fail closed. **Block Reason** must be exactly `P2 ready`; missing, unavailable, unknown, empty, and blocked states prevent pump activation.
- Preserved the existing P2 phase, soak, shot-count, and positive-target checks.
- Added Drain Tray safety to P2. The integration reports an unavailable or wet configured Drain Tray as a P2 blocker.
- The optional blueprint pump selector supports both `switch` and `input_boolean`, allowing safe dry-runs with a test helper.
- Pump control remains exclusively in the optional Home Assistant blueprint or user automations; there is no native Python irrigation or pump-control engine.
- HACS installs the custom integration but does not install the optional blueprint automatically. Real hardware still requires an independent physical/electrical failsafe.

### Runtime validation

- Manually dry-run tested P1 in Home Assistant with an `input_boolean` test pump: automatic start; no pump activation in the auto-start branch; a subsequent first shot; shot duration and pump-off behavior; counter increment and Last Shot update; max-shots completion; P2 Reference VWC capture; P1 Done on/P1 Active off; and transition from P1 to P2.
- Manually dry-run tested P2 with the same test pump: `P2 blocked: VWC drop not reached` produced no shot even with Soak at `0` and shots available; changing to a valid `P2 ready` state produced exactly one shot; the shot counter reached its target; and the phase transitioned to `p3_dryback`.

## v0.1.7 - 2026-06-15

Bugfix release for sensor-mode P1 phase transitions and diagnostics.

### Fixed

- Fixed sensor-mode P1 phase transition.
- P1 can now enter `p1_morning` during the active P1 window before `p1_active` is already true.
- This fixes a circular dependency where the phase waited for P1 Active while P1 start/shot logic waited for phase `p1_morning`.
- P1 Debug diagnostics now align with phase classification by using the same `p1_window_active` calculation.
- This fixes cases where the phase could jump from P0 directly to P3 even though P1 Debug showed no blocking reasons.

## v0.1.6 - 2026-06-12

Diagnostic pre-release for troubleshooting P1 readiness and phase transitions.

### Added

- Added a new P1 Debug diagnostic sensor.
- The sensor exposes P1 readiness checks.
- It shows timing/window state, LED day state, P0/P1 timing, P1 mode/state, VWC thresholds, shot/soak state, optional drain/drain tray diagnostics, missing entities, passed conditions, and blocking reasons.
- This helps troubleshoot cases where the phase jumps from P0 directly to P3.

## v0.1.5 - 2026-06-11

Bugfix release for optional drain and drain tray safety handling.

### Fixed

- Fixed optional drain and drain tray handling.
- Unconfigured optional drain sensors no longer block P1/P2 readiness.
- Unconfigured optional drain sensors no longer appear as missing required entities.
- Configured drain sensors still block safely when wet, unavailable, or unknown.

### Changed

- Added clearer diagnostics for optional drain and drain tray sensors:
  - configured
  - ignored
  - available
  - raw state
  - wet/clear evaluation

## v0.1.4 - 2026-06-11

Pre-release usability update focused on easier dashboard control.

### Added

- Added Home Assistant button entities for common integration actions:
  - Reset Cycle
  - Start P1
  - Stop Pump
  - Set Last Shot Now
  - Clear Last Shot
- Added/updated a German sections dashboard example with a **Steuerung** card for the new buttons.

### Changed

- Button-triggered service calls are scoped to the matching GrowAssistant config entry/device.
- Developer Tools service calls remain available.
- Dashboard documentation now notes that entity IDs can vary depending on Home Assistant language and entity registry.

### Testing notes

Please test after updating and restarting Home Assistant:

- The new button entities appear on the GrowAssistant device page.
- **Set Last Shot Now** updates the Last Shot sensor.
- **Clear Last Shot** clears the managed Last Shot value.
- **Start P1** sets P1 Active, clears P1 Done, opens today’s P1 window, and backdates Last Shot.
- **Reset Cycle** resets P1 state, shot counters, and P2 reference VWC.
- **Stop Pump** only turns off the configured pump entity.

### Notes

This release still does not include a native Python irrigation engine.

Pump control remains available only through the optional blueprint and user-created Home Assistant automations.

Always use physical/electrical failsafes for real pump hardware.

## v0.1.3 - 2026-06-09

### Added

- Added integration-managed P1 state switches:
  - P1 Active
  - P1 Done
  - P1 Window Opened Today
- Added integration-managed shot counter number entities:
  - P1 Shots Done
  - P2 Shots Done
- Added managed Last Shot timestamp sensor.
- Added services:
  - `set_last_shot_now`
  - `clear_last_shot`

### Changed

- New users no longer need to manually create P1 `input_boolean` helpers.
- New users no longer need to manually create `counter` helpers.
- New users no longer need a `last_shot` `input_datetime` helper.
- Optional shot engine blueprint now updates the managed Last Shot timestamp.

### Compatibility

- Legacy `input_boolean`, `counter`, and `last_shot` `input_datetime` helper setups remain supported as migration/backward compatibility fallback.

## v0.1.2 - 2026-06-09

### Added

- P1 and P2 steering modes are now configured directly in the integration.
- Added options flow so P1/P2 modes can be changed after setup.
- Added integration-managed number entities for numeric crop steering settings.
- Added managed P1 Shots numeric setting.

### Changed

- Removed the need for external `input_select` helpers for P1/P2 mode.
- New users no longer need to manually create `input_number` helpers for P0/P1/P2/VWC settings.
- Numeric settings are editable directly from the Home Assistant UI.

### Compatibility

- Existing `input_number` helper based setups remain supported for migration/backward compatibility.

## v0.1.0 - 2026-06-08

Initial public release of GrowAssistant – Crop Steering for Home Assistant.

### Added

- HACS custom repository support for installing the integration.
- UI config flow for selecting existing Home Assistant helper entities.
- Diagnostic status sensor.
- Diagnostic crop steering phase sensor.
- P1 and P2 soak remaining diagnostic sensors.
- Block reason diagnostic sensor.
- `growassistant_crop_steering.reset_cycle` service for resetting daily/cycle helper state.
- `growassistant_crop_steering.start_p1` service for preparing helper state for P1 workflows.
- `growassistant_crop_steering.stop_pump` manual/safety service for turning off the configured pump switch.
- Optional shot engine automation blueprint for users who intentionally enable YAML-based pump orchestration.

### Safety

- v0.1.0 does not include a native integration-side pump control engine.
- Pump-on control is available only through the optional blueprint when a user explicitly installs and configures it.
- Use independent physical or electrical failsafes, such as float switches, leak detector cutoffs, timer relays, fused circuits, or equivalent protections. Do not rely on Home Assistant, this integration, the optional blueprint, or software logic alone to prevent flooding, pump damage, crop damage, or electrical hazards.
