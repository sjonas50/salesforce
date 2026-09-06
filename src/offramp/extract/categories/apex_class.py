"""Apex Class extractor (C20-backed).

The ``-meta.xml`` companion carries status + API version; the body arrives
under ``payload['body']`` from the source tree reader or the Tooling API
client. The analyzer output is stored verbatim in ``raw['analysis']`` and the
dependency-relevant slice is repeated under ``raw['references']`` so every
category exposes the same reference shape.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.apex.references import analyze
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_str, get_body
from offramp.extract.pull.reconciler import ReconciledRecord


def analysis_payload(record: ReconciledRecord, body_key: str) -> dict[str, Any]:
    body = record.payload.get(body_key, "")
    if not isinstance(body, str):
        body = ""
    analysis = analyze(body, name_hint=record.api_name) if body else None
    a = analysis.to_dict() if analysis else {}
    refs: dict[str, Any] = {
        "apex_classes": a.get("class_references", []),
        "apex_class_candidates": a.get("candidate_class_references", []),
        "objects": a.get("sobject_references", []),
        "fields": a.get("field_references", []),
        "fields_written": a.get("field_writes", []),
        "dml_objects": sorted({d["sobject"] for d in a.get("dml", []) if d.get("sobject")}),
        "named_credentials": a.get("named_credentials", []),
        "custom_labels": a.get("custom_labels", []),
        "custom_settings": a.get("custom_settings", []),
        "type_forname": a.get("type_forname_literals", []),
        "dynamic_access": a.get("dynamic_access", []),
        "async_targets": [
            x["target_class"] for x in a.get("async_calls", []) if x.get("target_class")
        ],
    }
    return {
        "has_body": bool(body),
        "body": body,
        "body_lines": body.count("\n") + 1 if body else 0,
        "analysis": a,
        "entry_points": a.get("entry_points", []),
        "is_test": bool(a.get("is_test")),
        "inner_types": a.get("inner_types", []),
        "dynamic_access": a.get("dynamic_access", []),
        "references": refs,
    }


@register
class ApexClassExtractor(CategoryExtractor):
    """Apex class → canonical dict with static-analysis references."""

    category: ClassVar[CategoryName] = CategoryName.APEX_CLASS

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        try:
            meta = get_body(record, "ApexClass")
        except ValueError:
            meta = {}
        out = {
            "api_version": as_str(meta.get("apiVersion"), "66.0"),
            "status": as_str(meta.get("status"), "Active"),
        }
        out.update(analysis_payload(record, "body"))
        return out
