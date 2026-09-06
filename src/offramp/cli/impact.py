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
from offramp.core.models import DependencyKind, EvidenceChannel
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


Answer = tuple[dict[str, Any], list[str]]  # (json payload, text lines)


def _run(args: argparse.Namespace) -> int:
    if not (args.extract_dir / "graph.json").is_file():
        log.error("impact.graph_missing", path=str(args.extract_dir))
        return 1
    g = load_graph(args.extract_dir)
    try:
        if args.where_used:
            answer = _where_used(g, args.where_used)
        elif args.change:
            answer = _change(g, args.change, args.depth)
        elif args.save:
            answer = _save(g, args.save)
        elif args.unused:
            answer = _unused(g)
        else:
            answer = _legacy(g)
    except KeyError as exc:
        log.error("impact.node_not_found", name=str(exc))
        return 2
    payload, lines = answer
    print(json.dumps(payload, indent=2) if args.json else "\n".join(lines))
    return 0


def _find(g: DependencyGraph, name: str) -> GraphNode:
    n = g.find(name)
    if n is None:
        raise KeyError(name)
    return n


def _where_used(g: DependencyGraph, name: str) -> Answer:
    n = _find(g, name)
    wu = impact.where_used(g, n.id)
    lines = [
        f"Where used: {n.api_name} ({n.kind}) — {wu.total} references, {wu.api_only_count} API-only"
    ]
    for cat, refs in wu.by_category.items():
        lines.append(f"  {cat}")
        for r in refs:
            flag = " [api]" if r.corroborated_by_api else ""
            lines.append(
                f"    {r.node.api_name:40} {r.kind:11} {r.evidence:14} {r.confidence:.2f}{flag}"
                f"  {r.notes or ''}"
            )
    return wu.to_jsonable(), lines


def _change(g: DependencyGraph, name: str, depth: int) -> Answer:
    n = _find(g, name)
    rows = impact.impact_closure(g, n.id, max_depth=depth)
    payload = {
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
    lines = [f"Change impact: {n.api_name} — {len(rows)} nodes within depth {depth}"]
    lines += [
        f"  d={i.distance} {i.node.api_name:40} {i.node.category:26} {i.min_confidence:.2f}"
        f"  {' > '.join(i.path)}"
        for i in rows
    ]
    return payload, lines


def _save(g: DependencyGraph, sobject: str) -> Answer:
    rows = impact.save_impact(g, sobject)
    payload = {
        "object": sobject,
        "rows": [
            {
                "step": r.step,
                "step_name": r.step_name,
                "component": r.component.api_name,
                "category": r.component.category,
                "relation": r.relation,
                "evidence": r.evidence,
                "confidence": r.confidence,
            }
            for r in rows
        ],
    }
    lines = [f"Save impact: {sobject} — {len(rows)} automations in Order-of-Execution order"]
    lines += [
        f"  step {r.step:2} {r.step_name:22} {r.component.api_name:40} {r.relation:9} {r.evidence}"
        for r in rows
    ]
    return payload, lines


def _unused(g: DependencyGraph) -> Answer:
    unused = impact.unused_fields(g)
    payload = {
        "unused": [
            {"field": u.node.api_name, "reason": u.reason, "referenced_by": u.referenced_by}
            for u in unused
        ]
    }
    lines = [f"Unused custom fields: {len(unused)}"]
    lines += [f"  {u.node.api_name:40} {u.reason:16} {', '.join(u.referenced_by)}" for u in unused]
    return payload, lines


def _legacy(g: DependencyGraph) -> Answer:
    legacy = impact.legacy_automation(g, _components_from_graph(g))
    payload = {
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
    lines += [
        f"  {la.component.api_name:40} {la.category:16} active={la.active} "
        f"fields={len(la.fields_touched)} downstream={la.downstream}"
        for la in legacy
    ]
    return payload, lines


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
