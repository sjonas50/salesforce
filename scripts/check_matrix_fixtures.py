#!/usr/bin/env python3
"""Pre-commit guard: changes to the translation matrix MUST update fixtures.

The translation matrix is the table of (Salesforce category, conditions) →
target tier that drives the generate engine. Changing it without updating the
shadow-validation fixtures has historically masked correctness regressions —
the rule of thumb is "matrix change implies fixture change."

Phase 3 lands the actual matrix file. Until then this script is a no-op
placeholder so the pre-commit hook is wired and ready.
"""

from __future__ import annotations

import sys
from pathlib import Path

MATRIX_PATH = Path("src/offramp/generate/translation_matrix.py")
FIXTURES_DIR = Path("tests/integration/fixtures/translation_matrix")


def main() -> int:
    if not MATRIX_PATH.exists():
        # Phase 0/1/2 — matrix not yet introduced. Hook is a no-op.
        return 0

    if not FIXTURES_DIR.exists():
        sys.stderr.write(
            f"Translation matrix exists at {MATRIX_PATH} but no fixtures at "
            f"{FIXTURES_DIR}. Add fixtures before merging.\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
