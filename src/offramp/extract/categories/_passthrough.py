"""Passthrough extractors for categories awaiting full implementation.

Each one parses the XML payload via the shared utility but does not yet apply
category-specific field normalization. They satisfy the
:class:`CategoryExtractor` contract so the orchestrator + coverage audit work
end-to-end against the fixture org while the per-category translator backlog
is filled in.

Adding the real semantic shape for one of these is a focused change: pick the
class, replace ``parse_payload`` with the canonical mapping (compare to
:mod:`offramp.extract.categories.validation_rule` for the pattern), and add a
fixture exercising the new fields.
"""

from __future__ import annotations

from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor, register
from offramp.extract.categories.xml_utils import parse_xml
from offramp.extract.pull.reconciler import ReconciledRecord


class _XmlPassthroughExtractor(CategoryExtractor):
    """Parse XML to dict; record the file path; flag as ``passthrough=True``.

    The ``passthrough`` flag is consumed by the coverage audit to surface
    "category extracted but not yet semantically normalized" as a known gap
    rather than a silent loss.
    """

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        raw = record.payload.get("raw_xml", "")
        parsed: dict[str, Any] = {}
        if raw:
            try:
                parsed = parse_xml(raw)
            except Exception as exc:
                parsed = {"_parse_error": str(exc)}
        return {
            "passthrough": True,
            "path": record.payload.get("path"),
            "parsed_xml": parsed,
        }


@register
class ApexClassExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.APEX_CLASS


@register
class WorkflowRuleExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.WORKFLOW_RULE


@register
class ApprovalProcessExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.APPROVAL_PROCESS


@register
class AssignmentRuleExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.ASSIGNMENT_RULE


@register
class AutoResponseRuleExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.AUTO_RESPONSE_RULE


@register
class EscalationRuleExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.ESCALATION_RULE


@register
class SharingRuleExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.SHARING_RULE


@register
class RollupSummaryExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.ROLLUP_SUMMARY


@register
class PlatformEventExtractor(_XmlPassthroughExtractor):
    category: ClassVar[CategoryName] = CategoryName.PLATFORM_EVENT


@register
class ChangeDataCaptureExtractor(_XmlPassthroughExtractor):
    """CDC subscriptions are JSON, not XML — handled by special-case parser.

    The fixture client stores the JSON dump under ``payload['raw_xml']`` for
    interface uniformity; the parse_xml call will fail and the parse_error
    field carries the diagnostic.
    """

    category: ClassVar[CategoryName] = CategoryName.CHANGE_DATA_CAPTURE


# LWC bundles get their own real extractor in extract.lwc — register a tiny
# adapter here so the dispatch table is complete.
from offramp.extract.lwc.bundle import LWCBundleExtractor  # noqa: E402

register(LWCBundleExtractor)
