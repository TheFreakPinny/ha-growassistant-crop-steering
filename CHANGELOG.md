# Changelog

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
