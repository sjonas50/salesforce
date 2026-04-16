#!/usr/bin/env python3
"""Phase 2 gate: verify an X-Ray output directory.

Asserts the X-Ray product deliverables are all present and structurally
valid. Runnable as the build-plan Phase 2 gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify offramp xray output.")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--require-annotations",
        action="store_true",
        help="Fail unless every component has an LLM annotation.",
    )
    args = parser.parse_args()

    html = args.out_dir / "xray.html"
    js = args.out_dir / "xray.json"
    for p in (html, js):
        if not p.is_file():
            sys.stderr.write(f"missing required file: {p}\n")
            return 1
    if html.stat().st_size < 1024:
        sys.stderr.write(f"xray.html unexpectedly tiny: {html.stat().st_size} bytes\n")
        return 2

    payload = json.loads(js.read_text())
    if payload.get("schema_version") != "1.0":
        sys.stderr.write(f"unexpected schema_version: {payload.get('schema_version')}\n")
        return 3

    n = len(payload.get("components", []))
    if n == 0:
        sys.stderr.write("xray.json contains zero components\n")
        return 4

    nproc = len(payload.get("business_processes", []))
    if nproc == 0:
        sys.stderr.write("no business processes detected by clustering\n")
        return 5

    ooe = payload.get("ooe_surface_audit", [])
    if len(ooe) != 21:
        sys.stderr.write(f"OoE audit expected 21 rows, got {len(ooe)}\n")
        return 6

    if args.require_annotations:
        missing = [c["name"] for c in payload["components"] if c.get("annotation") is None]
        if missing:
            sys.stderr.write(f"missing LLM annotations on: {missing}\n")
            return 7

    in_scope = sum(1 for o in ooe if o["in_scope"])
    annotated = sum(1 for c in payload["components"] if c.get("annotation") is not None)
    print(
        f"OK: {n} components, {nproc} processes, {in_scope}/21 OoE steps in scope, "
        f"{annotated}/{n} annotated"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
