#!/usr/bin/env python3
"""Gate: verify an ``offramp xray`` output directory (report schema 2.0)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify offramp xray output.")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--min-edge-kinds", type=int, default=4)
    parser.add_argument("--min-evidence-channels", type=int, default=6)
    parser.add_argument("--require-annotations", action="store_true")
    args = parser.parse_args()

    html = args.out_dir / "xray.html"
    js = args.out_dir / "xray.json"
    for p in (html, js):
        if not p.is_file():
            sys.stderr.write(f"missing: {p}\n")
            return 1
    data = json.loads(js.read_text())
    if data.get("schema_version") != "2.0":
        sys.stderr.write(f"schema_version {data.get('schema_version')} != 2.0\n")
        return 2
    stats = data["graph"]["stats"]
    if len(stats["by_kind"]) < args.min_edge_kinds:
        sys.stderr.write(f"only {len(stats['by_kind'])} edge kinds\n")
        return 3
    if len(stats["by_evidence"]) < args.min_evidence_channels:
        sys.stderr.write(f"only {len(stats['by_evidence'])} evidence channels\n")
        return 4
    for e in data["graph"]["edges"]:
        if "evidence" not in e or "confidence" not in e:
            sys.stderr.write("edge without evidence/confidence\n")
            return 5
    if not data["business_processes"]:
        sys.stderr.write("no business processes\n")
        return 6
    if args.require_annotations and any(c["annotation"] is None for c in data["components"]):
        sys.stderr.write("component without annotation\n")
        return 7
    text = html.read_text()
    for section in ("Where is this used", "Save impact", "Unused", "Legacy"):
        if section not in text:
            sys.stderr.write(f"HTML missing section: {section}\n")
            return 8
    s = data["summary"]
    print(
        f"OK: {len(data['components'])} components, {stats['nodes']} nodes, {stats['edges']} edges "
        f"({len(stats['by_evidence'])} evidence channels), {len(data['business_processes'])} processes, "
        f"{s['unused_custom_fields']} unused fields, {s['legacy_automation']} legacy automations, "
        f"API cross-check {stats['api_matched']} matched / {stats['api_only']} API-only"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
