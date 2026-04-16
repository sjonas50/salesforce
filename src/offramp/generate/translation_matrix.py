"""Translation matrix: category x signal -> execution tier.

The matrix is a single source of truth consumed by:
* the per-component classifier (which translator to invoke)
* the X-Ray report (recommended_tier preview)
* the dual-target generator (boundary detection)
* the pre-commit ``check_matrix_fixtures.py`` guard

Adding a category or changing a tier assignment MUST be paired with a
fixture update — see ``scripts/check_matrix_fixtures.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from offramp.core.models import CategoryName, Component, Tier


@dataclass(frozen=True)
class TierAssignment:
    """One tier classification with the evidence that drove it."""

    tier: Tier
    confidence: float  # 0.0-1.0
    drivers: tuple[str, ...]


# Default category → tier assignment. The five-signal override matrix in
# v2.1 §5.4 (durability requirement, determinism, external context dep,
# branching complexity, natural-language input) is applied per-component on
# top of these defaults.
_DEFAULTS: dict[CategoryName, Tier] = {
    CategoryName.VALIDATION_RULE: Tier.TIER1_RULES,
    CategoryName.FORMULA_FIELD: Tier.TIER1_RULES,
    CategoryName.WORKFLOW_RULE: Tier.TIER1_RULES,
    CategoryName.ASSIGNMENT_RULE: Tier.TIER1_RULES,
    CategoryName.AUTO_RESPONSE_RULE: Tier.TIER2_TEMPORAL,
    CategoryName.SHARING_RULE: Tier.TIER1_RULES,
    CategoryName.ROLLUP_SUMMARY: Tier.TIER1_RULES,
    CategoryName.AUTOLAUNCHED_FLOW: Tier.TIER1_RULES,
    CategoryName.RECORD_TRIGGERED_FLOW: Tier.TIER1_RULES,
    CategoryName.SCREEN_FLOW: Tier.TIER3_LANGGRAPH,
    CategoryName.SCHEDULE_TRIGGERED_FLOW: Tier.TIER2_TEMPORAL,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: Tier.TIER2_TEMPORAL,
    CategoryName.FLOW_ORCHESTRATION: Tier.TIER2_TEMPORAL,
    CategoryName.PROCESS_BUILDER: Tier.TIER1_RULES,
    CategoryName.APEX_TRIGGER: Tier.TIER1_RULES,
    CategoryName.APEX_CLASS: Tier.TIER2_TEMPORAL,
    CategoryName.APPROVAL_PROCESS: Tier.TIER2_TEMPORAL,
    CategoryName.ESCALATION_RULE: Tier.TIER2_TEMPORAL,
    CategoryName.PLATFORM_EVENT: Tier.TIER2_TEMPORAL,
    CategoryName.CHANGE_DATA_CAPTURE: Tier.TIER2_TEMPORAL,
    CategoryName.LWC_BUNDLE: Tier.TIER3_LANGGRAPH,
}


def classify(component: Component) -> TierAssignment:
    """Apply the matrix + the v2.1 §5.4 override signals."""
    base = _DEFAULTS.get(component.category, Tier.TIER1_RULES)
    drivers: list[str] = [f"baseline for {component.category.value}: {base.value}"]
    raw = component.raw if isinstance(component.raw, dict) else {}

    # Signal 1 — durability requirement (callouts, scheduled, human waits)
    has_callouts = bool(raw.get("actionCalls") or raw.get("subflows"))
    has_screens = bool(raw.get("screens"))
    if has_callouts and base is Tier.TIER1_RULES:
        return TierAssignment(
            tier=Tier.TIER2_TEMPORAL,
            confidence=0.85,
            drivers=tuple([*drivers, "callouts/subflows present → upgraded to Tier 2"]),
        )

    # Signal 4 — branching complexity
    decisions = raw.get("decisions", [])
    if isinstance(decisions, list) and len(decisions) >= 20:
        return TierAssignment(
            tier=Tier.TIER3_LANGGRAPH,
            confidence=0.7,
            drivers=tuple([*drivers, f"{len(decisions)} decision nodes → Tier 3 candidate"]),
        )

    # Signal 5 — natural-language input (LWC business-logic-heavy + screens)
    if has_screens or raw.get("classification") == "business_logic_heavy":
        return TierAssignment(
            tier=Tier.TIER3_LANGGRAPH,
            confidence=0.75,
            drivers=tuple([*drivers, "screen/UI surface → Tier 3"]),
        )

    return TierAssignment(tier=base, confidence=0.9, drivers=tuple(drivers))


def is_dual_target_candidate(component: Component) -> bool:
    """Boundary detection for §9.3.1 dual-target generation.

    True when the component sits near the Tier 1 / Tier 2 boundary — emit
    both a Python rule and a Temporal wrapper; defer the deployment choice.
    """
    base = _DEFAULTS.get(component.category)
    return base in {Tier.TIER1_RULES, Tier.TIER2_TEMPORAL} and component.category in {
        CategoryName.AUTOLAUNCHED_FLOW,
        CategoryName.RECORD_TRIGGERED_FLOW,
        CategoryName.WORKFLOW_RULE,
        CategoryName.AUTO_RESPONSE_RULE,
    }
