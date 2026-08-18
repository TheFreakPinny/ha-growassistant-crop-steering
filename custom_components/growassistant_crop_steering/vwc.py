"""Shared volumetric water content (VWC) calculations."""

from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

_UNAVAILABLE_STATES = {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}


def normalize_vwc_sensors(config_value: Any) -> list[str]:
    """Return configured VWC sensors as a list of entity IDs."""
    if isinstance(config_value, str):
        return [config_value] if config_value else []

    if isinstance(config_value, list):
        return [entity_id for entity_id in config_value if isinstance(entity_id, str)]

    return []


def calculate_vwc_state(hass: HomeAssistant, config_value: Any) -> dict[str, Any]:
    """Return averaged VWC state and diagnostics for configured sensors."""
    vwc_sensors = normalize_vwc_sensors(config_value)
    vwc_values: dict[str, float] = {}

    for entity_id in vwc_sensors:
        state = hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            continue

        try:
            vwc_values[entity_id] = float(state.state)
        except (TypeError, ValueError):
            continue

    valid_count = len(vwc_values)
    vwc = sum(vwc_values.values()) / valid_count if valid_count else None

    return {
        "vwc": vwc,
        "vwc_sensors": vwc_sensors,
        "vwc_values": vwc_values,
        "vwc_valid_count": valid_count,
        "vwc_average": vwc,
    }
