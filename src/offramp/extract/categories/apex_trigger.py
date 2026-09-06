"""Apex Trigger extractor (C20-backed).

``trigger X on Obj (events)`` header plus full reference analysis of the
body so handler classes, SOQL targets, and DML are all visible to the graph.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.apex_class import analysis_payload
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_str, get_body
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class ApexTriggerExtractor(CategoryExtractor):
    """Apex trigger → canonical dict."""

    category: ClassVar[CategoryName] = CategoryName.APEX_TRIGGER

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        try:
            meta = get_body(record, "ApexTrigger")
        except ValueError:
            meta = {}
        out: dict[str, Any] = {
            "api_version": as_str(meta.get("apiVersion"), "66.0"),
            "status": as_str(meta.get("status"), "Active"),
        }
        payload = analysis_payload(record, "trigger_body")
        analysis = payload["analysis"]
        events = list(analysis.get("trigger_events", []))
        timings = sorted({e.split(" ")[0] for e in events})
        out.update(payload)
        out["sobject"] = analysis.get("trigger_object") or ""
        out["timing"] = "both" if len(timings) == 2 else (timings[0] if timings else "")
        out["events"] = events
        if out["sobject"]:
            out["references"]["objects"] = sorted(
                set(out["references"]["objects"]) | {out["sobject"]}, key=str.lower
            )
        return out
