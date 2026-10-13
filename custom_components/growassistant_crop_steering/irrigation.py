"""Inactive, deterministic irrigation decisions. No execution or HA mutation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, valid_entity_id

from .const import (
    BOOLEAN_STATE_DEFAULTS,
    CONF_LAST_SHOT,
    CONF_LAST_SHOT_TYPE,
    CONFIG_ENTRY_KEYS,
    NUMERIC_SETTING_DESCRIPTIONS,
)
from .vwc import normalize_vwc_sensors

_RANGES = {item.key: item for item in NUMERIC_SETTING_DESCRIPTIONS}
_CLEAR = {"off", "dry", "closed", "clear"}
_WET = {"on", "wet", "open", "problem"}
_PHASES = {
    "P0": "p0_transpiration",
    "P1": "p1_morning",
    "P2": "p2_midday",
    "P3": "p3_dryback",
}


@dataclass(frozen=True)
class Inputs:
    """Resolved config keys and entity states; None means absent/unavailable.

    Config uses existing keys with resolved managed values or legacy helper
    states. VWC is resolved separately into entity-id/state pairs. Mapping copies
    prevent later HA/config changes from changing an already captured input.
    """

    config: Mapping[str, Any]
    vwc: Mapping[str, Any]

    def __post_init__(self) -> None:
        def scalar(value: Any) -> Any:
            return (
                value
                if value is None or isinstance(value, (str, float, int, bool))
                else "invalid"
            )

        object.__setattr__(
            self,
            "config",
            MappingProxyType(
                {
                    key: tuple(value)
                    if key == "vwc_sensor" and isinstance(value, list)
                    else scalar(value)
                    for key, value in self.config.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "vwc",
            MappingProxyType({key: scalar(value) for key, value in self.vwc.items()}),
        )


@dataclass(frozen=True)
class Windows:
    """Local aware boundaries for the current sunrise-to-sunrise cycle.

    P1 end is the start window/manual end, not the sensor-mode active P1 end.
    P2 start depends on completion in sensor mode. P3 may start early on limits.
    """

    cycle_start: datetime
    cycle_end: datetime
    sunset: datetime
    p1_start: datetime
    p1_end: datetime
    p2_end: datetime


@dataclass(frozen=True)
class Decision:
    """Immutable authorization diagnostic, never an execution instruction."""

    phase: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    pump_entity: str | None
    duration_s: float | None
    cycle_id: str | None
    shots_done: int | None
    shot_limit: int | None
    remaining_shots: int | None
    next_eligible_time: datetime | None
    soak_until: datetime | None
    windows: Windows | None
    vwc_average: float | None
    vwc_valid_count: int
    p1_start_eligible: bool
    p1_start_blocking_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible decision with ISO timestamps."""

        def encode(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [encode(item) for item in value]
            return value

        return encode(asdict(self))


def capture_inputs(hass: HomeAssistant, entry: ConfigEntry) -> Inputs:
    """Read one HA snapshot without services, writes, clocks or subscriptions."""
    config = {}
    for key in (*CONFIG_ENTRY_KEYS, CONF_LAST_SHOT, CONF_LAST_SHOT_TYPE):
        if key in entry.options:
            value = entry.options[key]
        elif key in entry.data:
            value = entry.data[key]
        elif key in _RANGES:
            value = _RANGES[key].default_value
        elif key in BOOLEAN_STATE_DEFAULTS:
            value = BOOLEAN_STATE_DEFAULTS[key]
        elif key in {"p1_mode", "p2_mode"}:
            value = "sensor"
        else:
            value = None
        if (
            key
            not in {
                "pump_switch",
                "vwc_sensor",
                "p1_mode",
                "p2_mode",
                CONF_LAST_SHOT_TYPE,
            }
            and not (
                key in entry.options
                and key in {*_RANGES, *BOOLEAN_STATE_DEFAULTS, CONF_LAST_SHOT}
            )
            and isinstance(value, str)
            and valid_entity_id(value)
        ):
            state = hass.states.get(value)
            if key in {"led_sunrise", "led_sunset"} and state is not None:
                try:
                    attrs = state.attributes
                    if "hour" in attrs and "minute" in attrs:
                        parts = [attrs["hour"], attrs["minute"], attrs.get("second", 0)]
                        if any(
                            isinstance(part, bool)
                            or _number(part) is None
                            or not float(part).is_integer()
                            for part in parts
                        ):
                            raise ValueError("invalid helper clock")
                        value = time(*(int(part) for part in parts)).isoformat()
                    else:
                        value = state.state.rsplit("T", 1)[-1].rsplit(" ", 1)[-1]
                except (ValueError, TypeError, OverflowError):
                    value = "invalid"
            elif (
                key == CONF_LAST_SHOT
                and state is not None
                and "timestamp" in state.attributes
            ):
                try:
                    value = datetime.fromtimestamp(
                        float(state.attributes["timestamp"]), UTC
                    ).isoformat()
                except (ValueError, TypeError, OverflowError, OSError):
                    value = "invalid"
            else:
                value = (
                    "invalid"
                    if key == CONF_LAST_SHOT
                    and (state is None or state.state in {"", "unknown", "unavailable"})
                    else None
                    if state is None
                    else state.state
                )
        config[key] = value
    pump = config["pump_switch"]
    state = (
        hass.states.get(pump)
        if isinstance(pump, str) and valid_entity_id(pump)
        else None
    )
    config["pump_state"] = None if state is None else state.state
    for key in ("drain_sensor", "drain_tray_sensor"):
        # Keep configuration presence separate from unavailable/missing state.
        raw = entry.options[key] if key in entry.options else entry.data.get(key)
        config[key + "_configured"] = raw not in (None, "")
    values = {}
    for entity in normalize_vwc_sensors(config["vwc_sensor"]):
        state = hass.states.get(entity) if valid_entity_id(entity) else None
        values[entity] = None if state is None else state.state
    return Inputs(config, values)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in {"on", "true", "yes", "1"}:
            return True
        if value.lower() in {"off", "false", "no", "0"}:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _wall(day: Any, clock: time, zone: Any) -> datetime:
    value = datetime.combine(day, clock, zone)
    # Ambiguous/nonexistent helper times require an explicit future DST policy.
    if value.utcoffset() != value.replace(fold=1).utcoffset() or value.astimezone(
        UTC
    ).astimezone(zone).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError("ambiguous or nonexistent schedule boundary")
    return value


def phase_windows(inputs: Inputs, now: datetime) -> Windows:
    """Resolve local wall-clock windows; reject unsafe schedule boundaries."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    config = inputs.config
    sunrise = time.fromisoformat(config["led_sunrise"])
    sunset = time.fromisoformat(config["led_sunset"])
    if sunrise.tzinfo is not None or sunset.tzinfo is not None or sunrise == sunset:
        raise ValueError("lighting helpers must specify distinct local times")
    for key in ("p0_transpiration_min", "p1_duration_min", "p2_end_offset_min"):
        value = _number(config.get(key))
        spec = _RANGES[key]
        if value is None or not spec.native_min_value <= value <= spec.native_max_value:
            raise ValueError("invalid phase duration")
    day = (
        now.date()
        if now.timetz().replace(tzinfo=None) >= sunrise
        else now.date() - timedelta(days=1)
    )
    start = _wall(day, sunrise, now.tzinfo)
    end = _wall(day + timedelta(days=1), sunrise, now.tzinfo)
    off = _wall(day + timedelta(days=sunset < sunrise), sunset, now.tzinfo)
    p1_start = start + timedelta(minutes=float(config["p0_transpiration_min"]))
    p1_end = p1_start + timedelta(minutes=float(config["p1_duration_min"]))
    p2_end = off - timedelta(minutes=float(config["p2_end_offset_min"]))
    for boundary in (p1_start, p1_end, p2_end):
        _wall(boundary.date(), boundary.timetz().replace(tzinfo=None), now.tzinfo)
    return Windows(start, end, off, p1_start, p1_end, p2_end)


def evaluate(inputs: Inputs, now: datetime) -> Decision:
    """Evaluate exactly this instant. No timers, catch-up, mutation or services.

    now must already use HA's configured local timezone (dt_util.as_local).
    Soak arithmetic uses UTC elapsed time, including Berlin DST transitions.
    """
    config = inputs.config
    reasons: list[str] = []

    def number(key: str, *, integer: bool = False) -> float | None:
        value = _number(config.get(key))
        spec = _RANGES[key]
        if (
            value is None
            or not spec.native_min_value <= value <= spec.native_max_value
            or (integer and not value.is_integer())
        ):
            reasons.append("invalid_" + key)
            return None
        return value

    def flag(key: str) -> bool | None:
        value = _boolean(config.get(key))
        if value is None:
            reasons.append("invalid_" + key)
        return value

    windows = None
    try:
        windows = phase_windows(inputs, now)
    except (ValueError, TypeError, KeyError, OverflowError):
        reasons.append("invalid_schedule")
    aware = now.tzinfo is not None and now.utcoffset() is not None
    instant = now.astimezone(UTC) if aware else None
    active = flag("p1_active")
    complete = flag("p1_done")
    p1_mode = config.get("p1_mode")
    p2_mode = config.get("p2_mode")
    if p1_mode not in {"manual", "sensor"}:
        reasons.append("invalid_p1_mode")
    if p2_mode not in {"manual", "sensor"}:
        reasons.append("invalid_p2_mode")
    p2_limit = number("p2_shots", integer=True)
    p2_done = number("p2_shots_done", integer=True)
    phase = "off"
    if windows is not None:
        day = instant < windows.sunset.astimezone(UTC)
        time_ok = instant < windows.p2_end.astimezone(UTC)
        available = (
            p2_limit is not None
            and p2_done is not None
            and p2_done < p2_limit
            and time_ok
        )
        in_p1 = (
            windows.p1_start.astimezone(UTC) <= instant < windows.p1_end.astimezone(UTC)
        )
        if not day:
            phase = _PHASES["P3"]
        elif instant < windows.p1_start.astimezone(UTC):
            phase = _PHASES["P0"]
        elif p1_mode == "manual":
            phase = (
                _PHASES["P1"]
                if in_p1
                else _PHASES["P2"]
                if available
                else _PHASES["P3"]
            )
        elif active or (not complete and in_p1):
            phase = _PHASES["P1"]
        else:
            phase = _PHASES["P2"] if complete and available else _PHASES["P3"]
    values = [_number(value) for value in inputs.vwc.values()]
    valid = [value for value in values if value is not None]
    average = sum(valid) / len(valid) if valid else None
    # Missing/unavailable peers retain the existing available-sensor average.
    # Non-finite or out-of-range readings are corrupt safety data, not absence.
    corrupt = False
    for raw in inputs.vwc.values():
        if raw in (None, "", "unknown", "unavailable"):
            continue
        value = _number(raw)
        if value is None or not 0 <= value <= 100:
            corrupt = True
    if corrupt:
        reasons.append("invalid_vwc")
        average = None
    if average is None or not math.isfinite(average):
        average = None
        reasons.append("vwc_unavailable")
    pump = config.get("pump_switch")
    if (
        not isinstance(pump, str)
        or not valid_entity_id(pump)
        or pump.split(".")[0] not in {"switch", "input_boolean"}
    ):
        reasons.append("invalid_pump_entity")
        pump = None
    if config.get("pump_state") != "off":
        reasons.append("pump_not_off")
    prefix = {"p1_morning": "p1", "p2_midday": "p2", "p3_dryback": "p3_emergency"}.get(
        phase
    )
    duration = done = limit = remaining = soak_until = next_time = None
    start_reasons = ["p1_start_phase_inactive"]
    if prefix is None:
        reasons.append("p0_blocked" if phase == _PHASES["P0"] else "phase_inactive")
    else:
        duration = number(prefix + "_shot_duration_s")
        done_value = number(prefix + "_shots_done", integer=True)
        limit_value = number(
            prefix + ("_max_shots" if prefix == "p3_emergency" else "_shots"),
            integer=True,
        )
        done = int(done_value) if done_value is not None else None
        limit = int(limit_value) if limit_value is not None else None
        if done is not None and limit is not None:
            remaining = max(0, limit - done)
            if remaining == 0:
                reasons.append("shot_limit_reached")
        last_type = config.get(CONF_LAST_SHOT_TYPE)
        if last_type not in {None, "p1", "p2", "p3_emergency"}:
            reasons.append("invalid_last_shot_type")
        soak = number(
            (last_type if last_type in {"p1", "p2", "p3_emergency"} else prefix)
            + "_soak_min"
        )
        raw_last = config.get(CONF_LAST_SHOT)
        if raw_last not in (None, ""):
            try:
                last = datetime.fromisoformat(raw_last)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=now.tzinfo)
                    _wall(last.date(), last.timetz().replace(tzinfo=None), now.tzinfo)
                if instant is None or last.astimezone(UTC) > instant:
                    raise ValueError("future last shot")
                if soak is not None:
                    soak_until = (
                        last.astimezone(UTC) + timedelta(minutes=soak)
                    ).astimezone(now.tzinfo)
                    if instant < soak_until.astimezone(UTC):
                        reasons.append("soak_active")
            except (ValueError, TypeError, OverflowError):
                reasons.append("invalid_last_shot")
        elif any(
            (_number(config.get(key)) or 0) > 0
            for key in ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")
        ):
            reasons.append("missing_last_shot")
        for key in (
            ("drain_sensor", "drain_tray_sensor")
            if prefix == "p1"
            else ("drain_tray_sensor",)
        ):
            configured = config.get(key + "_configured")
            if not isinstance(configured, bool):
                reasons.append("invalid_" + key + "_configured")
            if not configured and prefix == "p3_emergency":
                reasons.append("drain_tray_required")
            elif configured:
                state = config.get(key)
                if state in _WET:
                    reasons.append(key + "_wet")
                elif state not in _CLEAR:
                    reasons.append(key + "_unavailable")
        if prefix == "p1":
            capacity = number("field_capacity_vwc")
            if average is not None and capacity is not None and average >= capacity:
                reasons.append("field_capacity_reached")
            start_reasons = list(reasons)
            if active is not False:
                start_reasons.append("p1_already_active")
            if complete is not False:
                start_reasons.append("p1_complete")
            if p1_mode != "sensor":
                start_reasons.append("p1_manual_start_required")
            opened = _boolean(config.get("p1_window_opened_today"))
            if opened is not False:
                start_reasons.append("p1_start_window_already_opened_or_invalid")
            if windows is None or not windows.p1_start.astimezone(
                UTC
            ) <= instant < windows.p1_end.astimezone(UTC):
                start_reasons.append("p1_start_window_closed")
            start_threshold = _number(config.get("p1_start_vwc"))
            if start_threshold is None or not 0 <= start_threshold <= 100:
                start_reasons.append("invalid_p1_start_vwc")
            elif average is not None and average > start_threshold:
                start_reasons.append("vwc_above_start_threshold")
            if active is not True:
                reasons.append("p1_not_active")
            if complete is not False:
                reasons.append("p1_complete")
        elif prefix == "p2":
            if p2_mode != "sensor":
                reasons.append("p2_manual")
            if complete is not True:
                reasons.append("p1_not_done")
            reference = number("p2_ref_vwc")
            drop = number("p2_vwc_drop")
            cap = number("vwc_cap")
            if reference is not None and reference <= 0:
                reasons.append("p2_reference_missing")
            if average is not None:
                if (
                    reference is not None
                    and drop is not None
                    and average > reference - drop
                ):
                    reasons.append("p2_drop_not_reached")
                if cap is not None and average >= cap:
                    reasons.append("vwc_cap_active")
        else:
            if flag("p3_emergency_enabled") is not True:
                reasons.append("p3_emergency_disabled")
            threshold = number("p3_emergency_threshold_vwc")
            if average is not None and threshold is not None and average > threshold:
                reasons.append("p3_threshold_not_reached")
        if not reasons or set(reasons) == {"soak_active"}:
            candidate = max(
                instant, soak_until.astimezone(UTC) if soak_until else instant
            )
            end = (
                windows.cycle_end
                if prefix == "p3_emergency"
                else windows.p2_end
                if prefix == "p2"
                else windows.p1_end
                if p1_mode == "manual"
                else windows.sunset
            )
            if candidate < end.astimezone(UTC):
                next_time = candidate.astimezone(now.tzinfo)
    return Decision(
        phase,
        not reasons,
        tuple(dict.fromkeys(reasons)),
        pump,
        duration,
        windows.cycle_start.isoformat() if windows else None,
        done,
        limit,
        remaining,
        next_time,
        soak_until,
        windows,
        average,
        len(valid),
        not start_reasons,
        tuple(dict.fromkeys(start_reasons)),
    )
