# Changelog

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
