"""Workflow Rule extractor.

Parses ``workflows/<Object>.workflow-meta.xml``. Each XML contains multiple
``<rules>`` blocks plus shared ``<fieldUpdates>``, ``<emailAlerts>``,
``<tasks>``, ``<outboundMessages>``, ``<alerts>``, ``<flowActions>`` that
rules reference by name. Workflow Rules reached end of support on
2025-12-31 but keep running; enterprise orgs still carry thousands.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import (
    as_bool,
    as_list,
    as_str,
    criteria_fields,
    criteria_items,
    get_body,
    object_from_path,
)
from offramp.extract.pull.reconciler import ReconciledRecord
from offramp.generate.formula.references import extract_references


@register
class WorkflowRuleExtractor(CategoryExtractor):
    """``workflows/<Object>.workflow-meta.xml`` → canonical dict (one Component per object)."""

    category: ClassVar[CategoryName] = CategoryName.WORKFLOW_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "Workflow")
        sobject = (
            as_str(record.payload.get("object_from_path"))
            or object_from_path(as_str(record.payload.get("path")))
            or record.api_name
        )

        rules = [_normalize_rule(r) for r in as_list(body.get("rules")) if isinstance(r, dict)]
        field_updates = [
            _normalize_field_update(fu)
            for fu in as_list(body.get("fieldUpdates"))
            if isinstance(fu, dict)
        ]
        email_alerts = [
            {
                "name": as_str(ea.get("fullName")),
                "template": as_str(ea.get("template")),
                "recipients": [
                    as_str(r.get("type"))
                    for r in as_list(ea.get("recipients"))
                    if isinstance(r, dict)
                ],
            }
            for ea in as_list(body.get("alerts")) + as_list(body.get("emailAlerts"))
            if isinstance(ea, dict)
        ]
        tasks = [
            {
                "name": as_str(t.get("fullName")),
                "subject": as_str(t.get("subject")),
                "assigned_to": as_str(t.get("assignedTo")),
            }
            for t in as_list(body.get("tasks"))
            if isinstance(t, dict)
        ]
        outbound = [
            {
                "name": as_str(om.get("fullName")),
                "endpoint_url": as_str(om.get("endpointUrl")),
                "fields": [as_str(f) for f in as_list(om.get("fields"))],
            }
            for om in as_list(body.get("outboundMessages"))
            if isinstance(om, dict)
        ]
        flow_actions = [
            {"name": as_str(fa.get("fullName")), "flow": as_str(fa.get("flow"))}
            for fa in as_list(body.get("flowActions"))
            if isinstance(fa, dict)
        ]

        fields: set[str] = set()
        globals_: set[str] = set()
        unparsed = 0
        for r in rules:
            for f in criteria_fields(r["criteria_items"]):
                fields.add(f if "." in f else f"{sobject}.{f}")
            if r["formula"]:
                refs = extract_references(r["formula"])
                fields.update(refs.qualified_fields(sobject))
                globals_.update(refs.globals)
                unparsed += 0 if refs.parsed else 1
        for fu in field_updates:
            if fu["field"]:
                fields.add(fu["field"] if "." in fu["field"] else f"{sobject}.{fu['field']}")
            if fu["formula"]:
                refs = extract_references(fu["formula"])
                fields.update(refs.qualified_fields(sobject))
                globals_.update(refs.globals)
                unparsed += 0 if refs.parsed else 1
        for om in outbound:
            for f in om["fields"]:
                fields.add(f"{sobject}.{f}")

        return {
            "object": sobject,
            "rules": rules,
            "field_updates": field_updates,
            "email_alerts": email_alerts,
            "tasks": tasks,
            "outbound_messages": outbound,
            "flow_actions": flow_actions,
            "references": {
                "objects": sorted(
                    {sobject} | {fu["target_object"] for fu in field_updates if fu["target_object"]}
                ),
                "fields": sorted(fields, key=str.lower),
                "fields_written": sorted(
                    {
                        (fu["field"] if "." in fu["field"] else f"{sobject}.{fu['field']}")
                        for fu in field_updates
                        if fu["field"]
                    },
                    key=str.lower,
                ),
                "globals": sorted(globals_),
                "email_templates": sorted(
                    {ea["template"] for ea in email_alerts if ea["template"]}
                ),
                "flows": sorted({fa["flow"] for fa in flow_actions if fa["flow"]}),
                "outbound_endpoints": sorted(
                    {om["endpoint_url"] for om in outbound if om["endpoint_url"]}
                ),
                "unparsed_formulas": unparsed,
            },
        }


def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    actions = as_list(rule.get("actions"))
    wf_time = as_list(rule.get("workflowTimeTriggers"))
    return {
        "name": as_str(rule.get("fullName")),
        "active": as_bool(rule.get("active"), True),
        "trigger_type": as_str(rule.get("triggerType"), "onCreateOnly"),
        "formula": as_str(rule.get("formula")) or None,
        "boolean_filter": as_str(rule.get("booleanFilter")),
        "criteria_items": criteria_items(rule.get("criteriaItems")),
        "immediate_actions": [
            {"name": as_str(a.get("name")), "type": as_str(a.get("type"))}
            for a in actions
            if isinstance(a, dict)
        ],
        "time_triggers": [
            {
                "offset": as_str(t.get("timeLength")),
                "unit": as_str(t.get("workflowTimeTriggerUnit")),
                "actions": [
                    {"name": as_str(a.get("name")), "type": as_str(a.get("type"))}
                    for a in as_list(t.get("actions"))
                    if isinstance(a, dict)
                ],
            }
            for t in wf_time
            if isinstance(t, dict)
        ],
    }


def _normalize_field_update(fu: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": as_str(fu.get("fullName")),
        "field": as_str(fu.get("field")),
        "literal_value": as_str(fu.get("literalValue")),
        "formula": as_str(fu.get("formula")) or None,
        "operation": as_str(fu.get("operation")),
        "reevaluate_on_change": as_bool(fu.get("reevaluateOnChange")),
        "target_object": as_str(fu.get("targetObject")),
        "lookup_value": as_str(fu.get("lookupValue")),
        "notify_assignee": as_bool(fu.get("notifyAssignee")),
    }
