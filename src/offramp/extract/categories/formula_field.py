"""Formula Field extractor.

Parses ``<Object>/fields/*.field-meta.xml`` for fields with a ``<formula>``
element. Roll-Up Summary fields are handled by
:mod:`offramp.extract.categories.rollup_summary`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_bool, as_str, get_body, object_from_path
from offramp.extract.pull.reconciler import ReconciledRecord
from offramp.generate.formula.references import extract_references


@register
class FormulaFieldExtractor(CategoryExtractor):
    """Salesforce formula field → canonical dict."""

    category: ClassVar[CategoryName] = CategoryName.FORMULA_FIELD

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "CustomField")
        object_name = as_str(record.payload.get("object_from_path")) or object_from_path(
            as_str(record.payload.get("path"))
        )
        formula = as_str(body.get("formula"))
        refs = extract_references(formula)
        field_name = as_str(body.get("fullName"), record.api_name)
        return {
            "object": object_name,
            "field_name": field_name,
            "label": as_str(body.get("label")),
            "type": as_str(body.get("type"), "Formula"),
            "formula": formula,
            "formula_treat_blanks_as": as_str(body.get("formulaTreatBlanksAs"), "BlankAsZero"),
            "return_type": as_str(body.get("type"), "Text"),
            "external_id": as_bool(body.get("externalId")),
            "references": {
                "objects": [object_name] if object_name else [],
                "fields": refs.qualified_fields(object_name),
                "globals": refs.globals,
                "functions": refs.functions,
                "formula_parsed": refs.parsed,
                "defines_field": f"{object_name}.{field_name}" if object_name else field_name,
            },
        }
