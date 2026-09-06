"""Escalation, Auto-Response, and Sharing rule extractors.

All three share the ``<criteriaItems>`` / ``<formula>`` entry shape; each
emits ``references`` so the graph links rules to the fields they evaluate.
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


def _host(record: ReconciledRecord) -> str:
    return (
        as_str(record.payload.get("object_from_path"))
        or object_from_path(as_str(record.payload.get("path")))
        or record.api_name
    )


def _entry_refs(entries: list[dict[str, Any]], sobject: str) -> tuple[list[str], list[str]]:
    fields: set[str] = set()
    globals_: set[str] = set()
    for e in entries:
        for f in criteria_fields(e.get("criteria_items", [])):
            fields.add(f if "." in f else f"{sobject}.{f}")
        if e.get("formula"):
            refs = extract_references(e["formula"])
            fields.update(refs.qualified_fields(sobject))
            globals_.update(refs.globals)
    return sorted(fields, key=str.lower), sorted(globals_)


@register
class EscalationRuleExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.ESCALATION_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "EscalationRules")
        sobject = _host(record)
        groups: list[dict[str, Any]] = []
        for g in as_list(body.get("escalationRule")):
            if not isinstance(g, dict):
                continue
            entries = []
            for e in as_list(g.get("ruleEntry")) + as_list(g.get("ruleEntries")):
                if not isinstance(e, dict):
                    continue
                actions = [
                    {
                        "assigned_to": as_str(a.get("assignedTo")),
                        "assigned_to_type": as_str(a.get("assignedToType")),
                        "minutes_to_escalation": as_str(a.get("minutesToEscalation")),
                        "notify_to": as_str(a.get("notifyTo")),
                        "notify_email": as_str(a.get("notifyEmail")),
                        "template": as_str(
                            a.get("assignedToTemplate")
                            or a.get("notifyToTemplate")
                            or a.get("notifyCaseOwnerTemplate")
                            or a.get("notifyTemplate")
                        ),
                    }
                    for a in as_list(e.get("escalationAction"))
                    if isinstance(a, dict)
                ]
                entries.append(
                    {
                        "formula": as_str(e.get("formula")) or None,
                        "boolean_filter": as_str(e.get("booleanFilter")),
                        "criteria_items": criteria_items(e.get("criteriaItems")),
                        "business_hours": as_str(e.get("businessHours")),
                        "escalation_start_time": as_str(e.get("escalationStartTime")),
                        "actions": actions,
                    }
                )
            groups.append(
                {
                    "name": as_str(g.get("fullName")),
                    "active": as_bool(g.get("active"), True),
                    "entries": entries,
                }
            )
        all_entries = [e for g in groups for e in g["entries"]]
        fields, globals_ = _entry_refs(all_entries, sobject)
        return {
            "object": sobject,
            "rule_groups": groups,
            "references": {
                "objects": [sobject],
                "fields": fields,
                "globals": globals_,
                "email_templates": sorted(
                    {a["template"] for e in all_entries for a in e["actions"] if a["template"]}
                ),
                "assignees": sorted(
                    {
                        a["assigned_to"]
                        for e in all_entries
                        for a in e["actions"]
                        if a["assigned_to"]
                    }
                ),
            },
        }


@register
class AutoResponseRuleExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.AUTO_RESPONSE_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "AutoResponseRules")
        sobject = _host(record)
        groups: list[dict[str, Any]] = []
        for g in as_list(body.get("autoResponseRule")):
            if not isinstance(g, dict):
                continue
            entries = [
                {
                    "formula": as_str(e.get("formula")) or None,
                    "boolean_filter": as_str(e.get("booleanFilter")),
                    "criteria_items": criteria_items(e.get("criteriaItems")),
                    "sender_email": as_str(e.get("senderEmail")),
                    "sender_name": as_str(e.get("senderName")),
                    "reply_to": as_str(e.get("replyToEmail")),
                    "template": as_str(e.get("template")),
                }
                for e in as_list(g.get("ruleEntry")) + as_list(g.get("ruleEntries"))
                if isinstance(e, dict)
            ]
            groups.append(
                {
                    "name": as_str(g.get("fullName")),
                    "active": as_bool(g.get("active"), True),
                    "entries": entries,
                }
            )
        all_entries = [e for g in groups for e in g["entries"]]
        fields, globals_ = _entry_refs(all_entries, sobject)
        return {
            "object": sobject,
            "rule_groups": groups,
            "references": {
                "objects": [sobject],
                "fields": fields,
                "globals": globals_,
                "email_templates": sorted({e["template"] for e in all_entries if e["template"]}),
            },
        }


@register
class SharingRuleExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.SHARING_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "SharingRules")
        sobject = _host(record)

        def _shared(d: Any) -> dict[str, str]:
            if not isinstance(d, dict):
                return {}
            return {k: as_str(v) for k, v in d.items() if not isinstance(v, dict | list)}

        criteria: list[dict[str, Any]] = [
            {
                "name": as_str(r.get("fullName")),
                "access_level": as_str(r.get("accessLevel")),
                "criteria_items": criteria_items(r.get("criteriaItems")),
                "boolean_filter": as_str(r.get("booleanFilter")),
                "shared_to": _shared(r.get("sharedTo")),
                "include_records_owned_by_all": as_bool(r.get("includeRecordsOwnedByAll")),
            }
            for r in as_list(body.get("sharingCriteriaRules"))
            if isinstance(r, dict)
        ]
        owner: list[dict[str, Any]] = [
            {
                "name": as_str(r.get("fullName")),
                "access_level": as_str(r.get("accessLevel")),
                "shared_from": _shared(r.get("sharedFrom")),
                "shared_to": _shared(r.get("sharedTo")),
            }
            for r in as_list(body.get("sharingOwnerRules"))
            if isinstance(r, dict)
        ]
        territory: list[dict[str, Any]] = [
            {
                "name": as_str(r.get("fullName")),
                "access_level": as_str(r.get("accessLevel")),
                "shared_to": _shared(r.get("sharedTo")),
            }
            for r in as_list(body.get("sharingTerritoryRules"))
            if isinstance(r, dict)
        ]
        guest: list[dict[str, Any]] = [
            {
                "name": as_str(r.get("fullName")),
                "access_level": as_str(r.get("accessLevel")),
                "criteria_items": criteria_items(r.get("criteriaItems")),
            }
            for r in as_list(body.get("sharingGuestRules"))
            if isinstance(r, dict)
        ]
        fields, _ = _entry_refs(criteria + guest, sobject)
        return {
            "object": sobject,
            "criteria_rules": criteria,
            "owner_rules": owner,
            "territory_rules": territory,
            "guest_rules": guest,
            "references": {
                "objects": [sobject],
                "fields": fields,
                "groups": sorted(
                    {
                        v
                        for r in criteria + owner + territory
                        for v in r.get("shared_to", {}).values()
                        if v
                    }
                ),
            },
        }
