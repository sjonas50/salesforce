"""Tooling / REST pull client (C1, AD-29 primary path).

Everything goes through the MCP gateway's backend so quota accounting and
Engram anchoring apply. No CLI on the customer side: an OAuth-connected
org is enough.

What it can and cannot see:

* **Full**: Apex classes + triggers (bodies), Flows (active version
  ``Metadata`` JSON), validation rules, workflow rules + actions, custom
  fields (formulas, roll-ups), LWC bundles, platform events, CDC channel
  members, CMT rows, CronTriggers, ``MetadataComponentDependency`` rows,
  the data model via ``describe``.
* **Partial**: approval processes (``ProcessDefinition`` exposes name,
  object, and state only), assignment / escalation / auto-response / sharing
  rules (name, object, active). Their bodies need the Metadata API — use the
  sf CLI path or a customer-supplied SFDX project for those. Partial records
  carry ``payload['partial'] = True`` and the coverage report says so.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, SchemaSnapshot
from offramp.extract.dispatch.cmt_reader import CMTRecord
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.schema import from_describe

log = get_logger(__name__)

_SKIP_OBJECT_SUFFIXES = ("Share", "History", "Feed", "ChangeEvent", "Tag", "__hd", "__b", "__x")
_STANDARD_OBJECTS_OF_INTEREST = {
    "Account",
    "Contact",
    "Lead",
    "Opportunity",
    "Case",
    "Task",
    "Event",
    "User",
    "Campaign",
    "CampaignMember",
    "Contract",
    "Order",
    "OrderItem",
    "Product2",
    "Pricebook2",
    "PricebookEntry",
    "Quote",
    "QuoteLineItem",
    "Asset",
    "OpportunityLineItem",
    "OpportunityContactRole",
    "Group",
    "UserRole",
    "Profile",
    "PermissionSet",
    "EmailMessage",
    "Entitlement",
    "WorkOrder",
    "ServiceAppointment",
    "Individual",
    "AccountContactRelation",
    "ContentVersion",
    "Solution",
}
_DEPENDENCY_TYPES = (
    "ApexClass",
    "ApexTrigger",
    "Flow",
    "CustomField",
    "CustomObject",
    "ValidationRule",
    "WorkflowRule",
    "WorkflowFieldUpdate",
    "WorkflowAlert",
    "Layout",
    "FlexiPage",
    "LightningComponentBundle",
    "AuraDefinitionBundle",
    "EmailTemplate",
    "QuickAction",
    "ApprovalProcess",
    "CustomLabel",
    "PermissionSet",
    "FieldSet",
    "ListView",
    "CustomMetadata",
)
_API_ROW_CAP = 2000


@dataclass
class ToolingPullStats:
    queries: int = 0
    records: int = 0
    dependency_rows: int = 0
    dependency_types_capped: list[str] = field(default_factory=list)
    partial_categories: list[str] = field(default_factory=list)


class ToolingApiPullClient:
    """Salesforce Tooling + REST client behind the MCP gateway backend."""

    source_name = "tooling_api"

    def __init__(self, *, gateway: Any, org_alias: str, api_version: str = "66.0") -> None:
        self.gateway = gateway  # offramp.mcp.server.MCPGateway
        self.org_alias = org_alias
        self.api_version = api_version
        self.source_version = f"tooling-{api_version}"
        self.stats = ToolingPullStats()

    # ---- PullClient ------------------------------------------------------------

    async def list_categories(self) -> set[CategoryName]:
        return set(CategoryName)

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        wanted = set(categories) if categories else set(CategoryName)
        out: list[RawMetadataRecord] = []
        if CategoryName.APEX_CLASS in wanted:
            out.extend(await self._apex_classes())
        if CategoryName.APEX_TRIGGER in wanted:
            out.extend(await self._apex_triggers())
        if wanted & _FLOW_CATEGORIES:
            out.extend([r for r in await self._flows() if r.category in wanted])
        if CategoryName.VALIDATION_RULE in wanted:
            out.extend(await self._validation_rules())
        if CategoryName.WORKFLOW_RULE in wanted:
            out.extend(await self._workflows())
        if wanted & {CategoryName.FORMULA_FIELD, CategoryName.ROLLUP_SUMMARY}:
            out.extend([r for r in await self._custom_fields() if r.category in wanted])
        if CategoryName.LWC_BUNDLE in wanted:
            out.extend(await self._lwc_bundles())
        if CategoryName.PLATFORM_EVENT in wanted:
            out.extend(await self._platform_events())
        if CategoryName.CHANGE_DATA_CAPTURE in wanted:
            out.extend(await self._cdc())
        if CategoryName.APPROVAL_PROCESS in wanted:
            out.extend(await self._approval_processes())
        for cat, obj in (
            (CategoryName.ASSIGNMENT_RULE, "AssignmentRule"),
            (CategoryName.ESCALATION_RULE, "EscalationRule"),
            (CategoryName.AUTO_RESPONSE_RULE, "AutoResponseRule"),
        ):
            if cat in wanted:
                out.extend(await self._partial_rules(cat, obj))
        if CategoryName.SHARING_RULE in wanted:
            out.extend(await self._sharing_rules())
        self.stats.records = len(out)
        log.info(
            "extract.tooling.pulled",
            records=len(out),
            queries=self.stats.queries,
            partial=self.stats.partial_categories,
        )
        return out

    # ---- supplement (CMT rows, dependency rows, cron, schema) --------------------

    async def cmt_records(self) -> list[CMTRecord]:
        """Every row of every Custom Metadata Type, via REST ``FIELDS(ALL)`` per type."""
        out: list[CMTRecord] = []
        types = await self._tq(
            "SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName LIKE '%__mdt'"
        )
        for t in types:
            name = str(t.get("QualifiedApiName", ""))
            if not name:
                continue
            try:
                rows = (
                    await self.gateway.sf_query(f"SELECT FIELDS(ALL) FROM {name} LIMIT 200")
                ).get("records", [])
            except Exception as exc:  # one bad type must not sink the run
                log.warning("extract.tooling.cmt_query_failed", cmt=name, error=str(exc))
                continue
            for r in rows:
                fields = {
                    k: str(v)
                    for k, v in r.items()
                    if k != "attributes" and v is not None and not isinstance(v, dict)
                }
                out.append(
                    CMTRecord(
                        cmt_type=name, developer_name=str(r.get("DeveloperName", "")), fields=fields
                    )
                )
        return out

    async def dependency_rows(
        self, *, types: Iterable[str] = _DEPENDENCY_TYPES
    ) -> list[dict[str, Any]]:
        """``MetadataComponentDependency`` rows, one query per component type (pitfall 12)."""
        out: list[dict[str, Any]] = []
        for t in types:
            try:
                rows = await self._tq(
                    "SELECT MetadataComponentId, MetadataComponentName, MetadataComponentType, "
                    "MetadataComponentNamespace, RefMetadataComponentId, RefMetadataComponentName, "
                    "RefMetadataComponentType, RefMetadataComponentNamespace "
                    f"FROM MetadataComponentDependency WHERE MetadataComponentType = '{t}'"
                )
            except Exception as exc:
                log.warning("extract.tooling.dependency_query_failed", type=t, error=str(exc))
                continue
            if len(rows) >= _API_ROW_CAP:
                self.stats.dependency_types_capped.append(t)
                log.warning("extract.tooling.dependency_rows_capped", type=t, rows=len(rows))
            out.extend(rows)
        self.stats.dependency_rows = len(out)
        return out

    async def cron_rows(self) -> list[dict[str, Any]]:
        crons = await self._tq(
            "SELECT Id, CronJobDetail.Name, CronJobDetail.JobType, CronExpression, State, NextFireTime FROM CronTrigger WHERE State IN ('WAITING', 'ACQUIRED', 'EXECUTING')"
        )
        jobs = await self._tq(
            "SELECT CronTriggerId, ApexClass.Name FROM AsyncApexJob WHERE JobType = 'ScheduledApex' AND Status IN ('Queued', 'Preparing', 'Processing', 'Holding')"
        )
        cls_by_cron = {
            str(j.get("CronTriggerId")): (j.get("ApexClass") or {}).get("Name") for j in jobs
        }
        for c in crons:
            c["apex_class"] = cls_by_cron.get(str(c.get("Id")), "")
        return crons

    async def schema(self) -> SchemaSnapshot:
        """Data model via ``describeGlobal`` + per-object ``describe``, scoped to customizable objects."""
        g = await self.gateway.sf_describe_global()
        self.stats.queries += 1
        names: list[str] = []
        for so in g.get("sobjects", []):
            n = str(so.get("name", ""))
            if not n or n.endswith(_SKIP_OBJECT_SUFFIXES):
                continue
            if (
                so.get("custom")
                or n in _STANDARD_OBJECTS_OF_INTEREST
                or n.endswith(("__mdt", "__e"))
            ):
                names.append(n)
        describes: dict[str, dict[str, Any]] = {}
        for n in names:
            try:
                describes[n] = await self.gateway.sf_describe(n)
                self.stats.queries += 1
            except Exception as exc:
                log.warning("extract.tooling.describe_failed", sobject=n, error=str(exc))
        return from_describe(
            {"sobjects": [s for s in g.get("sobjects", []) if s.get("name") in describes]},
            describes,
            org_alias=self.org_alias,
        )

    # ---- per-category pulls ----------------------------------------------------

    async def _tq(self, soql: str) -> list[dict[str, Any]]:
        self.stats.queries += 1
        resp = await self.gateway.sf_tooling_query(soql)
        return [r for r in resp.get("records", []) if isinstance(r, dict)]

    def _rec(
        self,
        cat: CategoryName,
        api_name: str,
        payload: dict[str, Any],
        namespace: str | None = None,
    ) -> RawMetadataRecord:
        return RawMetadataRecord(
            source=self.source_name,
            source_version=self.source_version,
            api_version=self.api_version,
            category=cat,
            api_name=api_name,
            namespace=namespace or None,
            payload=payload,
        )

    async def _apex_classes(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, Name, NamespacePrefix, ApiVersion, Status, Body, LengthWithoutComments FROM ApexClass"
        )
        out = []
        for r in rows:
            name = str(r.get("Name", ""))
            body = r.get("Body") or ""
            out.append(
                self._rec(
                    CategoryName.APEX_CLASS,
                    name,
                    {
                        "path": f"classes/{name}.cls-meta.xml",
                        "parsed": {
                            "ApexClass": {
                                "apiVersion": str(r.get("ApiVersion", "")),
                                "status": str(r.get("Status", "Active")),
                            }
                        },
                        "body": body if body != "(hidden)" else "",
                        "managed_hidden": body == "(hidden)",
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _apex_triggers(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, Name, NamespacePrefix, ApiVersion, Status, Body, TableEnumOrId, UsageBeforeInsert, UsageAfterInsert, UsageBeforeUpdate, UsageAfterUpdate, UsageBeforeDelete, UsageAfterDelete, UsageAfterUndelete FROM ApexTrigger"
        )
        out = []
        for r in rows:
            name = str(r.get("Name", ""))
            body = r.get("Body") or ""
            out.append(
                self._rec(
                    CategoryName.APEX_TRIGGER,
                    name,
                    {
                        "path": f"triggers/{name}.trigger-meta.xml",
                        "parsed": {
                            "ApexTrigger": {
                                "apiVersion": str(r.get("ApiVersion", "")),
                                "status": str(r.get("Status", "Active")),
                            }
                        },
                        "trigger_body": body if body != "(hidden)" else "",
                        "managed_hidden": body == "(hidden)",
                        "object_from_path": str(r.get("TableEnumOrId", "")),
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _flows(self) -> list[RawMetadataRecord]:
        defs = await self._tq(
            "SELECT Id, DeveloperName, NamespacePrefix, ActiveVersionId, LatestVersionId FROM FlowDefinition"
        )
        out = []
        for d in defs:
            vid = d.get("ActiveVersionId") or d.get("LatestVersionId")
            if not vid:
                continue
            versions = await self._tq(
                f"SELECT Id, FullName, ProcessType, Status, VersionNumber, Metadata FROM Flow WHERE Id = '{vid}'"
            )
            if not versions:
                continue
            v = versions[0]
            meta = v.get("Metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta.setdefault("processType", v.get("ProcessType", ""))
            meta.setdefault("status", v.get("Status", "Active"))
            name = str(d.get("DeveloperName", ""))
            cat = classify_flow(meta)
            out.append(
                self._rec(
                    cat,
                    name,
                    {
                        "path": f"flows/{name}.flow-meta.xml",
                        "parsed": {"Flow": meta},
                        "active": bool(d.get("ActiveVersionId")),
                        "version": v.get("VersionNumber"),
                        "tooling_id": v.get("Id"),
                    },
                    namespace=d.get("NamespacePrefix"),
                )
            )
        return out

    async def _validation_rules(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, ValidationName, Active, NamespacePrefix, EntityDefinition.QualifiedApiName, Metadata FROM ValidationRule"
        )
        out = []
        for r in rows:
            obj = str((r.get("EntityDefinition") or {}).get("QualifiedApiName", ""))
            name = str(r.get("ValidationName", ""))
            meta_raw = r.get("Metadata")
            meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
            meta.setdefault("active", bool(r.get("Active", True)))
            out.append(
                self._rec(
                    CategoryName.VALIDATION_RULE,
                    name,
                    {
                        "path": f"objects/{obj}/validationRules/{name}.validationRule-meta.xml",
                        "parsed": {"ValidationRule": meta},
                        "object_from_path": obj,
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _workflows(self) -> list[RawMetadataRecord]:
        """Compose one Workflow body per object from rules + actions."""
        by_obj: dict[str, dict[str, list[dict[str, Any]]]] = {}

        def bucket(obj: str) -> dict[str, list[dict[str, Any]]]:
            return by_obj.setdefault(
                obj,
                {
                    "rules": [],
                    "fieldUpdates": [],
                    "alerts": [],
                    "tasks": [],
                    "outboundMessages": [],
                },
            )

        for r in await self._tq("SELECT Id, Name, TableEnumOrId, Metadata FROM WorkflowRule"):
            meta = dict(r.get("Metadata") or {})
            meta.setdefault("fullName", r.get("Name"))
            bucket(str(r.get("TableEnumOrId", ""))).setdefault("rules", []).append(meta)
        for obj_name, key in (
            ("WorkflowFieldUpdate", "fieldUpdates"),
            ("WorkflowAlert", "alerts"),
            ("WorkflowTask", "tasks"),
            ("WorkflowOutboundMessage", "outboundMessages"),
        ):
            try:
                rows = await self._tq(
                    f"SELECT Id, Name, EntityDefinitionId, Metadata FROM {obj_name}"
                )
            except Exception as exc:
                log.warning(
                    "extract.tooling.workflow_action_query_failed", type=obj_name, error=str(exc)
                )
                continue
            for r in rows:
                meta = dict(r.get("Metadata") or {})
                meta.setdefault("fullName", r.get("Name"))
                bucket(str(r.get("EntityDefinitionId", "")))[key].append(meta)
        return [
            self._rec(
                CategoryName.WORKFLOW_RULE,
                obj,
                {
                    "path": f"workflows/{obj}.workflow-meta.xml",
                    "parsed": {"Workflow": body},
                    "object_from_path": obj,
                },
            )
            for obj, body in sorted(by_obj.items())
            if obj
        ]

    async def _custom_fields(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, DeveloperName, NamespacePrefix, TableEnumOrId, Metadata FROM CustomField"
        )
        out = []
        for r in rows:
            meta_raw = r.get("Metadata")
            meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
            obj = str(r.get("TableEnumOrId", ""))
            name = f"{r.get('DeveloperName', '')}__c"
            meta.setdefault("fullName", name)
            if meta.get("summaryOperation"):
                cat = CategoryName.ROLLUP_SUMMARY
            elif meta.get("formula"):
                cat = CategoryName.FORMULA_FIELD
            else:
                continue
            out.append(
                self._rec(
                    cat,
                    name,
                    {
                        "path": f"objects/{obj}/fields/{name}.field-meta.xml",
                        "parsed": {"CustomField": meta},
                        "object_from_path": obj,
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _lwc_bundles(self) -> list[RawMetadataRecord]:
        bundles = await self._tq(
            "SELECT Id, DeveloperName, NamespacePrefix FROM LightningComponentBundle"
        )
        if not bundles:
            return []
        resources = await self._tq(
            "SELECT LightningComponentBundleId, FilePath, Source FROM LightningComponentResource"
        )
        files_by_bundle: dict[str, dict[str, str]] = {}
        for res in resources:
            bid = str(res.get("LightningComponentBundleId", ""))
            fp = str(res.get("FilePath", "")).split("/")[-1]
            files_by_bundle.setdefault(bid, {})[fp] = str(res.get("Source") or "")
        return [
            self._rec(
                CategoryName.LWC_BUNDLE,
                str(b.get("DeveloperName", "")),
                {
                    "path": f"lwc/{b.get('DeveloperName', '')}",
                    "files": files_by_bundle.get(str(b.get("Id")), {}),
                },
                namespace=b.get("NamespacePrefix"),
            )
            for b in bundles
        ]

    async def _platform_events(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT QualifiedApiName, Label, NamespacePrefix FROM EntityDefinition WHERE QualifiedApiName LIKE '%__e'"
        )
        return [
            self._rec(
                CategoryName.PLATFORM_EVENT,
                str(r.get("QualifiedApiName", "")),
                {
                    "path": f"objects/{r.get('QualifiedApiName', '')}/{r.get('QualifiedApiName', '')}.object-meta.xml",
                    "parsed": {
                        "CustomObject": {
                            "label": r.get("Label", ""),
                            "eventType": "HighVolume",
                            "deploymentStatus": "Deployed",
                        }
                    },
                },
                namespace=r.get("NamespacePrefix"),
            )
            for r in rows
        ]

    async def _cdc(self) -> list[RawMetadataRecord]:
        try:
            rows = await self._tq(
                "SELECT SelectedEntity, PlatformEventChannel.DeveloperName FROM PlatformEventChannelMember"
            )
        except Exception as exc:
            log.warning("extract.tooling.cdc_query_failed", error=str(exc))
            rows = []
        by_channel: dict[str, list[str]] = {}
        for r in rows:
            ch = str((r.get("PlatformEventChannel") or {}).get("DeveloperName", "ChangeEvents"))
            by_channel.setdefault(ch, []).append(
                str(r.get("SelectedEntity", "")).replace("ChangeEvent", "")
            )
        return [
            self._rec(
                CategoryName.CHANGE_DATA_CAPTURE,
                ch,
                {
                    "path": f"_tooling/cdc_{ch}.json",
                    "parsed": {"subscribed_objects": sorted(objs), "channel": f"/data/{ch}"},
                },
            )
            for ch, objs in by_channel.items()
        ]

    async def _approval_processes(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, DeveloperName, Name, TableEnumOrId, State, Type FROM ProcessDefinition WHERE Type = 'Approval'"
        )
        if rows:
            self.stats.partial_categories.append(CategoryName.APPROVAL_PROCESS.value)
        return [
            self._rec(
                CategoryName.APPROVAL_PROCESS,
                f"{r.get('TableEnumOrId', '')}.{r.get('DeveloperName', '')}",
                {
                    "path": f"approvalProcesses/{r.get('TableEnumOrId', '')}.{r.get('DeveloperName', '')}.approvalProcess-meta.xml",
                    "parsed": {
                        "ApprovalProcess": {
                            "label": r.get("Name", ""),
                            "active": str(r.get("State", "")) == "Active",
                        }
                    },
                    "object_from_path": str(r.get("TableEnumOrId", "")),
                    "partial": True,
                },
            )
            for r in rows
        ]

    async def _partial_rules(
        self, cat: CategoryName, tooling_object: str
    ) -> list[RawMetadataRecord]:
        try:
            rows = await self._tq(f"SELECT Id, Name, SobjectType, Active FROM {tooling_object}")
        except Exception as exc:
            log.warning("extract.tooling.rule_query_failed", type=tooling_object, error=str(exc))
            return []
        by_obj: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_obj.setdefault(str(r.get("SobjectType", "")), []).append(
                {"fullName": r.get("Name", ""), "active": bool(r.get("Active", True))}
            )
        if by_obj:
            self.stats.partial_categories.append(cat.value)
        root = {
            CategoryName.ASSIGNMENT_RULE: ("AssignmentRules", "assignmentRule"),
            CategoryName.ESCALATION_RULE: ("EscalationRules", "escalationRule"),
            CategoryName.AUTO_RESPONSE_RULE: ("AutoResponseRules", "autoResponseRule"),
        }[cat]
        return [
            self._rec(
                cat,
                obj,
                {
                    "path": f"{root[0][0].lower() + root[0][1:]}/{obj}.{root[0][0].lower() + root[0][1:]}-meta.xml",
                    "parsed": {root[0]: {root[1]: groups}},
                    "object_from_path": obj,
                    "partial": True,
                },
            )
            for obj, groups in sorted(by_obj.items())
            if obj
        ]

    async def _sharing_rules(self) -> list[RawMetadataRecord]:
        # No Tooling object exposes sharing-rule bodies; record the objects that have any.
        try:
            rows = await self._tq("SELECT Id, DeveloperName, SobjectType FROM SharingRules")
        except Exception:
            rows = []
        by_obj: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_obj.setdefault(str(r.get("SobjectType", "")), []).append(
                {"fullName": r.get("DeveloperName", "")}
            )
        if by_obj:
            self.stats.partial_categories.append(CategoryName.SHARING_RULE.value)
        return [
            self._rec(
                CategoryName.SHARING_RULE,
                obj,
                {
                    "path": f"sharingRules/{obj}.sharingRules-meta.xml",
                    "parsed": {"SharingRules": {"sharingCriteriaRules": rules}},
                    "object_from_path": obj,
                    "partial": True,
                },
            )
            for obj, rules in sorted(by_obj.items())
            if obj
        ]


_FLOW_CATEGORIES = {
    CategoryName.RECORD_TRIGGERED_FLOW,
    CategoryName.SCREEN_FLOW,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.AUTOLAUNCHED_FLOW,
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.PROCESS_BUILDER,
}


def classify_flow(meta: dict[str, Any]) -> CategoryName:
    """Same discrimination as the source-tree reader, over a Flow metadata dict."""
    pt = str(meta.get("processType", ""))
    start_raw = meta.get("start")
    start: dict[str, Any] = start_raw if isinstance(start_raw, dict) else {}
    tt = str(start.get("triggerType", ""))
    if pt == "Orchestrator":
        return CategoryName.FLOW_ORCHESTRATION
    if pt in {"Workflow", "InvocableProcess", "CustomEvent"}:
        return CategoryName.PROCESS_BUILDER
    if tt == "PlatformEvent":
        return CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW
    if tt == "Scheduled" or pt == "ScheduleTriggered":
        return CategoryName.SCHEDULE_TRIGGERED_FLOW
    if tt.startswith("Record"):
        return CategoryName.RECORD_TRIGGERED_FLOW
    if pt == "Flow" and meta.get("screens"):
        return CategoryName.SCREEN_FLOW
    return CategoryName.AUTOLAUNCHED_FLOW
