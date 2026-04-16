"""OoE runtime — once-per-entity-per-transaction Flow rule + scope enforcement."""

from __future__ import annotations

import pytest

from offramp.extract.ooe_audit.audit import OoEStep
from offramp.runtime.ooe.state_machine import (
    OoERuntime,
    StepNotInScopeError,
    TransactionContext,
)
from offramp.runtime.rules.engine import Rule, RulesEngine

pytestmark = pytest.mark.ooe


def test_flow_fires_only_once_per_record_per_transaction() -> None:
    calls: list[str] = []
    engine = RulesEngine()
    engine.register(
        Rule(
            rule_id="LeadFlow",
            sobject="Lead",
            ooe_step=int(OoEStep.PROCESSES_AND_FLOWS),
            fn=lambda r, c: calls.append(r.get("Id", "?")),
            kind="computation",
            fixes_field=None,
        )
    )
    rt = OoERuntime(rules=engine)
    ctx = TransactionContext(
        transaction_id="t",
        triggering_record={},
        sobject="Lead",
    )
    rt.execute_save(sobject="Lead", record={"Id": "001"}, parent_ctx=ctx)
    rt.execute_save(sobject="Lead", record={"Id": "001"}, parent_ctx=ctx)
    rt.execute_save(sobject="Lead", record={"Id": "002"}, parent_ctx=ctx)
    # 001 fires once, 002 fires once; the second 001 attempt is suppressed.
    assert calls == ["001", "002"]


def test_runtime_excluded_step_raises() -> None:
    """Surface Audit excluded a step → runtime refuses to execute at it."""
    engine = RulesEngine()
    rt = OoERuntime(
        rules=engine,
        in_scope_steps={OoEStep.BEFORE_TRIGGERS},  # custom_validation EXCLUDED
    )
    with pytest.raises(StepNotInScopeError):
        rt.execute_save(sobject="Account", record={"Name": "A"})


def test_in_scope_step_with_no_rules_is_a_noop() -> None:
    rt = OoERuntime(rules=RulesEngine())
    ctx = rt.execute_save(sobject="Account", record={"Name": "A"})
    assert not ctx.aborted
    assert ctx.rule_results == []


def test_trace_includes_diagnostic_keys() -> None:
    rt = OoERuntime(rules=RulesEngine())
    ctx = rt.execute_save(sobject="Account", record={"Name": "A"})
    trace = ctx.trace()
    assert any(line.startswith("txn=") for line in trace)
    assert any("aborted=False" in line for line in trace)


def test_non_deterministic_ordering_is_reproducible_within_seed() -> None:
    """Two runs with the same seed see the same shuffled order."""
    engine = RulesEngine()
    log: list[str] = []
    for i in range(5):
        engine.register(
            Rule(
                rule_id=f"r{i}",
                sobject="Account",
                ooe_step=int(OoEStep.AFTER_TRIGGERS),
                fn=lambda r, c, name=f"r{i}": log.append(name),  # type: ignore[misc]
                kind="computation",
            )
        )
    rt = OoERuntime(rules=engine, seed=99)
    rt.execute_save(sobject="Account", record={"Id": "X"}, transaction_id="fixed-txn")
    first = list(log)
    log.clear()
    rt.execute_save(sobject="Account", record={"Id": "X"}, transaction_id="fixed-txn")
    second = list(log)
    assert first == second
