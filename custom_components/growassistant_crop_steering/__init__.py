"""The GrowAssistant Crop Steering integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config import configured_entity_value
from .const import (
    BOOLEAN_STATE_DEFAULTS,
    CONF_LAST_SHOT,
    CONF_LAST_SHOT_TYPE,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_SHOTS_DONE,
    CONF_P1_SOAK_MIN,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P2_REF_VWC,
    CONF_P2_SHOTS_DONE,
    CONF_P3_EMERGENCY_SHOTS_DONE,
    CONF_PUMP_SWITCH,
    CONF_VWC_SENSOR,
    DOMAIN,
    LAST_SHOT_TYPES,
    NUMERIC_SETTING_DEFAULTS,
    SERVICE_CLEAR_LAST_SHOT,
    SERVICE_COMPLETE_P1,
    SERVICE_RESET_CYCLE,
    SERVICE_SET_LAST_SHOT_NOW,
    SERVICE_START_P1,
    SERVICE_STOP_PUMP,
    SIGNAL_LAST_SHOT_UPDATED,
)
from .vwc import calculate_vwc_state

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
)

DOMAIN_COUNTER = "counter"
DOMAIN_INPUT_BOOLEAN = "input_boolean"
DOMAIN_INPUT_DATETIME = "input_datetime"
DOMAIN_INPUT_NUMBER = "input_number"
DOMAIN_HOMEASSISTANT = "homeassistant"

SERVICE_COUNTER_RESET = "reset"
SERVICE_SET_DATETIME = "set_datetime"
SERVICE_SET_VALUE = "set_value"
SERVICE_TURN_OFF = "turn_off"
SERVICE_TURN_ON = "turn_on"

SIGNAL_SWITCH_STATE_UPDATED = f"{DOMAIN}_switch_state_updated"
SIGNAL_NUMBER_STATE_UPDATED = f"{DOMAIN}_number_state_updated"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DATETIME = "datetime"
ATTR_VALUE = "value"
ATTR_SHOT_TYPE = "shot_type"

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.cycle_reset"
DATA_AUTO_RESET = "auto_reset"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up GrowAssistant Crop Steering services."""

    async def _handle_reset_cycle(call: ServiceCall) -> None:
        """Reset configured daily/cycle state helpers."""
        _LOGGER.info("GrowAssistant Crop Steering reset_cycle service requested")
        for entry in _entries_for_service(
            hass, SERVICE_RESET_CYCLE, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _reset_cycle_for_entry(hass, entry)

    async def _handle_start_p1(call: ServiceCall) -> None:
        """Prepare helpers so external automation can start P1 shots."""
        _LOGGER.info("GrowAssistant Crop Steering start_p1 service requested")
        for entry in _entries_for_service(
            hass, SERVICE_START_P1, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _start_p1_for_entry(hass, entry)

    async def _handle_set_last_shot_now(call: ServiceCall) -> None:
        """Set the managed last-shot timestamp to now."""
        _LOGGER.info("GrowAssistant Crop Steering set_last_shot_now service requested")
        for entry in _entries_for_service(
            hass, SERVICE_SET_LAST_SHOT_NOW, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _set_last_shot_for_entry(
                hass, entry, dt_util.now(), call.data.get(ATTR_SHOT_TYPE)
            )

    async def _handle_complete_p1(call: ServiceCall) -> None:
        """Capture the P2 reference and complete P1 without controlling the pump."""
        _LOGGER.info("GrowAssistant Crop Steering complete_p1 service requested")
        for entry in _entries_for_service(
            hass, SERVICE_COMPLETE_P1, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _complete_p1_for_entry(hass, entry)

    async def _handle_clear_last_shot(call: ServiceCall) -> None:
        """Clear the managed last-shot timestamp."""
        _LOGGER.info("GrowAssistant Crop Steering clear_last_shot service requested")
        for entry in _entries_for_service(
            hass, SERVICE_CLEAR_LAST_SHOT, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _clear_last_shot_for_entry(hass, entry)

    async def _handle_stop_pump(call: ServiceCall) -> None:
        """Turn off the configured pump switch or input_boolean helper."""
        _LOGGER.info("GrowAssistant Crop Steering stop_pump service requested")
        for entry in _entries_for_service(
            hass, SERVICE_STOP_PUMP, call.data.get(ATTR_CONFIG_ENTRY_ID)
        ):
            await _stop_pump_for_entry(hass, entry)

    hass.services.async_register(DOMAIN, SERVICE_RESET_CYCLE, _handle_reset_cycle)
    hass.services.async_register(DOMAIN, SERVICE_START_P1, _handle_start_p1)
    hass.services.async_register(DOMAIN, SERVICE_COMPLETE_P1, _handle_complete_p1)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LAST_SHOT_NOW,
        _handle_set_last_shot_now,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Optional(ATTR_SHOT_TYPE): vol.In(LAST_SHOT_TYPES),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_LAST_SHOT, _handle_clear_last_shot
    )
    hass.services.async_register(DOMAIN, SERVICE_STOP_PUMP, _handle_stop_pump)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GrowAssistant Crop Steering from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator = _CycleResetCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {}).setdefault(DATA_AUTO_RESET, {})[entry.entry_id] = (
        coordinator
    )
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a GrowAssistant Crop Steering config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = (
            hass.data.get(DOMAIN, {}).get(DATA_AUTO_RESET, {}).pop(entry.entry_id, None)
        )
        if coordinator is not None:
            coordinator.async_stop()
    return unloaded


class _CycleResetCoordinator:
    """Reset an entry once at the beginning of each configured light cycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )
        self.last_cycle: str | None = None
        self._lock = asyncio.Lock()
        self._remove_listeners: list[Any] = []
        self._sunrise: time | None = None
        self._sunset: time | None = None

    async def async_start(self) -> None:
        """Load the durable marker, install listeners, and catch up if needed."""
        stored = await self.store.async_load()
        if stored is not None:
            self.last_cycle = stored.get("last_cycle")

        sunrise_entity = configured_entity_value(self.entry, CONF_LED_SUNRISE)
        sunset_entity = configured_entity_value(self.entry, CONF_LED_SUNSET)
        self._sunrise = _configured_time(self.hass, sunrise_entity)
        self._sunset = _configured_time(self.hass, sunset_entity)
        tracked_entities = [
            entity_id
            for entity_id in (sunrise_entity, sunset_entity)
            if entity_id is not None
        ]
        if tracked_entities:
            self._remove_listeners.append(
                async_track_state_change_event(
                    self.hass, tracked_entities, self._async_time_helper_changed
                )
            )
        self._schedule_sunrise()
        await self.async_check()

    def async_stop(self) -> None:
        """Remove all registered listeners."""
        for remove_listener in self._remove_listeners:
            remove_listener()
        self._remove_listeners.clear()

    @callback
    def _async_time_helper_changed(self, event: Event) -> None:
        """Reschedule when either configured time helper changes."""
        self.hass.async_create_task(self._async_apply_time_helper_change())

    async def _async_apply_time_helper_change(
        self, now: datetime | None = None
    ) -> None:
        """Apply new light times without redefining an already-reset grow day."""
        now = now or dt_util.now()
        async with self._lock:
            old_cycle = _light_cycle_start(now, self._sunrise, self._sunset)
            sunrise_entity = configured_entity_value(self.entry, CONF_LED_SUNRISE)
            sunset_entity = configured_entity_value(self.entry, CONF_LED_SUNSET)
            new_sunrise = _configured_time(self.hass, sunrise_entity)
            new_sunset = _configured_time(self.hass, sunset_entity)

            # A time-helper edit changes the timestamp used as the cycle marker.
            # If the old cycle was already reset, carry that fact to the same
            # logical grow day under the new sunrise instead of resetting again.
            if (
                old_cycle is not None
                and old_cycle.isoformat() == self.last_cycle
                and new_sunrise is not None
            ):
                replacement = datetime.combine(
                    old_cycle.date(), new_sunrise, tzinfo=old_cycle.tzinfo
                ).isoformat()
                self.last_cycle = replacement
                await self.store.async_save({"last_cycle": replacement})

            self._sunrise = new_sunrise
            self._sunset = new_sunset
            self._remove_sunrise_listener()
            self._schedule_sunrise()

        # This still enables catch-up when previously unavailable helpers become
        # valid, while the carried marker prevents a second reset after an edit.
        await self.async_check(now)

    def _remove_sunrise_listener(self) -> None:
        """Remove only the daily sunrise listener, if installed."""
        if len(self._remove_listeners) > 1:
            self._remove_listeners.pop()()

    def _schedule_sunrise(self) -> None:
        """Schedule a callback at the currently configured sunrise time."""
        sunrise = self._sunrise
        if sunrise is None:
            return
        self._remove_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_sunrise,
                hour=sunrise.hour,
                minute=sunrise.minute,
                second=sunrise.second,
            )
        )

    async def _async_sunrise(self, now: datetime) -> None:
        """Handle the configured daily sunrise."""
        await self.async_check(now)

    async def async_check(self, now: datetime | None = None) -> bool:
        """Reset once if a not-yet-recorded light cycle is currently active."""
        async with self._lock:
            cycle_start = _current_light_cycle_start(self.hass, self.entry, now)
            if cycle_start is None:
                return False
            cycle_marker = cycle_start.isoformat()
            if cycle_marker == self.last_cycle:
                return False

            await _reset_cycle_for_entry(self.hass, self.entry)
            self.last_cycle = cycle_marker
            await self.store.async_save({"last_cycle": cycle_marker})
            _LOGGER.info(
                "GrowAssistant Crop Steering automatically reset config entry %s for light cycle %s",
                self.entry.entry_id,
                cycle_marker,
            )
            return True


def _configured_time(hass: HomeAssistant, entity_id: str | None) -> time | None:
    """Read a time value from a configured input_datetime helper."""
    if entity_id is None or (state := hass.states.get(entity_id)) is None:
        return None
    try:
        return time.fromisoformat(state.state.rsplit(" ", maxsplit=1)[-1]).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def _current_light_cycle_start(
    hass: HomeAssistant, entry: ConfigEntry, now: datetime | None = None
) -> datetime | None:
    """Return the active cycle's sunrise, including cycles crossing midnight."""
    sunrise = _configured_time(hass, configured_entity_value(entry, CONF_LED_SUNRISE))
    sunset = _configured_time(hass, configured_entity_value(entry, CONF_LED_SUNSET))
    now = now or dt_util.now()
    return _light_cycle_start(now, sunrise, sunset)


def _light_cycle_start(
    now: datetime, sunrise: time | None, sunset: time | None
) -> datetime | None:
    """Return the active cycle start for explicit light schedule values."""
    if sunrise is None or sunset is None:
        return None

    today_start = datetime.combine(now.date(), sunrise, tzinfo=now.tzinfo)
    now_time = now.timetz().replace(tzinfo=None)
    if sunrise < sunset:
        return today_start if sunrise <= now_time < sunset else None
    if sunrise > sunset:
        if now_time >= sunrise:
            return today_start
        if now_time < sunset:
            return today_start - timedelta(days=1)
        return None
    return today_start if now_time >= sunrise else today_start - timedelta(days=1)


def _entries_for_service(
    hass: HomeAssistant, service_name: str, config_entry_id: str | None = None
) -> list[ConfigEntry]:
    """Return configured entries that should receive a service call."""
    entries = list(hass.config_entries.async_entries(DOMAIN))

    if config_entry_id is not None:
        entries = [entry for entry in entries if entry.entry_id == config_entry_id]

    if not entries:
        _LOGGER.warning(
            "GrowAssistant Crop Steering service %s skipped because no matching config entries are configured",
            service_name,
        )
        return []

    if config_entry_id is None and len(entries) > 1:
        _LOGGER.warning(
            "GrowAssistant Crop Steering service %s will be applied to %s config entries; only one entry is expected",
            service_name,
            len(entries),
        )

    return entries


async def _reset_cycle_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reset daily/cycle helper state for one config entry without touching the pump."""
    await _set_boolean_state(hass, entry, CONF_P1_ACTIVE, False)
    await _set_boolean_state(hass, entry, CONF_P1_DONE, False)
    await _set_boolean_state(hass, entry, CONF_P1_WINDOW_OPENED_TODAY, False)
    await _reset_shots_done_counter(hass, entry, CONF_P1_SHOTS_DONE)
    await _reset_shots_done_counter(hass, entry, CONF_P2_SHOTS_DONE)
    await _reset_shots_done_counter(hass, entry, CONF_P3_EMERGENCY_SHOTS_DONE)
    await _set_numeric_setting(hass, entry, CONF_P2_REF_VWC, 0)
    _LOGGER.info(
        "GrowAssistant Crop Steering reset_cycle completed for config entry %s",
        entry.entry_id,
    )


async def _reset_shots_done_counter(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str
) -> None:
    """Reset a managed shot counter and a legacy counter helper if present."""
    if config_key in NUMERIC_SETTING_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = 0
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering reset managed shot counter %s for config entry %s",
            config_key,
            entry.entry_id,
        )

    entity_id = _legacy_counter_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_COUNTER,
        SERVICE_COUNTER_RESET,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering reset legacy shot counter %s (%s) for config entry %s",
        config_key,
        entity_id,
        entry.entry_id,
    )


async def _start_p1_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set P1 state flags for one config entry without starting the pump."""
    await _set_boolean_state(hass, entry, CONF_P1_ACTIVE, True)
    await _set_boolean_state(hass, entry, CONF_P1_WINDOW_OPENED_TODAY, True)
    await _set_boolean_state(hass, entry, CONF_P1_DONE, False)
    await _set_numeric_setting(hass, entry, CONF_P2_REF_VWC, 0)
    await _set_last_shot_before_soak(hass, entry)
    _LOGGER.info(
        "GrowAssistant Crop Steering start_p1 completed for config entry %s",
        entry.entry_id,
    )


async def _complete_p1_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Capture current VWC and atomically order the managed P1 completion state."""
    vwc_state = calculate_vwc_state(
        hass, configured_entity_value(entry, CONF_VWC_SENSOR)
    )
    vwc = vwc_state["vwc"]
    if vwc is None:
        _LOGGER.warning(
            "GrowAssistant Crop Steering complete_p1 skipped config entry %s because no valid VWC was available from configured sensors %s; P2 reference and P1 state were preserved",
            entry.entry_id,
            vwc_state["vwc_sensors"],
        )
        return False

    # This order is deliberate: phase must not leave P1 until its P2 reference and
    # completion flag are both ready.
    await _set_numeric_setting(hass, entry, CONF_P2_REF_VWC, vwc)
    await _set_boolean_state(hass, entry, CONF_P1_DONE, True)
    await _set_boolean_state(hass, entry, CONF_P1_ACTIVE, False)
    _LOGGER.info(
        "GrowAssistant Crop Steering complete_p1 completed for config entry %s with P2 reference VWC %s",
        entry.entry_id,
        vwc,
    )
    return True


async def _set_boolean_state(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, value: bool
) -> None:
    """Persist a managed boolean state flag and mirror it to a legacy helper if present."""
    if config_key in BOOLEAN_STATE_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = value
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_SWITCH_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering set managed boolean state %s to %s for config entry %s",
            config_key,
            value,
            entry.entry_id,
        )

    entity_id = _legacy_input_boolean_entity_id(entry, config_key)
    if entity_id is None:
        return

    service = SERVICE_TURN_ON if value else SERVICE_TURN_OFF
    await hass.services.async_call(
        DOMAIN_INPUT_BOOLEAN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering set legacy %s (%s) to %s for config entry %s",
        config_key,
        entity_id,
        value,
        entry.entry_id,
    )


async def _stop_pump_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn off the configured pump switch or input_boolean for one config entry."""
    await _call_helper_service(
        hass, entry, CONF_PUMP_SWITCH, DOMAIN_HOMEASSISTANT, SERVICE_TURN_OFF
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering stop_pump completed for config entry %s",
        entry.entry_id,
    )


async def _set_numeric_setting(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, value: float | int
) -> None:
    """Persist a managed numeric setting and mirror it to a legacy helper if present."""
    if config_key in NUMERIC_SETTING_DEFAULTS:
        options = dict(entry.options)
        options[config_key] = value
        hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(hass, f"{SIGNAL_NUMBER_STATE_UPDATED}_{entry.entry_id}")
        _LOGGER.info(
            "GrowAssistant Crop Steering set managed numeric setting %s to %s for config entry %s",
            config_key,
            value,
            entry.entry_id,
        )

    entity_id = _legacy_numeric_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_INPUT_NUMBER,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering set legacy %s (%s) to %s for config entry %s",
        config_key,
        entity_id,
        value,
        entry.entry_id,
    )


async def _set_last_shot_before_soak(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Backdate last_shot so external P1 automation can allow the first shot."""
    p1_soak_min = _numeric_setting_value(hass, entry, CONF_P1_SOAK_MIN, 0)
    last_shot = dt_util.now() - timedelta(minutes=p1_soak_min, seconds=1)
    await _set_last_shot_for_entry(hass, entry, last_shot)


async def _set_last_shot_for_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    last_shot: datetime,
    shot_type: str | None = None,
) -> None:
    """Persist a managed last-shot timestamp and type, then mirror the timestamp."""
    options = dict(entry.options)
    options[CONF_LAST_SHOT] = last_shot.isoformat()
    options[CONF_LAST_SHOT_TYPE] = shot_type
    hass.config_entries.async_update_entry(entry, options=options)
    async_dispatcher_send(hass, f"{SIGNAL_LAST_SHOT_UPDATED}_{entry.entry_id}")
    _LOGGER.info(
        "GrowAssistant Crop Steering set managed %s to %s for config entry %s",
        CONF_LAST_SHOT,
        last_shot.isoformat(),
        entry.entry_id,
    )

    last_shot_entity_id = _legacy_input_datetime_entity_id(entry, CONF_LAST_SHOT)
    if last_shot_entity_id is None:
        return

    await hass.services.async_call(
        DOMAIN_INPUT_DATETIME,
        SERVICE_SET_DATETIME,
        {
            ATTR_ENTITY_ID: last_shot_entity_id,
            ATTR_DATETIME: last_shot.strftime("%Y-%m-%d %H:%M:%S"),
        },
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering mirrored %s to legacy helper %s for config entry %s",
        CONF_LAST_SHOT,
        last_shot_entity_id,
        entry.entry_id,
    )


async def _clear_last_shot_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear the managed last-shot timestamp without requiring legacy helpers."""
    options = dict(entry.options)
    options[CONF_LAST_SHOT] = None
    options[CONF_LAST_SHOT_TYPE] = None
    hass.config_entries.async_update_entry(entry, options=options)
    async_dispatcher_send(hass, f"{SIGNAL_LAST_SHOT_UPDATED}_{entry.entry_id}")
    _LOGGER.info(
        "GrowAssistant Crop Steering cleared managed %s for config entry %s",
        CONF_LAST_SHOT,
        entry.entry_id,
    )

    if _legacy_input_datetime_entity_id(entry, CONF_LAST_SHOT) is not None:
        _LOGGER.info(
            "GrowAssistant Crop Steering skipped clearing legacy %s because input_datetime helpers cannot be emptied reliably",
            CONF_LAST_SHOT,
        )


async def _call_helper_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_key: str,
    domain: str,
    service: str,
) -> None:
    """Call a service for a configured helper entity if it is present."""
    entity_id = _configured_entity_id(entry, config_key)
    if entity_id is None:
        return

    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    _LOGGER.info(
        "GrowAssistant Crop Steering called %s.%s for %s (%s) on config entry %s",
        domain,
        service,
        config_key,
        entity_id,
        entry.entry_id,
    )


def _legacy_counter_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy counter helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_input_boolean_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy input_boolean helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_input_datetime_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy input_datetime helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _legacy_numeric_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a legacy input_number helper entity id if one is configured."""
    entity_id = entry.data.get(config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    return None


def _numeric_setting_value(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, default: float
) -> float:
    """Read a managed numeric setting, falling back to a legacy helper or default."""
    fallback = NUMERIC_SETTING_DEFAULTS.get(config_key, default)
    managed_value = entry.options.get(config_key)
    if managed_value is not None:
        try:
            return float(managed_value)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "GrowAssistant Crop Steering could not parse managed %s value %r as a number; using fallback",
                config_key,
                managed_value,
            )

    entity_id = _legacy_numeric_entity_id(entry, config_key)
    if entity_id is None:
        return fallback

    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not read %s (%s); using %s",
            config_key,
            entity_id,
            fallback,
        )
        return fallback

    try:
        return float(state.state)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not parse %s (%s) state %r as a number; using %s",
            config_key,
            entity_id,
            state.state,
            fallback,
        )
        return fallback


def _configured_entity_id(entry: ConfigEntry, config_key: str) -> str | None:
    """Return a configured entity id, logging and skipping if it is missing."""
    entity_id = configured_entity_value(entry, config_key)
    if isinstance(entity_id, str) and entity_id.strip():
        return entity_id

    _LOGGER.warning(
        "GrowAssistant Crop Steering config entry %s has no configured entity for %s; skipping",
        entry.entry_id,
        config_key,
    )
    return None


def _state_as_float(
    hass: HomeAssistant, entry: ConfigEntry, config_key: str, default: float
) -> float:
    """Read a configured entity state as a float."""
    entity_id = _configured_entity_id(entry, config_key)
    if entity_id is None:
        return default

    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not read %s (%s); using %s",
            config_key,
            entity_id,
            default,
        )
        return default

    try:
        return float(state.state)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "GrowAssistant Crop Steering could not parse %s (%s) state %r as a number; using %s",
            config_key,
            entity_id,
            state.state,
            default,
        )
        return default
