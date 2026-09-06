"""Schema extractor (C21): objects, fields, relationships, record types.

Two sources, one output (:class:`SchemaSnapshot`):

* :func:`from_source_tree` — ``objects/<Object>/`` files (fixtures, sf CLI,
  SFDX projects). Standard objects that only carry custom fields still get an
  object node so edges can land on them.
* :func:`from_describe` — REST ``describeGlobal`` + per-object ``describe``
  JSON (the Tooling/REST pull path). Richer for standard objects.
"""

from __future__ import annotations

from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import SchemaNode, SchemaNodeKind, SchemaSnapshot
from offramp.extract.categories.xml_utils import as_bool, as_list, as_str, parse_xml
from offramp.extract.pull.source_tree import ObjectFiles, SourceTree

log = get_logger(__name__)

_RELATIONSHIP_TYPES = {"Lookup", "MasterDetail", "Hierarchy", "MetadataRelationship"}


def from_source_tree(tree: SourceTree, *, org_alias: str) -> SchemaSnapshot:
    snap = SchemaSnapshot(org_alias=org_alias, source="source_tree")
    for of in tree.object_files():
        snap.nodes.extend(_object_nodes(of, org_alias))
    log.info("extract.schema.source_tree", objects=len(snap.objects()), fields=len(snap.fields()))
    return snap


def _object_nodes(of: ObjectFiles, org_alias: str) -> list[SchemaNode]:
    nodes: list[SchemaNode] = []
    obj_meta: dict[str, Any] = {}
    if of.object_xml:
        try:
            obj_meta = parse_xml(of.object_xml).get("CustomObject", {}) or {}
            if not isinstance(obj_meta, dict):
                obj_meta = {}
        except Exception as exc:  # malformed object file: still emit the node
            log.warning("extract.schema.object_parse_failed", object=of.name, error=str(exc))
    is_custom = of.name.endswith(("__c", "__e", "__mdt", "__b", "__x"))
    nodes.append(
        SchemaNode(
            org_alias=org_alias,
            kind=SchemaNodeKind.OBJECT,
            api_name=of.name,
            object_name=of.name,
            label=as_str(obj_meta.get("label"), of.name),
            custom=is_custom,
            raw={
                "sharing_model": as_str(obj_meta.get("sharingModel")),
                "deployment_status": as_str(obj_meta.get("deploymentStatus")),
                "event_type": as_str(obj_meta.get("eventType")),
                "enable_history": as_bool(obj_meta.get("enableHistory")),
                "inline_fields": len(as_list(obj_meta.get("fields"))),
            },
        )
    )
    # Fields inline in the object file (older format) + separate field files.
    inline = {
        as_str(f.get("fullName")): f for f in as_list(obj_meta.get("fields")) if isinstance(f, dict)
    }
    for name, xml in of.fields.items():
        try:
            body = parse_xml(xml).get("CustomField", {})
        except Exception as exc:
            log.warning(
                "extract.schema.field_parse_failed", object=of.name, field=name, error=str(exc)
            )
            continue
        if isinstance(body, dict):
            nodes.append(_field_node(of.name, name, body, org_alias))
    for name, body in inline.items():
        if name and name not in of.fields:
            nodes.append(_field_node(of.name, name, body, org_alias))
    for name, xml in of.record_types.items():
        try:
            body = parse_xml(xml).get("RecordType", {})
        except Exception:
            body = {}
        nodes.append(
            SchemaNode(
                org_alias=org_alias,
                kind=SchemaNodeKind.RECORD_TYPE,
                api_name=f"{of.name}.{name}",
                object_name=of.name,
                label=as_str(body.get("label"), name) if isinstance(body, dict) else name,
                raw={"active": as_bool(body.get("active"), True)} if isinstance(body, dict) else {},
            )
        )
    return nodes


def _field_node(obj: str, name: str, body: dict[str, Any], org_alias: str) -> SchemaNode:
    ftype = as_str(body.get("type"))
    reference_to = [as_str(r) for r in as_list(body.get("referenceTo")) if as_str(r)]
    picklist: list[str] = []
    vs = body.get("valueSet")
    if isinstance(vs, dict):
        vsd = vs.get("valueSetDefinition")
        if isinstance(vsd, dict):
            picklist = [
                as_str(v.get("fullName")) for v in as_list(vsd.get("value")) if isinstance(v, dict)
            ]
    return SchemaNode(
        org_alias=org_alias,
        kind=SchemaNodeKind.FIELD,
        api_name=f"{obj}.{name}",
        object_name=obj,
        label=as_str(body.get("label"), name),
        field_type=ftype or None,
        reference_to=reference_to,
        relationship_name=as_str(body.get("relationshipName")) or None,
        custom=name.endswith("__c"),
        picklist_values=picklist,
        formula=as_str(body.get("formula")) or None,
        required=as_bool(body.get("required")),
        raw={
            "external_id": as_bool(body.get("externalId")),
            "unique": as_bool(body.get("unique")),
            "length": as_str(body.get("length")),
            "track_history": as_bool(body.get("trackHistory")),
            "summary_operation": as_str(body.get("summaryOperation")),
        },
    )


def from_describe(
    global_describe: dict[str, Any],
    describes: dict[str, dict[str, Any]],
    *,
    org_alias: str,
) -> SchemaSnapshot:
    """Build a snapshot from REST describe payloads.

    ``global_describe`` is the ``/sobjects`` response; ``describes`` maps
    object API name → ``/sobjects/<Name>/describe`` response.
    """
    snap = SchemaSnapshot(org_alias=org_alias, source="describe")
    for so in global_describe.get("sobjects", []):
        name = as_str(so.get("name"))
        if not name:
            continue
        d = describes.get(name, {})
        snap.nodes.append(
            SchemaNode(
                org_alias=org_alias,
                kind=SchemaNodeKind.OBJECT,
                api_name=name,
                object_name=name,
                label=as_str(so.get("label"), name),
                custom=bool(so.get("custom")),
                raw={
                    "queryable": bool(so.get("queryable")),
                    "triggerable": bool(so.get("triggerable")),
                    "record_type_count": len(d.get("recordTypeInfos", [])),
                },
            )
        )
        for f in d.get("fields", []):
            fname = as_str(f.get("name"))
            if not fname:
                continue
            snap.nodes.append(
                SchemaNode(
                    org_alias=org_alias,
                    kind=SchemaNodeKind.FIELD,
                    api_name=f"{name}.{fname}",
                    object_name=name,
                    label=as_str(f.get("label"), fname),
                    field_type=_describe_type(f),
                    reference_to=[as_str(r) for r in f.get("referenceTo", [])],
                    relationship_name=as_str(f.get("relationshipName")) or None,
                    custom=bool(f.get("custom")),
                    picklist_values=[
                        as_str(p.get("value"))
                        for p in f.get("picklistValues", [])
                        if isinstance(p, dict)
                    ],
                    formula=as_str(f.get("calculatedFormula")) or None,
                    required=not bool(f.get("nillable", True))
                    and not bool(f.get("defaultedOnCreate", False)),
                    raw={
                        "external_id": bool(f.get("externalId")),
                        "unique": bool(f.get("unique")),
                        "calculated": bool(f.get("calculated")),
                        "updateable": bool(f.get("updateable")),
                    },
                )
            )
        for rt in d.get("recordTypeInfos", []):
            dev = as_str(rt.get("developerName"))
            if dev and dev != "Master":
                snap.nodes.append(
                    SchemaNode(
                        org_alias=org_alias,
                        kind=SchemaNodeKind.RECORD_TYPE,
                        api_name=f"{name}.{dev}",
                        object_name=name,
                        label=as_str(rt.get("name"), dev),
                        raw={"active": bool(rt.get("active", True))},
                    )
                )
    log.info("extract.schema.describe", objects=len(snap.objects()), fields=len(snap.fields()))
    return snap


def _describe_type(f: dict[str, Any]) -> str:
    t = as_str(f.get("type"))
    if t == "reference":
        return "MasterDetail" if f.get("cascadeDelete") else "Lookup"
    if f.get("calculated"):
        return (
            "Formula" if not as_str(f.get("calculatedFormula")).startswith("ROLLUP") else "Summary"
        )
    return {
        "string": "Text",
        "textarea": "TextArea",
        "double": "Number",
        "currency": "Currency",
        "percent": "Percent",
        "boolean": "Checkbox",
        "date": "Date",
        "datetime": "DateTime",
        "picklist": "Picklist",
        "multipicklist": "MultiselectPicklist",
        "email": "Email",
        "phone": "Phone",
        "url": "Url",
        "int": "Number",
        "id": "Id",
    }.get(t, t)


def merge(primary: SchemaSnapshot, secondary: SchemaSnapshot) -> SchemaSnapshot:
    """Union two snapshots; ``primary`` wins on conflicts."""
    out = SchemaSnapshot(org_alias=primary.org_alias, source=f"{primary.source}+{secondary.source}")
    seen: set[str] = set()
    for n in [*primary.nodes, *secondary.nodes]:
        if n.api_name in seen:
            continue
        seen.add(n.api_name)
        out.nodes.append(n)
    return out
