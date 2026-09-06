"""Passthrough extractor for categories with no category-specific shape.

Only Change Data Capture remains a passthrough: its "metadata" is a JSON
subscription list, not XML. Every other category has a real extractor.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class ChangeDataCaptureExtractor(CategoryExtractor):
    """CDC subscriptions: ``_tooling/cdc_subscriptions.json`` or a PlatformEventChannel."""

    category: ClassVar[CategoryName] = CategoryName.CHANGE_DATA_CAPTURE
    is_passthrough: ClassVar[bool] = True

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml") or record.payload.get("raw_json") or ""
        parsed: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                parsed = {"_parse_error": str(exc)}
        elif isinstance(record.payload.get("parsed"), dict):
            parsed = record.payload["parsed"]
        objects = [str(o) for o in parsed.get("subscribed_objects", []) if isinstance(o, str)]
        return {
            "path": record.payload.get("path"),
            "channel": parsed.get("channel", "/data/ChangeEvents"),
            "subscribed_objects": objects,
            "references": {"objects": objects},
        }
