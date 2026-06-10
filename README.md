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
- Optional helper services for resetting cycle helpers, preparing P1 helper state, updating the last-shot timestamp, and manually stopping the configured pump entity.
- Optional Home Assistant button entities for dashboard access to the common helper services.
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

GrowAssistant – Crop Steering creates editable Home Assistant **number entities** for numeric crop steering settings and completed shot counters, editable **switch entities** for P1 state flags, and an integration-managed **Last Shot** timestamp sensor. New users do **not** need to create numeric `input_number` helpers manually for P0/P1/P2/VWC settings, `counter` helpers manually for P1/P2 shots done, `input_boolean` helpers for P1 state flags, or an `input_datetime` helper for the last shot timestamp. Existing setups that already selected legacy helpers remain supported for migration and backward compatibility; when no managed value has been stored yet, the integration can still fall back to those legacy helper values.

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
- P1 state flags are created by the integration as editable switch entities, so new users do **not** need to manually create `input_boolean` helpers for P1 active/done/window state.
- P1/P2 completed shot counters are created by the integration as editable number entities, so new users do **not** need to manually create `counter` helpers for P1/P2 shots done.
- The last irrigation shot timestamp is stored by the integration and exposed as the **Last Shot** sensor, so new users do **not** need to manually create an `input_datetime` helper for last shot time.

### Integration-managed P1 state switches

The integration creates editable switch entities for boolean-like P1 state flags. These states are persisted by the integration and survive Home Assistant restarts:

- **P1 Active** (`switch`)
  - Tracks whether P1 is currently active.
- **P1 Done** (`switch`)
  - Tracks whether P1 has completed for the day.
- **P1 Window Opened Today** (`switch`)
  - Tracks whether the P1 window opened today.

Existing setups that configured legacy `input_boolean` helpers for these state flags remain supported for migration/backward compatibility. When a managed switch value is present, sensors prefer the integration-managed state and services mirror changes to any configured legacy `input_boolean` helper.

### Integration-managed numeric settings and shot counters

The integration creates editable number entities for these settings and counters:

- **P0 Transpiration**
- **P1 Duration**
- **P1 Interval**
- **P1 Shot Duration**
- **P1 Soak**
- **P1 Start VWC**
- **P1 Shots**
- **P1 Shots Done**
- **P2 Interval**
- **P2 Shot Duration**
- **P2 Soak**
- **P2 Shots**
- **P2 Shots Done**
- **P2 End Offset**
- **P2 VWC Drop**
- **P2 Reference VWC**
- **Field Capacity VWC**
- **VWC Cap**
- **VWC Hysteresis**

Managed shot counters default to `0`, use the `shots` unit, and can be edited from Home Assistant like the other integration-managed number entities. Legacy `counter` helpers for **P1 shots done** and **P2 shots done** remain supported for existing config entries; diagnostic sensors prefer the managed number values and fall back to legacy counters only when no managed value is stored.

### Integration-managed last shot timestamp

The integration stores the last irrigation shot timestamp in the config entry options and exposes it as **Last Shot** (`sensor.growassistant_crop_steering_last_shot`). New installations do **not** need a separate `input_datetime` helper for last shot time. Soak countdown and block-reason diagnostics prefer the managed timestamp and fall back to a legacy configured `input_datetime` helper only for existing config entries that do not yet have a managed timestamp.

Use `growassistant_crop_steering.set_last_shot_now` from external automations after a completed shot, or use the optional blueprint, to keep the managed timestamp current.

### Integration-managed service buttons

The integration also creates optional button entities for common manual service actions so dashboard users can trigger them directly from Lovelace without opening Developer Tools:

- **Reset Cycle** — calls `growassistant_crop_steering.reset_cycle` for this GrowAssistant config entry.
- **Start P1** — calls `growassistant_crop_steering.start_p1` for this GrowAssistant config entry.
- **Stop Pump** — calls `growassistant_crop_steering.stop_pump` for this GrowAssistant config entry.
- **Set Last Shot Now** — calls `growassistant_crop_steering.set_last_shot_now` for this GrowAssistant config entry.
- **Clear Last Shot** — calls `growassistant_crop_steering.clear_last_shot` for this GrowAssistant config entry.

The underlying services remain available through **Developer Tools → Services/Actions** for advanced use, scripts, and automations.

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
- **Last Shot**
  - Reports the integration-managed last irrigation shot timestamp as a timestamp sensor, or falls back to a legacy configured `input_datetime` helper for existing setups until a managed value is stored.
- **Block Reason**
  - Reports a short diagnostic reason describing the current irrigation state and why a P1/P2 shot would or would not be allowed.


## Services

GrowAssistant – Crop Steering provides basic read/write service helpers for maintenance and external automation workflows. These services update the configured Home Assistant helper entities only; they do **not** implement a full pump control loop or native irrigation shot engine. Existing YAML automations or future control logic remain responsible for deciding when shots may run. The same actions are also exposed as integration-managed button entities for optional dashboard control, while the services remain available through **Developer Tools → Services/Actions**.

### `growassistant_crop_steering.reset_cycle`

Resets daily/cycle helper state for the configured integration entry:

- Turns off the managed **P1 Active** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Turns off the managed **P1 Done** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Turns off the managed **P1 Window Opened Today** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Resets the managed **P1 Shots Done** number entity to `0` and also resets a legacy configured `counter` helper when present.
- Resets the managed **P2 Shots Done** number entity to `0` and also resets a legacy configured `counter` helper when present.
- Sets the managed **P2 Reference VWC** number entity to `0` and mirrors that value to a legacy configured `input_number` helper when present.

This service does **not** touch the configured pump switch or input_boolean test helper.

### `growassistant_crop_steering.start_p1`

Prepares helper state so existing Home Assistant YAML/automation logic can begin P1 behavior:

- Turns on the managed **P1 Active** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Turns on the managed **P1 Window Opened Today** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Turns off the managed **P1 Done** switch and mirrors the change to a legacy configured `input_boolean` helper when present.
- Sets the managed **P2 Reference VWC** number entity to `0` and mirrors that value to a legacy configured `input_number` helper when present.
- Sets the managed **Last Shot** timestamp to the current time minus **P1 soak minutes** minus one second, allowing external logic that checks soak time to permit the first P1 shot. If a legacy `input_datetime` helper is configured, the service mirrors the same timestamp to it.

This service does **not** turn on the pump.

### `growassistant_crop_steering.set_last_shot_now`

Sets the integration-managed **Last Shot** timestamp to the current time. If an existing config entry has a legacy `input_datetime` helper configured, the service mirrors the timestamp to that helper. This is useful for external automations after a completed irrigation shot.

### `growassistant_crop_steering.clear_last_shot`

Clears the integration-managed **Last Shot** timestamp. Legacy `input_datetime` helpers are left unchanged because Home Assistant input datetime helpers cannot be emptied reliably.

### `growassistant_crop_steering.stop_pump`

Turns off only the configured pump switch or input_boolean test helper. Use a `switch` for real pump hardware and an `input_boolean` helper for safe test/dummy pump simulation. This is an explicit safety/manual stop service and does not modify cycle state helpers.


## Example dashboards

Copy-paste Lovelace dashboard examples are available for quick setup and reference:

- German sections dashboard example: `dashboards/crop_steering_dashboard_de.yaml`

The German sections dashboard example is intended for the Home Assistant raw dashboard configuration editor because it uses a top-level `views:` key. It is not a single manual card.

Entity IDs may differ depending on your Home Assistant language and entity registry, including the integration-managed service button entity IDs. If an entity is not found, check the GrowAssistant device page in Home Assistant for the entity IDs created in your instance.

## Optional automation blueprint

This repository includes an optional Home Assistant automation blueprint for users who want YAML-based pump orchestration before native Python pump control is implemented:

```text
blueprints/automation/growassistant_crop_steering/shot_engine.yaml
```

The blueprint can run conservative P1/P2 shots from the integration's diagnostic sensors and your existing helpers or managed entities. It watches the **Phase**, **P1 Soak Remaining**, and **P2 Soak Remaining** sensors, checks the configured P1/P2 state, turns the selected pump switch on for the configured shot duration, increments the matching managed shot-counter number or legacy counter helper, updates the integration-managed last shot timestamp through `growassistant_crop_steering.set_last_shot_now`, and sends a second delayed pump-off command as a failsafe. Existing legacy blueprint automations can still select external `counter` helpers; new automations may select the integration-managed `number` entities instead.

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
