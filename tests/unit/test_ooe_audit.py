"""OoE Surface Audit — bounded-scope correctness."""

from __future__ import annotations

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.ooe_audit.audit import OoEStep, audit


def _component(category: CategoryName, name: str) -> Component:
    return Component(
        org_alias="t",
        category=category,
        name=name,
        api_name=name,
        content_hash="0" * 64,
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
    )


def test_audit_excludes_unexercised_steps() -> None:
    components = [
        _component(CategoryName.VALIDATION_RULE, "v1"),
        _component(CategoryName.APEX_TRIGGER, "t1"),
    ]
    report = audit(components, "fisher")
    by_step = {o.step: o for o in report.observations}
    # Custom validation is exercised → in scope.
    assert by_step[OoEStep.CUSTOM_VALIDATION].in_scope
    # Escalation rules unused → excluded.
    assert by_step[OoEStep.ESCALATION_RULES].in_scope is False
    assert by_step[OoEStep.ESCALATION_RULES].priority == "exclude"


def test_audit_marks_high_volume_step_critical() -> None:
    components = [_component(CategoryName.VALIDATION_RULE, f"v{i}") for i in range(15)]
    report = audit(components, "fisher")
    by_step = {o.step: o for o in report.observations}
    assert by_step[OoEStep.CUSTOM_VALIDATION].priority == "critical"


def test_audit_uses_observed_frequency_when_provided() -> None:
    components = [_component(CategoryName.VALIDATION_RULE, "v1")]
    report = audit(
        components,
        "fisher",
        observed_frequency_by_step={OoEStep.CUSTOM_VALIDATION: 0},
    )
    by_step = {o.step: o for o in report.observations}
    # Even though it exists structurally, observed frequency 0 → exclude.
    assert by_step[OoEStep.CUSTOM_VALIDATION].in_scope is False


def test_report_has_21_observations() -> None:
    report = audit([], "fisher")
    assert len(report.observations) == 21
