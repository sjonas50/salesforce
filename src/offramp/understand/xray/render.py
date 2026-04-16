"""X-Ray report rendering — HTML + JSON.

The HTML report is the customer-facing X-Ray product deliverable. The JSON
export is the machine-readable companion (consumed downstream by the X-Ray
report's own tooling, by Phase 3 generation scope decisions, and by Shadow
Mode in Phase 4 for cluster-level routing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.extract.audit import CoverageReport
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.extract.ooe_audit.audit import SurfaceAuditReport
from offramp.understand.annotate import Annotation
from offramp.understand.clustering import BusinessProcess
from offramp.understand.complexity import ComplexityScore
from offramp.understand.orphan.resolver import ResolutionReport

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[3].parent / "templates"


def _template_env() -> Environment:
    """Resolve the templates directory relative to the repo root."""
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
    dispatch_edges: list[DispatchEdge]
    annotations: list[Annotation]
    complexity: dict[str, ComplexityScore]
    processes: list[BusinessProcess]
    orphans: ResolutionReport


def _band(value: int) -> str:
    """Map a 0-100 score to a CSS class for the heatmap badges."""
    if value < 35:
        return "b-low"
    if value < 70:
        return "b-med"
    return "b-high"


def _tier_class(tier: str) -> str:
    return {
        "tier1_rules": "b-tier1",
        "tier2_temporal": "b-tier2",
        "tier3_langgraph": "b-tier3",
    }.get(tier, "b-med")


def _build_graph_json(
    components: list[Component],
    dispatch_edges: list[DispatchEdge],
    *,
    components_by_name: dict[str, str],
) -> dict[str, Any]:
    """D3-friendly JSON: nodes + links + categories list for the color scale."""
    nodes: list[dict[str, Any]] = [
        {
            "id": str(c.id),
            "name": c.name,
            "category": c.category.value,
            "degree": 1,
        }
        for c in components
    ]
    by_id: dict[str, dict[str, Any]] = {n["id"]: n for n in nodes}
    links: list[dict[str, Any]] = []

    # LWC -> Apex
    for c in components:
        if c.category.value != "lwc_bundle":
            continue
        for imp in c.raw.get("apex_imports", []) if isinstance(c.raw, dict) else []:
            if not isinstance(imp, str):
                continue
            tgt = components_by_name.get(imp.split(".", 1)[0])
            if tgt and tgt in by_id:
                links.append({"source": str(c.id), "target": tgt})
                by_id[str(c.id)]["degree"] += 1
                by_id[tgt]["degree"] += 1

    # Dispatch — keep edges between Apex classes (target only); skip the CMT
    # hubs to keep the graph readable.
    for e in dispatch_edges:
        tgt = components_by_name.get(e.handler_class)
        if tgt is None:
            continue
        # Add a virtual hub node so the cluster is visible.
        hub_id = f"cmt:{e.dispatcher_cmt}"
        if hub_id not in by_id:
            hub_node = {
                "id": hub_id,
                "name": e.dispatcher_cmt,
                "category": "dispatch_hub",
                "degree": 1,
            }
            nodes.append(hub_node)
            by_id[hub_id] = hub_node
        links.append({"source": hub_id, "target": tgt})
        by_id[hub_id]["degree"] += 1
        by_id[tgt]["degree"] += 1

    return {
        "nodes": nodes,
        "links": links,
        "categories": sorted({n["category"] for n in nodes}),
    }


def render_html(inputs: XRayInputs) -> str:
    """Render the interactive HTML report."""
    env = _template_env()
    template = env.get_template("xray.html.j2")

    components_by_name = {
        c.api_name: str(c.id) for c in inputs.components if c.api_name is not None
    }
    annotations_by_id = {a.component_id: a for a in inputs.annotations}

    component_rows: list[dict[str, Any]] = []
    for c in inputs.components:
        ann = annotations_by_id.get(str(c.id))
        score = inputs.complexity.get(str(c.id))
        component_rows.append(
            {
                "name": c.name,
                "category": c.category.value,
                "translation_difficulty": score.translation_difficulty if score else 50,
                "migration_risk": score.migration_risk if score else 50,
                "diff_class": _band(score.translation_difficulty) if score else "b-med",
                "risk_class": _band(score.migration_risk) if score else "b-med",
                "recommended_tier": ann.recommended_tier if ann else "tier1_rules",
                "tier_class": _tier_class(ann.recommended_tier if ann else "tier1_rules"),
                "domain": ann.domain if ann else "other",
                "summary": (ann.summary if ann else "(no annotation)"),
            }
        )
    component_rows.sort(
        key=lambda r: (-r["translation_difficulty"], -r["migration_risk"], r["name"])
    )

    coverage_rows = [
        {
            "category": cat.value,
            "attempted": cov.attempted,
            "succeeded": cov.succeeded,
            "coverage_ratio": cov.coverage_ratio,
        }
        for cat, cov in inputs.coverage.by_category.items()
        if cov.attempted > 0
    ]
    coverage_rows.sort(key=lambda r: r["category"])

    ooe_rows = [
        {
            "step": int(o.step),
            "step_name": o.step.name.replace("_", " ").title(),
            "structural_count": o.structural_count,
            "priority": o.priority,
        }
        for o in inputs.ooe.observations
    ]

    process_rows = [
        {"process_id": p.process_id, "label": p.label, "size": len(p.component_ids)}
        for p in inputs.processes
    ]

    orphan_rows = [
        {
            "apex_class_name": r.apex_class_name,
            "channel": r.channel,
            "confidence": r.confidence,
            "evidence": r.evidence,
        }
        for r in inputs.orphans.resolved
    ]

    graph_json = _build_graph_json(
        inputs.components, inputs.dispatch_edges, components_by_name=components_by_name
    )

    return template.render(
        org_alias=inputs.org_alias,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        component_count=len(inputs.components),
        process_count=len(inputs.processes),
        orphan_resolved=len(inputs.orphans.resolved),
        orphan_total=inputs.orphans.total_orphans,
        coverage_by_category=coverage_rows,
        ooe_observations=ooe_rows,
        component_rows=component_rows,
        processes=process_rows,
        orphan_resolutions=orphan_rows,
        unresolved_orphans=inputs.orphans.unresolved,
        graph_json=json.dumps(graph_json),
    )


def render_json(inputs: XRayInputs) -> dict[str, Any]:
    """Machine-readable export. Stable schema — Phase 3 + the X-Ray report
    rely on it being backward-compatible across minor releases."""
    annotations_by_id = {a.component_id: a for a in inputs.annotations}
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "org_alias": inputs.org_alias,
        "components": [
            {
                "id": str(c.id),
                "category": c.category.value,
                "name": c.name,
                "api_name": c.api_name,
                "namespace": c.namespace,
                "content_hash": c.content_hash,
                "annotation": annotations_by_id[str(c.id)].model_dump(mode="json")
                if str(c.id) in annotations_by_id
                else None,
                "complexity": _score_to_jsonable(inputs.complexity.get(str(c.id))),
            }
            for c in inputs.components
        ],
        "business_processes": [
            {
                "process_id": p.process_id,
                "label": p.label,
                "component_ids": p.component_ids,
            }
            for p in inputs.processes
        ],
        "orphan_resolutions": {
            "resolved": [
                {
                    "component_id": r.component_id,
                    "apex_class_name": r.apex_class_name,
                    "channel": r.channel,
                    "confidence": r.confidence,
                    "evidence": r.evidence,
                }
                for r in inputs.orphans.resolved
            ],
            "unresolved": inputs.orphans.unresolved,
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
    (out_dir / "xray.html").write_text(render_html(inputs), encoding="utf-8")
    (out_dir / "xray.json").write_text(
        json.dumps(render_json(inputs), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    log.info("understand.xray.written", out_dir=str(out_dir))
