"""Read-only native diagnostic binary sensors; no execution or actions."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .native_diagnostics import (
    BINARY_SENSOR_NAMES,
    DATA_DIAGNOSTICS,
    NativeDiagnosticEntity,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Attach diagnostic entities to the shared read-only coordinator."""
    coordinator = hass.data[DOMAIN][DATA_DIAGNOSTICS][entry.entry_id]
    async_add_entities(
        [
            GrowAssistantNativeDiagnosticBinarySensor(coordinator, key)
            for key in BINARY_SENSOR_NAMES
        ]
    )


class GrowAssistantNativeDiagnosticBinarySensor(
    NativeDiagnosticEntity, BinarySensorEntity
):
    """False/unknown values never grant permission to start native irrigation."""

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data[self.key][0]
