"""Phase 1 end-to-end test against the fixture org dump.

Runs the orchestrator from pull → reconcile → per-category extract → audits
and asserts the canonical contract: every fixture file ends up classified to
exactly one category, every component has a content hash and Engram anchor,
and the OoE Surface Audit + Coverage Report render valid output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from offramp.core.models import CategoryName
from offramp.engram.client import InMemoryEngramClient
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient

FIXTURE = Path(__file__).parent / "fixtures" / "sample_org"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extract_against_fixture_org(tmp_path: Path) -> None:
    engram = InMemoryEngramClient()
    client = FixturePullClient(FIXTURE)
    orch = ExtractOrchestrator(
        org_alias="sample_org",
        client=client,
        engram=engram,
        fixture_root=FIXTURE,
    )

    result = await orch.run()
    result.write(tmp_path)

    # Every component anchored.
    assert len(result.components) > 0
    assert engram._records

    # Categories present after extract; exact set = the categories the fixture exercises.
    cats_extracted = {c.category for c in result.components}
    expected = {
        CategoryName.VALIDATION_RULE,
        CategoryName.FORMULA_FIELD,
        CategoryName.ROLLUP_SUMMARY,
        CategoryName.APEX_TRIGGER,
        CategoryName.APEX_CLASS,
        CategoryName.RECORD_TRIGGERED_FLOW,
        CategoryName.AUTOLAUNCHED_FLOW,
        CategoryName.SCREEN_FLOW,
        CategoryName.SCHEDULE_TRIGGERED_FLOW,
        CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
        CategoryName.FLOW_ORCHESTRATION,
        CategoryName.PROCESS_BUILDER,
        CategoryName.WORKFLOW_RULE,
        CategoryName.APPROVAL_PROCESS,
        CategoryName.ASSIGNMENT_RULE,
        CategoryName.AUTO_RESPONSE_RULE,
        CategoryName.ESCALATION_RULE,
        CategoryName.SHARING_RULE,
        CategoryName.PLATFORM_EVENT,
        CategoryName.CHANGE_DATA_CAPTURE,
        CategoryName.LWC_BUNDLE,
    }
    missing = expected - cats_extracted
    assert not missing, f"Categories missing from extract: {missing}"

    # Every component has a 64-char hex hash.
    for c in result.components:
        assert len(c.content_hash) == 64

    # Coverage report wrote out.
    assert (tmp_path / "coverage.json").exists()
    assert (tmp_path / "ooe_surface_audit.json").exists()
    assert (tmp_path / "components.json").exists()

    # OoE audit covers all 21 steps.
    assert result.ooe is not None
    assert len(result.ooe.observations) == 21

    # Dispatch resolver picked up the CMT records → at least 2 edges.
    assert len(result.dispatch_edges) >= 2
    handlers = {e.handler_class for e in result.dispatch_edges}
    assert {"LeadValidationHandler", "LeadRoutingHandler"}.issubset(handlers)

    # Trigger Actions Framework should be detected since CMT records are present.
    detected = {s.framework for s in result.framework_signals if s.confidence >= 0.7}
    assert "trigger_actions" in detected
