"""Shared fixtures for the OoE runtime test suite (§18.3 of v2.1 plan).

Phase 3 ships ~30 cases covering re-fire, cascade, mixed-DML, and validation
short-circuiting. Target is 200+ over the build-out as more categories come
online.
"""

from __future__ import annotations

from typing import Any

import pytest

from offramp.extract.ooe_audit.audit import OoEStep
from offramp.runtime.ooe.state_machine import OoERuntime
from offramp.runtime.rules.engine import Rule, RulesEngine


def _validation(rule_id: str, sobject: str, fn) -> Rule:
    return Rule(
        rule_id=rule_id,
        sobject=sobject,
        ooe_step=int(OoEStep.CUSTOM_VALIDATION),
        fn=fn,
        kind="validation",
        error_message_template=f"{rule_id} failed",
    )


def _computation(rule_id: str, sobject: str, fixes_field: str, fn, step: OoEStep) -> Rule:
    return Rule(
        rule_id=rule_id,
        sobject=sobject,
        ooe_step=int(step),
        fn=fn,
        kind="computation",
        fixes_field=fixes_field,
    )


@pytest.fixture
def make_runtime():
    """Return a factory producing OoERuntime instances with custom rule sets."""

    def _factory(rules: list[Rule], **kwargs: Any) -> OoERuntime:
        engine = RulesEngine()
        for r in rules:
            engine.register(r)
        return OoERuntime(rules=engine, **kwargs)

    return _factory


@pytest.fixture
def helpers():
    """Convenience constructors so the cases are dense."""

    class H:
        validation = staticmethod(_validation)
        computation = staticmethod(_computation)

    return H()
