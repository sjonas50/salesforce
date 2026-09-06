"""``offramp impact`` — where-is-this-used, change impact, save impact, cleanup candidates.

Works from an ``offramp extract`` output directory (``graph.json``) so it
answers in milliseconds without touching the org again.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import Dependency, DependencyKind, EvidenceChannel
from offramp.understand import impact
from offramp.understand.dependencies import DependencyGraph, GraphNode

log = get_logger(__name__)


def add_impact_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "impact", help="Query the dependency graph from an extract output directory."
    )
    p.add_argument(
        "--from",
        dest="extract_dir",
        type=Path,
        required=True,
        help="Directory written by `offramp extract`.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--where-used",
        metavar="NAME",
        help="Inbound references to a field/object/class/flow (e.g. Lead.Country__c).",
    )
    mode.add_argument("--change", metavar="NAME", help="Transitive impact of changing NAME.")
    mode.add_argument(
        "--save",
        metavar="OBJECT",
        help="Automations that fire on a save to OBJECT, in Order-of-Execution order.",
    )
    mode.add_argument(
        "--unused",
        action="store_true",
        help="Custom fields with no automation, code, or UI references.",
    )
    mode.add_argument(
        "--legacy",
        action="store_true",
        help="Workflow Rules and Process Builders with migration blast radius.",
    )
    p.add_argument("--depth", type=int, default=4)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p.set_defaults(func=_run)


def load_graph(extract_dir: Path) -> DependencyGraph:
    data = json.loads((extract_dir / "graph.json").read_text(encoding="utf-8"))
    g = DependencyGraph(org_alias=str(data.get("org_alias", "")))
    for n in data["nodes"]:
        g.add_node(
            GraphNode(
                id=n["id"],
                kind=n["kind"],
                category=n["category"],
                name=n["name"],
                api_name=n["api_name"],
                object_name=n.get("object_name"),
                meta=n.get("meta") or {},
            )
        )
    for e in data["edges"]:
        dep = g.add_edge(
            e["source_id"],
            e["target_id"],
            DependencyKind(e["kind"]),
            evidence=EvidenceChannel(e["evidence"]),
            confidence=float(e["confidence"]),
            notes=e.get("notes"),
        )
        dep.corroborated_by_api = bool(e.get("corroborated_by_api"))
    stats = data.get("stats", {})
    g.api_rows_seen = int(stats.get("api_rows_seen", 0))
    g.api_matched = int(stats.get("api_matched", 0))
    g.api_only = int(stats.get("api_only", 0))
    return g


def _run(args: argparse.Namespace) -> int:
    if not (args.extract_dir / "graph.json").is_file():
        log.error("impact.graph_missing", path=str(args.extract_dir))
        return 1
    g = load_graph(args.extract_dir)
    out: dict[str, Any]
    lines: list[str]
    if args.where_used:
        n = g.find(args.where_used)
        if n is None:
            log.error("impact.node_not_found", name=args.where_used)
            return 2
        wu = impact.where_used(g, n.id)
        out = wu.to_jsonable()
        lines = [
            f"Where used: {n.api_name} ({n.kind}) — {wu.total} references, {wu.api_only_count} API-only"
        ]
        for cat, refs in wu.by_category.items():
            lines.append(f"  {cat}")
            for r in refs:
                flag = " [api]" if r.corroborated_by_api else ""
                lines.append(
                    f"    {r.node.api_name:40} {r.kind:11} {r.evidence:14} {r.confidence:.2f}{flag}  {r.notes or ''}"
                )
    elif args.change:
        n = g.find(args.change)
        if n is None:
            log.error("impact.node_not_found", name=args.change)
            return 2
        rows = impact.impact_closure(g, n.id, max_depth=args.depth)
        out = {
            "target": n.api_name,
            "impacted": [
                {
                    "name": i.node.api_name,
                    "kind": i.node.kind,
                    "category": i.node.category,
                    "distance": i.distance,
                    "confidence": i.min_confidence,
                    "path": i.path,
                }
                for i in rows
            ],
        }
        lines = [f"Change impact: {n.api_name} — {len(rows)} nodes within depth {args.depth}"]
        for i in rows:
            lines.append(
                f"  d={i.distance} {i.node.api_name:40} {i.node.category:26} {i.min_confidence:.2f}  {' > '.join(i.path)}"
            )
    elif args.save:
        save_rows = impact.save_impact(g, args.save)
        out = {
            "object": args.save,
            "rows": [
                {
                    "step": sr.step,
                    "step_name": sr.step_name,
                    "component": sr.component.api_name,
                    "category": sr.component.category,
                    "relation": sr.relation,
                    "evidence": sr.evidence,
                    "confidence": sr.confidence,
                }
                for sr in save_rows
            ],
        }
        lines = [
            f"Save impact: {args.save} — {len(save_rows)} automations in Order-of-Execution order"
        ]
        for sr in save_rows:
            lines.append(
                f"  step {sr.step:2} {sr.step_name:22} {sr.component.api_name:40} "
                f"{sr.relation:9} {sr.evidence}"
            )
    elif args.unused:
        unused = impact.unused_fields(g)
        out = {
            "unused": [
                {"field": u.node.api_name, "reason": u.reason, "api_references": u.api_references}
                for u in unused
            ]
        }
        lines = [f"Unused custom fields: {len(unused)}"]
        for u in unused:
            lines.append(f"  {u.node.api_name:40} {u.reason:16} {', '.join(u.api_references)}")
    else:
        legacy = impact.legacy_automation(g, _components_from_graph(g))
        out = {
            "legacy": [
                {
                    "name": la.component.api_name,
                    "category": la.category,
                    "active": la.active,
                    "fields": la.fields_touched,
                    "downstream": la.downstream,
                }
                for la in legacy
            ]
        }
        lines = [f"Legacy automation: {len(legacy)}"]
        for la in legacy:
            lines.append(
                f"  {la.component.api_name:40} {la.category:16} active={la.active} "
                f"fields={len(la.fields_touched)} downstream={la.downstream}"
            )
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("\n".join(lines))
    return 0


def _components_from_graph(g: DependencyGraph) -> list[Any]:
    """Minimal Component-like objects for legacy_automation when only graph.json is available."""
    from offramp.core.models import CategoryName, Component, Provenance

    out: list[Component] = []
    for n in g.nodes.values():
        if n.kind != "component" or n.category not in {"workflow_rule", "process_builder"}:
            continue
        out.append(
            Component(
                id=uuid.UUID(n.id),
                org_alias=g.org_alias,
                category=CategoryName(n.category),
                name=n.name,
                api_name=n.api_name,
                raw={
                    "rules": [{"active": n.meta.get("active", True)}],
                    "status": "Active" if n.meta.get("active", True) else "Inactive",
                },
                content_hash=str(n.meta.get("content_hash") or "0" * 64),
                provenance=Provenance(source_tool="graph", source_version="0", api_version="66.0"),
            )
        )
    return out


_ = Dependency
