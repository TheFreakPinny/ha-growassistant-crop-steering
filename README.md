# GrowAssistant – Crop Steering

[![Validate with hassfest](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hassfest.yml)
[![Validate with HACS](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml/badge.svg)](https://github.com/Philipp/ha-growassistant-crop-steering/actions/workflows/hacs.yml)

GrowAssistant – Crop Steering is a Home Assistant custom integration scaffold for future crop-steering irrigation control.

> **Status:** v0.1 scaffold only. The irrigation engine, steering strategies, substrate models, and actuator control logic are intentionally not implemented yet.

## What is included in v0.1

- HACS-compatible repository layout under `custom_components/growassistant_crop_steering/`.
- Config-flow based setup from the Home Assistant UI.
- A placeholder status sensor that reports the scaffold is ready.
- English and German translations.
- Placeholder `growassistant_crop_steering.refresh` service for future expansion.
- GitHub Actions for HACS and hassfest validation.

## Installation with HACS

1. In Home Assistant, open **HACS**.
2. Open **Custom repositories**.
3. Add this repository URL as an **Integration**.
4. Install **GrowAssistant – Crop Steering**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration** and search for **GrowAssistant – Crop Steering**.

## Manual installation

Copy the integration directory into your Home Assistant configuration directory:

```text
custom_components/growassistant_crop_steering/
```

Restart Home Assistant, then add the integration from **Settings → Devices & services**.

## Integration domain

```text
growassistant_crop_steering
```

## Roadmap

Future releases may add:

- Crop steering strategy profiles.
- Dryback and substrate water-content tracking.
- Irrigation shot scheduling and safeguards.
- Sensor calibration and actuator abstractions.
- Dashboards and diagnostics.

## Development

This repository currently contains the initial scaffold only. Validate changes with the included GitHub Actions or locally with Home Assistant development tooling when available.

## License

MIT License. See [LICENSE](LICENSE).
