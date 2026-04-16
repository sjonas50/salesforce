"""Formula Field extractor.

Parses ``<Object>/fields/*.field-meta.xml`` for fields with a ``<formula>``
element. Roll-Up Summary fields have ``<summaryOperation>`` instead and are
handled by :mod:`offramp.extract.categories.rollup_summary`.
"""

from __future__ import annotations

from typing import Any

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class FormulaFieldExtractor(CategoryExtractor):
    """Salesforce formula field → canonical dict."""

    category = CategoryName.FORMULA_FIELD

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml")
        if not isinstance(raw, str):
            raise ValueError(f"Formula field {record.api_name} missing raw_xml")
        parsed = parse_xml(raw)
        body = parsed.get("CustomField", {})
        if not isinstance(body, dict):
            raise ValueError(f"Formula field {record.api_name} XML root malformed")
        path = record.payload.get("path", "")
        object_name = ""
        parts = path.split("/")
        if "objects" in parts:
            i = parts.index("objects")
            if i + 1 < len(parts):
                object_name = parts[i + 1]
        return {
            "object": object_name,
            "field_name": body.get("fullName", record.api_name),
            "label": body.get("label", ""),
            "type": body.get("type", "Formula"),
            "formula": body.get("formula", ""),
            "formula_treat_blanks_as": body.get("formulaTreatBlanksAs", "BlankAsZero"),
            "return_type": body.get("type", "Text"),
            "external_id": body.get("externalId", "false").lower() == "true",
        }
