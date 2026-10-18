#!/usr/bin/env python3
"""Launch a scenario only inside pytest's isolated HomeAssistant runtime."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/native_harness/test_scenario_file.py",
            "--timeout=0",
            f"--crop-scenario={args.scenario.resolve()}",
            f"--crop-output={args.output.resolve()}",
        ],
        cwd=root,
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    )


if __name__ == "__main__":
    sys.exit(main())
