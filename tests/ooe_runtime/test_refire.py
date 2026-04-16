"""OoE runtime — workflow re-fire cycle (step 12 → re-execute steps 5+9 ONCE)."""

from __future__ import annotations

import pytest

from offramp.extract.ooe_audit.audit import OoEStep
from offramp.runtime.ooe.state_machine import OoERuntime
from offramp.runtime.rules.engine import RulesEngine

pytestmark = pytest.mark.ooe


def _make_engine_with_calls(calls: list[str]) -> RulesEngine:
    """Build an engine whose every rule appends a marker to ``calls``."""
    from offramp.runtime.rules.engine import Rule

    engine = RulesEngine()

    def before_trigger(record, ctx):
        calls.append("before_trigger")
        return None

    def after_trigger(record, ctx):
        calls.append("after_trigger")
        return None

    def workflow_field_update(record, ctx):
        calls.append("workflow_field_update")
        # Returning a value triggers field_mutations on a computation rule.
        return "X"

    engine.register(
        Rule(
            rule_id="bt",
            sobject="Account",
            ooe_step=int(OoEStep.BEFORE_TRIGGERS),
            fn=before_trigger,
            kind="computation",
            fixes_field=None,
        )
    )
    engine.register(
        Rule(
            rule_id="at",
            sobject="Account",
            ooe_step=int(OoEStep.AFTER_TRIGGERS),
            fn=after_trigger,
            kind="computation",
            fixes_field=None,
        )
    )
    engine.register(
        Rule(
            rule_id="wfu",
            sobject="Account",
            ooe_step=int(OoEStep.WORKFLOW_RULES),
            fn=workflow_field_update,
            kind="computation",
            fixes_field="Status",
        )
    )
    return engine


def test_workflow_field_update_re_fires_triggers_exactly_once() -> None:
    calls: list[str] = []
    rt = OoERuntime(rules=_make_engine_with_calls(calls))
    rt.execute_save(sobject="Account", record={"Name": "Acme"})
    # bt fires twice (initial + re-fire), at fires twice, wfu fires once.
    assert calls.count("before_trigger") == 2
    assert calls.count("after_trigger") == 2
    assert calls.count("workflow_field_update") == 1


def test_refire_flag_clears_after_one_cycle() -> None:
    """Even if WFU fires *during* the re-fire pass, no second re-fire happens."""
    from offramp.runtime.rules.engine import Rule

    engine = RulesEngine()
    refire_attempts: list[int] = []

    def wfu_always_mutates(record, ctx):
        refire_attempts.append(1)
        return "Status_Updated"

    engine.register(
        Rule(
            rule_id="wfu",
            sobject="Account",
            ooe_step=int(OoEStep.WORKFLOW_RULES),
            fn=wfu_always_mutates,
            kind="computation",
            fixes_field="Status",
        )
    )
    rt = OoERuntime(rules=engine)
    ctx = rt.execute_save(sobject="Account", record={"Name": "Acme"})
    assert ctx.refire_done is True
    assert ctx.workflow_refire_flag is False  # cleared after the cycle
    # WFU itself only ran once (re-fire only re-runs steps 5+9, not 12).
    assert len(refire_attempts) == 1
