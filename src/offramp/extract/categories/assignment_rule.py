"""Assignment Rule extractor (Lead + Case routing)."""

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
class AssignmentRuleExtractor(CategoryExtractor):
    """Lead/Case AssignmentRules → canonical dict per sObject."""

    category: ClassVar[CategoryName] = CategoryName.ASSIGNMENT_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "AssignmentRules")
        sobject = (
            as_str(record.payload.get("object_from_path"))
            or object_from_path(as_str(record.payload.get("path")))
            or record.api_name
        )
        groups = [
            _normalize_group(g) for g in as_list(body.get("assignmentRule")) if isinstance(g, dict)
        ]
        fields: set[str] = set()
        globals_: set[str] = set()
        templates: set[str] = set()
        for g in groups:
            for e in g["entries"]:
                for f in criteria_fields(e["criteria_items"]):
                    fields.add(f if "." in f else f"{sobject}.{f}")
                if e["formula"]:
                    refs = extract_references(e["formula"])
                    fields.update(refs.qualified_fields(sobject))
                    globals_.update(refs.globals)
                if e["template"]:
                    templates.add(e["template"])
        return {
            "object": sobject,
            "rule_groups": groups,
            "references": {
                "objects": [sobject],
                "fields": sorted(fields | {f"{sobject}.OwnerId"}, key=str.lower),
                "fields_written": [f"{sobject}.OwnerId"],
                "globals": sorted(globals_),
                "email_templates": sorted(templates),
                "assignees": sorted(
                    {e["assigned_to"] for g in groups for e in g["entries"] if e["assigned_to"]}
                ),
            },
        }


def _normalize_group(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": as_str(g.get("fullName")),
        "active": as_bool(g.get("active"), True),
        "entries": [
            _normalize_entry(e)
            for e in as_list(g.get("ruleEntry")) + as_list(g.get("ruleEntries"))
            if isinstance(e, dict)
        ],
    }


def _normalize_entry(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "assigned_to": as_str(e.get("assignedTo")),
        "assigned_to_type": as_str(e.get("assignedToType"), "User"),
        "formula": as_str(e.get("formula")) or None,
        "boolean_filter": as_str(e.get("booleanFilter")),
        "criteria_items": criteria_items(e.get("criteriaItems")),
        "team": as_str(e.get("team")),
        "template": as_str(e.get("template")),
        "overwrite_existing_teams": as_bool(e.get("overwriteExistingTeams")),
    }
