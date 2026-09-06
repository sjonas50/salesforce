"""Validation Rule extractor.

Parses ``<Object>/validationRules/*.validationRule-meta.xml`` (or the Tooling
``ValidationRule.Metadata`` JSON). Emits the canonical shape the Tier 1
translator consumes plus formula-derived ``references``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_bool, as_str, get_body, object_from_path
from offramp.extract.pull.reconciler import ReconciledRecord
from offramp.generate.formula.references import extract_references


@register
class ValidationRuleExtractor(CategoryExtractor):
    """Salesforce Validation Rule → canonical dict."""

    category: ClassVar[CategoryName] = CategoryName.VALIDATION_RULE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "ValidationRule")
        object_name = (
            as_str(record.payload.get("object_from_path"))
            or object_from_path(as_str(record.payload.get("path")))
            or as_str(
                body.get("EntityDefinition", {}).get("QualifiedApiName")
                if isinstance(body.get("EntityDefinition"), dict)
                else ""
            )
        )
        formula = as_str(body.get("errorConditionFormula"))
        refs = extract_references(formula)
        return {
            "object": object_name,
            "active": as_bool(body.get("active"), True),
            "description": as_str(body.get("description")),
            "error_condition_formula": formula,
            "error_message": as_str(body.get("errorMessage")),
            "error_display_field": as_str(body.get("errorDisplayField")) or None,
            "references": {
                "objects": [object_name] if object_name else [],
                "fields": refs.qualified_fields(object_name),
                "globals": refs.globals,
                "functions": refs.functions,
                "formula_parsed": refs.parsed,
            },
        }
