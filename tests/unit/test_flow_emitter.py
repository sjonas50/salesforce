"""Flow IR → Temporal emitter: the generated workflow must be valid Python and
structurally faithful (real branches/loops/faults/DML/subflow), not a stub."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.flow.parser import parse_flow
from offramp.generate import tier2
from offramp.generate.flow_emitter import emit_flow_workflow

FIXTURE = Path(__file__).parent / "fixtures" / "comprehensive_flow.flow-meta.xml"


def _component() -> Component:
    ir = parse_flow(FIXTURE.read_text(encoding="utf-8"), api_name="Opportunity_Risk_Router")
    return Component(
        org_alias="t",
        category=CategoryName.RECORD_TRIGGERED_FLOW,
        name="Opportunity_Risk_Router",
        api_name="Opportunity_Risk_Router",
        content_hash="h",
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
        raw={"flow_ir": ir.model_dump(mode="json")},
    )


@pytest.fixture(scope="module")
def emitted():
    comp = _component()
    ir = parse_flow(FIXTURE.read_text(encoding="utf-8"), api_name=comp.api_name)
    wf = emit_flow_workflow(comp, ir)
    return wf


def test_generated_code_is_valid_python(emitted) -> None:
    ast.parse(emitted.code)  # raises SyntaxError if the emitter produced junk


def test_workflow_class_named_after_flow(emitted) -> None:
    assert emitted.workflow_name == "Flow_Opportunity_Risk_Router"
    assert "@workflow.defn" in emitted.code
    assert "class Flow_Opportunity_Risk_Router:" in emitted.code


def test_one_activity_per_side_effecting_element(emitted) -> None:
    # Lookup, update, two apex actions, email action, create, delete → activities.
    # (decisions/loops/assignments are workflow-level, not activities.)
    for act in (
        "act_Get_Account",
        "act_Update_Opportunity",
        "act_Call_Scoring_Apex",
        "act_Create_Task",
        "act_Standard_Path",  # record delete
    ):
        assert act in emitted.activity_names, act
        assert f"async def {act}(" in emitted.code


def test_decision_becomes_real_branch(emitted) -> None:
    # The high-value decision compares accountRevenue > 1000000 — emitted as a
    # real Python condition over ctx, not a placeholder.
    assert "# Decision: HighValue_Decision" in emitted.code
    assert "self.ctx.get('accountRevenue') > 1000000.0" in emitted.code


def test_loop_becomes_for_over_collection(emitted) -> None:
    assert "# Loop: Loop_Contacts" in emitted.code
    assert "for _item in (self.ctx.get('relatedContacts') or []):" in emitted.code


def test_fault_connector_becomes_try_except(emitted) -> None:
    # Get_Account has a faultConnector → Log_Error, so a try/except wraps it.
    assert "try:" in emitted.code
    assert "except Exception:  # fault path" in emitted.code


def test_subflow_becomes_child_workflow(emitted) -> None:
    assert "execute_child_workflow" in emitted.code
    assert "Flow_ERP_Sync_Subflow.run" in emitted.code


def test_dml_record_update_against_record(emitted) -> None:
    # The record-update writes Risk_Tier__c; the activity detail names the op.
    assert "update Opportunity" in emitted.code


def test_tier2_translate_routes_flow_to_emitter() -> None:
    # End-to-end: tier2.translate dispatches a flow component to the IR emitter.
    wf = tier2.translate(_component())
    assert wf.workflow_name == "Flow_Opportunity_Risk_Router"
    ast.parse(wf.code)
    assert "perform_step" not in wf.code  # NOT the generic placeholder


def test_emitter_terminates_on_back_edges(emitted) -> None:
    # The assignment loops back to Loop_Contacts; the emitter must not recurse
    # forever — it marks the back-edge instead.
    assert "handled above)" in emitted.code
