"""Validation Rule extractor.

Parses ``<Object>/validationRules/*.validationRule-meta.xml``. The output
schema is the canonical Validation Rule shape consumed by the Tier 1
translator (formula → Python rule, with ``errorConditionFormula`` becoming
the negated guard).
"""

from __future__ import annotations

from typing import Any

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class ValidationRuleExtractor(CategoryExtractor):
    """Salesforce Validation Rule → canonical dict."""

    category = CategoryName.VALIDATION_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml")
        if not isinstance(raw, str):
            raise ValueError(f"Validation rule {record.api_name} missing raw_xml")
        parsed = parse_xml(raw)
        body = parsed.get("ValidationRule", {})
        if not isinstance(body, dict):
            raise ValueError(f"Validation rule {record.api_name} XML root malformed")
        # Object name is encoded in the path: objects/<Object>/validationRules/X.xml
        path = record.payload.get("path", "")
        object_name = ""
        parts = path.split("/")
        if "objects" in parts:
            i = parts.index("objects")
            if i + 1 < len(parts):
                object_name = parts[i + 1]
        return {
            "object": object_name,
            "active": body.get("active", "true").lower() == "true",
            "description": body.get("description", "") or "",
            "error_condition_formula": body.get("errorConditionFormula", ""),
            "error_message": body.get("errorMessage", ""),
            "error_display_field": body.get("errorDisplayField"),
        }
