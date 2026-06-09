# GrowAssistant – Crop Steering

[![Validate with hassfest](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml)
[![Validate with HACS](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml)

A Home Assistant custom integration for crop steering irrigation diagnostics and future irrigation control.

## Current status

**v0.1 is focused on read-only diagnostics, plus optional helper services and an optional blueprint for users who intentionally enable external automation.**

The integration reads existing Home Assistant helpers/entities and exposes diagnostic sensors for crop steering state. Its core integration logic does **not** run a native irrigation engine and does **not** autonomously control pumps in v0.1. The included services only update configured helper entities or manually turn the configured pump entity off, and the optional blueprint is the only provided path that can turn a pump on when a user explicitly installs and configures it.

## Current v0.1 features

- HACS-compatible Home Assistant custom integration.
- UI config flow through **Settings → Devices & services**.
- User selects existing Home Assistant helpers/entities during setup.
- Diagnostic sensors for status, phase, P1/P2 soak countdowns, and block reason.
- Optional helper services for resetting cycle helpers, preparing P1 helper state, and manually stopping the configured pump entity.
- Optional shot engine blueprint for YAML-based pump orchestration outside the integration.
- No native integration-side pump control or autonomous irrigation engine implemented yet.

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

GrowAssistant – Crop Steering creates editable Home Assistant **number entities** for numeric crop steering settings. New users do **not** need to create numeric `input_number` helpers manually for P0/P1/P2/VWC settings. Existing setups that already selected `input_number` helpers remain supported for migration and backward compatibility; when no managed number value has been changed yet, the integration can still fall back to those legacy helper values.

P1/P2 steering modes are configured directly during integration setup; no `input_select` helpers are required for those modes. During setup, choose each mode as either:

- `sensor` — sensor-controlled steering logic.
- `manual` — interval/manual schedule controlled behavior.

After setup, P1/P2 modes can be changed from the integration options without deleting and re-adding the integration. Options values override the setup values while preserving compatibility with existing config entries.

### Required

- **Pump switch or test helper** (`switch` or `input_boolean`)
  - Use a `switch` entity for real irrigation pump hardware.
  - Use an `input_boolean` helper for safe testing or dummy pump simulation without energizing real hardware.
  - Used only by the `growassistant_crop_steering.stop_pump` manual/safety service in v0.1.
  - Automatic pump control is not implemented yet.
- **VWC sensor(s)** (`sensor`)
  - Select one or multiple sensor entities that report substrate volumetric water content.
  - When multiple sensors are selected, all valid numeric values are averaged automatically.
  - Unknown, unavailable, or non-numeric sensor states are ignored.
  - If all selected sensors are unavailable or invalid, VWC-dependent diagnostics are blocked until at least one valid value is available.
- **LED sunrise input_datetime** (`input_datetime`)
  - Grow light start time. Create this helper as **Time only**.
  - Example LED sunrise/light start value: `19:00:00`.
- **LED sunset input_datetime** (`input_datetime`)
  - Grow light end time. Create this helper as **Time only**.
  - Example LED sunset/light end value: `07:00:00`.
  - `Date and time` helpers may work, but they are not recommended for LED sunrise/sunset because only the time-of-day schedule is expected.
  - The integration calculates LED day/night state, seconds since light-on, and seconds until light-off from the configured sunrise/sunset helpers. No external LED day binary sensor is required.
  - Fixed schedules that cross midnight, such as `19:00:00` to `07:00:00`, are supported.
- **P1 active** (`input_boolean`)
  - Tracks whether P1 is currently active.
- **P1 done** (`input_boolean`)
  - Tracks whether P1 has completed for the day.
- **P1 window opened today** (`input_boolean`)
  - Tracks whether the P1 window opened today.
- **P1 shots done** (`counter`)
  - Tracks completed P1 shots.
- **P2 shots done** (`counter`)
  - Tracks completed P2 shots.
- **Last shot time** (`input_datetime`)
  - Stores the last irrigation shot timestamp.

### Integration-managed numeric settings

The integration creates editable number entities for these settings:

- **P0 Transpiration**
- **P1 Duration**
- **P1 Interval**
- **P1 Shot Duration**
- **P1 Soak**
- **P1 Start VWC**
- **P1 Shots**
- **P2 Interval**
- **P2 Shot Duration**
- **P2 Soak**
- **P2 Shots**
- **P2 End Offset**
- **P2 VWC Drop**
- **P2 Reference VWC**
- **Field Capacity VWC**
- **VWC Cap**
- **VWC Hysteresis**

### Optional

- **Drain binary sensor** (`binary_sensor`)
  - Optional binary sensor that indicates irrigation drain/runoff activity.
- **Drain tray leak binary sensor** (`binary_sensor`)
  - Optional binary sensor that indicates a drain tray leak or high-water condition.

## Provided sensors

- **Status**
  - Reports integration readiness.
- **Phase**
  - Reports the current crop steering phase.
  - Calculates light state internally from the configured LED sunrise and LED sunset `input_datetime` helpers, including over-midnight schedules.
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
- Sets the managed **P2 Reference VWC** number entity to `0` and mirrors that value to a legacy configured `input_number` helper when present.

This service does **not** touch the configured pump switch or input_boolean test helper.

### `growassistant_crop_steering.start_p1`

Prepares helper state so existing Home Assistant YAML/automation logic can begin P1 behavior:

- Turns on the configured **P1 active** input boolean.
- Turns on the configured **P1 window opened today** input boolean.
- Turns off the configured **P1 done** input boolean.
- Sets the managed **P2 Reference VWC** number entity to `0` and mirrors that value to a legacy configured `input_number` helper when present.
- Sets the configured **Last shot** input datetime to the current time minus **P1 soak minutes** minus one second, allowing external logic that checks soak time to permit the first P1 shot.

This service does **not** turn on the pump.

### `growassistant_crop_steering.stop_pump`

Turns off only the configured pump switch or input_boolean test helper. Use a `switch` for real pump hardware and an `input_boolean` helper for safe test/dummy pump simulation. This is an explicit safety/manual stop service and does not modify cycle state helpers.

## Optional automation blueprint

This repository includes an optional Home Assistant automation blueprint for users who want YAML-based pump orchestration before native Python pump control is implemented:

```text
blueprints/automation/growassistant_crop_steering/shot_engine.yaml
```

The blueprint can run conservative P1/P2 shots from the integration's diagnostic sensors and your existing helpers. It watches the **Phase**, **P1 Soak Remaining**, and **P2 Soak Remaining** sensors, checks the configured P1/P2 helper state, turns the selected pump switch on for the configured shot duration, increments the matching shot counter, and sends a second delayed pump-off command as a failsafe.

Native Python pump control is **not implemented yet**. The blueprint is optional and is provided only for users who intentionally choose to build a Home Assistant automation around the current diagnostic sensors and helper services. You remain responsible for confirming the selected entities, shot durations, soak timing, and counter behavior in your own Home Assistant instance.

Because this blueprint can control real irrigation hardware, you must provide an independent physical/electrical failsafe such as an appropriate float switch, leak detector cutoff, timer relay, fused circuit, or other hardware protection. Do not rely on Home Assistant, this integration, the blueprint, or software logic alone to prevent flooding, pump damage, crop damage, or electrical hazards.

### Installing the blueprint manually

Copy the blueprint file into your Home Assistant configuration directory at the same relative path:

```text
blueprints/automation/growassistant_crop_steering/shot_engine.yaml
```

Then reload automations/blueprints or restart Home Assistant, create an automation from the blueprint, and review every input before enabling it.

## Phase states

The phase sensor can report these states:

- `off`
- `pre_on`
- `p0_transpiration`
- `p1_morning`
- `p2_midday`
- `p3_dryback`

## Safety warning

This integration is intended for irrigation diagnostics and basic helper services. Full automatic pump control is not implemented in v0.1; the only pump-facing service is the explicit manual/safety `growassistant_crop_steering.stop_pump` service, which turns off the configured `switch` or `input_boolean` test helper.

Future pump control should always use a physical/electrical failsafe and Home Assistant failsafe automation. Do not rely on Home Assistant, this integration, or software logic alone to prevent flooding, pump damage, crop damage, or electrical hazards.

## Roadmap

- **v0.1 initial release**
  - Read-only diagnostic setup with status, phase, soak countdown, and block reason sensors.
  - Optional helper services for reset, P1 start preparation, and manual/safety pump stop workflows.
  - Optional Home Assistant automation blueprint for users who intentionally choose YAML-based shot orchestration.
- **Future native Python irrigation engine**
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
