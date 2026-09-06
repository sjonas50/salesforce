"""Approval Process extractor.

``approvalProcesses/<Object>.<Name>.approvalProcess-meta.xml``. Entry
criteria, ordered steps with approver assignment, and the four action sets
(initial submission, final approval, final rejection, recall) that reference
field updates / email alerts / tasks / outbound messages by name.
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
)
from offramp.extract.pull.reconciler import ReconciledRecord
from offramp.generate.formula.references import extract_references


def _criteria(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"formula": None, "criteria_items": [], "boolean_filter": ""}
    return {
        "formula": as_str(block.get("formula")) or None,
        "criteria_items": criteria_items(block.get("criteriaItems")),
        "boolean_filter": as_str(block.get("booleanFilter")),
    }


def _actions(block: Any) -> list[dict[str, str]]:
    if not isinstance(block, dict):
        return []
    return [
        {"name": as_str(a.get("name")), "type": as_str(a.get("type"))}
        for a in as_list(block.get("action"))
        if isinstance(a, dict)
    ]


@register
class ApprovalProcessExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.APPROVAL_PROCESS

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "ApprovalProcess")
        sobject = (
            record.api_name.split(".", 1)[0]
            if "." in record.api_name
            else as_str(record.payload.get("object_from_path"))
        )
        steps: list[dict[str, Any]] = []
        for s in as_list(body.get("approvalStep")):
            if not isinstance(s, dict):
                continue
            aa_raw = s.get("assignedApprover")
            aa: dict[str, Any] = aa_raw if isinstance(aa_raw, dict) else {}
            approvers = [
                {"type": as_str(a.get("type")), "name": as_str(a.get("name"))}
                for a in as_list(aa.get("approver"))
                if isinstance(a, dict)
            ]
            steps.append(
                {
                    "name": as_str(s.get("name")),
                    "label": as_str(s.get("label")),
                    "allow_delegate": as_bool(s.get("allowDelegate")),
                    "approvers": approvers,
                    "when_multiple": as_str(aa.get("whenMultipleApprovers")),
                    "entry_criteria": _criteria(s.get("entryCriteria")),
                    "if_criteria_not_met": as_str(s.get("ifCriteriaNotMet")),
                    "approval_actions": _actions(s.get("approvalActions")),
                    "rejection_actions": _actions(s.get("rejectionActions")),
                    "reject_behavior": as_str(
                        s.get("rejectBehavior", {}).get("type")
                        if isinstance(s.get("rejectBehavior"), dict)
                        else ""
                    ),
                }
            )
        entry = _criteria(body.get("entryCriteria"))
        submitters = [
            {"type": as_str(x.get("type")), "submitter": as_str(x.get("submitter"))}
            for x in as_list(body.get("allowedSubmitters"))
            if isinstance(x, dict)
        ]
        ra = body.get("recordEditability")
        fields: set[str] = set()
        globals_: set[str] = set()
        crits: list[dict[str, Any]] = [entry, *(s["entry_criteria"] for s in steps)]
        for crit in crits:
            for f in criteria_fields(crit["criteria_items"]):
                fields.add(f if "." in f else f"{sobject}.{f}")
            if crit["formula"]:
                refs = extract_references(crit["formula"])
                fields.update(refs.qualified_fields(sobject))
                globals_.update(refs.globals)
        na_raw = body.get("nextAutomatedApprover")
        next_auto: dict[str, Any] = na_raw if isinstance(na_raw, dict) else {}
        approver_fields = {
            as_str(a["name"])
            for s in steps
            for a in s["approvers"]
            if a["type"] in {"userHierarchyField", "relatedUserField"} and a["name"]
        }
        if next_auto.get("userHierarchyField"):
            approver_fields.add(as_str(next_auto["userHierarchyField"]))
        # userHierarchyField approvers ('Manager') are fields on User, not on the host object.
        all_actions = (
            _actions(body.get("initialSubmissionActions"))
            + _actions(body.get("finalApprovalActions"))
            + _actions(body.get("finalRejectionActions"))
            + _actions(body.get("recallActions"))
            + [a for s in steps for a in s["approval_actions"] + s["rejection_actions"]]
        )
        return {
            "object": sobject,
            "label": as_str(body.get("label")),
            "active": as_bool(body.get("active"), True),
            "description": as_str(body.get("description")),
            "entry_criteria": entry,
            "record_editability": as_str(ra),
            "allowed_submitters": submitters,
            "email_template": as_str(body.get("emailTemplate")),
            "next_automated_approver": {k: as_str(v) for k, v in next_auto.items()},
            "steps": steps,
            "initial_submission_actions": _actions(body.get("initialSubmissionActions")),
            "final_approval_actions": _actions(body.get("finalApprovalActions")),
            "final_rejection_actions": _actions(body.get("finalRejectionActions")),
            "recall_actions": _actions(body.get("recallActions")),
            "references": {
                "objects": [sobject] if sobject else [],
                "fields": sorted(fields, key=str.lower),
                "globals": sorted(globals_),
                "workflow_actions": sorted(
                    {f"{a['type']}:{a['name']}" for a in all_actions if a["name"]}
                ),
                "approver_user_fields": sorted(f"User.{af}" for af in approver_fields),
                "email_templates": sorted({as_str(body.get("emailTemplate"))} - {""}),
            },
        }
