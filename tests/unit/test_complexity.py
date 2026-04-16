"""Complexity scoring — band invariants + driver-tracking."""

from __future__ import annotations

from offramp.core.models import CategoryName, Component, Provenance
from offramp.understand.complexity import score


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


def test_validation_rule_baseline_low() -> None:
    s = score(_component(CategoryName.VALIDATION_RULE))
    assert s.translation_difficulty < 35
    assert "baseline for validation_rule" in s.drivers[0]


def test_long_formula_increases_difficulty() -> None:
    short = score(
        _component(
            CategoryName.VALIDATION_RULE,
            {"error_condition_formula": "ISBLANK(Industry)"},
        )
    )
    long_formula = "AND(" + " OR ".join(f"ISPICKVAL(F__c, '{i}')" for i in range(50)) + ")"
    longer = score(
        _component(
            CategoryName.VALIDATION_RULE,
            {"error_condition_formula": long_formula},
        )
    )
    assert longer.translation_difficulty > short.translation_difficulty
    assert any("formula" in d for d in longer.drivers)


def test_business_logic_heavy_lwc_scores_higher() -> None:
    ui = score(_component(CategoryName.LWC_BUNDLE, {"classification": "ui_only"}))
    heavy = score(
        _component(
            CategoryName.LWC_BUNDLE,
            {"classification": "business_logic_heavy", "apex_imports": ["A.x", "B.y", "C.z"]},
        )
    )
    assert heavy.translation_difficulty > ui.translation_difficulty


def test_approval_process_has_high_baseline_risk() -> None:
    s = score(_component(CategoryName.APPROVAL_PROCESS))
    assert s.migration_risk >= 80
