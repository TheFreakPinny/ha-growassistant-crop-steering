"""Audit regressions: development fixtures must never become production authority."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.core import ServiceRegistry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import growassistant_crop_steering as integration
from custom_components.growassistant_crop_steering import (
    recovery_ledger,
    recovery_participant,
    recovery_release,
    recovery_transaction,
)
from custom_components.growassistant_crop_steering.const import DOMAIN


def test_production_sources_never_import_test_harness():
    """NRS-031: inspect every production module, including dormant modules."""
    root = Path(integration.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(a.name for a in node.names)]
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                # Computed dynamic imports need explicit review, not a silent pass.
                assert node.args and isinstance(node.args[0], ast.Constant), path
                names = [node.args[0].value]
            for name in names:
                assert isinstance(name, str), path
                parts = name.split(".")
                assert not {"tests", "native_harness"}.intersection(parts), path
                assert not any(part.startswith("fake_") for part in parts), path


async def test_entry_lifecycle_never_constructs_recovery_authority(hass, monkeypatch):
    """NRS-031: setup/unload may expose diagnostics, never recovery owners."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Production lifecycle constructed recovery authority")

    for cls in (
        recovery_ledger._RecoveryLedger,
        recovery_participant._RecoveryGateCoordinator,
        recovery_transaction._RecoveryTransactionCoordinator,
        recovery_release._CycleReleaseCoordinator,
        recovery_release._HarnessReleaseAuthority,
        recovery_release._CycleReleasePermit,
    ):
        monkeypatch.setattr(cls, "__init__", forbidden)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(integration._CycleResetCoordinator, "async_start", AsyncMock())
    dispatch = AsyncMock(side_effect=AssertionError("Unexpected actuator dispatch"))
    monkeypatch.setattr(ServiceRegistry, "async_call", dispatch)
    services = hass.services.async_services()
    assert await integration.async_setup_entry(hass, entry)
    assert await integration.async_unload_entry(hass, entry)
    assert hass.services.async_services() == services
    dispatch.assert_not_awaited()
