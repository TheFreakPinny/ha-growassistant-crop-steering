#!/usr/bin/env bash
# Run from any directory; retain the configured proxy and TLS trust.
set -euo pipefail
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SETUP_ROOT=${GROWASSISTANT_SETUP_ROOT:-"$REPO/work/cloud"}
mkdir -p "$SETUP_ROOT"
export UV_CACHE_DIR="$SETUP_ROOT/uv-cache"
export UV_PYTHON_INSTALL_DIR="$SETUP_ROOT/python"
export UV_PYTHON_BIN_DIR="$SETUP_ROOT/bin"
export PIP_CACHE_DIR="$SETUP_ROOT/pip-cache"
python3 -m venv "$SETUP_ROOT/bootstrap"
"$SETUP_ROOT/bootstrap/bin/python" -m pip install --disable-pip-version-check 'uv==0.12.23'
UV="$SETUP_ROOT/bootstrap/bin/uv"
"$UV" python install 3.13.16
"$UV" venv --python 3.13.16 "$SETUP_ROOT/venv"
PYTHON="$SETUP_ROOT/venv/bin/python"
"$UV" pip install --python "$PYTHON" -r "$REPO/.github/requirements-ci.txt"
"$PYTHON" -m pip check
# Documented fixture exception: metadata pins HA 2025.12.4, tests use 2025.12.5.
"$UV" pip install --python "$PYTHON" --no-deps pytest-homeassistant-custom-component==0.13.301
"$PYTHON" - <<'PY'
import importlib
import importlib.metadata as metadata
import subprocess
import sys
assert sys.version_info[:3] == (3, 13, 16)
assert metadata.version('homeassistant') == '2025.12.5'
assert metadata.version('pytest-homeassistant-custom-component') == '0.13.301'
result = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True)
expected = 'pytest-homeassistant-custom-component 0.13.301 has requirement homeassistant==2025.12.4, but you have homeassistant 2025.12.5.'
assert result.returncode == 1 and result.stdout.strip() == expected, (result.stdout, result.stderr)
for module in ('homeassistant.core', 'pytest_homeassistant_custom_component'):
    importlib.import_module(module)
print('Pinned runtime and fixture imports verified; only documented metadata exception remains.')
PY
cd "$REPO"
LITELLM_LOCAL_MODEL_COST_MAP=True "$PYTHON" - <<'PY'
import importlib
for module in ('', '.config_flow', '.pump', '.number', '.sensor', '.switch', '.button'):
    importlib.import_module('custom_components.growassistant_crop_steering' + module)
print('Integration imports verified')
PY
printf 'Activate with: source %s/venv/bin/activate\n' "$SETUP_ROOT"
