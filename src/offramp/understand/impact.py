"""Impact analysis (C23).

Answers the product questions over a :class:`DependencyGraph`:

* :func:`where_used` — every inbound reference to a node, grouped by the
  referencing component's category, with evidence, confidence, and whether
  the referrer is a test class or inactive automation;
* :func:`impact_closure` — everything that transitively depends on a node
  (what could break if it changes), with the path that got us there;
* :func:`save_impact` — the automations that fire on a save to an object,
  ordered by Salesforce Order-of-Execution step;
* :func:`unused_fields` / :func:`legacy_automation` — cleanup candidates,
  with data-level evidence (fill rate, record count) when a profile exists.

A reference *counts as usage* only when it comes from active, non-test
automation or code. Test classes, inactive rules, permission sets, layouts,
and reports are reported, never silently counted.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from offramp.core.models import (
    AUTOMATION_CATEGORIES,
    REPORTING_CATEGORIES,
    SECURITY_CATEGORIES,
    UI_CATEGORIES,
    CategoryName,
    Component,
    EvidenceChannel,
    SchemaSnapshot,
)
from offramp.extract.ooe_audit.audit import OoEStep, classify_steps
from offramp.understand.dependencies import DependencyGraph, GraphNode

_UI_API_TYPES = {"layout", "flexipage", "compactlayout", "listview", "quickaction"}
_REPORTING_API_TYPES = {"report", "dashboard"}
_SECURITY_API_TYPES = {"permissionset", "profile"}
_AUTOMATION_VALUES = {c.value for c in AUTOMATION_CATEGORIES}
_UI_VALUES = {c.value for c in UI_CATEGORIES}
_SECURITY_VALUES = {c.value for c in SECURITY_CATEGORIES}
_REPORTING_VALUES = {c.value for c in REPORTING_CATEGORIES}

UNUSED_REASON_ORDER = [
    "no_references",
    "test_only",
    "inactive_only",
    "security_only",
    "ui_only",
    "reporting_only",
]


# ---- classification helpers -----------------------------------------------------


def _defines(e: Any) -> bool:
    """The OWNS edge from a formula/roll-up component to the field it *is*."""
    return bool(e.kind.value == "owns" and e.notes == "defines")


def _is_test(n: GraphNode) -> bool:
    return bool(n.meta.get("is_test"))


def _is_active(n: GraphNode) -> bool:
    return bool(n.meta.get("active", True) is not False)


def _bucket(n: GraphNode) -> str:
    """automation | ui | security | reporting | other, for a referencing node."""
    if n.kind == "component":
        if n.category in _AUTOMATION_VALUES:
            return "automation"
        if n.category in _UI_VALUES:
            return "ui"
        if n.category in _SECURITY_VALUES:
            return "security"
        if n.category in _REPORTING_VALUES:
            return "reporting"
        return "other"
    if n.kind == "external":
        low = n.category.lower()
        if low in _UI_API_TYPES:
            return "ui"
        if low in _REPORTING_API_TYPES:
            return "reporting"
        if low in _SECURITY_API_TYPES:
            return "security"
    return "other"


# ---- where used ----------------------------------------------------------------


@dataclass
class Reference:
    node: GraphNode
    kind: str
    evidence: str
    confidence: float
    corroborated_by_api: bool
    notes: str | None
    active: bool = True
    is_test: bool = False

    @property
    def counts_as_usage(self) -> bool:
        """Active, non-test reference of any kind."""
        return self.active and not self.is_test


@dataclass
class WhereUsed:
    target: GraphNode
    total: int  # every reference, including tests and inactive automation
    by_category: dict[str, list[Reference]]
    api_only_count: int
    active_total: int = 0  # excludes test classes and inactive automation
    automation_total: int = 0  # active automation / code only: what a delete would break
    test_only: bool = False
    inactive_only: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "target": {
                "id": self.target.id,
                "kind": self.target.kind,
                "api_name": self.target.api_name,
            },
            "total": self.total,
            "active_total": self.active_total,
            "automation_total": self.automation_total,
            "test_only": self.test_only,
            "inactive_only": self.inactive_only,
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
                        "active": r.active,
                        "is_test": r.is_test,
                    }
                    for r in refs
                ]
                for cat, refs in self.by_category.items()
            },
        }


def _ref(src: GraphNode, e: Any, notes: str | None) -> Reference:
    return Reference(
        src,
        e.kind.value,
        e.evidence.value,
        e.confidence,
        e.corroborated_by_api,
        notes,
        active=_is_active(src),
        is_test=_is_test(src),
    )


def where_used(graph: DependencyGraph, node_id: str) -> WhereUsed:
    target = graph.node(node_id)
    if target is None:
        raise KeyError(node_id)
    by_cat: dict[str, list[Reference]] = defaultdict(list)
    api_only = 0
    seen: set[str] = set()
    for e in graph.inbound(node_id):
        src = graph.node(str(e.source_id))
        if src is None or _defines(e):
            continue
        key = f"{src.id}:{e.kind.value}"
        if key in seen:
            continue
        seen.add(key)
        if e.evidence is EvidenceChannel.DEPENDENCY_API:
            api_only += 1
        by_cat[src.category].append(_ref(src, e, e.notes))
    # Object-level query: include references to any of the object's fields.
    if target.kind == "object":
        for n in graph.nodes.values():
            if n.kind != "field" or n.object_name != target.api_name:
                continue
            for e in graph.inbound(n.id):
                src = graph.node(str(e.source_id))
                if src is None or src.kind != "component" or _defines(e):
                    continue
                key = f"{src.id}:{e.kind.value}:{n.id}"
                if key in seen:
                    continue
                seen.add(key)
                by_cat[src.category].append(_ref(src, e, f"via {n.api_name}"))
    for refs in by_cat.values():
        refs.sort(key=lambda r: (-r.confidence, r.node.api_name.lower()))
    all_refs = [r for refs in by_cat.values() for r in refs]
    active = [r for r in all_refs if r.counts_as_usage]
    return WhereUsed(
        target=target,
        total=len(all_refs),
        by_category=dict(sorted(by_cat.items())),
        api_only_count=api_only,
        active_total=len(active),
        automation_total=sum(1 for r in active if _bucket(r.node) == "automation"),
        test_only=bool(all_refs) and all(r.is_test for r in all_refs),
        inactive_only=bool(all_refs) and all(not r.active for r in all_refs),
    )


# ---- change impact -------------------------------------------------------------


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
    so on. Test classes are skipped; ``min_confidence`` drops edges we are not
    sure about so the closure reflects what we can defend.
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
            if n is None or _is_test(n):
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
                            wpath = [*path, n.api_name, wn.api_name]
                            wconf = min(c, w.confidence)
                            out.append(ImpactedNode(wn, dist + 2, wpath, wconf))
                            q.append((wn.id, dist + 2, wpath, wconf))
    out.sort(key=lambda i: (i.distance, -i.min_confidence, i.node.api_name.lower()))
    return out


# ---- save impact ---------------------------------------------------------------


@dataclass
class SaveImpactRow:
    step: int
    step_name: str
    component: GraphNode
    relation: str  # fires | reads | writes | validates | inactive
    evidence: str
    confidence: float


_RELATION_ORDER = {"fires": 0, "validates": 1, "writes": 2, "reads": 3, "inactive": 4}


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
        if src is None or src.kind != "component" or _is_test(src):
            return
        try:
            cat = CategoryName(src.category)
        except ValueError:
            return
        steps = classify_steps(cat)
        if not steps:
            return
        step = min(steps)
        if cat is CategoryName.APEX_TRIGGER and e.notes:
            if "before" in e.notes and "after" not in e.notes:
                step = OoEStep.BEFORE_TRIGGERS
            elif "after" in e.notes and "before" not in e.notes:
                step = OoEStep.AFTER_TRIGGERS
        if cat is CategoryName.RECORD_TRIGGERED_FLOW and e.notes:
            step = (
                OoEStep.PRE_TRIGGER_FLOW if "BeforeSave" in e.notes else OoEStep.PROCESSES_AND_FLOWS
            )
        rel = "inactive" if not _is_active(src) else relation
        existing = rows.get(src.id)
        if existing is None:
            rows[src.id] = SaveImpactRow(
                int(step), OoEStep(int(step)).name, src, rel, e.evidence.value, e.confidence
            )
            return
        if existing.relation == "inactive" or rel == existing.relation:
            return
        if rel == "fires":
            existing.relation = "fires"
            existing.step = int(step)
            existing.step_name = OoEStep(int(step)).name
        elif rel == "writes" and existing.relation == "reads":
            existing.relation = "writes"

    for e in graph.inbound(obj.id):
        consider(e, "fires" if e.kind.value == "triggers" else "reads")
    for fid in field_ids:
        for e in graph.inbound(fid):
            consider(e, "writes" if e.notes and "write" in e.notes else "reads")
    out = list(rows.values())
    out.sort(
        key=lambda r: (r.step, _RELATION_ORDER.get(r.relation, 5), r.component.api_name.lower())
    )
    return out


# ---- cleanup candidates --------------------------------------------------------


@dataclass
class UnusedField:
    node: GraphNode
    reason: str  # one of UNUSED_REASON_ORDER
    referenced_by: list[str] = field(default_factory=list)
    fill_rate: float | None = None
    record_count: int | None = None

    @property
    def empty(self) -> bool:
        return self.fill_rate is not None and self.fill_rate == 0.0


def unused_fields(graph: DependencyGraph) -> list[UnusedField]:
    """Custom fields no *active, non-test* automation or code references.

    Everything that still points at the field is listed so the customer can
    decide: only test classes, only inactive automation, only permission sets
    / profiles (FLS), only layouts / Lightning pages, only reports. Data
    evidence (fill rate, record count) rides along when a profile exists.
    """
    out: list[UnusedField] = []
    objects = {n.api_name: n for n in graph.nodes.values() if n.kind == "object"}
    for n in graph.nodes.values():
        if n.kind != "field" or not n.meta.get("custom"):
            continue
        sources = [
            s
            for e in graph.inbound(n.id)
            if not _defines(e) and (s := graph.node(str(e.source_id))) is not None
        ]
        live = [
            s for s in sources if _bucket(s) == "automation" and _is_active(s) and not _is_test(s)
        ]
        if live:
            continue
        buckets = {_bucket(s) for s in sources}
        if not sources:
            reason = "no_references"
        elif buckets == {"automation"} and all(_is_test(s) for s in sources):
            reason = "test_only"
        elif buckets == {"automation"}:
            reason = "inactive_only"
        elif "ui" in buckets:
            reason = "ui_only"
        elif "security" in buckets and "reporting" not in buckets:
            reason = "security_only"
        elif "reporting" in buckets:
            reason = "reporting_only"
        else:
            reason = "inactive_only"
        obj = objects.get(n.object_name or "")
        out.append(
            UnusedField(
                n,
                reason,
                sorted({s.api_name for s in sources}),
                fill_rate=n.meta.get("fill_rate"),
                record_count=obj.meta.get("record_count") if obj else None,
            )
        )
    out.sort(key=lambda u: (UNUSED_REASON_ORDER.index(u.reason), u.node.api_name.lower()))
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
        targets = [graph.node(str(e.target_id)) for e in graph.outbound(n.id)]
        field_names = sorted({t.api_name for t in targets if t is not None and t.kind == "field"})
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
    comps = [n for n in graph.nodes.values() if n.kind == "component"]
    fields = [n for n in graph.nodes.values() if n.kind == "field"]
    surface_reasons = {"ui_only", "security_only", "reporting_only"}
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "edges_by_evidence": graph.edges_by_evidence(),
        "edges_by_kind": graph.edges_by_kind(),
        "unresolved_references": len(graph.unresolved),
        "api_rows_seen": graph.api_rows_seen,
        "api_matched": graph.api_matched,
        "api_only": graph.api_only,
        "package_fields": graph.package_fields,
        "package_dependencies": sorted(
            n.api_name
            for n in graph.nodes.values()
            if n.kind == "external" and n.category == "Package"
        ),
        "custom_fields": sum(1 for n in fields if n.meta.get("custom")),
        "unused_custom_fields": sum(1 for u in unused if u.reason == "no_references"),
        "test_only_custom_fields": sum(1 for u in unused if u.reason == "test_only"),
        "inactive_only_custom_fields": sum(1 for u in unused if u.reason == "inactive_only"),
        "surface_only_custom_fields": sum(1 for u in unused if u.reason in surface_reasons),
        "empty_custom_fields": sum(1 for u in unused if u.empty),
        "profiled_fields": sum(1 for n in fields if "fill_rate" in n.meta),
        "dynamic_apex_classes": sum(1 for n in comps if n.meta.get("dynamic_access")),
        "test_classes": sum(1 for n in comps if n.meta.get("is_test")),
        "legacy_automation": len(legacy),
        "legacy_active": sum(1 for la in legacy if la.active),
        "schema_objects": len(schema.objects()) if schema else 0,
        "schema_fields": len(schema.fields()) if schema else 0,
    }
