# Home Assistant lifecycle tests

These tests execute the custom integration inside the real Home Assistant test
instance supplied by `pytest-homeassistant-custom-component`. They use HA's config
entry and flow managers, real platform forwarding, entity/device registries,
service dispatch, dispatcher subscriptions, state events, and timer callbacks.
Production integration code, configuration, and secrets are unchanged.

## Coverage

| Test | Verified behavior |
| --- | --- |
| `test_setup_registers_all_entities_and_device` | Entry reaches `LOADED`; all four platforms register every expected unique ID, device association, and entity state; coordinator loads and catches up without operating the pump. |
| `test_services_and_entity_options_operate_without_reload` | Number and switch services persist options and update HA states without replacing the coordinator; the stop-pump button calls the mocked external service with the configured entity. |
| `test_coordinator_updates_persists_and_reschedules` | Same-cycle checks are idempotent; a real helper edit reschedules sunrise and carries the marker; the next sunrise resets counters and saves the marker through HA storage. |
| `test_options_flow_and_explicit_reload` | HA options flow persists configuration; explicit reload removes old subscriptions, uses new helpers, and preserves entity IDs; the previous helper no longer changes the coordinator. |
| `test_unload_cleans_listeners_entities_and_scheduled_callbacks` | Entry reaches `NOT_LOADED`; coordinator mapping, listeners, and entry-specific dispatcher subscriptions are removed; old state/time callbacks do not run; entry can load again. |
| `test_unload_failure_retains_active_resources` | A failed platform unload retains coordinator resources, then a successful unload cleans up. |
| `test_setup_not_ready_recovers_on_retry` | Transient platform-forwarding failure reaches `SETUP_RETRY` without a coordinator; explicit reload cancels the retry and succeeds. |
| `test_unavailable_helpers_recover_via_real_state_events` | Unavailable sunrise does not block setup; a valid helper event installs the daily callback and catches up without reload. |
| `test_same_cycle_reload_restores_marker_without_reset` | Two consecutive reloads restore the stored cycle marker, preserve shot counts, replace coordinators, and remove old listeners. |
| `test_entry_data_change_applies_on_explicit_reload` | Entry data changes take effect on explicit reload and subsequent events use the replacement helper. |
| `test_user_flow_creates_entry_and_prevents_duplicate` | HA user flow creates and initializes an entry; the unique-ID guard rejects a second instance. |

The coordinator is `_CycleResetCoordinator`, not a polling `DataUpdateCoordinator`.
Its updates come from light-time helper events and a daily sunrise callback.

## Reproduce

Use Python **3.13.16** and Home Assistant **2025.12.5**, matching CI:

```sh
python -m venv work/venv
. work/venv/bin/activate
python -m pip install -r .github/requirements-ci.txt
python -m pip check
python -m pip install --no-deps pytest-homeassistant-custom-component==0.13.301
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q
python -m ruff check custom_components tests
python -m ruff format --check custom_components tests
python -m compileall -q custom_components tests
git diff --check
```

For just the lifecycle tests, use `python -m pytest -q tests/lifecycle` with the
same environment variable. `pytest.ini` enables async fixtures and a 30-second
per-test timeout. The freezer keeps cycle tests deterministic while leaving
asyncio's monotonic clock running.

### Fixture dependency compatibility

Fixture release **0.13.301** pins HA **2025.12.4** in its package metadata; the next
release targets **2026.1.0b2**. Neither provides the requested exact baseline.
CI therefore installs explicitly pinned supporting dependencies, checks them,
then installs the fixture package with `--no-deps` to retain HA **2025.12.5**.
Pydantic/core, pytest/pytest-asyncio, and tqdm pins were aligned with that fixture
release; HA and Python baseline pins were retained.

A `pip check` after the fixture installation reports exactly this known conflict:

```text
pytest-homeassistant-custom-component 0.13.301 has requirement homeassistant==2025.12.4, but you have homeassistant 2025.12.5.
```

This is a deliberate metadata exception, validated by executing the entire suite;
it is not a fully resolver-compatible dependency set. Revisit it when an exact
baseline-compatible fixture release becomes available. CI's dependency check runs
before the fixture installation so other dependency conflicts still fail the job.

## Results

Executed on Python **3.13.16**, HA **2025.12.5**:

- **105 passed**: all **94 existing tests** plus **11 lifecycle tests**; no skips or expected failures.
- Ruff lint passed.
- Ruff formatting passed: 20 Python files already formatted.
- Python compilation passed.
- Git whitespace check passed.

## Boundaries and remaining limitations

- All helper and sensor states are synthetic. The external pump service is an
  `AsyncMock`; no physical devices, production HA, or real irrigation automation
  are used. The fixture plugin blocks network sockets while allowing asyncio's
  local Unix sockets. LiteLLM's import-time remote cost-map fetch is disabled.
- HA storage is the fixture's in-memory backing store. Save/load and same-cycle
  reload are verified; disk corruption, process restart, and filesystem failures
  are not simulated.
- Options and data updates do **not** automatically reload the integration: there
  is no integration update listener. Managed entities use persisted options live;
  changing the configured helper subscriptions requires explicit HA reload. The
  tests preserve and document this existing behavior.
- HA retains entity/device registry entries and may expose unavailable entity
  states after unload. Domain services intentionally survive entry unload. The
  cleanup assertions concern active subscriptions and callbacks, not deletion of
  HA's persistent registry records.
- Failure injection covers forwarding before coordinator initialization and a
  rejected platform unload. Coordinator startup/storage exceptions after platform
  forwarding, partial individual-platform failures, automatic retry timer backoff,
  and races with callbacks already queued at unload remain unverified.
- UTC cycle behavior is tested. DST transitions, concurrent entries (the user flow
  enforces a singleton), browser UI, and external automation blueprints require
  separate end-to-end coverage.
- In the managed execution environment, the command sandbox blocked cross-thread
  asyncio wakeups. Tests were run with local IPC permitted; the fixture's socket
  guard remained active. This requirement does not change CI or production HA.
