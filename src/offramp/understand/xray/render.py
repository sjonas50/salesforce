"""X-Ray report rendering — HTML + JSON (schema 2.0).

The HTML report is the customer-facing deliverable; the JSON export is the
machine-readable companion. Both are built from the dependency graph, so
every number in the report can be traced to an edge with evidence (AD-30).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, SchemaSnapshot
from offramp.extract.audit import CoverageReport
from offramp.extract.ooe_audit.audit import SurfaceAuditReport
from offramp.understand import impact
from offramp.understand.annotate import Annotation
from offramp.understand.clustering import BusinessProcess
from offramp.understand.complexity import ComplexityScore
from offramp.understand.dependencies import DependencyGraph
from offramp.understand.orphan.resolver import ResolutionReport

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[3].parent / "templates"
_LEGACY_CATEGORIES = {CategoryName.WORKFLOW_RULE.value, CategoryName.PROCESS_BUILDER.value}


def _template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        keep_trailing_newline=True,
    )


@dataclass
class XRayInputs:
    """Everything the renderer consumes."""

    org_alias: str
    components: list[Component]
    coverage: CoverageReport
    ooe: SurfaceAuditReport
    graph: DependencyGraph
    processes: list[BusinessProcess]
    orphans: ResolutionReport
    complexity: dict[str, ComplexityScore] = field(default_factory=dict)
    annotations: list[Annotation] = field(default_factory=list)
    schema: SchemaSnapshot | None = None
    save_impact_objects: list[str] = field(default_factory=list)
    partial_categories: list[str] = field(default_factory=list)


def _band(value: int) -> str:
    if value < 35:
        return "b-low"
    if value < 70:
        return "b-med"
    return "b-high"


def _build_graph_json(g: DependencyGraph, *, max_nodes: int = 600) -> dict[str, Any]:
    """D3-friendly JSON: components + objects + fields with degree ≥ 1, edges with evidence."""
    degree: dict[str, int] = {}
    for e in g.edges:
        degree[str(e.source_id)] = degree.get(str(e.source_id), 0) + 1
        degree[str(e.target_id)] = degree.get(str(e.target_id), 0) + 1
    nodes = []
    for n in g.nodes.values():
        if n.kind == "external":
            continue
        if n.kind == "field" and degree.get(n.id, 0) < 2:
            continue  # keep the picture readable: only shared fields
        nodes.append(
            {
                "id": n.id,
                "name": n.api_name,
                "kind": n.kind,
                "category": n.category,
                "degree": degree.get(n.id, 0),
                "active": n.meta.get("active", True),
            }
        )
    nodes.sort(key=lambda x: -x["degree"])
    nodes = nodes[:max_nodes]
    keep = {n["id"] for n in nodes}
    links = [
        {
            "source": str(e.source_id),
            "target": str(e.target_id),
            "kind": e.kind.value,
            "evidence": e.evidence.value,
            "confidence": e.confidence,
            "api": e.corroborated_by_api,
        }
        for e in g.edges
        if str(e.source_id) in keep and str(e.target_id) in keep
    ]
    return {"nodes": nodes, "links": links, "categories": sorted({n["category"] for n in nodes})}


def _component_rows(inputs: XRayInputs) -> list[dict[str, Any]]:
    g = inputs.graph
    ann = {a.component_id: a for a in inputs.annotations}
    rows: list[dict[str, Any]] = []
    for c in inputs.components:
        n = g.node(str(c.id))
        score = inputs.complexity.get(str(c.id))
        a = ann.get(str(c.id))
        inbound = [e for e in g.inbound(str(c.id))]
        outbound = [e for e in g.outbound(str(c.id))]
        rows.append(
            {
                "id": str(c.id),
                "name": c.name,
                "category": c.category.value,
                "object": (n.object_name if n else None) or "",
                "active": bool(n.meta.get("active", True)) if n else True,
                "inbound": len(inbound),
                "outbound": len(outbound),
                "api_corroborated": sum(1 for e in outbound if e.corroborated_by_api),
                "translation_difficulty": score.translation_difficulty if score else None,
                "migration_risk": score.migration_risk if score else None,
                "diff_class": _band(score.translation_difficulty) if score else "",
                "risk_class": _band(score.migration_risk) if score else "",
                "summary": a.summary if a else "",
                "domain": a.domain if a else "",
                "partial": bool(c.raw.get("partial")) if isinstance(c.raw, dict) else False,
                "legacy": c.category.value in _LEGACY_CATEGORIES,
                "is_test": bool(n.meta.get("is_test")) if n else False,
                "dynamic_access": list(n.meta.get("dynamic_access", [])) if n else [],
                "surface": str(n.meta.get("surface", "")) if n else "",
            }
        )
    rows.sort(key=lambda r: (-r["inbound"] - r["outbound"], r["category"], r["name"].lower()))
    return rows


def _edge_rows(g: DependencyGraph, *, limit: int = 2000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e in g.edges:
        s = g.node(str(e.source_id))
        t = g.node(str(e.target_id))
        if s is None or t is None:
            continue
        rows.append(
            {
                "source": s.api_name,
                "source_kind": s.category if s.kind == "component" else s.kind,
                "kind": e.kind.value,
                "target": t.api_name,
                "target_kind": t.category if t.kind == "component" else t.kind,
                "evidence": e.evidence.value,
                "confidence": e.confidence,
                "api": e.corroborated_by_api,
                "notes": e.notes or "",
            }
        )
    rows.sort(key=lambda r: (r["source"].lower(), r["kind"], r["target"].lower()))
    return rows[:limit]


def _where_used_index(g: DependencyGraph) -> list[dict[str, Any]]:
    """Per field/object/class: inbound references grouped by category — the explorer's data."""
    out: list[dict[str, Any]] = []
    for n in g.nodes.values():
        if n.kind not in {"field", "object"} and not (
            n.kind == "component"
            and n.category in {"apex_class", "autolaunched_flow", "screen_flow"}
        ):
            continue
        inbound = g.inbound(n.id)
        if not inbound and n.kind != "field":
            continue
        wu = impact.where_used(g, n.id)
        if wu.total == 0 and n.kind != "field":
            continue
        out.append(
            {
                "id": n.id,
                "name": n.api_name,
                "kind": n.kind if n.kind != "component" else n.category,
                "object": n.object_name or "",
                "custom": bool(n.meta.get("custom")),
                "total": wu.total,
                "active_total": wu.active_total,
                "automation_total": wu.automation_total,
                "api_only": wu.api_only_count,
                "fill_rate": n.meta.get("fill_rate"),
                "refs": [
                    {
                        "name": r.node.api_name,
                        "category": r.node.category,
                        "kind": r.kind,
                        "evidence": r.evidence,
                        "confidence": r.confidence,
                        "api": r.corroborated_by_api,
                        "notes": r.notes or "",
                        "active": r.active,
                        "is_test": r.is_test,
                    }
                    for refs in wu.by_category.values()
                    for r in refs
                ],
            }
        )
    out.sort(key=lambda r: (-r["total"], r["name"].lower()))
    return out


def build_context(inputs: XRayInputs) -> dict[str, Any]:
    g = inputs.graph
    summary = impact.summarize(g, inputs.schema, inputs.components)
    unused = impact.unused_fields(g)
    legacy = impact.legacy_automation(g, inputs.components)
    objects = inputs.save_impact_objects or _default_save_objects(g)
    save_impacts = []
    for obj in objects:
        rows = impact.save_impact(g, obj)
        if rows:
            save_impacts.append(
                {
                    "object": obj,
                    "rows": [
                        {
                            "step": r.step,
                            "step_name": r.step_name.replace("_", " ").title(),
                            "component": r.component.api_name,
                            "category": r.component.category,
                            "relation": r.relation,
                            "evidence": r.evidence,
                            "confidence": r.confidence,
                        }
                        for r in rows
                    ],
                }
            )

    coverage_rows = sorted(
        [
            {
                "category": cat.value,
                "attempted": cov.attempted,
                "succeeded": cov.succeeded,
                "coverage_ratio": cov.coverage_ratio,
                "partial": cat.value in inputs.partial_categories,
            }
            for cat, cov in inputs.coverage.by_category.items()
            if cov.attempted > 0
        ],
        key=lambda r: r["category"],
    )
    ooe_rows = [
        {
            "step": int(o.step),
            "step_name": o.step.name.replace("_", " ").title(),
            "structural_count": o.structural_count,
            "priority": o.priority,
        }
        for o in inputs.ooe.observations
    ]

    return {
        "org_alias": inputs.org_alias,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary,
        "component_count": len(inputs.components),
        "process_count": len(inputs.processes),
        "orphan_resolved": len(inputs.orphans.resolved),
        "orphan_total": inputs.orphans.total_orphans,
        "coverage_by_category": coverage_rows,
        "ooe_observations": ooe_rows,
        "component_rows": _component_rows(inputs),
        "edge_rows": _edge_rows(g),
        "where_used": _where_used_index(g),
        "save_impacts": save_impacts,
        "unused_fields": [
            {
                "field": u.node.api_name,
                "object": u.node.object_name or "",
                "reason": u.reason,
                "referenced_by": u.referenced_by,
                "label": u.node.meta.get("label", ""),
                "fill_rate": u.fill_rate,
                "record_count": u.record_count,
                "empty": u.empty,
            }
            for u in unused
        ],
        "has_data_profile": any(
            "fill_rate" in n.meta for n in g.nodes.values() if n.kind == "field"
        ),
        "legacy": [
            {
                "name": la.component.api_name,
                "category": la.category,
                "active": la.active,
                "fields": la.fields_touched,
                "downstream": la.downstream,
            }
            for la in legacy
        ],
        "processes": [
            {
                "process_id": p.process_id,
                "label": p.label,
                "size": p.size,
                "objects": p.object_names,
                "categories": p.categories,
            }
            for p in inputs.processes
        ],
        "orphan_resolutions": [
            {
                "apex_class_name": r.apex_class_name,
                "channel": r.channel,
                "confidence": r.confidence,
                "evidence": r.evidence,
            }
            for r in inputs.orphans.resolved
        ],
        "unresolved_orphans": inputs.orphans.unresolved,
        "unresolved_references": [
            {
                "source": u.source_name,
                "kind": u.target_kind,
                "target": u.target_name,
                "evidence": u.evidence.value,
            }
            for u in g.unresolved
        ],
        "schema_objects": sorted(
            [
                {
                    "name": o.api_name,
                    "label": o.label,
                    "custom": o.custom,
                    "fields": sum(1 for f in inputs.schema.fields() if f.object_name == o.api_name),
                }
                for o in inputs.schema.objects()
            ]
            if inputs.schema
            else [],
            key=lambda r: r["name"].lower(),
        ),
        "graph_json": json.dumps(_build_graph_json(g)),
        "partial_categories": inputs.partial_categories,
    }


def _default_save_objects(g: DependencyGraph) -> list[str]:
    counts: dict[str, int] = {}
    for n in g.nodes.values():
        if n.kind == "component" and n.object_name:
            counts[n.object_name] = counts.get(n.object_name, 0) + 1
    return [o for o, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]


def render_html(inputs: XRayInputs, ctx: dict[str, Any] | None = None) -> str:
    env = _template_env()
    return env.get_template("xray.html.j2").render(**(ctx or build_context(inputs)))


def render_json(inputs: XRayInputs, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = ctx or build_context(inputs)
    ann = {a.component_id: a for a in inputs.annotations}
    return {
        "schema_version": "2.0",
        "generated_at": ctx["generated_at"],
        "org_alias": inputs.org_alias,
        "summary": ctx["summary"],
        "components": [
            {
                "id": str(c.id),
                "category": c.category.value,
                "name": c.name,
                "api_name": c.api_name,
                "namespace": c.namespace,
                "content_hash": c.content_hash,
                "references": (c.raw.get("references") if isinstance(c.raw, dict) else None),
                "annotation": ann[str(c.id)].model_dump(mode="json") if str(c.id) in ann else None,
                "complexity": _score_to_jsonable(inputs.complexity.get(str(c.id))),
            }
            for c in inputs.components
        ],
        "graph": inputs.graph.to_jsonable(),
        "business_processes": [
            {**p, "component_ids": bp.component_ids}
            for p, bp in zip(ctx["processes"], inputs.processes, strict=True)
        ],
        "where_used": ctx["where_used"],
        "save_impacts": ctx["save_impacts"],
        "unused_fields": ctx["unused_fields"],
        "legacy_automation": ctx["legacy"],
        "orphan_resolutions": {
            "resolved": ctx["orphan_resolutions"],
            "unresolved": ctx["unresolved_orphans"],
        },
        "ooe_surface_audit": [
            {
                "step": int(o.step),
                "step_name": o.step.name,
                "structural_count": o.structural_count,
                "priority": o.priority,
                "in_scope": o.in_scope,
            }
            for o in inputs.ooe.observations
        ],
        "coverage": ctx["coverage_by_category"],
        "schema": inputs.schema.model_dump(mode="json") if inputs.schema else None,
    }


def _score_to_jsonable(score: ComplexityScore | None) -> dict[str, Any] | None:
    if score is None:
        return None
    return {
        "translation_difficulty": score.translation_difficulty,
        "migration_risk": score.migration_risk,
        "drivers": list(score.drivers),
    }


def write_xray(inputs: XRayInputs, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = build_context(inputs)
    (out_dir / "xray.html").write_text(render_html(inputs, ctx), encoding="utf-8")
    (out_dir / "xray.json").write_text(
        json.dumps(render_json(inputs, ctx), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    log.info("understand.xray.written", out_dir=str(out_dir))
