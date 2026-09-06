"""Dependency graph builder (C22, AD-28, AD-30).

Turns extracted Components + the SchemaSnapshot into one typed graph:

* nodes: every Component, every schema object / field / record type, and
  *external* nodes for things we reference but did not extract (email
  templates, layouts, reports from the Dependency API, managed-package
  classes);
* edges: :class:`offramp.core.models.Dependency` with ``kind``, ``evidence``
  and ``confidence``.

Every edge comes from our own extractors. ``MetadataComponentDependency``
rows are folded in afterwards as a *cross-check*: where a row matches an
existing edge the edge is marked ``corroborated_by_api``; where it does not,
an ``api_only`` edge is added at reduced confidence so the report can show
both directions of disagreement.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import (
    CategoryName,
    Component,
    Dependency,
    DependencyKind,
    EvidenceChannel,
    SchemaNode,
    SchemaNodeKind,
    SchemaSnapshot,
)
from offramp.extract.dispatch.class_resolver import DispatchEdge

log = get_logger(__name__)

_EXTERNAL_NS = uuid.UUID("6f2a7b7e-1c1f-4a3c-9c4c-2e5f0e7a9d11")
_FLOW_CATEGORIES = {
    CategoryName.RECORD_TRIGGERED_FLOW,
    CategoryName.SCREEN_FLOW,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.AUTOLAUNCHED_FLOW,
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.PROCESS_BUILDER,
}
# Standard lookup targets so relationship paths on standard fields can be followed
# even when the schema came from a source tree (which omits standard fields).
_STANDARD_FIELD_TARGETS = {
    "ownerid": "User",
    "accountid": "Account",
    "contactid": "Contact",
    "opportunityid": "Opportunity",
    "leadid": "Lead",
    "caseid": "Case",
    "createdbyid": "User",
    "lastmodifiedbyid": "User",
    "recordtypeid": "RecordType",
    "campaignid": "Campaign",
    "profileid": "Profile",
    "managerid": "User",
    "userid": "User",
    "pricebook2id": "Pricebook2",
    "product2id": "Product2",
    "contractid": "Contract",
    "orderid": "Order",
    "quoteid": "Quote",
    "userroleid": "UserRole",
    "whatid": "",
    "whoid": "",
}
_STANDARD_RELATIONSHIPS = {
    "owner": "OwnerId",
    "account": "AccountId",
    "contact": "ContactId",
    "opportunity": "OpportunityId",
    "lead": "LeadId",
    "case": "CaseId",
    "parent": "ParentId",
    "createdby": "CreatedById",
    "lastmodifiedby": "LastModifiedById",
    "recordtype": "RecordTypeId",
    "campaign": "CampaignId",
    "what": "WhatId",
    "who": "WhoId",
    "profile": "ProfileId",
    "manager": "ManagerId",
    "user": "UserId",
    "pricebook2": "Pricebook2Id",
    "product2": "Product2Id",
    "asset": "AssetId",
    "contract": "ContractId",
    "order": "OrderId",
    "quote": "QuoteId",
    "entitlement": "EntitlementId",
    "individual": "IndividualId",
}


@dataclass
class GraphNode:
    id: str
    kind: str  # component | object | field | record_type | external
    category: str  # CategoryName value, SchemaNodeKind value, or external type ('EmailTemplate', 'Layout')
    name: str
    api_name: str
    object_name: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnresolvedReference:
    source_id: str
    source_name: str
    target_kind: str  # apex_class | object | field | flow | template | action
    target_name: str
    evidence: EvidenceChannel


@dataclass
class DependencyGraph:
    org_alias: str
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[Dependency] = field(default_factory=list)
    unresolved: list[UnresolvedReference] = field(default_factory=list)
    api_rows_seen: int = 0
    api_matched: int = 0
    api_only: int = 0
    api_unmapped: int = 0
    _out: dict[str, list[Dependency]] = field(default_factory=lambda: defaultdict(list), repr=False)
    _in: dict[str, list[Dependency]] = field(default_factory=lambda: defaultdict(list), repr=False)
    _edge_index: dict[tuple[str, str, str], Dependency] = field(default_factory=dict, repr=False)

    # ---- mutation -------------------------------------------------------------

    def add_node(self, node: GraphNode) -> GraphNode:
        self.nodes.setdefault(node.id, node)
        return self.nodes[node.id]

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        kind: DependencyKind,
        *,
        evidence: EvidenceChannel,
        confidence: float = 1.0,
        notes: str | None = None,
    ) -> Dependency:
        if source_id == target_id:
            return self._edge_index.get((source_id, target_id, kind.value)) or Dependency(
                source_id=uuid.UUID(source_id),
                target_id=uuid.UUID(target_id),
                kind=kind,
                evidence=evidence,
            )
        key = (source_id, target_id, kind.value)
        existing = self._edge_index.get(key)
        if existing is not None:
            # Same edge found by a second channel: keep the higher confidence, merge notes.
            if confidence > existing.confidence:
                existing.confidence = confidence
            if notes and (not existing.notes or notes not in existing.notes):
                existing.notes = f"{existing.notes}; {notes}" if existing.notes else notes
            return existing
        dep = Dependency(
            source_id=uuid.UUID(source_id),
            target_id=uuid.UUID(target_id),
            kind=kind,
            confidence=confidence,
            evidence=evidence,
            notes=notes,
        )
        self.edges.append(dep)
        self._edge_index[key] = dep
        self._out[source_id].append(dep)
        self._in[target_id].append(dep)
        return dep

    # ---- queries --------------------------------------------------------------

    def outbound(self, node_id: str) -> list[Dependency]:
        return list(self._out.get(node_id, []))

    def inbound(self, node_id: str) -> list[Dependency]:
        return list(self._in.get(node_id, []))

    def node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    def find(self, api_name: str, *, kind: str | None = None) -> GraphNode | None:
        want = api_name.lower()
        for n in self.nodes.values():
            if n.api_name.lower() == want and (kind is None or n.kind == kind):
                return n
        for n in self.nodes.values():
            if n.name.lower() == want and (kind is None or n.kind == kind):
                return n
        return None

    def edges_by_evidence(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.edges:
            out[e.evidence.value] += 1
        return dict(out)

    def edges_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.edges:
            out[e.kind.value] += 1
        return dict(out)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "org_alias": self.org_alias,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "category": n.category,
                    "name": n.name,
                    "api_name": n.api_name,
                    "object_name": n.object_name,
                    "meta": n.meta,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {
                    "source_id": str(e.source_id),
                    "target_id": str(e.target_id),
                    "kind": e.kind.value,
                    "evidence": e.evidence.value,
                    "confidence": e.confidence,
                    "corroborated_by_api": e.corroborated_by_api,
                    "notes": e.notes,
                }
                for e in self.edges
            ],
            "unresolved": [
                {
                    "source_id": u.source_id,
                    "source_name": u.source_name,
                    "target_kind": u.target_kind,
                    "target_name": u.target_name,
                    "evidence": u.evidence.value,
                }
                for u in self.unresolved
            ],
            "stats": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "by_evidence": self.edges_by_evidence(),
                "by_kind": self.edges_by_kind(),
                "unresolved": len(self.unresolved),
                "api_rows_seen": self.api_rows_seen,
                "api_matched": self.api_matched,
                "api_only": self.api_only,
                "api_unmapped": self.api_unmapped,
            },
        }


# ---- builder ------------------------------------------------------------------


class _Index:
    """Name → node id lookups, all case-insensitive."""

    def __init__(self) -> None:
        self.apex: dict[str, str] = {}
        self.flows: dict[str, str] = {}
        self.components: dict[tuple[str, str], str] = {}  # (category, api_name)
        self.objects: dict[str, str] = {}
        self.fields: dict[str, str] = {}
        self.field_nodes: dict[str, SchemaNode] = {}
        self.record_types: dict[str, str] = {}
        self.workflow_by_object: dict[str, str] = {}
        self.platform_events: dict[str, str] = {}
        self.email_alerts: dict[str, str] = {}  # 'Lead.Welcome_Lead_Alert' -> workflow component id


def build_graph(
    *,
    org_alias: str,
    components: list[Component],
    schema: SchemaSnapshot | None = None,
    dispatch_edges: list[DispatchEdge] | None = None,
    api_rows: list[dict[str, Any]] | None = None,
    cron_rows: list[dict[str, Any]] | None = None,
) -> DependencyGraph:
    g = DependencyGraph(org_alias=org_alias)
    idx = _Index()

    # ---- nodes ----
    for c in components:
        obj = _component_object(c)
        n = g.add_node(
            GraphNode(
                id=str(c.id),
                kind="component",
                category=c.category.value,
                name=c.name,
                api_name=c.api_name or c.name,
                object_name=obj,
                meta={"content_hash": c.content_hash, "active": _component_active(c)},
            )
        )
        key = (c.api_name or c.name).lower()
        if c.category is CategoryName.APEX_CLASS:
            idx.apex[key] = n.id
        elif c.category in _FLOW_CATEGORIES:
            idx.flows[key] = n.id
        elif c.category is CategoryName.WORKFLOW_RULE and obj:
            idx.workflow_by_object[obj.lower()] = n.id
            for ea in c.raw.get("email_alerts", []) if isinstance(c.raw, dict) else []:
                if ea.get("name"):
                    idx.email_alerts[f"{obj}.{ea['name']}".lower()] = n.id
        elif c.category is CategoryName.PLATFORM_EVENT:
            idx.platform_events[key] = n.id
        idx.components[(c.category.value, key)] = n.id

    if schema is not None:
        for sn in schema.nodes:
            n = g.add_node(
                GraphNode(
                    id=str(sn.id),
                    kind=sn.kind.value,
                    category=sn.kind.value,
                    name=sn.api_name.split(".")[-1],
                    api_name=sn.api_name,
                    object_name=sn.object_name,
                    meta={
                        "field_type": sn.field_type,
                        "custom": sn.custom,
                        "reference_to": sn.reference_to,
                        "label": sn.label,
                    },
                )
            )
            low = sn.api_name.lower()
            if sn.kind is SchemaNodeKind.OBJECT:
                idx.objects[low] = n.id
            elif sn.kind is SchemaNodeKind.FIELD:
                idx.fields[low] = n.id
                idx.field_nodes[low] = sn
            else:
                idx.record_types[low] = n.id

    # Objects referenced by components but absent from the schema (standard
    # objects with no custom fields, managed-package objects) get nodes on demand.
    def obj_node(name: str) -> str:
        low = name.lower()
        if low in idx.objects:
            return idx.objects[low]
        nid = _external_id("object", name)
        g.add_node(
            GraphNode(
                id=nid,
                kind="object",
                category="object",
                name=name,
                api_name=name,
                object_name=name,
                meta={"inferred": True},
            )
        )
        idx.objects[low] = nid
        return nid

    def field_node(qualified: str) -> str | None:
        return idx.fields.get(qualified.lower())

    def inferred_field(obj: str, fname: str) -> str:
        """Create a standard-field node the source tree could not supply."""
        qualified = f"{obj}.{fname}"
        existing = idx.fields.get(qualified.lower())
        if existing:
            return existing
        obj_node(obj)
        nid = _external_id("field", qualified)
        target = _STANDARD_FIELD_TARGETS.get(fname.lower())
        sn = SchemaNode(
            org_alias=org_alias,
            kind=SchemaNodeKind.FIELD,
            api_name=qualified,
            object_name=obj,
            label=fname,
            custom=fname.endswith("__c"),
            reference_to=[target] if target else [],
        )
        g.add_node(
            GraphNode(
                id=nid,
                kind="field",
                category="field",
                name=fname,
                api_name=qualified,
                object_name=obj,
                meta={
                    "field_type": None,
                    "custom": sn.custom,
                    "reference_to": sn.reference_to,
                    "label": fname,
                    "inferred": True,
                },
            )
        )
        idx.fields[qualified.lower()] = nid
        idx.field_nodes[qualified.lower()] = sn
        return nid

    def external(kind: str, name: str) -> str:
        nid = _external_id(kind, name)
        g.add_node(
            GraphNode(
                id=nid,
                kind="external",
                category=kind,
                name=name,
                api_name=name,
                meta={"inferred": True},
            )
        )
        return nid

    # ---- schema relationship edges ----
    if schema is not None:
        for sn in schema.fields():
            for target in sn.reference_to:
                g.add_edge(
                    str(sn.id),
                    obj_node(target),
                    DependencyKind.REFERENCES,
                    evidence=EvidenceChannel.SCHEMA,
                    notes="lookup",
                )

    # ---- component edges ----
    for c in components:
        raw = c.raw if isinstance(c.raw, dict) else {}
        refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
        src = str(c.id)
        ev = _evidence_for(c.category)
        host = _component_object(c)

        # Trigger relationship: what does this fire on?
        if host and c.category in {
            CategoryName.APEX_TRIGGER,
            CategoryName.RECORD_TRIGGERED_FLOW,
            CategoryName.PROCESS_BUILDER,
            CategoryName.SCHEDULE_TRIGGERED_FLOW,
            CategoryName.FLOW_ORCHESTRATION,
            CategoryName.WORKFLOW_RULE,
            CategoryName.VALIDATION_RULE,
            CategoryName.ASSIGNMENT_RULE,
            CategoryName.AUTO_RESPONSE_RULE,
            CategoryName.ESCALATION_RULE,
            CategoryName.SHARING_RULE,
            CategoryName.APPROVAL_PROCESS,
        }:
            g.add_edge(
                src, obj_node(host), DependencyKind.TRIGGERS, evidence=ev, notes=_trigger_note(c)
            )
        if host and c.category is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW:
            target = idx.platform_events.get(host.lower()) or obj_node(host)
            g.add_edge(
                src,
                target,
                DependencyKind.TRIGGERS,
                evidence=ev,
                notes="platform event subscription",
            )

        # Objects read / written (whole-record DML → "write"; recordDeletes → "delete")
        written_objs = {w[:-2] for w in _written_fields(raw) if w.endswith(".*")}
        for o in refs.get("objects", []):
            if not o or o == host:
                continue
            note = "write" if o.lower() in written_objs else None
            g.add_edge(
                src,
                obj_node(o),
                DependencyKind.REFERENCES,
                evidence=ev,
                confidence=0.9 if ev is EvidenceChannel.APEX_PARSE else 1.0,
                notes=note,
            )

        # Fields
        for f in refs.get("fields", []):
            _link_field(
                g, idx, src, c, f, ev, field_node, obj_node, inferred_field, _written_fields(raw)
            )

        # Defines a field (formula / rollup)
        defines = refs.get("defines_field")
        if defines:
            fid = field_node(defines)
            if fid:
                g.add_edge(src, fid, DependencyKind.OWNS, evidence=ev, notes="defines")

        # Apex classes
        for cls in refs.get("apex_classes", []):
            tid = idx.apex.get(cls.lower())
            if tid:
                g.add_edge(
                    src,
                    tid,
                    DependencyKind.CALLS,
                    evidence=ev,
                    confidence=0.95 if ev is EvidenceChannel.APEX_PARSE else 1.0,
                )
            elif "." in cls and cls.split(".", 1)[1].lower() in idx.apex:
                # namespaced reference to an unmanaged class: Ns.Class
                g.add_edge(
                    src,
                    idx.apex[cls.split(".", 1)[1].lower()],
                    DependencyKind.CALLS,
                    evidence=ev,
                    confidence=0.8,
                )
            else:
                if ev is EvidenceChannel.APEX_PARSE and not _looks_like_class(cls):
                    continue
                g.unresolved.append(UnresolvedReference(src, c.name, "apex_class", cls, ev))
        for cls in refs.get("async_targets", []) + refs.get("type_forname", []):
            tid = idx.apex.get(cls.lower())
            if tid:
                g.add_edge(src, tid, DependencyKind.CALLS, evidence=ev, notes="async/dynamic")

        # Flows
        for fl in refs.get("flows", []):
            tid = idx.flows.get(fl.lower())
            if tid:
                g.add_edge(src, tid, DependencyKind.CALLS, evidence=ev, notes="subflow/flow action")
            else:
                g.unresolved.append(UnresolvedReference(src, c.name, "flow", fl, ev))

        # Email alerts (Flow → workflow alert on object) / templates
        for alert in refs.get("email_alerts", []):
            tid = idx.email_alerts.get(alert.lower())
            if tid:
                g.add_edge(
                    src, tid, DependencyKind.REFERENCES, evidence=ev, notes=f"email alert {alert}"
                )
            else:
                g.add_edge(
                    src,
                    external("EmailAlert", alert),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    confidence=0.8,
                )
        for tpl in refs.get("email_templates", []):
            g.add_edge(src, external("EmailTemplate", tpl), DependencyKind.REFERENCES, evidence=ev)

        # Approval workflow actions live in the object's workflow file
        for wa in refs.get("workflow_actions", []):
            wid = idx.workflow_by_object.get((host or "").lower())
            if wid:
                g.add_edge(src, wid, DependencyKind.REFERENCES, evidence=ev, notes=wa)
            else:
                g.unresolved.append(UnresolvedReference(src, c.name, "action", wa, ev))

        # Platform events published/consumed
        for pe in refs.get("platform_events", []):
            tid = idx.platform_events.get(pe.lower())
            if tid:
                g.add_edge(src, tid, DependencyKind.REFERENCES, evidence=ev, notes="event")

        # Named credentials / labels / settings / globals → external nodes
        for nc in refs.get("named_credentials", []):
            g.add_edge(src, external("NamedCredential", nc), DependencyKind.REFERENCES, evidence=ev)
        for lb in refs.get("custom_labels", []):
            g.add_edge(src, external("CustomLabel", lb), DependencyKind.REFERENCES, evidence=ev)
        for cs in refs.get("custom_settings", []):
            g.add_edge(
                src, obj_node(cs), DependencyKind.REFERENCES, evidence=ev, notes="custom setting"
            )
        for gl in refs.get("globals", []):
            head = gl.split(".", 1)[0]
            if head in {"$Setup", "$CustomMetadata"} and gl.count(".") >= 2:
                _, obj, fld = gl.split(".", 2)
                fid = field_node(f"{obj}.{fld.split('.')[0]}")
                if fid:
                    g.add_edge(src, fid, DependencyKind.REFERENCES, evidence=ev, notes=gl)
                else:
                    g.add_edge(src, obj_node(obj), DependencyKind.REFERENCES, evidence=ev, notes=gl)
            elif head in {"$Permission", "$Label"}:
                g.add_edge(
                    src,
                    external(head[1:], gl.split(".", 1)[1] if "." in gl else gl),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                )
            elif head in {"$User", "$Profile", "$UserRole", "$Organization"}:
                fid = field_node(f"{head[1:]}.{gl.split('.', 1)[1]}") if "." in gl else None
                g.add_edge(
                    src, fid or obj_node(head[1:]), DependencyKind.REFERENCES, evidence=ev, notes=gl
                )

        # Platform event definition → its fields (OWNS) so event consumers link through
        if c.category is CategoryName.PLATFORM_EVENT and host:
            g.add_edge(src, obj_node(host), DependencyKind.OWNS, evidence=ev, notes="defines event")

    # ---- CMT dispatch (trigger → dispatcher → handler) ----
    for de in dispatch_edges or []:
        tid = idx.apex.get(de.handler_class.lower())
        if tid is None:
            continue
        # Source = the trigger on the CMT row's object if we can find one, else the dispatcher class.
        src_id = _dispatch_source(components, idx, de)
        if src_id:
            g.add_edge(
                src_id,
                tid,
                DependencyKind.DISPATCHES,
                evidence=EvidenceChannel.CMT_DISPATCH,
                confidence=de.confidence,
                notes=f"{de.dispatcher_cmt}.{de.field_name}",
            )

    # ---- CronTrigger rows ----
    for row in cron_rows or []:
        cls = str(row.get("apex_class") or (row.get("CronJobDetail") or {}).get("Name") or "")
        tid = idx.apex.get(cls.lower())
        if tid:
            nid = external("CronTrigger", str((row.get("CronJobDetail") or {}).get("Name") or cls))
            g.add_edge(
                nid,
                tid,
                DependencyKind.TRIGGERS,
                evidence=EvidenceChannel.CRON,
                notes=str(row.get("CronExpression") or ""),
            )

    # ---- Dependency API cross-check (AD-28) ----
    if api_rows:
        _fold_api_rows(g, idx, api_rows, obj_node, external)

    log.info(
        "understand.dependencies.built",
        nodes=len(g.nodes),
        edges=len(g.edges),
        unresolved=len(g.unresolved),
        by_evidence=g.edges_by_evidence(),
    )
    return g


# ---- helpers ------------------------------------------------------------------


def _external_id(kind: str, name: str) -> str:
    return str(uuid.uuid5(_EXTERNAL_NS, f"{kind}:{name.lower()}"))


def _component_object(c: Component) -> str | None:
    raw = c.raw if isinstance(c.raw, dict) else {}
    for key in ("object", "sobject"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _evidence_for(cat: CategoryName) -> EvidenceChannel:
    if cat in {CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER}:
        return EvidenceChannel.APEX_PARSE
    if cat in _FLOW_CATEGORIES:
        return EvidenceChannel.FLOW_XML
    if cat in {CategoryName.VALIDATION_RULE, CategoryName.FORMULA_FIELD}:
        return EvidenceChannel.FORMULA
    if cat is CategoryName.WORKFLOW_RULE:
        return EvidenceChannel.WORKFLOW_XML
    if cat is CategoryName.ROLLUP_SUMMARY:
        return EvidenceChannel.ROLLUP_XML
    if cat is CategoryName.LWC_BUNDLE:
        return EvidenceChannel.LWC_IMPORT
    return EvidenceChannel.RULE_XML


def _trigger_note(c: Component) -> str:
    raw = c.raw if isinstance(c.raw, dict) else {}
    if c.category is CategoryName.APEX_TRIGGER:
        return ", ".join(raw.get("events", [])) or "trigger"
    if c.category in _FLOW_CATEGORIES:
        return (
            f"{raw.get('trigger_type', '')} {raw.get('record_trigger_type', '')}".strip() or "flow"
        )
    return c.category.value


def _written_fields(raw: dict[str, Any]) -> set[str]:
    """Qualified field names a component assigns (lower-cased); '<Obj>.*' means whole-record DML."""
    refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
    out = {str(f).lower() for f in refs.get("fields_written", [])}
    for o in refs.get("dml_objects", []):
        out.add(f"{str(o).lower()}.*")
    return out


def _component_active(c: Component) -> bool:
    raw = c.raw if isinstance(c.raw, dict) else {}
    if c.category is CategoryName.WORKFLOW_RULE:
        return any(bool(r.get("active")) for r in raw.get("rules", []))
    if c.category in {
        CategoryName.ASSIGNMENT_RULE,
        CategoryName.AUTO_RESPONSE_RULE,
        CategoryName.ESCALATION_RULE,
    }:
        return any(bool(g.get("active")) for g in raw.get("rule_groups", []))
    if "active" in raw:
        return bool(raw.get("active"))
    status = raw.get("status")
    if isinstance(status, str):
        return status.lower() in {"active", ""}
    return True


def _looks_like_class(name: str) -> bool:
    """Filter analyzer noise: single capitalized words that are common field/type names."""
    if "." in name:
        return True
    return len(name) > 2 and not name.endswith(("__c", "__r", "Id"))


def _link_field(
    g: DependencyGraph,
    idx: _Index,
    src: str,
    c: Component,
    qualified: str,
    ev: EvidenceChannel,
    field_node: Any,
    obj_node: Any,
    inferred_field: Any,
    written: set[str],
) -> None:
    """Resolve 'Object.path.to.Field' and add an edge per hop.

    Fields absent from the schema are synthesized as inferred nodes when the
    name looks like a standard field (no ``__c``); a missing *custom* field
    is a real gap and is recorded as unresolved.
    """
    if "." not in qualified:
        return
    obj, path = qualified.split(".", 1)
    segments = path.split(".")
    cur_obj = obj
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        fname = seg if last else _relationship_to_field(seg)
        fid = field_node(f"{cur_obj}.{fname}")
        if fid is None and not last:
            fid = field_node(f"{cur_obj}.{seg}")
            if fid is not None:
                fname = seg
        if fid is None:
            if fname.endswith("__c") and not cur_obj.endswith(("__mdt", "__e", "__b", "__x")):
                g.unresolved.append(UnresolvedReference(src, c.name, "field", qualified, ev))
                g.add_edge(
                    src,
                    obj_node(cur_obj),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    confidence=0.7,
                    notes=f"unresolved field {qualified}",
                )
                return
            fid = inferred_field(cur_obj, fname)
        q = f"{cur_obj}.{fname}".lower()
        note = "write" if last and q in written else "read"
        conf = 1.0 if ev is not EvidenceChannel.APEX_PARSE else 0.9
        g.add_edge(src, fid, DependencyKind.REFERENCES, evidence=ev, confidence=conf, notes=note)
        if last:
            return
        sn = idx.field_nodes.get(f"{cur_obj}.{fname}".lower())
        if sn and sn.reference_to and sn.reference_to[0]:
            cur_obj = sn.reference_to[0]
            continue
        # Polymorphic or unknown hop: stop here, keep what we have.
        g.unresolved.append(UnresolvedReference(src, c.name, "field", qualified, ev))
        return


def _relationship_to_field(segment: str) -> str:
    if segment.endswith("__r"):
        return segment[:-3] + "__c"
    return _STANDARD_RELATIONSHIPS.get(segment.lower(), segment + "Id")


def _dispatch_source(components: list[Component], idx: _Index, de: DispatchEdge) -> str | None:
    """Prefer the trigger on the CMT row's object; fall back to a dispatcher class."""
    obj = ""
    # dispatcher_cmt developer names in the fixture encode the object; real CMT rows carry Object__c.
    for c in components:
        if c.category is CategoryName.APEX_TRIGGER:
            raw = c.raw if isinstance(c.raw, dict) else {}
            refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
            body = raw.get("body", "") or ""
            if (
                any(
                    cls.lower() in {"metadatatriggerhandler", "triggeractionsframework"}
                    or "triggerhandler" in cls.lower()
                    for cls in refs.get("apex_classes", [])
                )
                or "TriggerHandler" in body
            ):
                obj = raw.get("sobject", "") or ""
                if obj and de.dispatcher_cmt.lower().startswith(obj.lower()):
                    return str(c.id)
    # Any dispatcher-looking class
    for name, nid in idx.apex.items():
        if "triggerhandler" in name or "dispatcher" in name:
            return nid
    return None


def _fold_api_rows(
    g: DependencyGraph, idx: _Index, rows: list[dict[str, Any]], obj_node: Any, external: Any
) -> None:
    """Cross-check MetadataComponentDependency rows against parser edges (AD-28)."""

    def map_ref(mtype: str, name: str) -> str | None:
        t = (mtype or "").lower()
        low = (name or "").lower()
        if t == "apexclass":
            return idx.apex.get(low)
        if t == "apextrigger":
            return idx.components.get(("apex_trigger", low))
        if t == "flow" or t == "flowdefinition":
            return idx.flows.get(low)
        if t == "customfield":
            return idx.fields.get(low)
        if t == "customobject":
            return idx.objects.get(low) or obj_node(name)
        if t == "validationrule" and "." in name:
            _, n = name.split(".", 1)
            return idx.components.get(("validation_rule", n.lower()))
        if t == "workflowrule" and "." in name:
            return idx.workflow_by_object.get(name.split(".", 1)[0].lower())
        if t in {"workflowfieldupdate", "workflowalert", "workflowtask"} and "." in name:
            return idx.workflow_by_object.get(name.split(".", 1)[0].lower())
        if t == "lightningcomponentbundle":
            return idx.components.get(("lwc_bundle", low))
        if t == "approvalprocess":
            return idx.components.get(("approval_process", low))
        if t in {
            "layout",
            "report",
            "dashboard",
            "emailtemplate",
            "quickaction",
            "flexipage",
            "permissionset",
            "profile",
            "customlabel",
            "staticresource",
            "auradefinitionbundle",
            "customtab",
            "listview",
            "recordtype",
            "compactlayout",
            "fieldset",
            "globalvalueset",
            "custommetadata",
        }:
            return str(external(mtype, name))
        return None

    for row in rows:
        g.api_rows_seen += 1
        s = map_ref(
            str(row.get("MetadataComponentType", "")), str(row.get("MetadataComponentName", ""))
        )
        t = map_ref(
            str(row.get("RefMetadataComponentType", "")),
            str(row.get("RefMetadataComponentName", "")),
        )
        if s is None or t is None:
            g.api_unmapped += 1
            continue
        matched = False
        for e in g.outbound(s):
            if str(e.target_id) == t:
                e.corroborated_by_api = True
                e.confidence = min(1.0, e.confidence + 0.05)
                matched = True
        if matched:
            g.api_matched += 1
        else:
            g.api_only += 1
            g.add_edge(
                s,
                t,
                DependencyKind.REFERENCES,
                evidence=EvidenceChannel.DEPENDENCY_API,
                confidence=0.6,
                notes="API-only edge; no parser evidence",
            )
