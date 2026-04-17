#!/usr/bin/env python3
"""Run the Phase 5 load benchmarks and print a one-shot summary.

Usage::

    uv run python scripts/benchmark.py

Wraps ``pytest -m load`` so CI + the build-plan gate get the same numbers.
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    cmd = ["uv", "run", "pytest", "-m", "load", "-v", "-s"]
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
