"""Apex Trigger extractor.

The ``-meta.xml`` companion gives us status + version + sObject + events.
The actual trigger body lives in the sibling ``.trigger`` file. The extractor
records both as a canonical dict; semantic AST analysis happens in Phase 2
(summit-ast). The body is captured verbatim so the dispatch resolver can
later spot ``MetadataTriggerHandler.execute()``-style indirection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord

_TRIGGER_HEADER_RE = re.compile(
    r"trigger\s+(\w+)\s+on\s+(\w+)\s*\((before|after)\s+([^)]+)\)",
    re.IGNORECASE,
)


@register
class ApexTriggerExtractor(CategoryExtractor):
    """Apex trigger → canonical dict (header-parsed; full AST in Phase 2)."""

    category = CategoryName.APEX_TRIGGER

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw_xml = record.payload.get("raw_xml")
        if not isinstance(raw_xml, str):
            raise ValueError(f"Apex trigger {record.api_name} missing raw_xml")
        parsed = parse_xml(raw_xml)
        meta = parsed.get("ApexTrigger", {})
        if not isinstance(meta, dict):
            raise ValueError(f"Apex trigger {record.api_name} XML root malformed")

        # Find the .trigger source file alongside the .trigger-meta.xml.
        path_str = record.payload.get("path", "")
        trigger_body = ""
        sobject = ""
        events: list[str] = []
        timing = ""
        if path_str:
            trigger_path = Path(path_str).with_suffix("")
            # path was "...trigger-meta.xml"; with_suffix kills .xml leaving .trigger-meta
            trigger_path = trigger_path.with_suffix(".trigger")
            # We don't have the on-disk root here; store the relative hint and let the
            # orchestrator fill in the body at parse time if available. The fixture
            # client embeds trigger bodies under payload["trigger_body"] when present.
        if "trigger_body" in record.payload:
            trigger_body = record.payload["trigger_body"]
            match = _TRIGGER_HEADER_RE.search(trigger_body)
            if match:
                sobject = match.group(2)
                timing = match.group(3).lower()
                events = [e.strip().lower() for e in match.group(4).split(",")]

        return {
            "api_version": meta.get("apiVersion", "66.0"),
            "status": meta.get("status", "Active"),
            "sobject": sobject,
            "timing": timing,  # 'before' | 'after'
            "events": events,  # ['insert', 'update', ...]
            "body": trigger_body,
            "body_lines": trigger_body.count("\n") + 1 if trigger_body else 0,
        }
