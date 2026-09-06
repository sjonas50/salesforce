"""Platform Event definition extractor (``objects/<Name>__e/<Name>__e.object-meta.xml``)."""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import as_list, as_str, get_body
from offramp.extract.pull.reconciler import ReconciledRecord


@register
class PlatformEventExtractor(CategoryExtractor):
    category: ClassVar[CategoryName] = CategoryName.PLATFORM_EVENT

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        body = get_body(record, "CustomObject")
        name = record.api_name
        fields = [
            {
                "name": as_str(f.get("fullName")),
                "type": as_str(f.get("type")),
                "label": as_str(f.get("label")),
            }
            for f in as_list(body.get("fields"))
            if isinstance(f, dict)
        ]
        return {
            "object": name,
            "label": as_str(body.get("label")),
            "event_type": as_str(body.get("eventType")),
            "publish_behavior": as_str(body.get("publishBehavior")),
            "deployment_status": as_str(body.get("deploymentStatus")),
            "fields": fields,
            "references": {
                "objects": [name],
                "fields": [f"{name}.{f['name']}" for f in fields if f["name"]],
            },
        }
