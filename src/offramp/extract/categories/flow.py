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
    decisions = _as_list(body.get("decisions"))
    record_lookups = _as_list(body.get("recordLookups"))
    record_creates = _as_list(body.get("recordCreates"))
    record_updates = _as_list(body.get("recordUpdates"))
    action_calls = _as_list(body.get("actionCalls"))
    subflows = _as_list(body.get("subflows"))
    screens = _as_list(body.get("screens"))
    start = body.get("start") if isinstance(body.get("start"), dict) else {}

    return {
        "api_version": body.get("apiVersion", "66.0"),
        "process_type": body.get("processType", ""),
        "trigger_type": start.get("triggerType", "") if isinstance(start, dict) else "",
        "object": start.get("object", "") if isinstance(start, dict) else "",
        "status": body.get("status", "Active"),
        "decisions": decisions,
        "record_lookups": record_lookups,
        "record_creates": [
            _normalize_record_create(rc) for rc in record_creates if isinstance(rc, dict)
        ],
        "record_updates": [
            _normalize_record_update(ru) for ru in record_updates if isinstance(ru, dict)
        ],
        "action_calls": [
            {
                "name": a.get("name", ""),
                "action_name": a.get("actionName", ""),
                "type": a.get("actionType", ""),
            }
            for a in action_calls
            if isinstance(a, dict)
        ],
        "subflows": subflows,
        "screens": screens,
        "raw_root_keys": sorted(body.keys()),
    }


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _normalize_record_update(ru: dict[str, Any]) -> dict[str, Any]:
    """One <recordUpdates> block — normalized field/value assignments."""
    return {
        "name": ru.get("name", ""),
        "input_reference": ru.get("inputReference", ""),
        "input_assignments": [
            _normalize_assignment(a)
            for a in _as_list(ru.get("inputAssignments"))
            if isinstance(a, dict)
        ],
        "filter_logic": ru.get("filterLogic", ""),
        "filters": _as_list(ru.get("filters")),
    }


def _normalize_record_create(rc: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": rc.get("name", ""),
        "object": rc.get("object", ""),
        "input_assignments": [
            _normalize_assignment(a)
            for a in _as_list(rc.get("inputAssignments"))
            if isinstance(a, dict)
        ],
    }


def _normalize_assignment(a: dict[str, Any]) -> dict[str, Any]:
    """Flatten <value><stringValue>X</stringValue></value> into one literal."""
    value = a.get("value")
    literal: Any = None
    if isinstance(value, dict):
        # SF wraps literals as one of: stringValue, numberValue, booleanValue,
        # dateValue, dateTimeValue, elementReference (formula).
        for kind in ("stringValue", "numberValue", "booleanValue", "dateValue", "dateTimeValue"):
            if kind in value:
                literal = value[kind]
                break
        if literal is None:
            literal = value.get("elementReference")
    elif value is not None:
        literal = value
    return {"field": a.get("field", ""), "value": literal}


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
