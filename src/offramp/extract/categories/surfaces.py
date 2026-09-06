"""Surface extractors: page layouts, Lightning pages, permission sets, profiles, reports.

None of these fire on save; they exist so "where is this used" and the
unused-field verdict account for the UI, security, and reporting references
that admins check before deleting anything. Each emits the same ``references``
block the automation extractors do, plus a ``surface`` marker.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_bool, as_list, as_str, get_body
from offramp.extract.pull.reconciler import ReconciledRecord

# Lightning App Builder writes the Flow component under either name.
FLOW_COMPONENTS = frozenset({"flowruntime:flowRuntime", "flowruntime:flowRuntimeForFlexipage"})

_RECORD_FIELD = re.compile(r"^Record\.([A-Za-z_][A-Za-z0-9_.]*)$")
_REPORT_TYPE_SUFFIXES = ("List", "Report")


@register
class PageLayoutExtractor(CategoryExtractor):
    """``layouts/<Object>-<Name>.layout-meta.xml``: fields, related lists, actions."""

    category: ClassVar[CategoryName] = CategoryName.PAGE_LAYOUT

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "Layout")
        sobject = as_str(record.payload.get("object_from_path")) or record.api_name.split("-", 1)[0]
        fields: list[str] = []
        sections = []
        for sec in as_list(body.get("layoutSections")):
            if not isinstance(sec, dict):
                continue
            sec_fields = []
            for col in as_list(sec.get("layoutColumns")):
                if not isinstance(col, dict):
                    continue
                for item in as_list(col.get("layoutItems")):
                    if isinstance(item, dict) and as_str(item.get("field")):
                        sec_fields.append(as_str(item.get("field")))
            fields.extend(sec_fields)
            sections.append({"label": as_str(sec.get("label")), "fields": sec_fields})
        related = [
            {
                "name": as_str(rl.get("relatedList")),
                "fields": [as_str(f) for f in as_list(rl.get("fields"))],
            }
            for rl in as_list(body.get("relatedLists"))
            if isinstance(rl, dict)
        ]
        qa_raw = body.get("quickActionList")
        qa: dict[str, Any] = qa_raw if isinstance(qa_raw, dict) else {}
        quick_actions = [
            as_str(q.get("quickActionName"))
            for q in as_list(qa.get("quickActionListItems"))
            if isinstance(q, dict)
        ]
        buttons = [as_str(b) for b in as_list(body.get("customButtons"))]
        qualified = sorted({f"{sobject}.{f}" for f in fields if f})
        return {
            "object": sobject,
            "surface": "ui",
            "sections": sections,
            "related_lists": related,
            "quick_actions": [q for q in quick_actions if q],
            "custom_buttons": [b for b in buttons if b],
            "field_count": len(qualified),
            "references": {
                "objects": [sobject],
                "fields": qualified,
                "quick_actions": [q for q in quick_actions if q],
            },
        }


@register
class FlexiPageExtractor(CategoryExtractor):
    """``flexipages/<Name>.flexipage-meta.xml``: components, fields, embedded flows."""

    category: ClassVar[CategoryName] = CategoryName.FLEXIPAGE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "FlexiPage")
        sobject = as_str(body.get("sobjectType"))
        page_type = as_str(body.get("type"))
        components: list[dict[str, Any]] = []
        fields: set[str] = set()
        flows: set[str] = set()
        lwc: set[str] = set()
        vf_pages: set[str] = set()
        for region in as_list(body.get("flexiPageRegions")):
            if not isinstance(region, dict):
                continue
            for inst in as_list(region.get("itemInstances")):
                if not isinstance(inst, dict):
                    continue
                fi = inst.get("fieldInstance")
                if isinstance(fi, dict):
                    item = as_str(fi.get("fieldItem"))
                    m = _RECORD_FIELD.match(item)
                    if m and sobject:
                        fields.add(f"{sobject}.{m.group(1)}")
                    continue
                ci = inst.get("componentInstance")
                if not isinstance(ci, dict):
                    continue
                name = as_str(ci.get("componentName"))
                props = {
                    as_str(p.get("name")): as_str(p.get("value"))
                    for p in as_list(ci.get("componentInstanceProperties"))
                    if isinstance(p, dict)
                }
                components.append(
                    {"name": name, "region": as_str(region.get("name")), "properties": props}
                )
                if name in FLOW_COMPONENTS and props.get("flowName"):
                    flows.add(props["flowName"])
                elif name == "flexipage:visualforcePage" and props.get("pageName"):
                    vf_pages.add(props["pageName"])
                elif name.startswith("c:"):
                    lwc.add(name[2:])
                elif name and ":" not in name:
                    # Salesforce writes custom LWC bundles without the ``c:`` prefix;
                    # every standard component carries a namespace prefix.
                    lwc.add(name)
        return {
            "object": sobject,
            "surface": "ui",
            "label": as_str(body.get("masterLabel"), record.api_name),
            "page_type": page_type,
            "components": components,
            "references": {
                "objects": [sobject] if sobject else [],
                "fields": sorted(fields),
                "flows": sorted(flows),
                "lwc_bundles": sorted(lwc),
                "visualforce_pages": sorted(vf_pages),
            },
        }


def _permission_body(body: dict[str, Any], *, api_name: str, surface: str) -> dict[str, Any]:
    field_perms: list[dict[str, Any]] = [
        {
            "field": as_str(fp.get("field")),
            "readable": as_bool(fp.get("readable")),
            "editable": as_bool(fp.get("editable")),
        }
        for fp in as_list(body.get("fieldPermissions"))
        if isinstance(fp, dict)
    ]
    object_perms = [
        {
            "object": as_str(op.get("object")),
            "read": as_bool(op.get("allowRead")),
            "create": as_bool(op.get("allowCreate")),
            "edit": as_bool(op.get("allowEdit")),
            "delete": as_bool(op.get("allowDelete")),
        }
        for op in as_list(body.get("objectPermissions"))
        if isinstance(op, dict)
    ]
    classes = [
        as_str(c.get("apexClass"))
        for c in as_list(body.get("classAccesses"))
        if isinstance(c, dict) and as_bool(c.get("enabled"))
    ]
    pages = [
        as_str(p.get("apexPage"))
        for p in as_list(body.get("pageAccesses"))
        if isinstance(p, dict) and as_bool(p.get("enabled"))
    ]
    flows = [
        as_str(f.get("flow"))
        for f in as_list(body.get("flowAccesses"))
        if isinstance(f, dict) and as_bool(f.get("enabled"))
    ]
    record_types = [
        as_str(r.get("recordType"))
        for r in as_list(body.get("recordTypeVisibilities"))
        if isinstance(r, dict) and as_bool(r.get("visible"))
    ]
    readable = sorted(
        {fp["field"] for fp in field_perms if fp["field"] and (fp["readable"] or fp["editable"])}
    )
    editable = sorted({fp["field"] for fp in field_perms if fp["field"] and fp["editable"]})
    return {
        "surface": surface,
        "label": as_str(body.get("label"), api_name),
        "description": as_str(body.get("description")),
        "field_permissions": field_perms,
        "object_permissions": object_perms,
        "class_accesses": [c for c in classes if c],
        "page_accesses": [p for p in pages if p],
        "flow_accesses": [f for f in flows if f],
        "record_type_visibilities": [r for r in record_types if r],
        "references": {
            "objects": sorted({op["object"] for op in object_perms if op["object"] and op["read"]}),
            "fields": readable,
            "fields_editable": editable,
            "apex_classes": sorted({c for c in classes if c}),
            "flows": sorted({f for f in flows if f}),
            "visualforce_pages": sorted({p for p in pages if p}),
            "record_types": sorted({r for r in record_types if r}),
        },
    }


@register
class PermissionSetExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.PERMISSION_SET

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "PermissionSet")
        return _permission_body(body, api_name=record.api_name, surface="security")


@register
class ProfileExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.PROFILE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "Profile")
        out = _permission_body(body, api_name=record.api_name, surface="security")
        out["custom"] = as_bool(body.get("custom"))
        return out


def report_object(report_type: str) -> str:
    """Best-effort sObject for a report type: 'Opportunity', 'AccountList' → Account."""
    rt = report_type
    for suffix in _REPORT_TYPE_SUFFIXES:
        if rt.endswith(suffix) and len(rt) > len(suffix):
            rt = rt[: -len(suffix)]
    return rt


_REPORT_COLUMN_ALIASES = {
    "FULL_NAME": "Name",
    "NAME": "Name",
    "LAST_UPDATE": "LastModifiedDate",
    "LAST_UPDATE_BY": "LastModifiedById",
    "CREATED": "CreatedById",
    "OWNER": "OwnerId",
    "OWNER_FULL_NAME": "OwnerId",
    "ACCOUNT_ID": "AccountId",
    "CONTACT_ID": "ContactId",
}


def _report_column_to_field(column: str) -> str:
    """Standard report columns are UPPER_SNAKE (``LAST_NAME``, ``CREATED_DATE``); custom
    fields already carry their API name (``Score__c``)."""
    if column in _REPORT_COLUMN_ALIASES:
        return _REPORT_COLUMN_ALIASES[column]
    if column.isupper() and not column.endswith("__C"):
        return "".join(part.capitalize() for part in column.split("_") if part)
    return column


def _report_field(column: str, default_object: str) -> str:
    """Report columns come as 'LEAD.NAME', 'LAST_NAME' or 'Lead.Score__c'; return 'Object.Field'."""
    if "." in column:
        obj, _, name = column.partition(".")
        # Upper-case prefixes ('LEAD.NAME') are report-type aliases, not object API names.
        obj = default_object if obj.isupper() and default_object else obj
        return f"{obj}.{_report_column_to_field(name)}"
    field = _report_column_to_field(column)
    return f"{default_object}.{field}" if default_object else field


@register
class ReportExtractor(CategoryExtractor):
    """``reports/<Folder>/<Name>.report-meta.xml``: columns, groupings, filters."""

    category: ClassVar[CategoryName] = CategoryName.REPORT

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "Report")
        report_type = as_str(body.get("reportType"))
        sobject = report_object(report_type)
        columns = [
            as_str(c.get("field")) for c in as_list(body.get("columns")) if isinstance(c, dict)
        ]
        groupings = [
            as_str(g.get("field"))
            for key in ("groupingsDown", "groupingsAcross")
            for g in as_list(body.get(key))
            if isinstance(g, dict)
        ]
        flt_raw = body.get("filter")
        flt: dict[str, Any] = flt_raw if isinstance(flt_raw, dict) else {}
        filters = [
            as_str(c.get("column"))
            for c in as_list(flt.get("criteriaItems"))
            if isinstance(c, dict)
        ]
        fields = sorted({_report_field(f, sobject) for f in [*columns, *groupings, *filters] if f})
        folder = record.api_name.rsplit("/", 1)[0] if "/" in record.api_name else ""
        return {
            "object": sobject,
            "surface": "reporting",
            "name": as_str(body.get("name"), record.api_name.rsplit("/", 1)[-1]),
            "folder": folder,
            "report_type": report_type,
            "format": as_str(body.get("format")),
            "columns": [c for c in columns if c],
            "groupings": [g for g in groupings if g],
            "filters": [f for f in filters if f],
            "references": {"objects": [sobject] if sobject else [], "fields": fields},
        }


@register
class CustomTabExtractor(CategoryExtractor):
    """``tabs/<Name>.tab-meta.xml``: an object tab, a Lightning page tab, a component tab."""

    category: ClassVar[CategoryName] = CategoryName.CUSTOM_TAB

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "CustomTab")
        objects: list[str] = []
        if as_bool(body.get("customObject")):
            objects.append(record.api_name)  # the tab is named after the object
        flexipage = as_str(body.get("flexiPage"))
        components = [
            n for n in (as_str(body.get("lwcComponent")), as_str(body.get("auraComponent"))) if n
        ]
        return {
            "surface": "ui",
            "label": as_str(body.get("label"), record.api_name),
            "tab_kind": (
                "object"
                if objects
                else "flexipage"
                if flexipage
                else "component"
                if components
                else "web"
            ),
            "references": {
                "objects": objects,
                "flexipages": [flexipage] if flexipage else [],
                "lwc_bundles": [c.split(":", 1)[-1] for c in components],
                "visualforce_pages": [as_str(body.get("page"))] if as_str(body.get("page")) else [],
            },
        }


@register
class CustomApplicationExtractor(CategoryExtractor):
    """``applications/<Name>.app-meta.xml``: tabs, record-page overrides, utility bar."""

    category: ClassVar[CategoryName] = CategoryName.CUSTOM_APPLICATION

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "CustomApplication")
        tabs = [as_str(t) for t in as_list(body.get("tabs")) if as_str(t)]
        flexipages: set[str] = set()
        objects: set[str] = set()
        overrides = []
        for ov in as_list(body.get("actionOverrides")) + as_list(
            body.get("profileActionOverrides")
        ):
            if not isinstance(ov, dict):
                continue
            content = as_str(ov.get("content"))
            obj = as_str(ov.get("pageOrSobjectType"))
            if as_str(ov.get("type")).lower() == "flexipage" and content:
                flexipages.add(content)
            if obj and obj[0].isupper():
                objects.add(obj)
            overrides.append(
                {
                    "action": as_str(ov.get("actionName")),
                    "object": obj,
                    "content": content,
                    "type": as_str(ov.get("type")),
                    "profile": as_str(ov.get("profile")),
                }
            )
        utility = as_str(body.get("utilityBar"))
        if utility:
            flexipages.add(utility)
        # Standard tabs are 'standard-Lead'; custom ones are the tab api name.
        for t in tabs:
            if t.startswith("standard-"):
                objects.add(t[len("standard-") :])
        return {
            "surface": "ui",
            "label": as_str(body.get("label"), record.api_name),
            "nav_type": as_str(body.get("navType")),
            "overrides": overrides,
            "references": {
                "objects": sorted(objects),
                "tabs": [t for t in tabs if not t.startswith("standard-")],
                "flexipages": sorted(flexipages),
            },
        }


@register
class PathAssistantExtractor(CategoryExtractor):
    """``pathAssistants/<Name>.pathAssistant-meta.xml``: the picklist a path is built on."""

    category: ClassVar[CategoryName] = CategoryName.PATH_ASSISTANT

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "PathAssistant")
        obj = as_str(body.get("entityName"))
        field = as_str(body.get("fieldName"))
        fields: set[str] = set()
        if obj and field:
            fields.add(f"{obj}.{field}")
        steps = []
        for st in as_list(body.get("pathAssistantSteps")):
            if not isinstance(st, dict):
                continue
            key_fields = [as_str(f) for f in as_list(st.get("fieldNames")) if as_str(f)]
            fields.update(f"{obj}.{f}" for f in key_fields if obj)
            steps.append({"value": as_str(st.get("picklistValueName")), "fields": key_fields})
        rt = as_str(body.get("recordTypeName"))
        return {
            "surface": "ui",
            "object": obj,
            "label": as_str(body.get("masterLabel"), record.api_name),
            "active": as_bool(body.get("active")),
            "picklist_field": f"{obj}.{field}" if obj and field else "",
            "record_type": "" if rt == "__MASTER__" else rt,
            "steps": steps,
            "references": {"objects": [obj] if obj else [], "fields": sorted(fields)},
        }
