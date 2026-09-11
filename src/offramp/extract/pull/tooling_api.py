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
* **Full via REST too**: page layouts and Lightning pages (Tooling
  ``Metadata``), permission sets and profiles (composed from
  ``FieldPermissions`` / ``ObjectPermissions`` / ``SetupEntityAccess``).
* **Partial**: approval processes (``ProcessDefinition`` exposes name,
  object, and state only), assignment / escalation / auto-response / sharing
  rules (name, object, active), reports beyond the newest 300. The CLI
  fills those from the Metadata API (:mod:`offramp.extract.pull.mdapi`)
  automatically; partial records carry ``payload['partial'] = True`` and
  the coverage report says so.
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
from offramp.extract.schema import from_describe, supplement_from_field_definitions

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
    metadata_fetch_failures: int = 0


class ToolingApiPullClient:
    """Salesforce Tooling + REST client behind the MCP gateway backend."""

    source_name = "tooling_api"

    def __init__(
        self,
        *,
        gateway: Any,
        org_alias: str,
        api_version: str = "66.0",
        skip_categories: set[CategoryName] | None = None,
    ) -> None:
        self.gateway = gateway  # offramp.mcp.server.MCPGateway
        self.skip_categories: set[CategoryName] = set(skip_categories or ())
        self.org_alias = org_alias
        self.api_version = api_version
        self.source_version = f"tooling-{api_version}"
        self.stats = ToolingPullStats()
        self._object_names: dict[str, str] | None = None

    # ---- PullClient ------------------------------------------------------------

    async def list_categories(self) -> set[CategoryName]:
        return set(CategoryName)

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        wanted = set(categories) if categories else set(CategoryName)
        # Surfaces the Metadata API path retrieves in one call each cost this client
        # one Tooling query *per record* (Metadata is single-record only): ~420 of the
        # ~600 calls a scan made on a small org. Skip what another path covers.
        wanted -= self.skip_categories
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
        if CategoryName.AURA_BUNDLE in wanted:
            out.extend(await self._aura_bundles())
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
        if CategoryName.PAGE_LAYOUT in wanted:
            out.extend(await self._layouts())
        if CategoryName.FLEXIPAGE in wanted:
            out.extend(await self._flexipages())
        if wanted & {CategoryName.PERMISSION_SET, CategoryName.PROFILE}:
            out.extend([r for r in await self._permissions() if r.category in wanted])
        if CategoryName.REPORT in wanted:
            out.extend(await self._reports())
        for cat, sobject, root in (
            (CategoryName.CUSTOM_TAB, "CustomTab", "CustomTab"),
            (CategoryName.CUSTOM_APPLICATION, "CustomApplication", "CustomApplication"),
            (CategoryName.PATH_ASSISTANT, "PathAssistant", "PathAssistant"),
        ):
            if cat in wanted:
                out.extend(await self._metadata_surface(cat, sobject, root))
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
        # ``_`` is a single-character wildcard in SOQL LIKE and EntityDefinition rejects
        # the ``\_`` escape, so the suffix is confirmed client-side.
        types = await self._tq(
            "SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName LIKE '%__mdt'"
        )
        for t in types:
            name = str(t.get("QualifiedApiName", ""))
            if not name.endswith("__mdt"):
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
        """CronTrigger + AsyncApexJob are standard REST objects, not Tooling objects."""
        crons = await self._q(
            "SELECT Id, CronJobDetail.Name, CronJobDetail.JobType, CronExpression, State, NextFireTime "
            "FROM CronTrigger WHERE State IN ('WAITING', 'ACQUIRED', 'EXECUTING')"
        )
        jobs = await self._q(
            "SELECT CronTriggerId, ApexClass.Name FROM AsyncApexJob "
            "WHERE JobType = 'ScheduledApex' AND Status IN ('Queued', 'Preparing', 'Processing', 'Holding')"
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
        snap = from_describe(
            {"sobjects": [s for s in g.get("sobjects", []) if s.get("name") in describes]},
            describes,
            org_alias=self.org_alias,
        )
        # describe hides fields the running user has no FLS on; FieldDefinition does not.
        by_object: dict[str, list[dict[str, Any]]] = {}
        for n in describes:
            try:
                # No ``IsCustom`` column; ``EntityDefinitionId`` accepts the API name and
                # the relationship form (``EntityDefinition.QualifiedApiName``) is rejected.
                by_object[n] = await self._tq(
                    "SELECT QualifiedApiName, Label, DataType FROM FieldDefinition "
                    f"WHERE EntityDefinitionId = '{n}'"
                )
            except Exception as exc:
                log.warning("extract.tooling.field_definition_failed", sobject=n, error=str(exc))
        supplement_from_field_definitions(snap, by_object)
        return snap

    # ---- per-category pulls ----------------------------------------------------

    async def _custom_object_names(self) -> dict[str, str]:
        """``TableEnumOrId`` holds a CustomObject Id (01I...) for custom objects; map it back."""
        if self._object_names is None:
            # CustomObject rows cover custom metadata types and platform events too, all
            # with 01I ids; the suffix comes from EntityDefinition (``%__mdt``/``%__e``
            # unescaped: ``_`` is a LIKE wildcard, so confirm the suffix client-side).
            suffix_by_dev: dict[str, str] = {}
            for like, suffix in (("%__mdt", "__mdt"), ("%__e", "__e")):
                try:
                    rows = await self._tq(
                        "SELECT QualifiedApiName FROM EntityDefinition "
                        f"WHERE QualifiedApiName LIKE '{like}'"
                    )
                except Exception as exc:
                    log.warning("extract.tooling.entity_suffix_failed", error=str(exc)[:160])
                    rows = []
                for r in rows:
                    qn = str(r.get("QualifiedApiName", ""))
                    if qn.endswith(suffix):
                        suffix_by_dev[qn[: -len(suffix)]] = suffix
            names: dict[str, str] = {}
            for r in await self._tq("SELECT Id, DeveloperName, NamespacePrefix FROM CustomObject"):
                ns = r.get("NamespacePrefix")
                dev = str(r.get("DeveloperName", ""))
                if dev:
                    full_dev = f"{ns}__{dev}" if ns else dev
                    names[str(r.get("Id"))] = full_dev + suffix_by_dev.get(full_dev, "__c")
            self._object_names = names
        return self._object_names

    async def _object_name(self, table: Any) -> str:
        t = str(table or "")
        if t.startswith("01I"):
            return (await self._custom_object_names()).get(t, t)
        return t

    async def _with_metadata(
        self, sobject: str, id_fields: str, *, detail_fields: str = "Id, Metadata"
    ) -> list[dict[str, Any]]:
        """Tooling ``Metadata`` (and ``FullName``) are only queryable for a single record:
        list ids, then fetch each."""
        rows = await self._tq(f"SELECT {id_fields} FROM {sobject}")
        out: list[dict[str, Any]] = []
        for r in rows:
            rid = r.get("Id")
            if not rid:
                continue
            try:
                detail = await self._tq(f"SELECT {detail_fields} FROM {sobject} WHERE Id = '{rid}'")
            except Exception as exc:
                # Salesforce-internal records (e.g. the ``CssDetail`` layout) answer
                # with HTTP 500; one bad row must not sink the whole category.
                log.warning(
                    "extract.tooling.metadata_fetch_failed",
                    sobject=sobject,
                    id=rid,
                    name=r.get("Name") or r.get("DeveloperName"),
                    error=str(exc)[:200],
                )
                self.stats.metadata_fetch_failures += 1
                continue
            first = detail[0] if detail else {}
            meta = first.get("Metadata")
            extra = {k: v for k, v in first.items() if k not in {"Id", "Metadata", "attributes"}}
            out.append({**r, **extra, "Metadata": meta if isinstance(meta, dict) else {}})
        return out

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
            "SELECT Id, Name, NamespacePrefix, ApiVersion, Status, Body, LengthWithoutComments, IsValid "
            "FROM ApexClass"
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
                        "length_without_comments": r.get("LengthWithoutComments"),
                        "is_valid": r.get("IsValid"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def installed_packages(self) -> list[dict[str, Any]]:
        """Installed managed packages: the only identity a hidden managed class has."""
        rows = await self._tq(
            "SELECT Id, SubscriberPackage.Name, SubscriberPackage.NamespacePrefix, "
            "SubscriberPackageVersion.Name, SubscriberPackageVersion.MajorVersion, "
            "SubscriberPackageVersion.MinorVersion, SubscriberPackageVersion.PatchVersion "
            "FROM InstalledSubscriberPackage"
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            pkg = r.get("SubscriberPackage") or {}
            ver = r.get("SubscriberPackageVersion") or {}
            out.append(
                {
                    "namespace": pkg.get("NamespacePrefix"),
                    "name": pkg.get("Name"),
                    "version": ver.get("Name"),
                    "version_number": ".".join(
                        str(ver.get(k) or 0)
                        for k in ("MajorVersion", "MinorVersion", "PatchVersion")
                    ),
                }
            )
        return out

    async def _apex_triggers(self) -> list[RawMetadataRecord]:
        rows = await self._tq(
            "SELECT Id, Name, NamespacePrefix, ApiVersion, Status, Body, TableEnumOrId FROM ApexTrigger"
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
                        "object_from_path": await self._object_name(r.get("TableEnumOrId")),
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
        rows = await self._with_metadata(
            "ValidationRule",
            "Id, ValidationName, Active, NamespacePrefix, EntityDefinition.QualifiedApiName",
        )
        out = []
        for r in rows:
            obj = str((r.get("EntityDefinition") or {}).get("QualifiedApiName", ""))
            name = str(r.get("ValidationName", ""))
            meta: dict[str, Any] = r["Metadata"]
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

        for r in await self._with_metadata("WorkflowRule", "Id, Name, TableEnumOrId"):
            meta = dict(r["Metadata"])
            meta.setdefault("fullName", r.get("Name"))
            bucket(await self._object_name(r.get("TableEnumOrId")))["rules"].append(meta)
        # The list query has no common name column (WorkflowAlert: DeveloperName,
        # WorkflowTask: Subject, the rest: Name); ``FullName`` ("Lead.Welcome_Lead_Alert")
        # is only queryable per record, so it rides along with the Metadata fetch.
        for obj_name, key in (
            ("WorkflowFieldUpdate", "fieldUpdates"),
            ("WorkflowAlert", "alerts"),
            ("WorkflowTask", "tasks"),
            ("WorkflowOutboundMessage", "outboundMessages"),
        ):
            try:
                rows = await self._with_metadata(
                    obj_name, "Id, EntityDefinitionId", detail_fields="Id, FullName, Metadata"
                )
            except Exception as exc:
                log.warning(
                    "extract.tooling.workflow_action_query_failed", type=obj_name, error=str(exc)
                )
                continue
            for r in rows:
                meta = dict(r["Metadata"])
                full = str(r.get("FullName") or "")
                meta.setdefault("fullName", full.split(".", 1)[1] if "." in full else full)
                bucket(await self._object_name(r.get("EntityDefinitionId")))[key].append(meta)
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
        rows = await self._with_metadata(
            "CustomField", "Id, DeveloperName, NamespacePrefix, TableEnumOrId"
        )
        out = []
        for r in rows:
            meta: dict[str, Any] = r["Metadata"]
            obj = await self._object_name(r.get("TableEnumOrId"))
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

    async def _aura_bundles(self) -> list[RawMetadataRecord]:
        bundles = await self._tq(
            "SELECT Id, DeveloperName, NamespacePrefix FROM AuraDefinitionBundle"
        )
        if not bundles:
            return []
        defs = await self._tq("SELECT AuraDefinitionBundleId, DefType, Source FROM AuraDefinition")
        name_by_id = {str(b.get("Id")): str(b.get("DeveloperName", "")) for b in bundles}
        suffix = {
            "COMPONENT": ".cmp",
            "APPLICATION": ".app",
            "EVENT": ".evt",
            "INTERFACE": ".intf",
            "CONTROLLER": "Controller.js",
            "HELPER": "Helper.js",
            "RENDERER": "Renderer.js",
            "STYLE": ".css",
            "DOCUMENTATION": ".auradoc",
            "DESIGN": ".design",
            "SVG": ".svg",
            "TOKENS": ".tokens",
        }
        files_by_bundle: dict[str, dict[str, str]] = {}
        for d in defs:
            bid = str(d.get("AuraDefinitionBundleId", ""))
            ext = suffix.get(str(d.get("DefType", "")).upper(), ".txt")
            files_by_bundle.setdefault(bid, {})[name_by_id.get(bid, "") + ext] = str(
                d.get("Source") or ""
            )
        return [
            self._rec(
                CategoryName.AURA_BUNDLE,
                name_by_id[str(b.get("Id"))],
                {
                    "path": f"aura/{name_by_id[str(b.get('Id'))]}",
                    "files": files_by_bundle.get(str(b.get("Id")), {}),
                },
                namespace=b.get("NamespacePrefix"),
            )
            for b in bundles
        ]

    async def _platform_events(self) -> list[RawMetadataRecord]:
        # Unescaped ``%__e`` also matches e.g. ``OrderShare`` (see cmt_records).
        rows = [
            r
            for r in await self._tq(
                "SELECT QualifiedApiName, Label, NamespacePrefix FROM EntityDefinition WHERE QualifiedApiName LIKE '%__e'"
            )
            if str(r.get("QualifiedApiName", "")).endswith("__e")
        ]
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
                "SELECT SelectedEntity, EventChannel FROM PlatformEventChannelMember"
            )
        except Exception as exc:
            log.warning("extract.tooling.cdc_query_failed", error=str(exc))
            rows = []
        by_channel: dict[str, list[str]] = {}
        for r in rows:
            ch = str(r.get("EventChannel") or "ChangeEvents")
            by_channel.setdefault(ch, []).append(
                _entity_from_change_event(str(r.get("SelectedEntity", "")))
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
        """``ProcessDefinition`` is a standard REST object; it exposes name, object, and state only."""
        rows = await self._q(
            "SELECT Id, DeveloperName, Name, TableEnumOrId, State, Type FROM ProcessDefinition WHERE Type = 'Approval'"
        )
        if rows:
            self.stats.partial_categories.append(CategoryName.APPROVAL_PROCESS.value)
        out = []
        for r in rows:
            obj = await self._object_name(r.get("TableEnumOrId"))
            name = f"{obj}.{r.get('DeveloperName', '')}"
            out.append(
                self._rec(
                    CategoryName.APPROVAL_PROCESS,
                    name,
                    {
                        "path": f"approvalProcesses/{name}.approvalProcess-meta.xml",
                        "parsed": {
                            "ApprovalProcess": {
                                "label": r.get("Name", ""),
                                "active": str(r.get("State", "")) == "Active",
                            }
                        },
                        "object_from_path": obj,
                        "partial": True,
                    },
                )
            )
        return out

    async def _partial_rules(
        self, cat: CategoryName, tooling_object: str
    ) -> list[RawMetadataRecord]:
        if tooling_object == "EscalationRule":
            # No Tooling or REST object exposes escalation rules; the Metadata API
            # path (sf CLI / source tree) is the only source.
            log.info("extract.tooling.rule_object_unavailable", type=tooling_object)
            return []
        try:
            # Tooling exposes the owning object as EntityDefinitionId (REST calls it
            # SobjectType): the API name for standard objects, the 01I id for custom.
            rows = await self._tq(
                f"SELECT Id, Name, EntityDefinitionId, Active FROM {tooling_object}"
            )
        except Exception as exc:
            log.warning("extract.tooling.rule_query_failed", type=tooling_object, error=str(exc))
            return []
        by_obj: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            obj = await self._object_name(r.get("EntityDefinitionId"))
            by_obj.setdefault(obj, []).append(
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

    # ---- surface categories ------------------------------------------------------

    async def _layouts(self) -> list[RawMetadataRecord]:
        rows = await self._with_metadata("Layout", "Id, Name, TableEnumOrId, NamespacePrefix")
        out = []
        for r in rows:
            meta: dict[str, Any] = r["Metadata"]
            obj = await self._object_name(r.get("TableEnumOrId"))
            name = f"{obj}-{r.get('Name', '')}"
            out.append(
                self._rec(
                    CategoryName.PAGE_LAYOUT,
                    name,
                    {
                        "path": f"layouts/{name}.layout-meta.xml",
                        "parsed": {"Layout": meta},
                        "object_from_path": obj,
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _metadata_surface(
        self, cat: CategoryName, sobject: str, root: str
    ) -> list[RawMetadataRecord]:
        """Tabs, apps, path assistants: per-record ``Metadata`` (the Metadata API path
        is cheaper for these; the composite client skips them here)."""
        try:
            rows = await self._with_metadata(sobject, "Id, DeveloperName, NamespacePrefix")
        except Exception as exc:
            log.warning("extract.tooling.surface_query_failed", type=sobject, error=str(exc)[:200])
            return []
        folder = {"CustomTab": "tabs", "CustomApplication": "applications"}.get(
            sobject, "pathAssistants"
        )
        ext = {"CustomTab": "tab", "CustomApplication": "app"}.get(sobject, "pathAssistant")
        return [
            self._rec(
                cat,
                str(r.get("DeveloperName", "")),
                {
                    "path": f"{folder}/{r.get('DeveloperName', '')}.{ext}-meta.xml",
                    "parsed": {root: r["Metadata"]},
                    "tooling_id": r.get("Id"),
                },
                namespace=r.get("NamespacePrefix"),
            )
            for r in rows
            if r.get("DeveloperName")
        ]

    async def _flexipages(self) -> list[RawMetadataRecord]:
        rows = await self._with_metadata("FlexiPage", "Id, DeveloperName, NamespacePrefix, Type")
        out = []
        for r in rows:
            meta: dict[str, Any] = r["Metadata"]
            meta.setdefault("type", r.get("Type", ""))
            name = str(r.get("DeveloperName", ""))
            out.append(
                self._rec(
                    CategoryName.FLEXIPAGE,
                    name,
                    {
                        "path": f"flexipages/{name}.flexipage-meta.xml",
                        "parsed": {"FlexiPage": meta},
                        "tooling_id": r.get("Id"),
                    },
                    namespace=r.get("NamespacePrefix"),
                )
            )
        return out

    async def _permissions(self) -> list[RawMetadataRecord]:
        """Permission sets + profiles composed from FieldPermissions / ObjectPermissions / SetupEntityAccess."""
        parents = await self._q(
            "SELECT Id, Name, Label, IsOwnedByProfile, Profile.Name, IsCustom FROM PermissionSet"
        )
        if not parents:
            return []
        bodies: dict[str, dict[str, Any]] = {}
        meta_by_id: dict[str, dict[str, Any]] = {}
        for p in parents:
            pid = str(p.get("Id"))
            meta_by_id[pid] = p
            bodies[pid] = {
                "label": p.get("Label", ""),
                "fieldPermissions": [],
                "objectPermissions": [],
                "classAccesses": [],
                "pageAccesses": [],
                "flowAccesses": [],
            }
        for fp in await self._q(
            "SELECT ParentId, Field, PermissionsRead, PermissionsEdit FROM FieldPermissions"
        ):
            b = bodies.get(str(fp.get("ParentId")))
            if b is not None:
                b["fieldPermissions"].append(
                    {
                        "field": fp.get("Field", ""),
                        "readable": bool(fp.get("PermissionsRead")),
                        "editable": bool(fp.get("PermissionsEdit")),
                    }
                )
        for op in await self._q(
            "SELECT ParentId, SobjectType, PermissionsRead, PermissionsCreate, PermissionsEdit, PermissionsDelete FROM ObjectPermissions"
        ):
            b = bodies.get(str(op.get("ParentId")))
            if b is not None:
                b["objectPermissions"].append(
                    {
                        "object": op.get("SobjectType", ""),
                        "allowRead": bool(op.get("PermissionsRead")),
                        "allowCreate": bool(op.get("PermissionsCreate")),
                        "allowEdit": bool(op.get("PermissionsEdit")),
                        "allowDelete": bool(op.get("PermissionsDelete")),
                    }
                )
        try:
            access = await self._q(
                "SELECT ParentId, SetupEntityId, SetupEntityType FROM SetupEntityAccess WHERE SetupEntityType IN ('ApexClass', 'ApexPage', 'FlowDefinition')"
            )
        except Exception as exc:
            log.warning("extract.tooling.setup_entity_access_failed", error=str(exc))
            access = []
        if access:
            names = await self._setup_entity_names(access)
            for a in access:
                b = bodies.get(str(a.get("ParentId")))
                nm = names.get(str(a.get("SetupEntityId")))
                if b is None or not nm:
                    continue
                t = str(a.get("SetupEntityType"))
                if t == "ApexClass":
                    b["classAccesses"].append({"apexClass": nm, "enabled": True})
                elif t == "ApexPage":
                    b["pageAccesses"].append({"apexPage": nm, "enabled": True})
                elif t == "FlowDefinition":
                    b["flowAccesses"].append({"flow": nm, "enabled": True})
        out = []
        for pid, body in bodies.items():
            p = meta_by_id[pid]
            if p.get("IsOwnedByProfile"):
                name = str((p.get("Profile") or {}).get("Name") or p.get("Label") or pid)
                body["custom"] = bool(p.get("IsCustom"))
                out.append(
                    self._rec(
                        CategoryName.PROFILE,
                        name,
                        {
                            "path": f"profiles/{name}.profile-meta.xml",
                            "parsed": {"Profile": body},
                            "tooling_id": pid,
                        },
                    )
                )
            else:
                name = str(p.get("Name") or pid)
                out.append(
                    self._rec(
                        CategoryName.PERMISSION_SET,
                        name,
                        {
                            "path": f"permissionsets/{name}.permissionset-meta.xml",
                            "parsed": {"PermissionSet": body},
                            "tooling_id": pid,
                        },
                    )
                )
        return out

    async def _setup_entity_names(self, access: list[dict[str, Any]]) -> dict[str, str]:
        names: dict[str, str] = {}
        by_type: dict[str, set[str]] = {}
        for a in access:
            by_type.setdefault(str(a.get("SetupEntityType")), set()).add(
                str(a.get("SetupEntityId"))
            )
        for t, ids in by_type.items():
            obj, col = {
                "ApexClass": ("ApexClass", "Name"),
                "ApexPage": ("ApexPage", "Name"),
                "FlowDefinition": ("FlowDefinition", "DeveloperName"),
            }[t]
            idl = list(ids)
            for i in range(0, len(idl), 200):
                chunk = ", ".join(f"'{x}'" for x in idl[i : i + 200])
                for r in await self._tq(f"SELECT Id, {col} FROM {obj} WHERE Id IN ({chunk})"):
                    names[str(r.get("Id"))] = str(r.get(col, ""))
        return names

    async def _reports(self, *, max_reports: int = 300) -> list[RawMetadataRecord]:
        """Report columns via the Analytics ``describe`` endpoint, capped (use the Metadata API path for all)."""
        rows = await self._q(
            "SELECT Id, DeveloperName, Name, FolderName FROM Report ORDER BY LastRunDate DESC NULLS LAST"
        )
        if len(rows) > max_reports:
            self.stats.partial_categories.append(CategoryName.REPORT.value)
            rows = rows[:max_reports]
        out = []
        for r in rows:
            try:
                desc = await self.gateway.sf_restful(f"analytics/reports/{r.get('Id')}/describe")
                self.stats.queries += 1
            except Exception as exc:
                log.warning(
                    "extract.tooling.report_describe_failed",
                    report=r.get("DeveloperName"),
                    error=str(exc),
                )
                continue
            rm = (desc or {}).get("reportMetadata") or {}
            body = {
                "name": r.get("Name", ""),
                "reportType": str((rm.get("reportType") or {}).get("type", "")),
                "format": rm.get("reportFormat", ""),
                "columns": [{"field": c} for c in rm.get("detailColumns", [])],
                "groupingsDown": [
                    {"field": g.get("name", "")}
                    for g in rm.get("groupingsDown", [])
                    if isinstance(g, dict)
                ],
                "filter": {
                    "criteriaItems": [
                        {"column": f.get("column", "")}
                        for f in rm.get("reportFilters", [])
                        if isinstance(f, dict)
                    ]
                },
            }
            folder = str(r.get("FolderName") or "unfiled$public").replace(" ", "_")
            name = f"{folder}/{r.get('DeveloperName', '')}"
            out.append(
                self._rec(
                    CategoryName.REPORT,
                    name,
                    {
                        "path": f"reports/{name}.report-meta.xml",
                        "parsed": {"Report": body},
                        "tooling_id": r.get("Id"),
                    },
                )
            )
        return out

    async def _q(self, soql: str) -> list[dict[str, Any]]:
        """Plain REST query (non-Tooling objects: PermissionSet, FieldPermissions, Report)."""
        self.stats.queries += 1
        resp = await self.gateway.sf_query(soql)
        return [r for r in resp.get("records", []) if isinstance(r, dict)]


_FLOW_CATEGORIES = {
    CategoryName.RECORD_TRIGGERED_FLOW,
    CategoryName.SCREEN_FLOW,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.AUTOLAUNCHED_FLOW,
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.PROCESS_BUILDER,
}


def _entity_from_change_event(name: str) -> str:
    """``AccountChangeEvent`` → ``Account``; ``Deal__ChangeEvent`` → ``Deal__c``."""
    if name.endswith("__ChangeEvent"):
        return name[: -len("__ChangeEvent")] + "__c"
    if name.endswith("ChangeEvent"):
        return name[: -len("ChangeEvent")]
    return name


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


_SCAN_CALL_ESTIMATE = (
    250  # Tooling + describe + FieldDefinition + data profile; surfaces via Metadata API
)


def api_budget(limits: Any, needed: int = _SCAN_CALL_ESTIMATE) -> tuple[int | None, bool]:
    """``(remaining, ok)`` from a ``/limits`` payload; ``(None, True)`` when it says nothing.

    Developer Edition allows 15,000 requests per rolling 24 h and a scan costs a few
    hundred; twelve scans in one day exhausted an org (pitfall 4). Check before pulling.
    """
    if not isinstance(limits, dict):
        return None, True
    row = limits.get("DailyApiRequests")
    if not isinstance(row, dict) or row.get("Remaining") is None:
        return None, True
    remaining = int(row["Remaining"])
    return remaining, remaining >= needed
