"""Phase 0.7: shared Pydantic model contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from offramp.core.models import (
    AST,
    CategoryName,
    Component,
    Dependency,
    DependencyKind,
    DivergenceCategory,
    Provenance,
    RoutingDecision,
    ShadowComparison,
    Tier,
    TranslationArtifact,
)


def _provenance() -> Provenance:
    return Provenance(source_tool="sf_cli", source_version="2.42.0", api_version="66.0")


def test_component_round_trips_through_json() -> None:
    c = Component(
        org_alias="dev",
        category=CategoryName.VALIDATION_RULE,
        name="Account_Required_Industry",
        api_name="Account.Account_Required_Industry",
        raw={"errorMessage": "Industry required"},
        content_hash="a" * 64,
        provenance=_provenance(),
    )
    blob = c.model_dump_json()
    reloaded = Component.model_validate_json(blob)
    assert reloaded == c


def test_component_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Component.model_validate(
            {
                "org_alias": "dev",
                "category": "validation_rule",
                "name": "X",
                "content_hash": "a" * 64,
                "provenance": _provenance().model_dump(mode="json"),
                "made_up_extra_field": True,
            }
        )


def test_dependency_confidence_bounded() -> None:
    with pytest.raises(ValidationError):
        Dependency(
            source_id=Component(
                org_alias="d",
                category=CategoryName.APEX_CLASS,
                name="A",
                content_hash="a" * 64,
                provenance=_provenance(),
            ).id,
            target_id=Component(
                org_alias="d",
                category=CategoryName.APEX_TRIGGER,
                name="B",
                content_hash="b" * 64,
                provenance=_provenance(),
            ).id,
            kind=DependencyKind.DISPATCHES,
            confidence=1.5,
        )


def test_ast_attaches_to_component() -> None:
    c = Component(
        org_alias="d",
        category=CategoryName.APEX_CLASS,
        name="LeadHandler",
        content_hash="c" * 64,
        provenance=_provenance(),
    )
    ast = AST(
        component_id=c.id,
        parser="summit-ast",
        parser_version="1.0.0",
        tree={"nodeType": "ApexClass", "name": "LeadHandler"},
    )
    assert ast.component_id == c.id


def test_translation_artifact_tier_enum() -> None:
    component_id = Component(
        org_alias="d",
        category=CategoryName.VALIDATION_RULE,
        name="V",
        content_hash="d" * 64,
        provenance=_provenance(),
    ).id
    a = TranslationArtifact(
        component_id=component_id,
        tier=Tier.TIER1_RULES,
        code_path="rules/v.py",
        code_hash="e" * 64,
        translator_version="0.1.0",
    )
    assert a.tier is Tier.TIER1_RULES
    assert not a.is_dual_target


def test_shadow_comparison_seven_categories() -> None:
    """AD-22: gap_event_full_refetch_required must be a recognized category."""
    assert DivergenceCategory.GAP_EVENT_FULL_REFETCH_REQUIRED in set(DivergenceCategory)
    assert len(set(DivergenceCategory)) == 7


def test_shadow_comparison_records_field_diff() -> None:
    sc = ShadowComparison(
        process_id=Component(
            org_alias="d",
            category=CategoryName.RECORD_TRIGGERED_FLOW,
            name="LeadRouting",
            content_hash="f" * 64,
            provenance=_provenance(),
        ).id,
        cdc_event_replay_id="0042000",
        diverged=True,
        category=DivergenceCategory.OOE_ORDERING_MISMATCH,
        field_diffs={"Status": ("Approved", "Pending")},
    )
    assert sc.diverged
    assert sc.field_diffs["Status"] == ("Approved", "Pending")


def test_routing_decision_pattern_enforced() -> None:
    pid = Component(
        org_alias="d",
        category=CategoryName.RECORD_TRIGGERED_FLOW,
        name="LR",
        content_hash="0" * 64,
        provenance=_provenance(),
    ).id
    with pytest.raises(ValidationError):
        RoutingDecision(
            process_id=pid,
            record_id="00Q000000000001",
            routed_to="elsewhere",  # only salesforce|runtime allowed
            stage_percent=25,
            engram_anchor="engram:abc",
        )


def test_category_enum_has_21_entries() -> None:
    """v2.1 reference defines 21 automation categories."""
    assert len(set(CategoryName)) == 21
