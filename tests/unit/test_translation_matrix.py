"""Translation matrix — defaults + override-signal application."""

from __future__ import annotations

from offramp.core.models import CategoryName, Component, Provenance, Tier
from offramp.generate.translation_matrix import classify, is_dual_target_candidate


def _component(category: CategoryName, raw: dict[str, object] | None = None) -> Component:
    return Component(
        org_alias="t",
        category=category,
        name="X",
        api_name="X",
        raw=raw or {},
        content_hash="0" * 64,
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
    )


def test_validation_rule_is_tier_1_by_default() -> None:
    assert classify(_component(CategoryName.VALIDATION_RULE)).tier is Tier.TIER1_RULES


def test_approval_process_is_tier_2() -> None:
    assert classify(_component(CategoryName.APPROVAL_PROCESS)).tier is Tier.TIER2_TEMPORAL


def test_screen_flow_is_tier_3() -> None:
    assert classify(_component(CategoryName.SCREEN_FLOW)).tier is Tier.TIER3_LANGGRAPH


def test_callout_signal_promotes_tier_1_flow_to_tier_2() -> None:
    out = classify(_component(CategoryName.AUTOLAUNCHED_FLOW, {"actionCalls": [{"name": "x"}]}))
    assert out.tier is Tier.TIER2_TEMPORAL


def test_business_logic_heavy_lwc_is_tier_3() -> None:
    out = classify(_component(CategoryName.LWC_BUNDLE, {"classification": "business_logic_heavy"}))
    assert out.tier is Tier.TIER3_LANGGRAPH


def test_high_decision_count_promotes_to_tier_3() -> None:
    out = classify(_component(CategoryName.RECORD_TRIGGERED_FLOW, {"decisions": [{}] * 25}))
    assert out.tier is Tier.TIER3_LANGGRAPH


def test_dual_target_candidate_for_record_triggered_flow() -> None:
    assert is_dual_target_candidate(_component(CategoryName.RECORD_TRIGGERED_FLOW))


def test_dual_target_not_candidate_for_screen_flow() -> None:
    assert not is_dual_target_candidate(_component(CategoryName.SCREEN_FLOW))
