"""Validate the existing input resolver without a second parameter store."""

from homeassistant.core import valid_entity_id
from homeassistant.util import dt as dt_util

from .config import configured_entity_value
from .config_flow import _OPTIONAL_ENTITY_FIELDS, _REQUIRED_ENTITY_FIELDS
from .const import BOOLEAN_STATE_DEFAULTS, NUMERIC_SETTING_DESCRIPTIONS
from .irrigation import _boolean, _number, capture_inputs, phase_windows
from .pump import HARD_MAX_RUNTIME
from .vwc import normalize_vwc_sensors


def validated_configuration(hass, entry):
    """Return the normal immutable snapshot or concrete configuration failures."""
    inputs = capture_inputs(hass, entry)
    reasons = []
    for key, domains in (
        *_REQUIRED_ENTITY_FIELDS,
        *_OPTIONAL_ENTITY_FIELDS,
        ("led_day_sensor", "binary_sensor"),
    ):
        raw = configured_entity_value(entry, key)
        if key == "vwc_sensor":
            values = normalize_vwc_sensors(raw)
        else:
            values = [raw] if raw not in (None, "") else []
        if not values and key in dict(_REQUIRED_ENTITY_FIELDS):
            reasons.append(f"missing_{key}")
        allowed = [domains] if isinstance(domains, str) else domains
        for value in values:
            if (
                not isinstance(value, str)
                or not valid_entity_id(value)
                or value.split(".")[0] not in allowed
            ):
                reasons.append(f"invalid_{key}_domain")
    config = inputs.config
    if config["pump_state"] not in {"on", "off"}:
        reasons.append("pump_target_unavailable")
    for key in BOOLEAN_STATE_DEFAULTS:
        if _boolean(config[key]) is None:
            reasons.append(f"invalid_{key}")
    if _boolean(config["p1_active"]) and _boolean(config["p1_done"]):
        reasons.append("contradictory_p1_flags")
    if (
        _boolean(config["p3_emergency_enabled"])
        and not config["drain_tray_sensor_configured"]
    ):
        reasons.append("p3_emergency_requires_drain_tray_sensor")
    for spec in NUMERIC_SETTING_DESCRIPTIONS:
        value = _number(config[spec.key])
        if (
            value is None
            or not spec.native_min_value <= value <= spec.native_max_value
            or (spec.native_step == 1 and not value.is_integer())
        ):
            reasons.append(f"invalid_{spec.key}")
        if spec.key.endswith("shot_duration_s") and (
            value is None or not 0 < value <= HARD_MAX_RUNTIME
        ):
            reasons.append(f"invalid_{spec.key}_pump_hardlimit")
    for key in ("p1_mode", "p2_mode"):
        if config[key] not in {"sensor", "manual"}:
            reasons.append(f"invalid_{key}")
    fc = _number(config["field_capacity_vwc"])
    start = _number(config["p1_start_vwc"])
    drop = _number(config["p2_vwc_drop"])
    if fc is not None and start is not None and start > fc:
        reasons.append("p1_start_above_field_capacity")
    if drop is not None and fc is not None and drop > fc:
        reasons.append("p2_drop_above_field_capacity")
    try:
        windows = phase_windows(inputs, dt_util.now())
        if not (
            windows.cycle_start
            <= windows.p1_start
            < windows.p1_end
            <= windows.p2_end
            <= windows.sunset
            < windows.cycle_end
        ):
            reasons.append("inconsistent_phase_windows")
    except (ValueError, TypeError, KeyError, OverflowError):
        reasons.append("invalid_schedule")
    if reasons:
        raise ValueError(
            "Native configuration blocked: " + ", ".join(dict.fromkeys(reasons))
        )
    return inputs
