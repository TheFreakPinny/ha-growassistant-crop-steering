"""Test configuration for local integration imports."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))


def pytest_addoption(parser):
    """Test-only scenario invocation; no integration option is registered."""
    parser.addoption("--crop-scenario", help="Test-only JSON scenario path")
    parser.addoption("--crop-output", help="Scenario artifacts directory")
