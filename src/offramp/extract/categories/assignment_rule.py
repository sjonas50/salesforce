"""Assignment Rule extractor (Lead + Case routing).

Parses ``assignmentRules/<Object>.assignmentRules-meta.xml``. Each file
contains multiple ``<assignmentRule>`` blocks per sObject, each with
ordered ``<ruleEntries>`` evaluated top-to-bottom — the first matching
entry sets the OwnerId.
"""

from __future__ import annotations

from typing import Any

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class AssignmentRuleExtractor(CategoryExtractor):
    """Lead/Case AssignmentRules -> canonical dict per sObject."""

    category = CategoryName.ASSIGNMENT_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml")
        if not isinstance(raw, str):
            raise ValueError(f"Assignment rule {record.api_name} missing raw_xml")
        parsed = parse_xml(raw)
        body = parsed.get("AssignmentRules", {})
        if not isinstance(body, dict):
            raise ValueError(f"Assignment rule {record.api_name} XML root malformed")

        path = record.payload.get("path", "")
        sobject = ""
        parts = path.split("/")
        if parts and parts[-1].endswith(".assignmentRules-meta.xml"):
            sobject = parts[-1][: -len(".assignmentRules-meta.xml")]

        groups = _as_list(body.get("assignmentRule"))
        return {
            "object": sobject,
            "rule_groups": [_normalize_group(g) for g in groups if isinstance(g, dict)],
        }


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _normalize_group(g: dict[str, Any]) -> dict[str, Any]:
    entries = _as_list(g.get("ruleEntries"))
    return {
        "name": g.get("fullName", ""),
        "active": _bool(g.get("active", "true")),
        # The first matching entry wins — preserve XML order.
        "entries": [_normalize_entry(e) for e in entries if isinstance(e, dict)],
    }


def _normalize_entry(e: dict[str, Any]) -> dict[str, Any]:
    criteria_items = _as_list(e.get("criteriaItems"))
    return {
        "assigned_to": e.get("assignedTo", ""),
        "assigned_to_type": e.get("assignedToType", "User"),  # User | Queue
        "formula": e.get("formula", "") or None,  # mutually exclusive with criteriaItems
        "criteria_items": [
            {
                "field": c.get("field", ""),
                "operation": c.get("operation", ""),
                "value": c.get("value", ""),
            }
            for c in criteria_items
            if isinstance(c, dict)
        ],
        "team": e.get("team", ""),
        "template": e.get("template", ""),
    }


def _bool(s: Any) -> bool:
    return str(s).lower() == "true"
