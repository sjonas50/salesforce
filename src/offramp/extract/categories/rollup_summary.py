"""Roll-Up Summary field extractor: parent field fed by a child object."""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import (
    as_str,
    criteria_items,
    get_body,
    object_from_path,
)
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class RollupSummaryExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.ROLLUP_SUMMARY

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "CustomField")
        parent = as_str(record.payload.get("object_from_path")) or object_from_path(
            as_str(record.payload.get("path"))
        )
        field_name = as_str(body.get("fullName"), record.api_name)
        summarized = as_str(body.get("summarizedField"))
        foreign_key = as_str(body.get("summaryForeignKey"))
        child = (
            summarized.split(".", 1)[0]
            if "." in summarized
            else (foreign_key.split(".", 1)[0] if "." in foreign_key else "")
        )
        filters = criteria_items(body.get("summaryFilterItems"))
        fields = {f"{parent}.{field_name}"}
        if summarized:
            fields.add(summarized)
        if foreign_key:
            fields.add(foreign_key)
        for f in filters:
            if f["field"]:
                fields.add(f["field"] if "." in f["field"] else f"{child}.{f['field']}")
        return {
            "object": parent,
            "field_name": field_name,
            "label": as_str(body.get("label")),
            "child_object": child,
            "summarized_field": summarized,
            "summary_foreign_key": foreign_key,
            "summary_operation": as_str(body.get("summaryOperation")),
            "summary_filters": filters,
            "references": {
                "objects": sorted({parent, child} - {""}),
                "fields": sorted(fields, key=str.lower),
                "defines_field": f"{parent}.{field_name}",
            },
        }
