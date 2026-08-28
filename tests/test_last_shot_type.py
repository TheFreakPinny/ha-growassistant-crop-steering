"""Regression tests for persistent last-shot type tracking."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol

from custom_components.growassistant_crop_steering import (
    _clear_last_shot_for_entry,
    _set_last_shot_for_entry,
    _start_p1_for_entry,
    async_setup,
)
from custom_components.growassistant_crop_steering import sensor
from custom_components.growassistant_crop_steering.const import (
    CONF_LAST_SHOT,
    CONF_LAST_SHOT_TYPE,
    CONF_P1_SOAK_MIN,
    CONF_P2_SOAK_MIN,
    CONF_P3_EMERGENCY_SOAK_MIN,
    DOMAIN,
    LAST_SHOT_TYPE_P1,
    LAST_SHOT_TYPE_P2,
    LAST_SHOT_TYPE_P3_EMERGENCY,
    SERVICE_SET_LAST_SHOT_NOW,
)

NOW = datetime(2026, 8, 28, 6, 10, tzinfo=timezone.utc)


def _hass(entry=None):
    schemas = {}
    handlers = {}

    def register(domain, service, handler, schema=None):
        handlers[(domain, service)] = handler
        schemas[(domain, service)] = schema

    services = SimpleNamespace(async_register=register, async_call=AsyncMock())
    hass = SimpleNamespace(
        services=services,
        config_entries=SimpleNamespace(
            async_entries=lambda _domain: [] if entry is None else [entry],
            async_update_entry=lambda target, *, options: setattr(
                target, "options", options
            ),
        ),
        states=SimpleNamespace(get=lambda _entity_id: None),
        data={},
    )
    return hass, handlers, schemas


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shot_type",
    [LAST_SHOT_TYPE_P1, LAST_SHOT_TYPE_P2, LAST_SHOT_TYPE_P3_EMERGENCY],
)
async def test_typed_timestamp_is_persisted_atomically(shot_type) -> None:
    entry = SimpleNamespace(entry_id="entry", options={}, data={})
    hass, _, _ = _hass(entry)
    updates = Mock(
        side_effect=lambda target, *, options: setattr(target, "options", options)
    )
    hass.config_entries.async_update_entry = updates

    with patch("custom_components.growassistant_crop_steering.async_dispatcher_send"):
        await _set_last_shot_for_entry(hass, entry, NOW, shot_type)

    assert updates.call_count == 1
    saved = updates.call_args.kwargs["options"]
    assert saved[CONF_LAST_SHOT] == NOW.isoformat()
    assert saved[CONF_LAST_SHOT_TYPE] == shot_type


@pytest.mark.asyncio
async def test_untyped_service_call_clears_stale_type() -> None:
    entry = SimpleNamespace(
        entry_id="entry",
        options={CONF_LAST_SHOT_TYPE: LAST_SHOT_TYPE_P2},
        data={},
    )
    hass, handlers, _ = _hass(entry)
    await async_setup(hass, {})

    with (
        patch(
            "custom_components.growassistant_crop_steering.dt_util.now",
            return_value=NOW,
        ),
        patch("custom_components.growassistant_crop_steering.async_dispatcher_send"),
    ):
        await handlers[(DOMAIN, SERVICE_SET_LAST_SHOT_NOW)](SimpleNamespace(data={}))

    assert entry.options[CONF_LAST_SHOT] == NOW.isoformat()
    assert entry.options[CONF_LAST_SHOT_TYPE] is None


@pytest.mark.asyncio
async def test_invalid_service_shot_type_is_rejected_by_schema() -> None:
    hass, _, schemas = _hass()
    await async_setup(hass, {})

    schema = schemas[(DOMAIN, SERVICE_SET_LAST_SHOT_NOW)]
    with pytest.raises(vol.Invalid):
        schema({"shot_type": "invalid"})


@pytest.mark.asyncio
async def test_clear_last_shot_clears_timestamp_and_type_atomically() -> None:
    entry = SimpleNamespace(
        entry_id="entry",
        options={
            CONF_LAST_SHOT: NOW.isoformat(),
            CONF_LAST_SHOT_TYPE: LAST_SHOT_TYPE_P2,
        },
        data={},
    )
    hass, _, _ = _hass(entry)
    updates = Mock(
        side_effect=lambda target, *, options: setattr(target, "options", options)
    )
    hass.config_entries.async_update_entry = updates

    with patch("custom_components.growassistant_crop_steering.async_dispatcher_send"):
        await _clear_last_shot_for_entry(hass, entry)

    assert updates.call_count == 1
    assert entry.options[CONF_LAST_SHOT] is None
    assert entry.options[CONF_LAST_SHOT_TYPE] is None


@pytest.mark.asyncio
async def test_start_p1_backdates_timestamp_with_unknown_type() -> None:
    entry = SimpleNamespace(entry_id="entry", options={CONF_P1_SOAK_MIN: 15}, data={})
    hass, _, _ = _hass(entry)

    with (
        patch(
            "custom_components.growassistant_crop_steering.dt_util.now",
            return_value=NOW,
        ),
        patch("custom_components.growassistant_crop_steering.async_dispatcher_send"),
    ):
        await _start_p1_for_entry(hass, entry)

    assert entry.options[CONF_LAST_SHOT] == "2026-08-28T05:54:59+00:00"
    assert entry.options[CONF_LAST_SHOT_TYPE] is None
    hass.services.async_call.assert_not_awaited()


def _soak_state(
    last_shot,
    shot_type,
    requested_key=CONF_P3_EMERGENCY_SOAK_MIN,
    now=NOW,
):
    options = {
        CONF_LAST_SHOT: last_shot.isoformat(),
        CONF_P1_SOAK_MIN: 15,
        CONF_P2_SOAK_MIN: 30,
        CONF_P3_EMERGENCY_SOAK_MIN: 60,
    }
    if shot_type is not ...:
        options[CONF_LAST_SHOT_TYPE] = shot_type
    entry = SimpleNamespace(options=options, data={})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))
    with (
        patch.object(sensor, "_calculate_phase", return_value=("p3_dryback", {})),
        patch.object(sensor.dt_util, "now", return_value=now),
    ):
        return sensor._calculate_soak_remaining(
            hass, entry, requested_key, "p3_dryback"
        )


def test_p2_protection_carries_into_p3_without_extension() -> None:
    state = _soak_state(
        datetime(2026, 8, 28, 5, 30, tzinfo=timezone.utc),
        LAST_SHOT_TYPE_P2,
        now=datetime(2026, 8, 28, 5, 40, tzinfo=timezone.utc),
    )
    assert state["remaining_s"] == 20 * 60
    assert state["effective_soak_type"] == LAST_SHOT_TYPE_P2


def test_p2_protection_reaches_zero_after_thirty_minutes() -> None:
    state = _soak_state(
        datetime(2026, 8, 28, 5, 30, tzinfo=timezone.utc),
        LAST_SHOT_TYPE_P2,
        now=datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc),
    )
    assert state["remaining_s"] == 0


def test_p3_emergency_shot_uses_p3_soak() -> None:
    state = _soak_state(
        datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc),
        LAST_SHOT_TYPE_P3_EMERGENCY,
    )
    assert state["remaining_s"] == 50 * 60
    assert state["effective_soak_type"] == LAST_SHOT_TYPE_P3_EMERGENCY


@pytest.mark.parametrize("shot_type", [..., "invalid"])
def test_unknown_type_uses_caller_specific_legacy_soak(shot_type) -> None:
    state = _soak_state(datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc), shot_type)
    assert state["last_shot_type"] is None
    assert state["effective_soak_type"] == LAST_SHOT_TYPE_P3_EMERGENCY
    assert state["remaining_s"] == 50 * 60
