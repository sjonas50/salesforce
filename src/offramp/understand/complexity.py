"""Two-axis complexity scoring (architecture §C5 / build-plan 2.4).

Each component is scored on:

* **translation difficulty** — how hard is it to generate correct external code
* **migration risk** — what happens if the translation is subtly wrong

Heuristic and deterministic. The LLM annotation runs separately and produces
its own complexity *band* (low/med/high); the two are independent signals
that the X-Ray report cross-references.
"""

from __future__ import annotations

from dataclasses import dataclass

from offramp.core.models import CategoryName, Component


@dataclass(frozen=True)
class ComplexityScore:
    """Both axes scored 0-100. Higher = harder / riskier."""

    component_id: str
    translation_difficulty: int
    migration_risk: int
    drivers: tuple[str, ...]  # human-readable reasons that contributed


# Baseline difficulty per category. Tuned from the v2.1 plan's tier mapping +
# externally-observed translation effort. These are starting points; the
# X-Ray engagement adjusts per-org based on observed Compare Mode bug density.
_BASE_DIFFICULTY: dict[CategoryName, int] = {
    CategoryName.VALIDATION_RULE: 20,
    CategoryName.FORMULA_FIELD: 35,  # AD-19: formula audit gap
    CategoryName.WORKFLOW_RULE: 30,
    CategoryName.ROLLUP_SUMMARY: 40,
    CategoryName.AUTOLAUNCHED_FLOW: 50,
    CategoryName.RECORD_TRIGGERED_FLOW: 55,
    CategoryName.SCREEN_FLOW: 65,
    CategoryName.SCHEDULE_TRIGGERED_FLOW: 50,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: 55,
    CategoryName.PROCESS_BUILDER: 60,
    CategoryName.FLOW_ORCHESTRATION: 75,
    CategoryName.APEX_TRIGGER: 70,
    CategoryName.APEX_CLASS: 65,
    CategoryName.APPROVAL_PROCESS: 80,
    CategoryName.ASSIGNMENT_RULE: 25,
    CategoryName.AUTO_RESPONSE_RULE: 25,
    CategoryName.ESCALATION_RULE: 60,
    CategoryName.SHARING_RULE: 70,
    CategoryName.PLATFORM_EVENT: 30,
    CategoryName.CHANGE_DATA_CAPTURE: 40,
    CategoryName.LWC_BUNDLE: 50,
}

# Baseline migration risk per category — what's the blast radius of a wrong
# translation? Approval Processes touch revenue; LWC UI bugs are visible but
# survivable.
_BASE_RISK: dict[CategoryName, int] = {
    CategoryName.VALIDATION_RULE: 50,  # silent-bypass = bad data
    CategoryName.FORMULA_FIELD: 70,  # silent-wrong = bad numbers (AD-19)
    CategoryName.WORKFLOW_RULE: 40,
    CategoryName.ROLLUP_SUMMARY: 55,
    CategoryName.AUTOLAUNCHED_FLOW: 45,
    CategoryName.RECORD_TRIGGERED_FLOW: 60,
    CategoryName.SCREEN_FLOW: 30,  # UI; user-visible failure is fast-feedback
    CategoryName.SCHEDULE_TRIGGERED_FLOW: 50,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: 55,
    CategoryName.PROCESS_BUILDER: 55,
    CategoryName.FLOW_ORCHESTRATION: 70,
    CategoryName.APEX_TRIGGER: 75,
    CategoryName.APEX_CLASS: 60,
    CategoryName.APPROVAL_PROCESS: 90,  # stuck approvals halt revenue
    CategoryName.ASSIGNMENT_RULE: 40,
    CategoryName.AUTO_RESPONSE_RULE: 25,
    CategoryName.ESCALATION_RULE: 65,
    CategoryName.SHARING_RULE: 80,  # data leakage if wrong
    CategoryName.PLATFORM_EVENT: 50,
    CategoryName.CHANGE_DATA_CAPTURE: 50,
    CategoryName.LWC_BUNDLE: 35,
}


def score(component: Component) -> ComplexityScore:
    """Score one component."""
    cat = component.category
    difficulty = _BASE_DIFFICULTY.get(cat, 50)
    risk = _BASE_RISK.get(cat, 50)
    drivers: list[str] = [f"baseline for {cat.value}"]

    raw = component.raw if isinstance(component.raw, dict) else {}

    # Flow-family modifiers — count decision nodes, DML ops, subflows.
    if cat.value.endswith("_flow") or cat is CategoryName.PROCESS_BUILDER:
        decisions = _count(raw, "decisions")
        record_updates = _count(raw, "record_updates")
        record_creates = _count(raw, "record_creates")
        subflows = _count(raw, "subflows")
        screens = _count(raw, "screens")
        if decisions >= 20:
            difficulty += 20
            drivers.append(f"{decisions} decision nodes (>=20)")
        elif decisions >= 8:
            difficulty += 8
            drivers.append(f"{decisions} decision nodes")
        if record_updates + record_creates >= 5:
            difficulty += 10
            risk += 10
            drivers.append(f"{record_updates + record_creates} DML operations")
        if subflows:
            difficulty += 5 * min(subflows, 4)
            drivers.append(f"{subflows} subflow references")
        if screens and cat is CategoryName.SCREEN_FLOW:
            risk -= 5  # UI screens lower the silent-failure risk somewhat

    # Apex modifiers — body length proxies cyclomatic complexity.
    if cat is CategoryName.APEX_TRIGGER:
        lines = int(raw.get("body_lines", 0) or 0)
        events = raw.get("events", [])
        if lines >= 200:
            difficulty += 20
            drivers.append(f"{lines}-line trigger body")
        if isinstance(events, list) and len(events) >= 3:
            difficulty += 10
            drivers.append(f"{len(events)} trigger events")

    # LWC: business-logic-heavy bundles raise difficulty.
    if cat is CategoryName.LWC_BUNDLE:
        classification = raw.get("classification", "")
        if classification == "business_logic_heavy":
            difficulty += 25
            drivers.append("business-logic-heavy LWC")
        elif classification == "mixed":
            difficulty += 10
            drivers.append("mixed LWC")
        n_imports = len(raw.get("apex_imports", []))
        if n_imports >= 3:
            difficulty += 5
            drivers.append(f"{n_imports} Apex imports")

    # Validation rule: long formulas are riskier to translate.
    if cat is CategoryName.VALIDATION_RULE:
        formula = raw.get("error_condition_formula", "") or ""
        if len(formula) > 200:
            difficulty += 15
            risk += 10
            drivers.append(f"{len(formula)}-char formula")

    # Formula field: long formulas + cross-object refs (AD-19 audit territory).
    if cat is CategoryName.FORMULA_FIELD:
        formula = raw.get("formula", "") or ""
        if "." in formula and any(s.isupper() for s in formula.split(".")[0]):
            difficulty += 10
            drivers.append("cross-object reference in formula")
        if len(formula) > 300:
            difficulty += 15
            drivers.append(f"{len(formula)}-char formula")

    return ComplexityScore(
        component_id=str(component.id),
        translation_difficulty=_clamp(difficulty),
        migration_risk=_clamp(risk),
        drivers=tuple(drivers),
    )


def _count(raw: dict[str, object], key: str) -> int:
    val = raw.get(key)
    if isinstance(val, list):
        return len(val)
    if val:
        return 1
    return 0


def _clamp(v: int) -> int:
    return max(0, min(100, v))


def score_all(components: list[Component]) -> dict[str, ComplexityScore]:
    """Map component_id → ComplexityScore for the whole corpus."""
    return {str(c.id): score(c) for c in components}
