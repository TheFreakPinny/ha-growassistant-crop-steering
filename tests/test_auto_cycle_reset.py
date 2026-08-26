"""Tests for the automatic light-cycle reset."""

from __future__ import annotations

from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.growassistant_crop_steering import (
    _CycleResetCoordinator,
    _current_light_cycle_start,
    _reset_cycle_for_entry,
)
from custom_components.growassistant_crop_steering.const import (
    CONF_LAST_SHOT,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_SHOTS_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS_DONE,
    CONF_P3_EMERGENCY_SHOTS_DONE,
)


class _States:
    def __init__(self, sunrise: str = "08:00:00", sunset: str = "20:00:00") -> None:
        self.values = {
            "input_datetime.sunrise": SimpleNamespace(state=sunrise),
            "input_datetime.sunset": SimpleNamespace(state=sunset),
        }

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self.values.get(entity_id)


class _Store:
    def __init__(self, marker: str | None = None) -> None:
        self.marker = marker
        self.async_save = AsyncMock(side_effect=self._save)

    async def async_load(self) -> dict[str, str] | None:
        return {"last_cycle": self.marker} if self.marker else None

    def _save(self, data: dict[str, str]) -> None:
        self.marker = data["last_cycle"]


def _entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry-1",
        data={
            CONF_LED_SUNRISE: "input_datetime.sunrise",
            CONF_LED_SUNSET: "input_datetime.sunset",
        },
        options={},
    )


def _hass(sunrise: str = "08:00:00", sunset: str = "20:00:00") -> SimpleNamespace:
    return SimpleNamespace(states=_States(sunrise, sunset))


@pytest.mark.asyncio
async def test_reset_at_start_of_new_light_cycle() -> None:
    """The first check after sunrise resets and records that cycle."""
    coordinator = _CycleResetCoordinator(_hass(), _entry())
    coordinator.store = _Store()

    with patch(
        "custom_components.growassistant_crop_steering._reset_cycle_for_entry",
        new_callable=AsyncMock,
    ) as reset:
        assert await coordinator.async_check(
            datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        )

    reset.assert_awaited_once()
    assert coordinator.store.marker == "2026-08-20T08:00:00+00:00"


@pytest.mark.asyncio
async def test_no_duplicate_reset_in_same_light_cycle() -> None:
    """Repeated callbacks in one cycle use the in-memory marker."""
    coordinator = _CycleResetCoordinator(_hass(), _entry())
    coordinator.store = _Store()
    with patch(
        "custom_components.growassistant_crop_steering._reset_cycle_for_entry",
        new_callable=AsyncMock,
    ) as reset:
        assert await coordinator.async_check(
            datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        )
        assert not await coordinator.async_check(
            datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        )
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_sunrise_change_does_not_reset_current_grow_day_twice() -> None:
    """A changed sunrise is adopted without redefining the running grow day."""
    hass = _hass()
    coordinator = _CycleResetCoordinator(hass, _entry())
    coordinator.store = _Store()
    coordinator._sunrise = time(8)
    coordinator._sunset = time(20)

    with (
        patch.object(coordinator, "_schedule_sunrise"),
        patch(
            "custom_components.growassistant_crop_steering._reset_cycle_for_entry",
            new_callable=AsyncMock,
        ) as reset,
    ):
        assert await coordinator.async_check(
            datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        )

        hass.states.values["input_datetime.sunrise"].state = "09:00:00"
        await coordinator._async_apply_time_helper_change(
            datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        )

        assert reset.await_count == 1
        assert coordinator.store.marker == "2026-08-20T09:00:00+00:00"
        assert await coordinator.async_check(
            datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        )

    assert reset.await_count == 2
    assert coordinator.store.marker == "2026-08-21T09:00:00+00:00"


@pytest.mark.asyncio
async def test_restart_during_cycle_uses_persistent_marker() -> None:
    """A newly created coordinator honors the marker loaded after a restart."""
    marker = "2026-08-20T08:00:00+00:00"
    coordinator = _CycleResetCoordinator(_hass(), _entry())
    coordinator.store = _Store(marker)
    coordinator.last_cycle = (await coordinator.store.async_load())["last_cycle"]
    with patch(
        "custom_components.growassistant_crop_steering._reset_cycle_for_entry",
        new_callable=AsyncMock,
    ) as reset:
        assert not await coordinator.async_check(
            datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
        )
    reset.assert_not_awaited()


def test_overnight_cycle_uses_previous_day_sunrise() -> None:
    """After midnight, a 19:00-07:00 cycle retains the prior date's identity."""
    start = _current_light_cycle_start(
        _hass("19:00:00", "07:00:00"),
        _entry(),
        datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc),
    )
    assert start == datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_automatic_reset_preserves_last_shot_and_never_calls_pump() -> None:
    """Cycle fields reset without touching the last-shot value or any service."""
    entry = _entry()
    entry.options = {
        CONF_P1_ACTIVE: True,
        CONF_P1_DONE: True,
        CONF_P1_WINDOW_OPENED_TODAY: True,
        CONF_P1_SHOTS_DONE: 4,
        CONF_P2_SHOTS_DONE: 2,
        CONF_P3_EMERGENCY_SHOTS_DONE: 3,
        CONF_P2_REF_VWC: 55.5,
        CONF_LAST_SHOT: "2026-08-20T07:55:00+00:00",
    }
    services = SimpleNamespace(async_call=AsyncMock())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda target, *, options: setattr(
                target, "options", options
            )
        ),
        data={},
        services=services,
    )
    with patch("custom_components.growassistant_crop_steering.async_dispatcher_send"):
        await _reset_cycle_for_entry(hass, entry)

    assert entry.options[CONF_LAST_SHOT] == "2026-08-20T07:55:00+00:00"
    assert entry.options[CONF_P1_ACTIVE] is False
    assert entry.options[CONF_P1_DONE] is False
    assert entry.options[CONF_P1_WINDOW_OPENED_TODAY] is False
    assert entry.options[CONF_P1_SHOTS_DONE] == 0
    assert entry.options[CONF_P2_SHOTS_DONE] == 0
    assert entry.options[CONF_P3_EMERGENCY_SHOTS_DONE] == 0
    assert entry.options[CONF_P2_REF_VWC] == 0
    services.async_call.assert_not_awaited()
