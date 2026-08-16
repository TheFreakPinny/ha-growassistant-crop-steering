"""Helpers for reading GrowAssistant config entry values."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry


def configured_entity_value(entry: ConfigEntry, config_key: str) -> Any:
    """Return an entity setting from options, falling back to entry data.

    Checking membership rather than truthiness is intentional: an optional entity
    can be cleared in the options flow and must not then fall back to its old value.
    """
    if config_key in entry.options:
        return entry.options.get(config_key)

    return entry.data.get(config_key)
