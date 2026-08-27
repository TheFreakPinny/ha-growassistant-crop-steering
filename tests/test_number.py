"""Tests for GrowAssistant number entities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.number import NumberEntityDescription, NumberMode

from custom_components.growassistant_crop_steering.const import (
    NUMERIC_SETTING_DESCRIPTIONS,
)
from custom_components.growassistant_crop_steering.number import (
    GrowAssistantSettingNumber,
)


def test_setting_number_description_uses_box_mode() -> None:
    """All integration-managed number descriptions use direct numeric input."""
    entry = SimpleNamespace(entry_id="test-entry", data={}, options={})

    for setting_description in NUMERIC_SETTING_DESCRIPTIONS:
        entity = GrowAssistantSettingNumber(
            SimpleNamespace(), entry, setting_description
        )

        assert isinstance(entity.entity_description, NumberEntityDescription)
        assert entity.entity_description.mode == NumberMode.BOX
