#!/usr/bin/env python3
"""Phase 1 gate: verify an ``offramp extract`` output directory.

Asserts coverage targets and structural invariants, prints a summary, exits
non-zero on failure. Used as the runnable gate in the build plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify offramp extract output.")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--min-categories",
        type=int,
        default=21,
        help="Minimum number of categories that must have at least one component.",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.95,
        help="Minimum success ratio per category (where attempted > 0).",
    )
    args = parser.parse_args()

    coverage_path = args.out_dir / "coverage.json"
    components_path = args.out_dir / "components.json"
    ooe_path = args.out_dir / "ooe_surface_audit.json"
    for p in (coverage_path, components_path, ooe_path):
        if not p.is_file():
            sys.stderr.write(f"missing required output file: {p}\n")
            return 1

    coverage = json.loads(coverage_path.read_text())
    components = json.loads(components_path.read_text())
    ooe = json.loads(ooe_path.read_text())

    cats_with_data = {row["category"] for row in coverage["by_category"] if row["succeeded"] > 0}
    n_cats = len(cats_with_data)
    if n_cats < args.min_categories:
        sys.stderr.write(
            f"only {n_cats} categories produced components; required >= {args.min_categories}\n"
        )
        return 2

    failed_categories: list[tuple[str, float]] = []
    for row in coverage["by_category"]:
        if row["attempted"] > 0 and row["coverage_ratio"] < args.min_coverage:
            failed_categories.append((row["category"], row["coverage_ratio"]))
    if failed_categories:
        for cat, ratio in failed_categories:
            sys.stderr.write(f"category {cat}: coverage {ratio:.2%} below threshold\n")
        return 3

    if len(ooe["observations"]) != 21:
        sys.stderr.write(f"OoE audit should report 21 steps, got {len(ooe['observations'])}\n")
        return 4

    print(
        f"OK: {len(components)} components across {n_cats} categories"
        f" (overall coverage {coverage['overall_coverage']:.2%}); "
        f"{sum(1 for o in ooe['observations'] if o['in_scope'])}/21 OoE steps in scope"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
