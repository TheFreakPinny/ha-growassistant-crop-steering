"""Test configuration for local integration imports."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))


@pytest.fixture(autouse=True)
def participant_envelope_storage(hass_storage, monkeypatch):
    """Mirror HA's existing in-memory Store fixture at the strict read boundary.

    Real-file tests still use the actual strict reader. Preserve both payload
    and revision metadata, and run the production envelope validator.
    """
    from custom_components.growassistant_crop_steering.recovery_revision import (
        _ParticipantStore,
    )

    records = hass_storage
    original = _ParticipantStore.async_read_versioned

    async def read(store):
        if Path(store.path).exists() or store.key not in records:
            return await original(store)
        envelope = deepcopy(records[store.key])
        # Older unit fixtures omit fields emitted by HA's real writer.
        envelope.setdefault("key", store.key)
        return store.decode_envelope(envelope)

    monkeypatch.setattr(_ParticipantStore, "async_read_versioned", read)


def pytest_addoption(parser):
    """Test-only scenario invocation; no integration option is registered."""
    parser.addoption("--crop-scenario", help="Test-only JSON scenario path")
    parser.addoption("--crop-output", help="Scenario artifacts directory")
