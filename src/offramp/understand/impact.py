"""Impact analysis (C23).

Answers the four product questions over a :class:`DependencyGraph`:

* :func:`where_used` — every inbound reference to a node, grouped by the
  referencing component's category, with evidence and confidence;
* :func:`impact_closure` — everything that transitively depends on a node
  (what could break if it changes), with the path that got us there;
* :func:`save_impact` — the automations that fire on a save to an object,
  ordered by Salesforce Order-of-Execution step;
* :func:`unused_fields` / :func:`legacy_automation` — cleanup candidates.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from offramp.core.models import CategoryName, Component, EvidenceChannel, SchemaSnapshot
from offramp.extract.ooe_audit.audit import OoEStep, classify_steps
from offramp.understand.dependencies import DependencyGraph, GraphNode

_AUTOMATION_KINDS = {"component"}
_UI_API_TYPES = {"layout", "flexipage", "compactlayout", "listview", "quickaction"}
_REPORTING_API_TYPES = {"report", "dashboard"}


@dataclass
class Reference:
    node: GraphNode
    kind: str
    evidence: str
    confidence: float
    corroborated_by_api: bool
    notes: str | None


@dataclass
class WhereUsed:
    target: GraphNode
    total: int
    by_category: dict[str, list[Reference]]
    api_only_count: int

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "target": {
                "id": self.target.id,
                "kind": self.target.kind,
                "api_name": self.target.api_name,
            },
            "total": self.total,
            "api_only_count": self.api_only_count,
            "by_category": {
                cat: [
                    {
                        "id": r.node.id,
                        "name": r.node.api_name,
                        "kind": r.kind,
                        "evidence": r.evidence,
                        "confidence": r.confidence,
                        "corroborated_by_api": r.corroborated_by_api,
                        "notes": r.notes,
                    }
                    for r in refs
                ]
                for cat, refs in self.by_category.items()
            },
        }


def where_used(graph: DependencyGraph, node_id: str) -> WhereUsed:
    target = graph.node(node_id)
    if target is None:
        raise KeyError(node_id)
    by_cat: dict[str, list[Reference]] = defaultdict(list)
    api_only = 0
    seen: set[str] = set()
    for e in graph.inbound(node_id):
        src = graph.node(str(e.source_id))
        if src is None:
            continue
        key = f"{src.id}:{e.kind.value}"
        if key in seen:
            continue
        seen.add(key)
        if e.evidence is EvidenceChannel.DEPENDENCY_API:
            api_only += 1
        by_cat[src.category].append(
            Reference(
                src, e.kind.value, e.evidence.value, e.confidence, e.corroborated_by_api, e.notes
            )
        )
    # Object-level query: include references to any of the object's fields.
    if target.kind == "object":
        for n in graph.nodes.values():
            if n.kind == "field" and n.object_name == target.api_name:
                for e in graph.inbound(n.id):
                    src = graph.node(str(e.source_id))
                    if src is None or src.kind != "component":
                        continue
                    key = f"{src.id}:{e.kind.value}:{n.id}"
                    if key in seen:
                        continue
                    seen.add(key)
                    by_cat[src.category].append(
                        Reference(
                            src,
                            e.kind.value,
                            e.evidence.value,
                            e.confidence,
                            e.corroborated_by_api,
                            f"via {n.api_name}",
                        )
                    )
    for refs in by_cat.values():
        refs.sort(key=lambda r: (-r.confidence, r.node.api_name.lower()))
    total = sum(len(v) for v in by_cat.values())
    return WhereUsed(
        target=target,
        total=total,
        by_category=dict(sorted(by_cat.items())),
        api_only_count=api_only,
    )


@dataclass
class ImpactedNode:
    node: GraphNode
    distance: int
    path: list[str]  # api names from the changed node to this one
    min_confidence: float


def impact_closure(
    graph: DependencyGraph, node_id: str, *, max_depth: int = 4, min_confidence: float = 0.5
) -> list[ImpactedNode]:
    """Breadth-first over *inbound* edges: everything that depends on ``node_id``.

    Field → automation → objects it writes → automation on those objects, and
    so on. ``min_confidence`` drops edges we are not sure about so the closure
    reflects what we can defend.
    """
    start = graph.node(node_id)
    if start is None:
        raise KeyError(node_id)
    out: list[ImpactedNode] = []
    seen = {node_id}
    q: deque[tuple[str, int, list[str], float]] = deque([(node_id, 0, [start.api_name], 1.0)])
    while q:
        cur, dist, path, conf = q.popleft()
        if dist >= max_depth:
            continue
        for e in graph.inbound(cur):
            if e.confidence < min_confidence:
                continue
            nid = str(e.source_id)
            if nid in seen:
                continue
            n = graph.node(nid)
            if n is None:
                continue
            seen.add(nid)
            c = min(conf, e.confidence)
            out.append(ImpactedNode(n, dist + 1, [*path, n.api_name], c))
            q.append((nid, dist + 1, [*path, n.api_name], c))
            # An automation that *writes* fields propagates impact to those fields' readers.
            if n.kind == "component":
                for w in graph.outbound(nid):
                    if w.notes and "write" in w.notes and str(w.target_id) not in seen:
                        wn = graph.node(str(w.target_id))
                        if wn is not None:
                            seen.add(wn.id)
                            out.append(
                                ImpactedNode(
                                    wn,
                                    dist + 2,
                                    [*path, n.api_name, wn.api_name],
                                    min(c, w.confidence),
                                )
                            )
                            q.append(
                                (
                                    wn.id,
                                    dist + 2,
                                    [*path, n.api_name, wn.api_name],
                                    min(c, w.confidence),
                                )
                            )
    out.sort(key=lambda i: (i.distance, -i.min_confidence, i.node.api_name.lower()))
    return out


@dataclass
class SaveImpactRow:
    step: int
    step_name: str
    component: GraphNode
    relation: str  # fires | reads | writes | validates
    evidence: str
    confidence: float


def save_impact(graph: DependencyGraph, object_name: str) -> list[SaveImpactRow]:
    """Automations that participate when a record of ``object_name`` is saved, in OoE order."""
    obj = graph.find(object_name, kind="object")
    if obj is None:
        return []
    field_ids = {
        n.id
        for n in graph.nodes.values()
        if n.kind == "field" and (n.object_name or "").lower() == object_name.lower()
    }
    rows: dict[str, SaveImpactRow] = {}

    def consider(e: Any, relation: str) -> None:
        src = graph.node(str(e.source_id))
        if src is None or src.kind != "component":
            return
        try:
            cat = CategoryName(src.category)
        except ValueError:
            return
        steps = classify_steps(cat)
        if not steps:
            return
        step = min(steps)
        # Before-save vs after-save for triggers/flows
        if cat is CategoryName.APEX_TRIGGER and e.notes:
            if "before" in e.notes and "after" not in e.notes:
                step = OoEStep.BEFORE_TRIGGERS
            elif "after" in e.notes and "before" not in e.notes:
                step = OoEStep.AFTER_TRIGGERS
        if cat in {CategoryName.RECORD_TRIGGERED_FLOW} and e.notes:
            step = (
                OoEStep.PRE_TRIGGER_FLOW if "BeforeSave" in e.notes else OoEStep.PROCESSES_AND_FLOWS
            )
        key = src.id
        existing = rows.get(key)
        rel = relation
        if existing is not None and existing.relation == "inactive":
            return
        if existing is not None:
            if existing.relation == "fires" or rel == existing.relation:
                return
            if rel == "fires":
                existing.relation = "fires"
                existing.step = int(step)
                existing.step_name = OoEStep(int(step)).name
            elif rel == "writes" and existing.relation == "reads":
                existing.relation = "writes"
            return
        if src.meta.get("active") is False:
            rel = "inactive"
        rows[key] = SaveImpactRow(
            int(step), OoEStep(int(step)).name, src, rel, e.evidence.value, e.confidence
        )

    for e in graph.inbound(obj.id):
        consider(e, "fires" if e.kind.value == "triggers" else "reads")
    for fid in field_ids:
        for e in graph.inbound(fid):
            consider(e, "writes" if e.notes and "write" in e.notes else "reads")
    out = list(rows.values())
    order = {"fires": 0, "validates": 1, "writes": 2, "reads": 3, "inactive": 4}
    out.sort(key=lambda r: (r.step, order.get(r.relation, 5), r.component.api_name.lower()))
    return out


@dataclass
class UnusedField:
    node: GraphNode
    reason: str  # no_references | ui_only | reporting_only
    api_references: list[str] = field(default_factory=list)


def unused_fields(graph: DependencyGraph) -> list[UnusedField]:
    """Custom fields no automation, code, or UI references.

    A field referenced only by layouts/reports (visible only through the
    Dependency API) is reported separately so the customer can decide.
    """
    out: list[UnusedField] = []
    for n in graph.nodes.values():
        if n.kind != "field" or not n.meta.get("custom"):
            continue
        inbound = graph.inbound(n.id)
        sources = [graph.node(str(e.source_id)) for e in inbound]
        automation = [s for s in sources if s is not None and s.kind == "component"]
        if automation:
            continue
        ext = [s for s in sources if s is not None and s.kind == "external"]
        ui = [s.api_name for s in ext if s.category.lower() in _UI_API_TYPES]
        rep = [s.api_name for s in ext if s.category.lower() in _REPORTING_API_TYPES]
        if ui:
            out.append(UnusedField(n, "ui_only", ui + rep))
        elif rep:
            out.append(UnusedField(n, "reporting_only", rep))
        else:
            out.append(UnusedField(n, "no_references"))
    out.sort(key=lambda u: (u.reason, u.node.api_name.lower()))
    return out


@dataclass
class LegacyAutomation:
    component: GraphNode
    category: str
    fields_touched: list[str]
    downstream: int  # components in the impact closure of the fields it writes
    active: bool


def legacy_automation(
    graph: DependencyGraph, components: list[Component]
) -> list[LegacyAutomation]:
    """Workflow Rules and Process Builders with their Flow-migration blast radius."""
    out: list[LegacyAutomation] = []
    for c in components:
        if c.category not in {CategoryName.WORKFLOW_RULE, CategoryName.PROCESS_BUILDER}:
            continue
        n = graph.node(str(c.id))
        if n is None:
            continue
        raw = c.raw if isinstance(c.raw, dict) else {}
        fields = [
            graph.node(str(e.target_id))
            for e in graph.outbound(n.id)
            if e.kind.value in {"references", "owns"}
        ]
        field_names = sorted({f.api_name for f in fields if f is not None and f.kind == "field"})
        downstream: set[str] = set()
        for e in graph.outbound(n.id):
            if e.notes and "write" in e.notes:
                for imp in impact_closure(graph, str(e.target_id), max_depth=2):
                    if imp.node.kind == "component" and imp.node.id != n.id:
                        downstream.add(imp.node.id)
        if c.category is CategoryName.WORKFLOW_RULE:
            active = any(r.get("active") for r in raw.get("rules", []))
        else:
            active = str(raw.get("status", "Active")) == "Active"
        out.append(LegacyAutomation(n, c.category.value, field_names, len(downstream), active))
    out.sort(key=lambda la: (-la.downstream, la.component.api_name.lower()))
    return out


def summarize(
    graph: DependencyGraph, schema: SchemaSnapshot | None, components: list[Component]
) -> dict[str, Any]:
    """Headline numbers for the report."""
    unused = unused_fields(graph)
    legacy = legacy_automation(graph, components)
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "edges_by_evidence": graph.edges_by_evidence(),
        "edges_by_kind": graph.edges_by_kind(),
        "unresolved_references": len(graph.unresolved),
        "api_rows_seen": graph.api_rows_seen,
        "api_matched": graph.api_matched,
        "api_only": graph.api_only,
        "custom_fields": sum(
            1 for n in graph.nodes.values() if n.kind == "field" and n.meta.get("custom")
        ),
        "unused_custom_fields": sum(1 for u in unused if u.reason == "no_references"),
        "ui_only_custom_fields": sum(1 for u in unused if u.reason != "no_references"),
        "legacy_automation": len(legacy),
        "legacy_active": sum(1 for la in legacy if la.active),
        "schema_objects": len(schema.objects()) if schema else 0,
        "schema_fields": len(schema.fields()) if schema else 0,
    }
