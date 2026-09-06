"""Flow extractor (all 7 Flow variants, full element coverage).

Salesforce stores every Flow variant in the same ``Flow`` schema;
``processType`` + ``start.triggerType`` discriminate. This extractor keeps
the keys the Tier 1 translators already consume (``decisions``,
``record_updates``, ``record_creates``, ``action_calls``, ``subflows``,
``screens``, ``object``, ``trigger_type``) and adds every other element type
plus a derived ``references`` block for the dependency graph.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_bool, as_list, as_str, get_body
from offramp.extract.pull.reconciler import ReconciledRecord
from offramp.generate.formula.references import extract_references

_ELEMENT_TYPES = (
    "actionCalls",
    "apexPluginCalls",
    "assignments",
    "collectionProcessors",
    "customErrors",
    "decisions",
    "loops",
    "orchestratedStages",
    "recordCreates",
    "recordDeletes",
    "recordLookups",
    "recordRollbacks",
    "recordUpdates",
    "screens",
    "steps",
    "subflows",
    "transforms",
    "waits",
)
_RESOURCE_TYPES = (
    "formulas",
    "variables",
    "constants",
    "textTemplates",
    "choices",
    "dynamicChoiceSets",
)
_RECORD_REF = re.compile(
    r"\{!\$Record(?:__Prior)?\.([A-Za-z_][A-Za-z0-9_.]*)\}|\$Record(?:__Prior)?\.([A-Za-z_][A-Za-z0-9_.]*)"
)
_MERGE_FIELD = re.compile(r"\{!([^}]+)\}")
_GLOBAL_REF = re.compile(
    r"\$(?:User|Profile|Organization|Setup|Label|Permission|UserRole|Api|System|CustomMetadata)\.[A-Za-z_][A-Za-z0-9_.]*"
)


def _value(v: Any) -> Any:
    """Flatten ``{stringValue: X}`` / ``{elementReference: Y}`` value wrappers."""
    if isinstance(v, dict):
        for kind in (
            "stringValue",
            "numberValue",
            "booleanValue",
            "dateValue",
            "dateTimeValue",
            "apexValue",
            "sobjectValue",
        ):
            if kind in v:
                return v[kind]
        if "elementReference" in v:
            return {"ref": v["elementReference"]}
        return v
    return v


def _connector(c: Any) -> str | None:
    if isinstance(c, dict):
        t = c.get("targetReference")
        return str(t) if t else None
    return None


def _conditions(rule: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "left": as_str(c.get("leftValueReference")),
            "operator": as_str(c.get("operator")),
            "right": _value(c.get("rightValue")),
        }
        for c in as_list(rule.get("conditions"))
        if isinstance(c, dict)
    ]


def _filters(el: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "field": as_str(f.get("field")),
            "operator": as_str(f.get("operator")),
            "value": _value(f.get("value")),
        }
        for f in as_list(el.get("filters"))
        if isinstance(f, dict)
    ]


def _assignments(items: Any, key: str = "field") -> list[dict[str, Any]]:
    return [
        {
            "field": as_str(a.get(key)),
            "value": _value(a.get("value")),
            "operator": as_str(a.get("operator")),
        }
        for a in as_list(items)
        if isinstance(a, dict)
    ]


def _base(el: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": as_str(el.get("name")),
        "label": as_str(el.get("label")),
        "next": _connector(el.get("connector")),
        "fault": _connector(el.get("faultConnector")),
    }


def _element(kind: str, el: dict[str, Any]) -> dict[str, Any]:
    out = _base(el, kind)
    if kind == "decisions":
        out["default_next"] = _connector(el.get("defaultConnector"))
        out["rules"] = [
            {
                "name": as_str(r.get("name")),
                "label": as_str(r.get("label")),
                "logic": as_str(r.get("conditionLogic"), "and"),
                "conditions": _conditions(r),
                "next": _connector(r.get("connector")),
            }
            for r in as_list(el.get("rules"))
            if isinstance(r, dict)
        ]
    elif kind == "assignments":
        out["items"] = [
            {
                "assign_to": as_str(a.get("assignToReference")),
                "operator": as_str(a.get("operator")),
                "value": _value(a.get("value")),
            }
            for a in as_list(el.get("assignmentItems"))
            if isinstance(a, dict)
        ]
    elif kind == "loops":
        out["collection"] = as_str(el.get("collectionReference"))
        out["next"] = _connector(el.get("nextValueConnector"))
        out["after"] = _connector(el.get("noMoreValuesConnector"))
    elif kind in {"recordLookups", "recordCreates", "recordUpdates", "recordDeletes"}:
        out["object"] = as_str(el.get("object"))
        out["input_reference"] = as_str(el.get("inputReference"))
        out["filter_logic"] = as_str(el.get("filterLogic"))
        out["filters"] = _filters(el)
        out["input_assignments"] = _assignments(el.get("inputAssignments"))
        out["queried_fields"] = [as_str(f) for f in as_list(el.get("queriedFields"))]
        out["output_assignments"] = _assignments(el.get("outputAssignments"))
        out["get_first_record_only"] = as_bool(el.get("getFirstRecordOnly"))
        out["store_output_automatically"] = as_bool(el.get("storeOutputAutomatically"))
    elif kind == "actionCalls":
        out["action_name"] = as_str(el.get("actionName"))
        out["action_type"] = as_str(el.get("actionType"))
        out["inputs"] = _assignments(el.get("inputParameters"), key="name")
        out["flow_transaction_model"] = as_str(el.get("flowTransactionModel"))
    elif kind == "apexPluginCalls":
        out["apex_class"] = as_str(el.get("apexClass"))
    elif kind == "subflows":
        out["flow_name"] = as_str(el.get("flowName"))
        out["inputs"] = _assignments(el.get("inputAssignments"), key="name")
    elif kind == "screens":
        out["fields"] = [
            {
                "name": as_str(f.get("name")),
                "type": as_str(f.get("fieldType")),
                "extension": as_str(f.get("extensionName")),
                "object_field": as_str(f.get("objectFieldReference")),
            }
            for f in as_list(el.get("fields"))
            if isinstance(f, dict)
        ]
    elif kind == "waits":
        out["events"] = [
            {
                "name": as_str(e.get("name")),
                "type": as_str(e.get("eventType")),
                "next": _connector(e.get("connector")),
            }
            for e in as_list(el.get("waitEvents"))
            if isinstance(e, dict)
        ]
        out["default_next"] = _connector(el.get("defaultConnector"))
    elif kind == "collectionProcessors":
        out["collection"] = as_str(el.get("collectionReference"))
        out["processor_type"] = as_str(el.get("collectionProcessorType"))
        out["formula"] = as_str(el.get("formula"))
    elif kind == "customErrors":
        out["messages"] = [
            as_str(m.get("errorMessage"))
            for m in as_list(el.get("customErrorMessages"))
            if isinstance(m, dict)
        ]
    elif kind == "transforms":
        out["object"] = as_str(el.get("objectType"))
    elif kind == "orchestratedStages":
        out["steps"] = [
            as_str(s.get("name")) for s in as_list(el.get("stageSteps")) if isinstance(s, dict)
        ]
    return out


def _start(body: dict[str, Any]) -> dict[str, Any]:
    start = body.get("start")
    if not isinstance(start, dict):
        return {}
    sched = start.get("schedule") if isinstance(start.get("schedule"), dict) else {}
    return {
        "object": as_str(start.get("object")),
        "trigger_type": as_str(start.get("triggerType")),
        "record_trigger_type": as_str(start.get("recordTriggerType")),
        "filter_logic": as_str(start.get("filterLogic")),
        "filters": _filters(start),
        "entry_formula": as_str(start.get("filterFormula")),
        "requires_record_changed": as_bool(start.get("doesRequireRecordChangedToMeetCriteria")),
        "next": _connector(start.get("connector")),
        "schedule": {
            "frequency": as_str(sched.get("frequency")),
            "start_date": as_str(sched.get("startDate")),
            "start_time": as_str(sched.get("startTime")),
        }
        if sched
        else {},
        "scheduled_paths": [
            {
                "name": as_str(p.get("name")),
                "offset": as_str(p.get("offsetNumber")),
                "unit": as_str(p.get("offsetUnit")),
                "next": _connector(p.get("connector")),
            }
            for p in as_list(start.get("scheduledPaths"))
            if isinstance(p, dict)
        ],
        "platform_event": as_str(start.get("object"))
        if as_str(start.get("triggerType")) == "PlatformEvent"
        else "",
    }


def parse_flow(body: dict[str, Any], *, api_name: str) -> dict[str, Any]:
    """Normalize one Flow body (XML-as-dict or Tooling JSON) into the canonical shape."""
    start = _start(body)
    elements: list[dict[str, Any]] = []
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for kind in _ELEMENT_TYPES:
        items = [_element(kind, e) for e in as_list(body.get(kind)) if isinstance(e, dict)]
        by_kind[kind] = items
        elements.extend(items)
    resources: dict[str, list[dict[str, Any]]] = {}
    for kind in _RESOURCE_TYPES:
        resources[kind] = [
            {
                "name": as_str(r.get("name")),
                "data_type": as_str(r.get("dataType")),
                "object_type": as_str(r.get("objectType")),
                "expression": as_str(r.get("expression")),
                "text": as_str(r.get("text")),
                "is_collection": as_bool(r.get("isCollection")),
                "is_input": as_bool(r.get("isInput")),
                "is_output": as_bool(r.get("isOutput")),
            }
            for r in as_list(body.get(kind))
            if isinstance(r, dict)
        ]

    process_type = as_str(body.get("processType"))
    out: dict[str, Any] = {
        "api_version": as_str(body.get("apiVersion"), "66.0"),
        "label": as_str(body.get("label"), api_name),
        "process_type": process_type,
        "trigger_type": start.get("trigger_type", ""),
        "record_trigger_type": start.get("record_trigger_type", ""),
        "object": start.get("object", ""),
        "status": as_str(body.get("status"), "Active"),
        "run_in_mode": as_str(body.get("runInMode")),
        "trigger_order": as_str(body.get("triggerOrder")),
        "start": start,
        "elements": elements,
        "element_counts": {k: len(v) for k, v in by_kind.items() if v},
        "resources": resources,
        # Back-compat keys consumed by the Tier 1 translators / complexity scorer.
        "decisions": by_kind["decisions"],
        "record_lookups": by_kind["recordLookups"],
        "record_creates": [
            {"name": e["name"], "object": e["object"], "input_assignments": e["input_assignments"]}
            for e in by_kind["recordCreates"]
        ],
        "record_updates": [
            {
                "name": e["name"],
                "input_reference": e["input_reference"],
                "input_assignments": e["input_assignments"],
                "filter_logic": e["filter_logic"],
                "filters": e["filters"],
                "object": e["object"],
            }
            for e in by_kind["recordUpdates"]
        ],
        "action_calls": [
            {"name": e["name"], "action_name": e["action_name"], "type": e["action_type"]}
            for e in by_kind["actionCalls"]
        ],
        "subflows": by_kind["subflows"],
        "screens": by_kind["screens"],
        "raw_root_keys": sorted(str(k) for k in body),
    }
    out["references"] = _references(out)
    return out


def _references(flow: dict[str, Any]) -> dict[str, list[str]]:
    host = flow["object"]
    objects: set[str] = set()
    fields: set[str] = set()
    written: set[str] = set()
    apex: set[str] = set()
    flows: set[str] = set()
    email_alerts: set[str] = set()
    actions: set[str] = set()
    globals_: set[str] = set()
    platform_events: set[str] = set()

    if host:
        objects.add(host)
        if flow["start"].get("platform_event"):
            platform_events.add(host)
    for f in flow["start"].get("filters", []):
        if host and f.get("field"):
            fields.add(f"{host}.{f['field']}")
    _scan_text(flow["start"].get("entry_formula", ""), host, fields, globals_)

    # Variables typed as sObjects
    var_types: dict[str, str] = {}
    for v in flow["resources"].get("variables", []):
        if v["object_type"]:
            objects.add(v["object_type"])
            var_types[v["name"]] = v["object_type"]
    for fm in flow["resources"].get("formulas", []):
        _scan_text(fm["expression"], host, fields, globals_)
    for tt in flow["resources"].get("textTemplates", []):
        _scan_text(tt["text"], host, fields, globals_)

    for el in flow["elements"]:
        kind = el["kind"]
        obj = el.get("object", "")
        if obj:
            objects.add(obj)
        if kind in {"recordLookups", "recordCreates", "recordUpdates", "recordDeletes"}:
            target = obj
            if not target and el.get("input_reference"):
                ref = el["input_reference"]
                target = host if ref.startswith("$Record") else var_types.get(ref.split(".")[0], "")
                if target:
                    objects.add(target)
                    el["resolved_object"] = target
            for f in el.get("filters", []):
                if target and f.get("field"):
                    fields.add(f"{target}.{f['field']}")
                _scan_value(f.get("value"), host, fields, globals_)
            for a in el.get("input_assignments", []):
                if target and a.get("field"):
                    fields.add(f"{target}.{a['field']}")
                    if kind in {"recordCreates", "recordUpdates"}:
                        written.add(f"{target}.{a['field']}")
                _scan_value(a.get("value"), host, fields, globals_)
            if kind == "recordDeletes" and target:
                written.add(f"{target}.*")
            for qf in el.get("queried_fields", []):
                if target and qf:
                    fields.add(f"{target}.{qf}")
        elif kind == "decisions":
            for r in el.get("rules", []):
                for c in r.get("conditions", []):
                    _scan_text(c.get("left", ""), host, fields, globals_)
                    _scan_value(c.get("right"), host, fields, globals_)
        elif kind == "assignments":
            for it in el.get("items", []):
                _scan_text(it.get("assign_to", ""), host, fields, globals_)
                _scan_value(it.get("value"), host, fields, globals_)
        elif kind == "actionCalls":
            at = el.get("action_type", "")
            an = el.get("action_name", "")
            if at == "apex" and an:
                apex.add(an)
            elif at == "flow" and an:
                flows.add(an)
            elif at == "emailAlert" and an:
                email_alerts.add(an)
            elif an:
                actions.add(f"{at}:{an}" if at else an)
            for i in el.get("inputs", []):
                _scan_value(i.get("value"), host, fields, globals_)
        elif kind == "apexPluginCalls":
            if el.get("apex_class"):
                apex.add(el["apex_class"])
        elif kind == "subflows":
            if el.get("flow_name"):
                flows.add(el["flow_name"])
            for i in el.get("inputs", []):
                _scan_value(i.get("value"), host, fields, globals_)
        elif kind == "screens":
            for f in el.get("fields", []):
                of = f.get("object_field", "")
                if of and "." in of:
                    var, path = of.split(".", 1)
                    typ = var_types.get(var) or (host if var == "$Record" else "")
                    if typ:
                        fields.add(f"{typ}.{path}")
                elif of:
                    _scan_text(of, host, fields, globals_)
        elif kind == "collectionProcessors":
            _scan_text(el.get("formula", ""), host, fields, globals_)

    return {
        "objects": sorted(objects, key=str.lower),
        "fields": sorted(fields, key=str.lower),
        "fields_written": sorted(written, key=str.lower),
        "apex_classes": sorted(apex, key=str.lower),
        "flows": sorted(flows, key=str.lower),
        "email_alerts": sorted(email_alerts, key=str.lower),
        "invocable_actions": sorted(actions, key=str.lower),
        "globals": sorted(globals_),
        "platform_events": sorted(platform_events),
    }


def _scan_value(v: Any, host: str, fields: set[str], globals_: set[str]) -> None:
    if isinstance(v, dict) and "ref" in v:
        _scan_text(str(v["ref"]), host, fields, globals_)
    elif isinstance(v, str):
        _scan_text(v, host, fields, globals_)


def _scan_text(text: str, host: str, fields: set[str], globals_: set[str]) -> None:
    if not text:
        return
    for m in _RECORD_REF.finditer(text):
        path = m.group(1) or m.group(2)
        if host and path:
            fields.add(f"{host}.{path}")
    for g in _GLOBAL_REF.findall(text):
        globals_.add(g)
    # Formula-style expressions (no braces) still reference fields via $Record above;
    # merge fields like {!varName.Field} are element refs, not schema refs.
    if (
        "{!" not in text
        and "$Record" not in text
        and text.strip()
        and any(op in text for op in ("(", "=", "<", ">", "&&", "||"))
    ):
        refs = extract_references(text)
        for g in refs.globals:
            if g.startswith("$Record"):
                if host:
                    fields.add(f"{host}.{g.split('.', 1)[1]}") if "." in g else None
            else:
                globals_.add(g)


class _FlowVariantBase(CategoryExtractor):
    """Shared parser; subclasses bind a specific :attr:`category`."""

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "Flow")
        return parse_flow(body, api_name=record.api_name)


@register
class RecordTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.RECORD_TRIGGERED_FLOW


@register
class ScreenFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.SCREEN_FLOW


@register
class ScheduleTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.SCHEDULE_TRIGGERED_FLOW


@register
class PlatformEventTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW


@register
class AutolaunchedFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.AUTOLAUNCHED_FLOW


@register
class FlowOrchestrationExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.FLOW_ORCHESTRATION


@register
class ProcessBuilderExtractor(_FlowVariantBase):
    """Process Builder is stored as a Flow with processType=Workflow."""

    category: ClassVar[CategoryName] = CategoryName.PROCESS_BUILDER
