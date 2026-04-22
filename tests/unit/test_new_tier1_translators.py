"""Tests for the 4 new Tier 1 translators: workflow_rule, assignment_rule,
autolaunched_flow, process_builder."""

from __future__ import annotations

import types
from typing import Any

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.ooe_audit.audit import OoEStep
from offramp.generate.tier1 import translate


def _component(category: CategoryName, raw: dict[str, Any], name: str = "X") -> Component:
    return Component(
        org_alias="t",
        category=category,
        name=name,
        api_name=name,
        raw=raw,
        content_hash="0" * 64,
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
    )


def _exec(code: str) -> types.ModuleType:
    """Compile + exec generated code, return the resulting module object."""
    mod = types.ModuleType("generated_test")
    exec(code, mod.__dict__)
    return mod


# -- Workflow rule -----------------------------------------------------------


def test_workflow_rule_with_criteria_match_applies_field_update() -> None:
    raw = {
        "object": "Account",
        "rules": [
            {
                "name": "HighValueFlag",
                "active": True,
                "criteria_items": [
                    {
                        "field": "Account.AnnualRevenue",
                        "operation": "greaterThan",
                        "value": "1000000",
                    },
                ],
                "immediate_actions": [{"name": "MarkStrategic", "type": "FieldUpdate"}],
            }
        ],
        "field_updates": [
            {
                "name": "MarkStrategic",
                "field": "Account.IsStrategic__c",
                "literal_value": "true",
                "formula": None,
            }
        ],
    }
    gen = translate(_component(CategoryName.WORKFLOW_RULE, raw, "Account"))
    assert gen.ooe_step == int(OoEStep.WORKFLOW_RULES)
    mod = _exec(gen.code)
    out = getattr(mod, gen.function_name)({"AnnualRevenue": 2_000_000}, {})
    assert out == {"IsStrategic__c": "true"}


def test_workflow_rule_with_unmet_criteria_returns_none() -> None:
    raw = {
        "object": "Account",
        "rules": [
            {
                "name": "OnlyWhenHuge",
                "active": True,
                "criteria_items": [
                    {"field": "AnnualRevenue", "operation": "greaterThan", "value": "999999999"}
                ],
                "immediate_actions": [{"name": "FU1", "type": "FieldUpdate"}],
            }
        ],
        "field_updates": [{"name": "FU1", "field": "Flag__c", "literal_value": "x"}],
    }
    gen = translate(_component(CategoryName.WORKFLOW_RULE, raw, "Account"))
    mod = _exec(gen.code)
    assert getattr(mod, gen.function_name)({"AnnualRevenue": 100}, {}) is None


def test_workflow_rule_with_formula_guard_parses_and_evaluates() -> None:
    raw = {
        "object": "Lead",
        "rules": [
            {
                "name": "ByFormula",
                "active": True,
                "formula": "ISPICKVAL(Status, 'Open')",
                "criteria_items": [],
                "immediate_actions": [{"name": "Touch", "type": "FieldUpdate"}],
            }
        ],
        "field_updates": [{"name": "Touch", "field": "Touched__c", "literal_value": "yes"}],
    }
    gen = translate(_component(CategoryName.WORKFLOW_RULE, raw, "Lead"))
    mod = _exec(gen.code)
    # ISPICKVAL compares a picklist value — in our runtime helper that's
    # just equality on the record field.
    assert getattr(mod, gen.function_name)({"Status": "Open"}, {}) == {"Touched__c": "yes"}
    assert getattr(mod, gen.function_name)({"Status": "Closed"}, {}) is None


def test_inactive_workflow_rules_are_dropped() -> None:
    raw = {
        "object": "Account",
        "rules": [
            {"name": "Dormant", "active": False, "criteria_items": [], "immediate_actions": []}
        ],
        "field_updates": [],
    }
    gen = translate(_component(CategoryName.WORKFLOW_RULE, raw, "Account"))
    mod = _exec(gen.code)
    assert getattr(mod, gen.function_name)({}, {}) is None


def test_workflow_rule_with_non_field_update_action_emits_todo() -> None:
    raw = {
        "object": "Lead",
        "rules": [
            {
                "name": "NotifyOwner",
                "active": True,
                "criteria_items": [],
                "immediate_actions": [{"name": "SendAlert", "type": "Email"}],  # not Tier 1
            }
        ],
        "field_updates": [],
    }
    gen = translate(_component(CategoryName.WORKFLOW_RULE, raw, "Lead"))
    assert "TODO" in gen.code  # translator flags the Email action clearly


# -- Assignment rule ---------------------------------------------------------


def test_assignment_rule_first_entry_wins() -> None:
    raw = {
        "object": "Lead",
        "rule_groups": [
            {
                "name": "ByCountry",
                "active": True,
                "entries": [
                    {
                        "assigned_to": "US_Queue",
                        "assigned_to_type": "Queue",
                        "formula": None,
                        "criteria_items": [
                            {"field": "Country", "operation": "equals", "value": "US"}
                        ],
                    },
                    {
                        "assigned_to": "EU_Queue",
                        "assigned_to_type": "Queue",
                        "formula": None,
                        "criteria_items": [
                            {"field": "Country", "operation": "equals", "value": "EU"}
                        ],
                    },
                ],
            }
        ],
    }
    gen = translate(_component(CategoryName.ASSIGNMENT_RULE, raw, "Lead"))
    assert gen.ooe_step == int(OoEStep.ASSIGNMENT_RULES)
    mod = _exec(gen.code)
    assert getattr(mod, gen.function_name)({"Country": "US"}, {}) == {"OwnerId": "US_Queue"}
    assert getattr(mod, gen.function_name)({"Country": "EU"}, {}) == {"OwnerId": "EU_Queue"}
    assert getattr(mod, gen.function_name)({"Country": "AU"}, {}) is None


def test_assignment_rule_skips_inactive_groups() -> None:
    raw = {
        "object": "Lead",
        "rule_groups": [
            {
                "name": "Dormant",
                "active": False,
                "entries": [
                    {
                        "assigned_to": "X",
                        "assigned_to_type": "User",
                        "formula": None,
                        "criteria_items": [],
                    }
                ],
            }
        ],
    }
    gen = translate(_component(CategoryName.ASSIGNMENT_RULE, raw, "Lead"))
    mod = _exec(gen.code)
    assert getattr(mod, gen.function_name)({}, {}) is None


# -- Autolaunched flow -------------------------------------------------------


def test_autolaunched_flow_with_record_update_emits_assignments() -> None:
    raw = {
        "process_type": "AutoLaunchedFlow",
        "trigger_type": "",
        "object": "Lead",
        "record_updates": [
            {
                "name": "SetStatus",
                "input_reference": "$Record",
                "input_assignments": [{"field": "Status", "value": "Working"}],
                "filter_logic": "",
                "filters": [],
            }
        ],
        "record_creates": [],
        "action_calls": [],
        "subflows": [],
        "screens": [],
        "decisions": [],
    }
    gen = translate(_component(CategoryName.AUTOLAUNCHED_FLOW, raw, "LeadStatus"))
    assert gen.ooe_step == int(OoEStep.PROCESSES_AND_FLOWS)
    mod = _exec(gen.code)
    out = getattr(mod, gen.function_name)({"Status": "New"}, {})
    assert out == {"Status": "Working"}


def test_autolaunched_flow_rejects_flow_with_callouts() -> None:
    raw = {
        "process_type": "AutoLaunchedFlow",
        "action_calls": [{"name": "SendEmail", "action_name": "emailAlert", "type": "emailAlert"}],
        "record_updates": [],
        "record_creates": [],
        "subflows": [],
        "screens": [],
        "decisions": [],
    }
    import pytest

    with pytest.raises(ValueError, match="callouts"):
        translate(_component(CategoryName.AUTOLAUNCHED_FLOW, raw, "HasCallout"))


def test_autolaunched_flow_rejects_flow_with_screens() -> None:
    raw = {
        "process_type": "Flow",
        "action_calls": [],
        "record_updates": [],
        "record_creates": [],
        "subflows": [],
        "screens": [{"name": "ConfirmStep"}],
        "decisions": [],
    }
    import pytest

    with pytest.raises(ValueError, match="screens"):
        translate(_component(CategoryName.AUTOLAUNCHED_FLOW, raw, "HasScreen"))


def test_before_save_flow_routes_to_before_triggers_step() -> None:
    raw = {
        "process_type": "AutoLaunchedFlow",
        "trigger_type": "RecordBeforeSave",
        "object": "Account",
        "record_updates": [
            {
                "name": "Normalize",
                "input_assignments": [{"field": "Country", "value": "US"}],
            }
        ],
        "record_creates": [],
        "action_calls": [],
        "subflows": [],
        "screens": [],
        "decisions": [],
    }
    gen = translate(_component(CategoryName.AUTOLAUNCHED_FLOW, raw, "Normalize"))
    # Before-save flows fire at step 5, not 13.
    assert gen.ooe_step == int(OoEStep.BEFORE_TRIGGERS)


# -- Process builder ---------------------------------------------------------


def test_process_builder_treated_as_simple_flow() -> None:
    raw = {
        "process_type": "Workflow",  # PB discriminator
        "trigger_type": "",
        "object": "Opportunity",
        "record_updates": [
            {
                "name": "MarkLegacy",
                "input_assignments": [{"field": "LegacyMigrated__c", "value": True}],
            }
        ],
        "record_creates": [],
        "action_calls": [],
        "subflows": [],
        "screens": [],
        "decisions": [],
    }
    gen = translate(_component(CategoryName.PROCESS_BUILDER, raw, "LegacyOpp"))
    mod = _exec(gen.code)
    out = getattr(mod, gen.function_name)({}, {})
    # Boolean literals survive round-trip as Python bools (not strings).
    assert out == {"LegacyMigrated__c": True}
