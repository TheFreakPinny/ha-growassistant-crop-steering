# GrowAssistant – Crop Steering

[![Validate with hassfest](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml)
[![Validate with HACS](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml)

A Home Assistant custom integration for crop steering irrigation diagnostics and future irrigation control.

## Current status

**v0.1 is read-only and diagnostic only.**

The integration reads existing Home Assistant helpers/entities and exposes diagnostic sensors for crop steering state. It does **not** control pumps yet, does **not** run an irrigation engine, and should not be treated as an autonomous irrigation controller in v0.1.

## Current v0.1 features

- HACS-compatible Home Assistant custom integration.
- UI config flow through **Settings → Devices & services**.
- User selects existing Home Assistant helpers/entities during setup.
- Diagnostic sensors for status, phase, soak countdowns, and block reason.
- Read-only diagnostics only.
- No pump control implemented yet.

## Installation via HACS custom repository

1. Open **HACS** in Home Assistant.
2. Go to **Integrations**.
3. Open the three-dot menu and choose **Custom repositories**.
4. Add this repository URL.
5. Set **Category** to **Integration**.
6. Select **Add**, then install **GrowAssistant – Crop Steering** from HACS.
7. Restart Home Assistant.
8. Add the integration from **Settings → Devices & services → Add integration**.
9. Search for **GrowAssistant – Crop Steering** and complete the UI config flow.

## Manual installation

Copy the integration directory into your Home Assistant configuration directory:

```text
custom_components/growassistant_crop_steering/
```

Restart Home Assistant, then add the integration from **Settings → Devices & services → Add integration**.

## Integration domain

```text
growassistant_crop_steering
```

## Required existing Home Assistant helpers/entities

GrowAssistant – Crop Steering v0.1 does not create or manage the irrigation helpers for you. Create the needed Home Assistant helpers/entities first, then select them in the integration config flow.

### Required

- **Pump switch** (`switch`)
  - Used only by the `growassistant_crop_steering.stop_pump` manual/safety service in v0.1.
  - Automatic pump control is not implemented yet.
- **LED day binary sensor** (`binary_sensor`)
  - Indicates whether the grow light day period is active.
- **VWC sensor** (`sensor`)
  - Reports substrate volumetric water content.
- **LED sunrise input_datetime** (`input_datetime`)
  - Grow light start time.
- **LED sunset input_datetime** (`input_datetime`)
  - Grow light end time.
- **P1 mode input_select** (`input_select`)
  - Stores the P1 steering mode.
- **P2 mode input_select** (`input_select`)
  - Stores the P2 steering mode.
- **P0 transpiration minutes input_number** (`input_number`)
  - Minimum P0 transpiration duration.
- **P1 duration minutes input_number** (`input_number`)
  - P1 morning steering window duration.
- **P1 shot duration seconds input_number** (`input_number`)
  - P1 shot duration setting for future control logic.
- **P2 shot duration seconds input_number** (`input_number`)
  - P2 shot duration setting for future control logic.
- **P2 shots input_number** (`input_number`)
  - Target number of P2 shots.
- **P1 soak minutes input_number** (`input_number`)
  - P1 soak countdown duration.
- **P2 soak minutes input_number** (`input_number`)
  - P2 soak countdown duration.
- **P2 end offset minutes input_number** (`input_number`)
  - Time before LED sunset when P2 should no longer be considered available.
- **Field capacity VWC input_number** (`input_number`)
  - Field capacity threshold used by diagnostics.
- **P1 start VWC input_number** (`input_number`)
  - VWC threshold for P1 start diagnostics.
- **P2 VWC drop input_number** (`input_number`)
  - Drop target used for P2 diagnostics.
- **P2 reference VWC input_number** (`input_number`)
  - Reference VWC used to calculate the P2 drop threshold.
- **P1 active input_boolean** (`input_boolean`)
  - Tracks whether P1 is currently active.
- **P1 done input_boolean** (`input_boolean`)
  - Tracks whether P1 has completed for the day.
- **P1 window opened today input_boolean** (`input_boolean`)
  - Tracks whether the P1 window opened today.
- **P1 shots done counter** (`counter`)
  - Tracks completed P1 shots.
- **P2 shots done counter** (`counter`)
  - Tracks completed P2 shots.
- **Last shot input_datetime** (`input_datetime`)
  - Timestamp used by the soak remaining diagnostics.

### Optional

- **Drain binary sensor** (`binary_sensor`)
  - Optional drain/runoff activity diagnostic input.
- **Drain tray binary sensor** (`binary_sensor`)
  - Optional drain tray leak or high-water diagnostic input.
- **VWC cap input_number** (`input_number`)
  - Optional maximum VWC cap used by diagnostics.
- **VWC hysteresis input_number** (`input_number`)
  - Optional VWC hysteresis value reserved for diagnostics/future logic.
- **P1 interval minutes input_number** (`input_number`)
  - Optional P1 interval helper reserved for diagnostics/future logic.
- **P2 interval minutes input_number** (`input_number`)
  - Optional P2 interval helper reserved for diagnostics/future logic.

## Provided sensors

- **Status**
  - Reports integration readiness.
- **Phase**
  - Reports the current crop steering phase.
- **P1 Soak Remaining**
  - Reports remaining P1 soak time in seconds.
- **P2 Soak Remaining**
  - Reports remaining P2 soak time in seconds.
- **Block Reason**
  - Reports a short diagnostic reason describing the current irrigation state and why a P1/P2 shot would or would not be allowed.


## Services

GrowAssistant – Crop Steering provides basic read/write service helpers for maintenance and external automation workflows. These services update the configured Home Assistant helper entities only; they do **not** implement a full pump control loop or native irrigation shot engine. Existing YAML automations or future control logic remain responsible for deciding when shots may run.

### `growassistant_crop_steering.reset_cycle`

Resets daily/cycle helper state for the configured integration entry:

- Turns off the configured **P1 active** input boolean.
- Turns off the configured **P1 done** input boolean.
- Turns off the configured **P1 window opened today** input boolean.
- Resets the configured **P1 shots done** counter.
- Resets the configured **P2 shots done** counter.
- Sets the configured **P2 reference VWC** input number to `0` when configured.

This service does **not** touch the pump switch.

### `growassistant_crop_steering.start_p1`

Prepares helper state so existing Home Assistant YAML/automation logic can begin P1 behavior:

- Turns on the configured **P1 active** input boolean.
- Turns on the configured **P1 window opened today** input boolean.
- Turns off the configured **P1 done** input boolean.
- Sets the configured **P2 reference VWC** input number to `0` when configured.
- Sets the configured **Last shot** input datetime to the current time minus **P1 soak minutes** minus one second, allowing external logic that checks soak time to permit the first P1 shot.

This service does **not** turn on the pump.

### `growassistant_crop_steering.stop_pump`

Turns off only the configured pump switch. This is an explicit safety/manual stop service and does not modify cycle state helpers.

## Phase states

The phase sensor can report these states:

- `off`
- `pre_on`
- `p0_transpiration`
- `p1_morning`
- `p2_midday`
- `p3_dryback`

## Safety warning

This integration is intended for irrigation diagnostics and basic helper services. Full automatic pump control is not implemented in v0.1; the only pump-facing service is the explicit manual/safety `growassistant_crop_steering.stop_pump` service.

Future pump control should always use a physical/electrical failsafe and Home Assistant failsafe automation. Do not rely on Home Assistant, this integration, or software logic alone to prevent flooding, pump damage, crop damage, or electrical hazards.

## Roadmap

- **v0.1 diagnostics**
  - Read-only setup, diagnostic sensors, phase state, soak countdowns, and block reason diagnostics.
- **v0.2 services / reset helpers**
  - Basic service helpers for operational resets, P1 start preparation, and manual/safety pump stop workflows.
- **v0.3 optional blueprint-based shot engine**
  - Optional Home Assistant automation/blueprint approach for shot orchestration.
- **v0.4 native Python irrigation engine**
  - Native integration-side irrigation engine with appropriate safeguards and controls.

## Development

Recommended validation checks for changes:

- HACS validation.
- Hassfest validation.
- Python compile checks.

Example local checks:

```bash
python -m compileall custom_components/growassistant_crop_steering
```

Use Home Assistant development tooling and CI workflows for full HACS and hassfest validation when available.

## License

MIT License. See [LICENSE](LICENSE).
