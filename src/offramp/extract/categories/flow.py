"""Flow extractor (covers all 7 Flow variants).

Salesforce stores every Flow variant in the same ``.flow-meta.xml`` schema —
``processType`` + ``triggerType`` discriminate. The fixture client already
filters records to the right ``CategoryName`` via XML signal; this extractor
parses the body once per variant. Each Flow variant gets its own concrete
class so ``base.register`` can dispatch by category.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


def _parse_flow_xml(record: ReconciledRecord) -> dict[str, Any]:
    raw = record.payload.get("raw_xml")
    if not isinstance(raw, str):
        raise ValueError(f"Flow {record.api_name} missing raw_xml")
    parsed = parse_xml(raw)
    body = parsed.get("Flow", {})
    if not isinstance(body, dict):
        raise ValueError(f"Flow {record.api_name} XML root malformed")
    decisions = body.get("decisions") or []
    record_lookups = body.get("recordLookups") or []
    record_creates = body.get("recordCreates") or []
    record_updates = body.get("recordUpdates") or []
    return {
        "api_version": body.get("apiVersion", "66.0"),
        "process_type": body.get("processType", ""),
        "trigger_type": (body.get("start") or {}).get("triggerType", "")
        if isinstance(body.get("start"), dict)
        else "",
        "status": body.get("status", "Active"),
        "decisions": decisions if isinstance(decisions, list) else [decisions],
        "record_lookups": record_lookups if isinstance(record_lookups, list) else [record_lookups],
        "record_creates": record_creates if isinstance(record_creates, list) else [record_creates],
        "record_updates": record_updates if isinstance(record_updates, list) else [record_updates],
        "subflows": body.get("subflows") or [],
        "screens": body.get("screens") or [],
        "raw_root_keys": sorted(body.keys()),
    }


class _FlowVariantBase(CategoryExtractor):
    """Shared parser; subclasses bind a specific :attr:`category`."""

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        return _parse_flow_xml(record)


@register
class RecordTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.RECORD_TRIGGERED_FLOW


@register
class ScreenFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.SCREEN_FLOW


@register
class ScheduleTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.SCHEDULE_TRIGGERED_FLOW


@register
class PlatformEventTriggeredFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW


@register
class AutolaunchedFlowExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.AUTOLAUNCHED_FLOW


@register
class FlowOrchestrationExtractor(_FlowVariantBase):
    category: ClassVar[CategoryName] = CategoryName.FLOW_ORCHESTRATION


@register
class ProcessBuilderExtractor(_FlowVariantBase):
    """Process Builder is stored as a Flow with processType=Workflow."""

    category: ClassVar[CategoryName] = CategoryName.PROCESS_BUILDER
