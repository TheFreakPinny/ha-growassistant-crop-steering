"""Tests for GrowAssistant number entities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.number import NumberMode

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

        assert entity.entity_description.key == setting_description.key
        assert (
            entity.entity_description.native_min_value
            == setting_description.native_min_value
        )
        assert (
            entity.entity_description.native_max_value
            == setting_description.native_max_value
        )
        assert entity.entity_description.native_step == setting_description.native_step
        assert entity.entity_description.mode == NumberMode.BOX
