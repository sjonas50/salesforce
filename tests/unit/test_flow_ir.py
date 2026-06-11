"""Comprehensive Flow parser contract.

Asserts the parser reconstructs the full execution graph — every element type,
typed connectors (incl. fault/loop/rule/default/scheduled-path), data deps, and
resources — from a realistic record-triggered Flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from offramp.extract.flow.ir import ConnectorKind, FlowElementType, FlowResourceKind
from offramp.extract.flow.parser import parse_flow

FIXTURE = Path(__file__).parent / "fixtures" / "comprehensive_flow.flow-meta.xml"


@pytest.fixture(scope="module")
def ir():
    return parse_flow(FIXTURE.read_text(encoding="utf-8"), api_name="Opportunity_Risk_Router")


def test_metadata(ir) -> None:
    assert ir.api_name == "Opportunity_Risk_Router"
    assert ir.label == "Opportunity Risk Router"
    assert ir.process_type == "AutoLaunchedFlow"
    assert ir.run_in_mode == "SystemModeWithoutSharing"
    assert ir.api_version == "66.0"


def test_start_trigger_and_paths(ir) -> None:
    assert ir.start is not None
    assert ir.start.object == "Opportunity"
    assert ir.start.trigger_type == "RecordAfterSave"
    assert ir.start.record_trigger_type == "CreateAndUpdate"
    assert ir.start.connector is not None
    assert ir.start.connector.target == "Get_Account"
    # Entry filter is captured as a read.
    assert any(f.field == "StageName" for f in ir.start.filters)
    # Scheduled path with its own connector.
    assert len(ir.start.scheduled_paths) == 1
    sp = ir.start.scheduled_paths[0]
    assert sp.offset_number == 1 and sp.offset_unit == "Days" and sp.time_source == "CloseDate"
    assert sp.connector is not None and sp.connector.target == "Notify_Owner"


def test_all_element_types_present(ir) -> None:
    types = {e.element_type for e in ir.elements}
    expected = {
        FlowElementType.RECORD_LOOKUP,
        FlowElementType.DECISION,
        FlowElementType.LOOP,
        FlowElementType.ASSIGNMENT,
        FlowElementType.RECORD_UPDATE,
        FlowElementType.ACTION_CALL,
        FlowElementType.SUBFLOW,
        FlowElementType.RECORD_CREATE,
        FlowElementType.WAIT,
        FlowElementType.RECORD_DELETE,
    }
    assert expected <= types


def test_connectors_are_typed(ir) -> None:
    lookup = ir.element_by_name("Get_Account")
    kinds = {c.kind for c in lookup.connectors}
    assert ConnectorKind.NEXT in kinds
    assert ConnectorKind.FAULT in kinds  # faultConnector → Log_Error
    fault = next(c for c in lookup.connectors if c.kind is ConnectorKind.FAULT)
    assert fault.target == "Log_Error"

    loop = ir.element_by_name("Loop_Contacts")
    loop_kinds = {c.kind for c in loop.connectors}
    assert loop_kinds == {ConnectorKind.LOOP_NEXT, ConnectorKind.LOOP_END}

    decision = ir.element_by_name("HighValue_Decision")
    dkinds = {c.kind for c in decision.connectors}
    assert ConnectorKind.RULE in dkinds and ConnectorKind.DEFAULT in dkinds


def test_decision_conditions_extracted(ir) -> None:
    decision = ir.element_by_name("HighValue_Decision")
    assert len(decision.rules) == 1
    rule = decision.rules[0]
    assert rule.name == "IsHighValue"
    cond = rule.conditions[0]
    assert cond.left == "accountRevenue"
    assert cond.operator == "GreaterThan"
    assert cond.right == "1000000.0"
    assert cond.right_kind == "numberValue"


def test_control_flow_edges_include_start(ir) -> None:
    edges = ir.control_flow_edges()
    # start → Get_Account (immediate) and start → Notify_Owner (scheduled path)
    start_targets = {c.target for src, c in edges if src == "__start__"}
    assert {"Get_Account", "Notify_Owner"} <= start_targets
    # Total edges should comfortably exceed the element count (branches + faults).
    assert len(edges) >= len(ir.elements)


def test_data_dependencies(ir) -> None:
    # Objects read (lookup Account, start Opportunity, delete Temp_Score__c).
    assert {"Account", "Opportunity", "Temp_Score__c", "Task"} <= ir.referenced_objects()
    # Objects written: Task (create), Opportunity (update via $Record), Temp_Score__c (delete).
    written = ir.written_objects()
    assert "Task" in written
    assert "Opportunity" in written  # record-update with inputReference=$Record
    assert "Temp_Score__c" in written


def test_external_invocations(ir) -> None:
    # Two apex action calls → both surface as called Apex; emailSimple does not.
    assert ir.called_apex() == {"OpportunityScoringService", "ErrorLogger"}
    assert ir.invoked_subflows() == {"ERP_Sync_Subflow"}


def test_record_create_writes_fields(ir) -> None:
    create = ir.element_by_name("Create_Task")
    fields = {w.field for w in create.field_writes}
    assert {"Subject", "WhatId"} <= fields
    whatid = next(w for w in create.field_writes if w.field == "WhatId")
    assert whatid.value_kind == "elementReference"
    assert whatid.value == "$Record.Id"


def test_resources_parsed(ir) -> None:
    by_name = {r.name: r for r in ir.resources}
    assert by_name["accountRevenue"].kind is FlowResourceKind.VARIABLE
    contacts = by_name["relatedContacts"]
    assert contacts.is_sobject and contacts.object_type == "Contact" and contacts.is_collection
    formula = by_name["RevenueLabel"]
    assert formula.kind is FlowResourceKind.FORMULA
    assert "accountRevenue" in formula.references  # {!accountRevenue} merge field
    assert by_name["HIGH_VALUE_THRESHOLD"].kind is FlowResourceKind.CONSTANT


def test_lookup_reads_and_writes(ir) -> None:
    lookup = ir.element_by_name("Get_Account")
    assert lookup.object == "Account"
    assert "AnnualRevenue" in lookup.queried_fields
    # Output assignment writes the accountRevenue variable.
    assert any(w.value == "accountRevenue" for w in lookup.field_writes)
    # Filter reads Id.
    assert any(r.field == "Id" for r in lookup.field_reads)
