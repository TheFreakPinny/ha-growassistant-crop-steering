# v0.2.0-beta.1 controlled beta testing

This beta exposes the development work through Phase 3C, including 12 read-only
native diagnostics. The Blueprint remains the production executor. Normal HA
setup leaves native execution disabled and constructs no execution controllers.
There is no native activation interface, automatic migration or pump activation.
Do not attempt to enable native irrigation for beta testing.

## Before installing

Use an isolated test Home Assistant instance with simulated entities first.
Make a **complete Home Assistant backup**, download it somewhere safe, and confirm
you can restore it. Record the installed stable version (previous stable:
`v0.1.16`), integration options, sensor/pump selections, entity IDs, Blueprint
version/inputs, automation definitions and dashboards. Record current counters,
last-shot timestamp/type, P1 state, P2 reference and cycle/reset markers. Preserve
any native journal files as evidence. Do not manually delete or edit `.storage`.

## Select this beta through HACS

These instructions apply **after a maintainer separately publishes a GitHub
prerelease tagged `v0.2.0-beta.1`** with manifest `0.2.0-beta.1`. Release preparation
and a draft PR do not make a HACS-selectable release. No tag/release is created by
this preparation.

1. Open HACS and the GrowAssistant – Crop Steering repository. If needed, add
   `TheFreakPinny/ha-growassistant-crop-steering` as a custom repository of category
   **Integration** using HACS's custom repositories menu.
2. Refresh repository information. Open **Download** for a first installation or
   the repository menu's **Redownload** for an existing installation. Enable
   **Show beta versions** / beta releases where your HACS version provides it.
   UI labels and placement vary between HACS versions.
3. Use the version selector to choose **`v0.2.0-beta.1`** explicitly. Check the
   selected version before confirming download. Do not choose the default branch
   as a substitute for this exact release.
4. Restart Home Assistant after download. For a new installation, add the
   GrowAssistant integration in Settings → Devices & services and use simulated
   test inputs. For an upgrade, retain the existing entry and configuration.
5. Confirm the installed version in HACS and the device software version is
   `0.2.0-beta.1`; the installed integration's manifest must match.

Prereleases are filtered unless beta visibility is enabled; do not assume an
update notification or automatic installation. HACS builds its version choices
from published, non-draft GitHub releases, with a bounded recent-release list
(current backend examines up to 30 releases). A bare tag, draft release or branch
is not equivalent to a selectable published prerelease. Older releases can fall
out of the list. If the exact beta is absent, refresh, check beta visibility and
GitHub publication status, and consult your installed HACS version's UI. Do not
install a different version assuming it is this beta. Use a supported HACS version
with explicit beta/version selection, or wait until the intended release is
available. Turning beta visibility on can expose later betas too; always select
this exact version for reproducible testing.

### Packaging review

`hacs.json` uses the standard integration defaults, with neither `zip_release`
nor `content_in_root`. HACS discovers the single integration at
`custom_components/growassistant_crop_steering/` and downloads that directory from
the selected release ref; no separately built ZIP asset is required. The manifest
domain matches the directory and includes configuration flow, documentation,
issue tracker and codeowners. Planned tag `v0.2.0-beta.1` matches manifest and
`const.VERSION` `0.2.0-beta.1` after the conventional `v` prefix is removed.
Configuration and storage schema versions remain unchanged.

The repository has HACS and hassfest validation workflows on push/PR, plus Python
CI. No workflow publishes releases or creates release archives. HACS configuration
requires no change for this beta. This review verifies packaging and selection
semantics; installation of an unpublished beta through HACS cannot be exercised.
References: [HACS release filtering and downloads](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/base.py)
and [integration directory/manifest discovery](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/integration.py)
(reviewed 2026-10-14; installed HACS behavior may vary).

## Beta checklist

- Confirm backup, exact version and existing entity IDs/options before proceeding.
- Confirm setup/reload completes without new errors in HA logs. Existing Blueprint
  configuration and entity references must remain intact. Do not trigger a real
  irrigation action merely to test this beta.
- Open Settings → Devices & services → GrowAssistant → its device and inspect
  the diagnostic entities. Use the 12-key table in the
  [native diagnostics reference](native-diagnostics.md#entity-reference): there
  should be **8 sensors and 4 binary sensors per config entry**. HA-generated IDs
  vary by device name and user renaming; match entity names/unique ID suffixes to
  the table rather than assuming example IDs. Check entity settings for disabled
  or hidden entities if any appear missing.
- In Developer Tools → States / More info inspect all 12: engine `disabled`,
  activation `off`, pump ownership `unverified`, migration `ineligible` and
  `native_activation_allowed: false`. Those states must remain so even with
  healthy simulated sensors.
- Check configured/valid/rejected VWC counts, quorum, disagreement and sensor
  readiness against simulated inputs and the documented provisional policy.
  Disagreement may be `unknown` with fewer than two valid readings; readiness is
  not authorization. Allow up to 60 seconds for ageing displays without events.
- Inspect recovery classification and last-shot outcome with `loaded_at`/Store
  statuses. Missing history or `unknown` is legitimate and not proof of safe
  recovery. Reload the integration to obtain a new journal snapshot, then verify
  all 12 entity IDs are preserved and no duplicate entities appear.
- Confirm counters, last-shot data, P1/P2 settings and existing dashboards remain
  as expected after restart/reload. Observe Blueprint operation only within an
  independently supervised test plan; the beta does not transfer pump ownership.
- Report version, sanitized logs, expected/actual diagnostic states and steps to
  reproduce. Do not include credentials or unredacted backups.

Known blockers remain: no production activation interface, unenforceable ownership
against arbitrary external writers, no independent hardware watchdog/physical-off
verification, provisional sensor defaults and native recovery/persistence limits.
See [native safety and migration](native-safety-migration.md). Software completion
and HA off do not prove delivered water or physical shutdown.

## Roll back to stable

1. Save beta logs and a new backup before rollback; retain the pre-beta backup too.
   Preserve journals and note configuration/state changes made during testing.
   In an operator-controlled maintenance window, pause the existing irrigation
   automation through its established controls and independently confirm pump-off
   before restarting/downgrading HA. Do not use native irrigation for this step.
2. In HACS's repository **Redownload** dialog, choose the recorded previous stable
   version explicitly (`v0.1.16` unless your prior stable differed), with beta
   visibility off if appropriate. Confirm the version, download and restart HA.
   If that stable release is outside HACS's available version list, restore the
   complete pre-beta backup using Home Assistant's supported restore workflow.
3. Keep the existing integration entry; do not remove/re-add it as a downgrade
   shortcut. This beta performs no configuration schema migration. A code-only
   downgrade does not undo changed options, counters, automation inputs or state.
   Stable no longer provides the 12 native diagnostics: remove beta-only dashboard
   references as needed, while preserving existing entity identifiers. Inactive
   entity-registry entries may remain; their presence is not running beta code.
4. Verify stable manifest/device version, configuration, entity IDs, logs,
   Blueprint inputs, counters, last-shot/soak timing, P1 state, P2 reference and
   cycle/reset marker against your records. Preserve native journals; stable code
   cannot establish that their uncertain-delivery/recovery evidence is resolved.
5. If configuration/state is incompatible or rollback fails, restore the complete
   pre-beta backup. A backup restores earlier state as well as code: elapsed time
   and any water delivered since that backup are not undone. Review stale counters,
   last-shot timing, phases and reset markers before resuming the established
   Blueprint. Do not replay missed shots or infer zero delivery from an old state.
   Keep irrigation paused if shutdown or accounting is uncertain; resolve it with
   the established operator procedure before resuming.

This is package rollback for an inactive native beta, not transfer of ownership
from an active native controller. No production access is required by release
preparation or its automated validation.
