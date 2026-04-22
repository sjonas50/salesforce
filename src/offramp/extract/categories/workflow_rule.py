"""Workflow Rule extractor.

Parses ``workflows/<Object>.workflow-meta.xml`` files. Each XML contains
multiple ``<rules>`` blocks plus shared ``<fieldUpdates>``,
``<emailAlerts>``, ``<tasks>``, and ``<outboundMessages>`` elements that
the rules reference by name.

Salesforce stops creating new Workflow Rules in 2025 (Process Builder /
Workflow Rules end-of-support per the v2.1 plan §pitfalls), but enterprise
orgs still have thousands of them in production. The translator emits
each rule as one Tier 1 computation rule that fires at OoE step 12.
"""

from __future__ import annotations

from typing import Any

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class WorkflowRuleExtractor(CategoryExtractor):
    """``workflows/<Object>.workflow-meta.xml`` -> canonical dict.

    The workflow file scopes ALL rules for one sObject. We emit a single
    Component per file with the rules + shared actions inlined. The
    translator splits per-rule downstream.
    """

    category = CategoryName.WORKFLOW_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml")
        if not isinstance(raw, str):
            raise ValueError(f"Workflow rule {record.api_name} missing raw_xml")
        parsed = parse_xml(raw)
        body = parsed.get("Workflow", {})
        if not isinstance(body, dict):
            raise ValueError(f"Workflow rule {record.api_name} XML root malformed")

        path = record.payload.get("path", "")
        # Path shape: workflows/<Object>.workflow-meta.xml
        sobject = ""
        parts = path.split("/")
        if parts and parts[-1].endswith(".workflow-meta.xml"):
            sobject = parts[-1][: -len(".workflow-meta.xml")]

        rules = _as_list(body.get("rules"))
        field_updates = _as_list(body.get("fieldUpdates"))
        email_alerts = _as_list(body.get("emailAlerts"))
        tasks = _as_list(body.get("tasks"))
        outbound_messages = _as_list(body.get("outboundMessages"))

        return {
            "object": sobject,
            "rules": [_normalize_rule(r) for r in rules if isinstance(r, dict)],
            "field_updates": [
                _normalize_field_update(fu) for fu in field_updates if isinstance(fu, dict)
            ],
            "email_alerts": [
                {"name": ea.get("fullName", ""), "template": ea.get("template", "")}
                for ea in email_alerts
                if isinstance(ea, dict)
            ],
            "tasks": [
                {"name": t.get("fullName", ""), "subject": t.get("subject", "")}
                for t in tasks
                if isinstance(t, dict)
            ],
            "outbound_messages": [
                {"name": om.get("fullName", ""), "endpoint_url": om.get("endpointUrl", "")}
                for om in outbound_messages
                if isinstance(om, dict)
            ],
        }


def _as_list(v: Any) -> list[Any]:
    """SF XML repeats elements rather than wrapping in arrays — normalize."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Single workflow rule -> canonical dict the translator consumes."""
    actions = _as_list(rule.get("actions"))
    criteria_items = _as_list(rule.get("criteriaItems"))
    return {
        "name": rule.get("fullName", ""),
        "active": _bool(rule.get("active", "true")),
        "trigger_type": rule.get("triggerType", "onCreateOnly"),
        "formula": rule.get("formula", "") or None,  # mutually exclusive with criteriaItems
        "criteria_items": [_normalize_criteria(c) for c in criteria_items if isinstance(c, dict)],
        "immediate_actions": [
            {"name": a.get("name", ""), "type": a.get("type", "")}
            for a in actions
            if isinstance(a, dict)
        ],
    }


def _normalize_criteria(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "field": c.get("field", ""),
        "operation": c.get("operation", ""),
        "value": c.get("value", ""),
    }


def _normalize_field_update(fu: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": fu.get("fullName", ""),
        "field": fu.get("field", ""),
        "literal_value": fu.get("literalValue", ""),
        "formula": fu.get("formula", "") or None,
        "operation": fu.get("operation", ""),
        "reevaluate_on_change": _bool(fu.get("reevaluateOnChange", "false")),
        "target_object": fu.get("targetObject", ""),
    }


def _bool(s: Any) -> bool:
    return str(s).lower() == "true"
