"""Full-coverage Flow extractor: elements, resources, references."""

from __future__ import annotations

from pathlib import Path

from offramp.core.models import CategoryName
from offramp.extract.categories.base import get_extractor
from offramp.extract.pull.reconciler import ReconciledRecord

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org" / "flows"


def _record(name: str, cat: CategoryName) -> ReconciledRecord:
    xml = (FIX / f"{name}.flow-meta.xml").read_text()
    return ReconciledRecord(
        category=cat,
        api_name=name,
        namespace=None,
        payload={"path": f"flows/{name}.flow-meta.xml", "raw_xml": xml},
    )


def test_record_triggered_flow_full_shape() -> None:
    out = get_extractor(CategoryName.RECORD_TRIGGERED_FLOW).parse_payload(
        _record("LeadRouting", CategoryName.RECORD_TRIGGERED_FLOW)
    )
    assert out["object"] == "Lead"
    assert out["trigger_type"] == "RecordAfterSave"
    assert out["record_trigger_type"] == "CreateAndUpdate"
    assert out["start"]["requires_record_changed"] is True
    assert out["start"]["filters"][0]["field"] == "Status"
    kinds = out["element_counts"]
    assert kinds == {
        "actionCalls": 1,
        "decisions": 1,
        "recordLookups": 1,
        "recordUpdates": 1,
        "subflows": 1,
    }
    dec = out["decisions"][0]
    assert dec["rules"][0]["conditions"][0] == {
        "left": "$Record.Country__c",
        "operator": "EqualTo",
        "right": "US",
    }
    assert dec["default_next"] == "ScoreLead"
    assert out["record_updates"][0]["input_assignments"][0] == {
        "field": "OwnerId",
        "value": {"ref": "FindTerritory.Owner__c"},
        "operator": "",
    }
    assert out["resources"]["formulas"][0]["name"] == "IsEnterprise"


def test_record_triggered_flow_references() -> None:
    out = get_extractor(CategoryName.RECORD_TRIGGERED_FLOW).parse_payload(
        _record("LeadRouting", CategoryName.RECORD_TRIGGERED_FLOW)
    )
    refs = out["references"]
    assert refs["objects"] == ["Lead", "Territory__c"]
    assert {
        "Lead.Country__c",
        "Lead.Status",
        "Lead.OwnerId",
        "Lead.Routed__c",
        "Lead.AnnualRevenue",
        "Lead.Industry",
        "Territory__c.Country_Code__c",
        "Territory__c.Owner__c",
    } <= set(refs["fields"])
    assert refs["apex_classes"] == ["LeadScoringService"]
    assert refs["flows"] == ["SendWelcomeEmail"]


def test_scheduled_and_platform_event_flows() -> None:
    sched = get_extractor(CategoryName.SCHEDULE_TRIGGERED_FLOW).parse_payload(
        _record("NightlyHousekeeping", CategoryName.SCHEDULE_TRIGGERED_FLOW)
    )
    assert sched["start"]["schedule"]["frequency"] == "Daily"
    assert sched["element_counts"] == {"decisions": 1, "recordDeletes": 1}
    assert "Lead.LastModifiedDate" in sched["references"]["fields"]
    pe = get_extractor(CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW).parse_payload(
        _record("OnCaseEscalation", CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW)
    )
    assert pe["references"]["platform_events"] == ["CaseEscalation__e"]
    assert "Task.WhatId" in pe["references"]["fields"]
    assert "CaseEscalation__e.CaseId__c" in pe["references"]["fields"]


def test_email_alert_and_variable_typed_lookup() -> None:
    out = get_extractor(CategoryName.AUTOLAUNCHED_FLOW).parse_payload(
        _record("SendWelcomeEmail", CategoryName.AUTOLAUNCHED_FLOW)
    )
    assert out["references"]["email_alerts"] == ["Lead.Welcome_Lead_Alert"]
    assert "Lead.Email" in out["references"]["fields"]


def test_screen_flow_object_field_via_variable() -> None:
    out = get_extractor(CategoryName.SCREEN_FLOW).parse_payload(
        _record("CaptureLeadDetails", CategoryName.SCREEN_FLOW)
    )
    assert out["screens"][0]["fields"][0]["object_field"] == "lead.Company"
    assert "Lead.Company" in out["references"]["fields"]
    assert out["references"]["objects"] == ["Lead"]


def test_tooling_json_shape_is_accepted() -> None:
    body = {
        "processType": "AutoLaunchedFlow",
        "status": "Active",
        "start": {
            "object": "Account",
            "triggerType": "RecordBeforeSave",
            "recordTriggerType": "Create",
            "filters": [
                {"field": "Industry", "operator": "IsNull", "value": {"booleanValue": False}}
            ],
        },
        "recordUpdates": [
            {
                "name": "SetFlag",
                "inputReference": "$Record",
                "inputAssignments": [{"field": "IsStrategic__c", "value": {"booleanValue": True}}],
            }
        ],
        "decisions": [],
    }
    rec = ReconciledRecord(
        category=CategoryName.RECORD_TRIGGERED_FLOW,
        api_name="AcctFlag",
        namespace=None,
        payload={"parsed": {"Flow": body}},
    )
    out = get_extractor(CategoryName.RECORD_TRIGGERED_FLOW).parse_payload(rec)
    assert out["object"] == "Account"
    assert out["record_updates"][0]["input_assignments"][0]["value"] is True
    assert set(out["references"]["fields"]) == {"Account.Industry", "Account.IsStrategic__c"}
