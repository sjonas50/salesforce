"""``offramp health``: static health checks over an extract directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from offramp.core.models import Component, SchemaSnapshot
from offramp.core.process import ProcessDefinition
from offramp.understand.health import HealthFinding, run_health_checks, summarize


def add_health_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "health", help="Static checks: dead picklist values, callouts in save paths, owner alerts…"
    )
    p.add_argument("--from", dest="extract_dir", type=Path, required=True, help="Extract dir.")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--fail-on",
        choices=["error", "warning", "never"],
        default="never",
        help="Exit 1 when a finding of this severity (or worse) exists.",
    )
    p.set_defaults(func=_run)


def load_extract(
    extract_dir: Path,
) -> tuple[list[Component], list[ProcessDefinition], SchemaSnapshot | None]:
    comps = [
        Component.model_validate(c)
        for c in json.loads((extract_dir / "components.json").read_text(encoding="utf-8"))
    ]
    raw = json.loads((extract_dir / "processes.json").read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("processes", [])
    defs = [ProcessDefinition.model_validate(i) for i in items]
    schema_path = extract_dir / "schema.json"
    schema = (
        SchemaSnapshot.model_validate_json(schema_path.read_text(encoding="utf-8"))
        if schema_path.exists()
        else None
    )
    return comps, defs, schema


def print_findings(findings: list[HealthFinding], as_json: bool) -> None:
    if as_json:
        print(json.dumps([f.to_jsonable() for f in findings], indent=2))
        return
    for f in findings:
        obj = f" [{f.object}]" if f.object else ""
        print(f"{f.severity:<8} {f.code:<34} {f.component}{obj}")
        print(f"         {f.message}")
    s = summarize(findings)
    print(f"health: {s['errors']} errors, {s['warnings']} warnings, {s['infos']} infos")


def _run(args: argparse.Namespace) -> int:
    comps, defs, schema = load_extract(args.extract_dir)
    findings = run_health_checks(comps, defs, schema)
    print_findings(findings, args.json)
    s = summarize(findings)
    if args.fail_on == "error" and s["errors"]:
        return 1
    if args.fail_on == "warning" and (s["errors"] or s["warnings"]):
        return 1
    return 0
