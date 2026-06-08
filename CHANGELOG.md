# Changelog

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
