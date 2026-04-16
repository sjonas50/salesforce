"""OoE runtime — validation short-circuit + multi-rule aggregation."""

from __future__ import annotations

import pytest

from offramp.runtime.ooe.state_machine import OoERuntime, ValidationFailedError

pytestmark = pytest.mark.ooe


def test_passing_validation_completes(make_runtime, helpers) -> None:
    rt: OoERuntime = make_runtime(
        [helpers.validation("Account.HasName", "Account", lambda r, c: r.get("Name") in (None, ""))]
    )
    ctx = rt.execute_save(sobject="Account", record={"Name": "Acme"})
    assert not ctx.aborted
    assert all(rr.passed for rr in ctx.rule_results)


def test_failing_validation_aborts(make_runtime, helpers) -> None:
    rt: OoERuntime = make_runtime(
        [helpers.validation("Account.HasName", "Account", lambda r, c: not r.get("Name"))]
    )
    with pytest.raises(ValidationFailedError):
        rt.execute_save(sobject="Account", record={"Name": ""})


def test_multiple_validations_all_collected_before_raising(make_runtime, helpers) -> None:
    rt: OoERuntime = make_runtime(
        [
            helpers.validation("Account.HasName", "Account", lambda r, c: not r.get("Name")),
            helpers.validation(
                "Account.HasIndustry", "Account", lambda r, c: not r.get("Industry")
            ),
        ]
    )
    with pytest.raises(ValidationFailedError) as ei:
        rt.execute_save(sobject="Account", record={})
    rule_ids = sorted(rr.rule_id for rr in ei.value.results)
    assert rule_ids == ["Account.HasIndustry", "Account.HasName"]


def test_validation_failure_does_not_silently_pass(make_runtime, helpers) -> None:
    """Regression guard: a rule that returns truthy MUST be treated as failing."""
    rt: OoERuntime = make_runtime([helpers.validation("Always", "Account", lambda r, c: True)])
    with pytest.raises(ValidationFailedError):
        rt.execute_save(sobject="Account", record={"Name": "Acme"})


def test_unrelated_sobject_rules_do_not_fire(make_runtime, helpers) -> None:
    rt: OoERuntime = make_runtime(
        [helpers.validation("Lead.HasEmail", "Lead", lambda r, c: not r.get("Email"))]
    )
    ctx = rt.execute_save(sobject="Account", record={"Name": "Acme"})
    assert not ctx.aborted
    assert ctx.rule_results == []


def test_rule_exception_is_a_validation_failure(make_runtime, helpers) -> None:
    def boom(r, c):
        raise KeyError("boom")

    rt: OoERuntime = make_runtime([helpers.validation("Boom", "Account", boom)])
    with pytest.raises(ValidationFailedError) as ei:
        rt.execute_save(sobject="Account", record={"Name": "X"})
    assert "raised KeyError" in (ei.value.results[0].error_message or "")
