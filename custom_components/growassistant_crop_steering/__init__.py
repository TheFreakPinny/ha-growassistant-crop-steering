"""The GrowAssistant Crop Steering integration scaffold."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, SERVICE_REFRESH

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR,)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up GrowAssistant Crop Steering services."""

    async def _handle_refresh(call: ServiceCall) -> None:
        """Handle a scaffold refresh request."""
        _LOGGER.info(
            "GrowAssistant Crop Steering refresh requested; irrigation engine is not implemented yet"
        )

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _handle_refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GrowAssistant Crop Steering from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a GrowAssistant Crop Steering config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
