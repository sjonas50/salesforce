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

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import (
    AUTOMATION_CATEGORIES,
    CategoryName,
    Component,
    DataProfile,
    Dependency,
    DependencyKind,
    EvidenceChannel,
    SchemaNode,
    SchemaNodeKind,
    SchemaSnapshot,
)
from offramp.extract.apex.references import is_sobject_name
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.extract.dispatch.cmt_reader import CMTRecord

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
    package_fields: int = 0  # fields resolved to installed-package nodes
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
        """Look a node up by API name, then display name.

        Several component categories are named after their object (the sharing
        rules, workflow and assignment-rule files for ``Reservation__c`` are all
        called ``Reservation__c``), so with no ``kind`` the data-model node wins.
        """
        want = api_name.lower()
        exact = [n for n in self.nodes.values() if n.api_name.lower() == want]
        if kind is not None:
            exact = [n for n in exact if n.kind == kind]
        if exact:
            rank = {"object": 0, "field": 1, "record_type": 2}
            return min(exact, key=lambda n: rank.get(n.kind, 9))
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
        self.inner_types: dict[str, str] = {}  # 'customer' -> node id of the class declaring it


_FIRES_ON_HOST = {
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
}
_USER_CONTEXT_GLOBALS = {"$User", "$Profile", "$UserRole", "$Organization"}
_SECURITY = {CategoryName.PERMISSION_SET, CategoryName.PROFILE}


def build_graph(
    *,
    org_alias: str,
    components: list[Component],
    schema: SchemaSnapshot | None = None,
    dispatch_edges: list[DispatchEdge] | None = None,
    cmt_records: list[CMTRecord] | None = None,
    api_rows: list[dict[str, Any]] | None = None,
    cron_rows: list[dict[str, Any]] | None = None,
    data_profile: DataProfile | None = None,
) -> DependencyGraph:
    """Build the org graph: nodes first, then one edge pass per source of evidence."""
    b = _Builder(org_alias)
    b.add_component_nodes(components)
    if schema is not None:
        b.add_schema(schema)
    if data_profile is not None:
        b.add_data_profile(data_profile)
    if cmt_records:
        b.add_cmt_records(cmt_records, components)
    for c in components:
        b.add_component_edges(c)
    b.add_dispatch_edges(components, dispatch_edges or [], cmt_records)
    b.add_cron_edges(cron_rows or [])
    if api_rows:
        _fold_api_rows(b.g, b.idx, api_rows, b.obj_node, b.external)
    log.info(
        "understand.dependencies.built",
        nodes=len(b.g.nodes),
        edges=len(b.g.edges),
        unresolved=len(b.g.unresolved),
        by_evidence=b.g.edges_by_evidence(),
    )
    return b.g


class _Builder:
    """Holds the graph under construction plus the name → node indexes."""

    def __init__(self, org_alias: str) -> None:
        self.own_namespaces: set[str] = set()  # the project's own package namespace(s)
        self.org_alias = org_alias
        self.g = DependencyGraph(org_alias=org_alias)
        self.idx = _Index()

    # ---- nodes ----------------------------------------------------------------

    def add_component_nodes(self, components: list[Component]) -> None:
        for c in components:
            obj = _component_object(c)
            n = self.g.add_node(
                GraphNode(
                    id=str(c.id),
                    kind="component",
                    category=c.category.value,
                    name=c.name,
                    api_name=c.api_name or c.name,
                    object_name=obj,
                    meta=_component_meta(c),
                )
            )
            key = (c.api_name or c.name).lower()
            if c.category is CategoryName.APEX_CLASS:
                self.idx.apex[key] = n.id
                raw_c = c.raw if isinstance(c.raw, dict) else {}
                for inner in raw_c.get("inner_types", []) or []:
                    self.idx.inner_types.setdefault(str(inner).lower(), n.id)
            elif c.category in _FLOW_CATEGORIES:
                self.idx.flows[key] = n.id
            elif c.category is CategoryName.WORKFLOW_RULE and obj:
                self.idx.workflow_by_object[obj.lower()] = n.id
                raw = c.raw if isinstance(c.raw, dict) else {}
                for ea in raw.get("email_alerts", []):
                    if ea.get("name"):
                        self.idx.email_alerts[f"{obj}.{ea['name']}".lower()] = n.id
            elif c.category is CategoryName.PLATFORM_EVENT:
                self.idx.platform_events[key] = n.id
            self.idx.components[(c.category.value, key)] = n.id

    def add_schema(self, schema: SchemaSnapshot) -> None:
        for sn in schema.nodes:
            n = self.g.add_node(
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
                self.idx.objects[low] = n.id
            elif sn.kind is SchemaNodeKind.FIELD:
                self.idx.fields[low] = n.id
                self.idx.field_nodes[low] = sn
            else:
                self.idx.record_types[low] = n.id
        for sn in schema.fields():
            for target in sn.reference_to:
                self.g.add_edge(
                    str(sn.id),
                    self.obj_node(target),
                    DependencyKind.REFERENCES,
                    evidence=EvidenceChannel.SCHEMA,
                    notes="lookup",
                )

    def add_data_profile(self, profile: DataProfile) -> None:
        """Attach record counts to objects and fill rates to fields (nodes created as needed)."""
        for obj, op in profile.objects.items():
            n = self.g.node(self.obj_node(obj))
            if n is not None:
                n.meta["record_count"] = op.record_count
                n.meta["last_modified"] = op.last_modified.isoformat() if op.last_modified else None
            for q, fp in op.fields.items():
                fid = self.field_node(q) or self.inferred_field(obj, q.split(".", 1)[1])
                fn = self.g.node(fid)
                if fn is not None:
                    fn.meta["fill_rate"] = fp.fill_rate
                    fn.meta["non_null"] = fp.non_null

    def canonical_object(self, name: str) -> str:
        """Schema spelling for an object referenced in any case (report columns say LEAD)."""
        nid = self.idx.objects.get(name.lower())
        n = self.g.node(nid) if nid else None
        return n.api_name if n else name

    def obj_node(self, name: str) -> str:
        """Object node id, created on demand for objects the schema did not supply."""
        low = name.lower()
        if low in self.idx.objects:
            return self.idx.objects[low]
        nid = _external_id("object", name)
        self.g.add_node(
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
        self.idx.objects[low] = nid
        return nid

    def field_node(self, qualified: str) -> str | None:
        return self.idx.fields.get(qualified.lower())

    def inferred_field(self, obj: str, fname: str) -> str:
        """Create a standard-field node the source tree could not supply."""
        qualified = f"{obj}.{fname}"
        existing = self.idx.fields.get(qualified.lower())
        if existing:
            return existing
        self.obj_node(obj)
        nid = _external_id("field", qualified)
        target = _STANDARD_FIELD_TARGETS.get(fname.lower())
        sn = SchemaNode(
            org_alias=self.org_alias,
            kind=SchemaNodeKind.FIELD,
            api_name=qualified,
            object_name=obj,
            label=fname,
            custom=fname.endswith("__c"),
            reference_to=[target] if target else [],
        )
        self.g.add_node(
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
        self.idx.fields[qualified.lower()] = nid
        self.idx.field_nodes[qualified.lower()] = sn
        return nid

    def add_cmt_records(self, records: list[CMTRecord], components: list[Component]) -> None:
        """Custom metadata rows are configuration: a ``Customer_Fields__mdt`` row says
        which Contact fields the customer form shows, a ``Trigger_Action__mdt`` row says
        which handler runs. Each row becomes a node linked to its type, to every field
        or class its values name, and from every Apex class that reads the type — so a
        where-used on ``Contact.MailingCity`` reaches ``CustomerServices`` through the row.
        """
        readers: dict[str, list[str]] = {}
        for c in components:
            raw = c.raw if isinstance(c.raw, dict) else {}
            refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
            for obj in refs.get("objects", []):
                if str(obj).endswith("__mdt"):
                    readers.setdefault(str(obj).lower(), []).append(str(c.id))
        for r in records:
            api = f"{r.cmt_type}.{r.developer_name}"
            nid = _external_id("cmt_record", api)
            self.g.add_node(
                GraphNode(
                    id=nid,
                    kind="cmt_record",
                    category="custom_metadata_record",
                    name=r.developer_name,
                    api_name=api,
                    object_name=r.cmt_type,
                    meta={"fields": dict(r.fields)},
                )
            )
            ev = EvidenceChannel.CMT_RECORD
            self.g.add_edge(
                nid,
                self.obj_node(r.cmt_type),
                DependencyKind.REFERENCES,
                evidence=ev,
                notes="row of",
            )
            # Values that name objects give the other values an object to resolve against.
            hosts = [v for v in r.fields.values() if v and v.lower() in self.idx.objects]
            for fname, value in r.fields.items():
                if not value or len(value) > 120:
                    continue
                if "." in value and (fid := self.field_node(value)):
                    self.g.add_edge(nid, fid, DependencyKind.REFERENCES, evidence=ev, notes=fname)
                    continue
                if (tid := self.idx.apex.get(value.lower())) is not None:
                    self.g.add_edge(nid, tid, DependencyKind.CALLS, evidence=ev, notes=fname)
                    continue
                for host in hosts:
                    fid = self.field_node(f"{host}.{value}")
                    if fid is None and _IDENTIFIER_RE.match(value) and not value.endswith("__c"):
                        # A standard field the schema snapshot did not list (source trees
                        # carry custom fields only): infer it, as Apex references do.
                        fid = self.inferred_field(host, value)
                    if fid:
                        self.g.add_edge(
                            nid, fid, DependencyKind.REFERENCES, evidence=ev, notes=fname
                        )
                        break
            for reader in readers.get(r.cmt_type.lower(), []):
                self.g.add_edge(
                    reader,
                    nid,
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    confidence=0.8,
                    notes="reads configuration",
                )

    def package_field(self, namespace: str, obj: str, fname: str) -> str:
        """A field that belongs to an installed package: an inferred field under an
        inferred object, plus one ``Package`` node per namespace that the org depends on."""
        pkg = self.external("Package", namespace)
        fid = self.inferred_field(obj, fname)
        node = self.g.node(fid)
        if node is not None and not node.meta.get("package"):
            node.meta["package"] = namespace
            self.g.add_edge(
                fid,
                pkg,
                DependencyKind.REFERENCES,
                evidence=EvidenceChannel.SCHEMA,
                confidence=0.9,
                notes="field of installed package",
            )
            self.g.package_fields += 1
        return fid

    def external(self, kind: str, name: str) -> str:
        nid = _external_id(kind, name)
        self.g.add_node(
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

    # ---- edges ----------------------------------------------------------------

    def add_component_edges(self, c: Component) -> None:
        raw = c.raw if isinstance(c.raw, dict) else {}
        refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
        src = str(c.id)
        ev = _evidence_for(c.category)
        host = _component_object(c)
        written = _written_fields(raw)

        self._trigger_edges(c, src, ev, host)
        self._object_edges(refs, src, ev, host, written)
        for f in refs.get("fields", []):
            _link_field(self, src, c, f, ev, written)
        defines = refs.get("defines_field")
        if defines and (fid := self.field_node(defines)):
            self.g.add_edge(src, fid, DependencyKind.OWNS, evidence=ev, notes="defines")
        self._apex_edges(c, refs, src, ev)
        self._flow_edges(c, refs, src, ev)
        self._surface_edges(refs, src, ev)
        self._action_edges(c, refs, src, ev, host)
        self._global_edges(refs, src, ev)
        if c.category is CategoryName.PLATFORM_EVENT and host:
            self.g.add_edge(
                src, self.obj_node(host), DependencyKind.OWNS, evidence=ev, notes="defines event"
            )

    def _trigger_edges(self, c: Component, src: str, ev: EvidenceChannel, host: str | None) -> None:
        """What does this component fire on (or, for surfaces, what does it display)?"""
        if not host:
            return
        if c.category not in AUTOMATION_CATEGORIES:
            self.g.add_edge(
                src, self.obj_node(host), DependencyKind.REFERENCES, evidence=ev, notes="surface"
            )
        elif c.category in _FIRES_ON_HOST:
            self.g.add_edge(
                src,
                self.obj_node(host),
                DependencyKind.TRIGGERS,
                evidence=ev,
                notes=_trigger_note(c),
            )
        elif c.category is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW:
            target = self.idx.platform_events.get(host.lower()) or self.obj_node(host)
            self.g.add_edge(
                src,
                target,
                DependencyKind.TRIGGERS,
                evidence=ev,
                notes="platform event subscription",
            )

    def _object_edges(
        self,
        refs: dict[str, Any],
        src: str,
        ev: EvidenceChannel,
        host: str | None,
        written: set[str],
    ) -> None:
        """Objects read or written as whole records ('<Obj>.*' in the written set)."""
        written_objs = {w[:-2] for w in written if w.endswith(".*")}
        conf = 0.9 if ev is EvidenceChannel.APEX_PARSE else 1.0
        for o in refs.get("objects", []):
            if not o or o == host:
                continue
            note = "write" if o.lower() in written_objs else None
            self.g.add_edge(
                src,
                self.obj_node(o),
                DependencyKind.REFERENCES,
                evidence=ev,
                confidence=conf,
                notes=note,
            )
        for cs in refs.get("custom_settings", []):
            self.g.add_edge(
                src,
                self.obj_node(cs),
                DependencyKind.REFERENCES,
                evidence=ev,
                notes="custom setting",
            )

    def _apex_edges(
        self, c: Component, refs: dict[str, Any], src: str, ev: EvidenceChannel
    ) -> None:
        conf = 0.95 if ev is EvidenceChannel.APEX_PARSE else 1.0
        if refs.get("dynamic_access"):
            conf = min(conf, 0.75)  # the class also reaches things we cannot see
        grant = c.category in _SECURITY
        kind = DependencyKind.REFERENCES if grant else DependencyKind.CALLS
        for cls in refs.get("apex_classes", []):
            tid = self.idx.apex.get(cls.lower())
            if tid:
                self.g.add_edge(
                    src,
                    tid,
                    kind,
                    evidence=ev,
                    confidence=conf,
                    notes="class access" if grant else None,
                )
                continue
            if "." in cls and (tid := self.idx.apex.get(cls.split(".", 1)[1].lower())):
                # namespaced reference to an unmanaged class: Ns.Class
                self.g.add_edge(src, tid, DependencyKind.CALLS, evidence=ev, confidence=0.8)
                continue
            if ev is EvidenceChannel.APEX_PARSE and not _looks_like_class(cls):
                continue
            if "." in cls and (tid := self.idx.apex.get(cls.split(".", 1)[0].lower())):
                # Outer.Inner: the inner class lives in the outer class's body.
                self.g.add_edge(src, tid, DependencyKind.CALLS, evidence=ev, confidence=0.8)
                continue
            if cls.split(".", 1)[0] in _PLATFORM_TYPES:
                continue  # System / Schema / Database namespace types are not org code
            if "." not in cls and is_sobject_name(cls) and cls[0].isupper():
                # ``EntityDefinition``, ``LeadStatus``, ``ContentDistribution`` …: standard
                # objects used as Apex types; the schema snapshot may not list them.
                self.g.add_edge(src, self.obj_node(cls), DependencyKind.REFERENCES, evidence=ev)
                continue
            if tid := self.idx.inner_types.get(cls.lower()):
                # An inner class named without its outer class (``Customer`` for
                # ``CustomerServices.Customer``): the dependency is on the declaring class.
                if tid != src:
                    self.g.add_edge(
                        src,
                        tid,
                        DependencyKind.CALLS,
                        evidence=ev,
                        confidence=0.8,
                        notes="inner type",
                    )
                continue
            self.g.unresolved.append(UnresolvedReference(src, c.name, "apex_class", cls, ev))
        for cls in refs.get("async_targets", []) + refs.get("type_forname", []):
            tid = self.idx.apex.get(cls.lower())
            if tid:
                self.g.add_edge(src, tid, DependencyKind.CALLS, evidence=ev, notes="async/dynamic")
        for cls in refs.get("apex_class_candidates", []):
            # Case-insensitive qualifier that names a real class (Apex allows it);
            # silently a variable otherwise.
            tid = self.idx.apex.get(cls.lower())
            if tid:
                self.g.add_edge(
                    src,
                    tid,
                    DependencyKind.CALLS,
                    evidence=ev,
                    confidence=0.8,
                    notes="case-insensitive qualifier",
                )
        for lwc in refs.get("lwc_bundles", []):
            # ``c:name`` is an LWC or an Aura bundle; markup does not say which.
            tid = self.idx.components.get(("lwc_bundle", lwc.lower())) or self.idx.components.get(
                ("aura_bundle", lwc.lower())
            )
            if tid and tid != src:
                self.g.add_edge(
                    src, tid, DependencyKind.CALLS, evidence=ev, notes="embedded component"
                )
        for ch in refs.get("message_channels", []):
            # Publish/subscribe over a Lightning message channel: every bundle on the
            # channel is coupled to every other one through this node.
            self.g.add_edge(
                src,
                self.external("LightningMessageChannel", ch),
                DependencyKind.REFERENCES,
                evidence=ev,
                notes="message channel",
            )

    def _surface_edges(self, refs: dict[str, Any], src: str, ev: EvidenceChannel) -> None:
        """Tabs open pages; apps list tabs and override record pages."""
        for fp in refs.get("flexipages", []):
            tid = self.idx.components.get(("flexipage", fp.lower()))
            if tid:
                self.g.add_edge(
                    src, tid, DependencyKind.REFERENCES, evidence=ev, notes="opens page"
                )
        for tab in refs.get("tabs", []):
            tid = self.idx.components.get(("custom_tab", tab.lower()))
            if tid:
                self.g.add_edge(src, tid, DependencyKind.REFERENCES, evidence=ev, notes="tab")

    def _flow_edges(
        self, c: Component, refs: dict[str, Any], src: str, ev: EvidenceChannel
    ) -> None:
        grant = c.category in _SECURITY
        for fl in refs.get("flows", []):
            tid = self.idx.flows.get(fl.lower())
            if tid:
                self.g.add_edge(
                    src,
                    tid,
                    DependencyKind.REFERENCES if grant else DependencyKind.CALLS,
                    evidence=ev,
                    notes="flow access" if grant else "subflow/flow action",
                )
            else:
                self.g.unresolved.append(UnresolvedReference(src, c.name, "flow", fl, ev))
        for comp in refs.get("lwc_bundles", []) if c.category in _FLOW_CATEGORIES else []:
            # Screen components (``extensionName``) and component actions
            # (``actionType=component``): an LWC bundle or an Aura bundle.
            tid = self.idx.components.get(("lwc_bundle", comp.lower())) or self.idx.components.get(
                ("aura_bundle", comp.lower())
            )
            if tid:
                self.g.add_edge(
                    src, tid, DependencyKind.CALLS, evidence=ev, notes="screen component"
                )
            else:
                self.g.unresolved.append(UnresolvedReference(src, c.name, "lwc_bundle", comp, ev))
        for pe in refs.get("platform_events", []):
            tid = self.idx.platform_events.get(pe.lower())
            if tid:
                self.g.add_edge(src, tid, DependencyKind.REFERENCES, evidence=ev, notes="event")

    def _action_edges(
        self, c: Component, refs: dict[str, Any], src: str, ev: EvidenceChannel, host: str | None
    ) -> None:
        """Email alerts, templates, workflow actions, named credentials, labels."""
        for alert in refs.get("email_alerts", []):
            tid = self.idx.email_alerts.get(alert.lower())
            if tid:
                self.g.add_edge(
                    src, tid, DependencyKind.REFERENCES, evidence=ev, notes=f"email alert {alert}"
                )
            else:
                self.g.add_edge(
                    src,
                    self.external("EmailAlert", alert),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    confidence=0.8,
                )
        for tpl in refs.get("email_templates", []):
            self.g.add_edge(
                src, self.external("EmailTemplate", tpl), DependencyKind.REFERENCES, evidence=ev
            )
        for wa in refs.get("workflow_actions", []):
            wid = self.idx.workflow_by_object.get((host or "").lower())
            if wid:
                self.g.add_edge(src, wid, DependencyKind.REFERENCES, evidence=ev, notes=wa)
            else:
                self.g.unresolved.append(UnresolvedReference(src, c.name, "action", wa, ev))
        for nc in refs.get("named_credentials", []):
            self.g.add_edge(
                src, self.external("NamedCredential", nc), DependencyKind.REFERENCES, evidence=ev
            )
        for lb in refs.get("custom_labels", []):
            self.g.add_edge(
                src, self.external("CustomLabel", lb), DependencyKind.REFERENCES, evidence=ev
            )

    def _global_edges(self, refs: dict[str, Any], src: str, ev: EvidenceChannel) -> None:
        """``$Setup.X.Y``, ``$Permission.Z``, ``$User.Field`` and friends."""
        for gl in refs.get("globals", []):
            head, _, rest = gl.partition(".")
            if head in {"$Setup", "$CustomMetadata"} and gl.count(".") >= 2:
                obj, _, fld = rest.partition(".")
                target = self.field_node(f"{obj}.{fld.split('.')[0]}") or self.obj_node(obj)
                self.g.add_edge(src, target, DependencyKind.REFERENCES, evidence=ev, notes=gl)
            elif head in {"$Permission", "$Label"}:
                self.g.add_edge(
                    src, self.external(head[1:], rest or gl), DependencyKind.REFERENCES, evidence=ev
                )
            elif head in _USER_CONTEXT_GLOBALS:
                fid = self.field_node(f"{head[1:]}.{rest}") if rest else None
                self.g.add_edge(
                    src,
                    fid or self.obj_node(head[1:]),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    notes=gl,
                )

    def add_dispatch_edges(
        self,
        components: list[Component],
        dispatch_edges: list[DispatchEdge],
        cmt_records: list[CMTRecord] | None = None,
    ) -> None:
        """CMT-driven dispatch: trigger (or dispatcher class) → handler class.

        Rows that name their object (``Object__c``, as TDTM tables do) point straight
        at the trigger on that object; the developer-name convention is the fallback.
        """
        object_by_row = {
            r.developer_name: str(r.fields.get("Object__c") or "") for r in (cmt_records or [])
        }
        for de in dispatch_edges:
            tid = self.idx.apex.get(de.handler_class.lower())
            if tid is None:
                continue
            src_id = _dispatch_source(
                components, self.idx, de, target_object=object_by_row.get(de.dispatcher_cmt, "")
            )
            if src_id:
                self.g.add_edge(
                    src_id,
                    tid,
                    DependencyKind.DISPATCHES,
                    evidence=EvidenceChannel.CMT_DISPATCH,
                    confidence=de.confidence,
                    notes=f"{de.dispatcher_cmt}.{de.field_name}",
                )

    def add_cron_edges(self, cron_rows: list[dict[str, Any]]) -> None:
        for row in cron_rows:
            detail = row.get("CronJobDetail") or {}
            cls = str(row.get("apex_class") or detail.get("Name") or "")
            tid = self.idx.apex.get(cls.lower())
            if tid:
                nid = self.external("CronTrigger", str(detail.get("Name") or cls))
                self.g.add_edge(
                    nid,
                    tid,
                    DependencyKind.TRIGGERS,
                    evidence=EvidenceChannel.CRON,
                    notes=str(row.get("CronExpression") or ""),
                )


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


def _component_meta(c: Component) -> dict[str, Any]:
    raw = c.raw if isinstance(c.raw, dict) else {}
    meta: dict[str, Any] = {"content_hash": c.content_hash, "active": _component_active(c)}
    if c.category is CategoryName.APEX_CLASS:
        meta["is_test"] = bool(raw.get("is_test"))
        meta["dynamic_access"] = list(raw.get("dynamic_access", []))
    if c.category not in AUTOMATION_CATEGORIES:
        meta["surface"] = str(raw.get("surface", ""))
    return meta


def _evidence_for(cat: CategoryName) -> EvidenceChannel:
    if cat in {CategoryName.PAGE_LAYOUT, CategoryName.FLEXIPAGE}:
        return EvidenceChannel.LAYOUT_XML
    if cat in _SECURITY:
        return EvidenceChannel.PERMISSION_XML
    if cat is CategoryName.REPORT:
        return EvidenceChannel.REPORT_XML
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
    if cat is CategoryName.AURA_BUNDLE:
        return EvidenceChannel.AURA_MARKUP
    if cat in {
        CategoryName.CUSTOM_TAB,
        CategoryName.CUSTOM_APPLICATION,
        CategoryName.PATH_ASSISTANT,
    }:
        return EvidenceChannel.LAYOUT_XML
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


# Apex platform types that read like class references but are not org code. Only
# names the tokenizer would otherwise report as unresolved classes belong here.
_PLATFORM_TYPES = frozenset(
    {
        "AggregateResult",
        "HttpCalloutMock",
        "WebServiceMock",
        "SaveResult",
        "DeleteResult",
        "UpsertResult",
        "UndeleteResult",
        "MergeResult",
        "SoapType",
        "DisplayType",
        "IllegalArgumentException",
        "NoAccessException",
        "NoDataFoundException",
        "InvalidParameterValueException",
        "JSONException",
        "MathException",
        "StringException",
        "ListException",
        "SObjectException",
        "SecurityException",
        "LimitException",
        "AccessLevel",
        "Comparator",
        "DataWeave",
        "DataWeaveScriptResource",
        "DataWeaveScriptException",
        "Continuation",
        "Cookie",
        "Version",
        "Callable",
        "SandboxPostCopy",
        "SandboxContext",
        "Stack",
        "Deque",
        "AccessType",
        "Security",
        "SObjectAccessDecision",
        "Schema",
        "Database",
        "System",
        "Test",
        "Limits",
        "Math",
        "JSON",
        "JSONParser",
        "JSONGenerator",
        "Http",
        "HttpRequest",
        "HttpResponse",
        "Messaging",
        "Crypto",
        "EncodingUtil",
        "PageReference",
        "ApexPages",
        "UserInfo",
        "Datetime",
        "DateTime",
        "Date",
        "Time",
        "Decimal",
        "Integer",
        "Long",
        "Double",
        "Boolean",
        "String",
        "Id",
        "Blob",
        "Map",
        "List",
        "Set",
        "Object",
        "SObject",
        "SObjectType",
        "SObjectField",
        "DescribeSObjectResult",
        "DescribeFieldResult",
        "Exception",
        "DmlException",
        "QueryException",
        "AuraHandledException",
        "CalloutException",
        "NullPointerException",
        "TypeException",
        "Type",
        "Pattern",
        "Matcher",
        "URL",
        "Url",
        "Site",
        "Network",
        "Auth",
        "Flow",
        "Process",
        "Approval",
        "ConnectApi",
        "Cache",
        "Label",
        "Trigger",
        "Savepoint",
        "QueueableContext",
        "BatchableContext",
        "SchedulableContext",
        "FinalizerContext",
        "Iterator",
        "Iterable",
        "Comparable",
        "InstallHandler",
        "InstallContext",
        "UninstallHandler",
        "LoggingLevel",
        "Assert",
        "Formula",
        "EventBus",
        "Queueable",
        "Batchable",
        "Schedulable",
        "StaticResource",
        "SelectOption",
        "Component",
        "Reports",
        "Dom",
        "XmlStreamReader",
        "XmlStreamWriter",
        "Search",
        "Metadata",
        "Quiddity",
        "Request",
        "Invocable",
        "InvocableVariable",
        "InvocableMethod",
        "AppLauncher",
        "Canvas",
        "TxnSecurity",
        "Wave",
        "Sfc",
        "Support",
        "KbManagement",
        "QuickAction",
    }
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


_NS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)__[A-Za-z0-9_]+__(?:c|mdt|e|b|x|r)$")


def _namespace_of(name: str) -> str | None:
    """``npo02__Household__c`` → ``npo02``; ``Score__c`` → None."""
    m = _NS_RE.match(name or "")
    return m.group(1) if m else None


def _looks_like_class(name: str) -> bool:
    """Filter analyzer noise: single capitalized words that are common field/type names,
    and ALL_CAPS identifiers (constants, enum values), which are never classes."""
    if "." in name:
        return True
    if name.isupper() or re.fullmatch(r"[A-Z0-9_]+", name):
        return False
    return len(name) > 2 and not name.endswith(("__c", "__r", "Id"))


def _link_field(
    b: _Builder, src: str, c: Component, qualified: str, ev: EvidenceChannel, written: set[str]
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
    cur_obj = b.canonical_object(obj)
    conf = 0.9 if ev is EvidenceChannel.APEX_PARSE else 1.0
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        fname = seg if last else _relationship_to_field(seg)
        fid = b.field_node(f"{cur_obj}.{fname}")
        if fid is None and not last and (fid := b.field_node(f"{cur_obj}.{seg}")):
            fname = seg
        if fid is None:
            ns = _namespace_of(fname) or _namespace_of(cur_obj)
            if ns and ns.lower() not in b.own_namespaces:
                # A field of an installed package (npe01__, npo02__ …): the org has it,
                # a source tree does not. Record the package dependency, not a gap.
                fid = b.package_field(ns, cur_obj, fname)
            elif fname.endswith("__c") and not cur_obj.endswith(("__mdt", "__e", "__b", "__x")):
                b.g.unresolved.append(UnresolvedReference(src, c.name, "field", qualified, ev))
                b.g.add_edge(
                    src,
                    b.obj_node(cur_obj),
                    DependencyKind.REFERENCES,
                    evidence=ev,
                    confidence=0.7,
                    notes=f"unresolved field {qualified}",
                )
                return
            if fid is None:
                fid = b.inferred_field(cur_obj, fname)
        q = f"{cur_obj}.{fname}".lower()
        note = "write" if last and q in written else "read"
        b.g.add_edge(src, fid, DependencyKind.REFERENCES, evidence=ev, confidence=conf, notes=note)
        if last:
            return
        sn = b.idx.field_nodes.get(q)
        if sn and sn.reference_to and sn.reference_to[0]:
            cur_obj = sn.reference_to[0]
            continue
        # Polymorphic or unknown hop: stop here, keep what we have. The edge to the
        # relationship field itself (e.g. ``Customer_Fields__mdt.Customer_City__c``, a
        # metadata relationship whose ``__r.QualifiedApiName`` lives on FieldDefinition)
        # is the real dependency, so this is a partial resolution, not a gap.
        if sn is None:
            b.g.unresolved.append(UnresolvedReference(src, c.name, "field", qualified, ev))
        return


def _relationship_to_field(segment: str) -> str:
    if segment.endswith("__r"):
        return segment[:-3] + "__c"
    return _STANDARD_RELATIONSHIPS.get(segment.lower(), segment + "Id")


def _dispatch_source(
    components: list[Component], idx: _Index, de: DispatchEdge, *, target_object: str = ""
) -> str | None:
    """Prefer the trigger on the CMT row's object; fall back to a dispatcher class."""
    if target_object:
        # The row says which object it serves: the trigger on that object dispatches it.
        for c in components:
            if c.category is CategoryName.APEX_TRIGGER:
                raw = c.raw if isinstance(c.raw, dict) else {}
                if str(raw.get("sobject", "")).lower() == target_object.lower():
                    return str(c.id)
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
    # Fall back to the dispatcher class the row's trigger actually calls; never a random handler.
    for c in components:
        if c.category is CategoryName.APEX_TRIGGER:
            raw = c.raw if isinstance(c.raw, dict) else {}
            refs = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
            for cls in refs.get("apex_classes", []):
                low = cls.lower()
                if ("triggerhandler" in low or "dispatcher" in low) and low in idx.apex:
                    return idx.apex[low]
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
            # The API drops the suffix ("Territory" for Territory__c, "Customer_Fields"
            # for Customer_Fields__mdt); resolve to a known object before inventing one.
            # Custom suffixes first: "Territory" means Territory__c even though a
            # standard Territory object exists.
            for cand in (low + "__c", low + "__mdt", low + "__e", low + "__b", low + "__x", low):
                if cand in idx.objects:
                    return str(idx.objects[cand])
            return str(obj_node(name))
        if t == "validationrule" and "." in name:
            _, n = name.split(".", 1)
            return idx.components.get(("validation_rule", n.lower()))
        if t == "workflowrule" and "." in name:
            return idx.workflow_by_object.get(name.split(".", 1)[0].lower())
        if t in {"workflowfieldupdate", "workflowalert", "workflowtask"} and "." in name:
            return idx.workflow_by_object.get(name.split(".", 1)[0].lower())
        if t == "lightningcomponentbundle":
            return idx.components.get(("lwc_bundle", low))
        if t == "auradefinitionbundle":
            # The API suffixes Aura rows with ".<n>" (``customerDetails.2``).
            return idx.components.get(("aura_bundle", low.split(".", 1)[0])) or str(
                external(mtype, name)
            )
        if t == "flexipage" and low.startswith("flexipage:"):
            return None  # standard page templates/components (flexipage:tabset …), not org metadata
        if t in {
            "layout",
            "flexipage",
            "permissionset",
            "profile",
            "customtab",
            "customapplication",
            "pathassistant",
        }:
            cat = {
                "layout": "page_layout",
                "flexipage": "flexipage",
                "permissionset": "permission_set",
                "profile": "profile",
                "customtab": "custom_tab",
                "customapplication": "custom_application",
                "pathassistant": "path_assistant",
            }[t]
            return idx.components.get((cat, low)) or str(external(mtype, name))
        if t == "report":
            hit = idx.components.get(("report", low))
            if hit is None:  # API rows carry the developer name without the folder
                hit = next(
                    (
                        nid
                        for (cat, key), nid in idx.components.items()
                        if cat == "report" and key.rsplit("/", 1)[-1] == low
                    ),
                    None,
                )
            return hit or str(external(mtype, name))
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
        if not matched:
            # The API points some rows the other way (CustomObject -> FlexiPage for a
            # record page assignment); a parser edge in either direction is a match.
            for e in g.outbound(t):
                if str(e.target_id) == s:
                    e.corroborated_by_api = True
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
