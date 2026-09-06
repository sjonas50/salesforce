from __future__ import annotations

from pathlib import Path

from offramp.core.models import CategoryName, SchemaNodeKind
from offramp.extract.pull.source_tree import SourceTree, derive_api_name, locate_source_root
from offramp.extract.schema import from_describe, from_source_tree

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def test_apex_bodies_are_captured() -> None:
    tree = SourceTree(FIX)
    recs = tree.records(
        source="fixture",
        source_version="0",
        api_version="66.0",
        categories={CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER},
    )
    classes = {r.api_name: r for r in recs if r.category is CategoryName.APEX_CLASS}
    assert "LeadRoutingHandler" in classes
    assert "implements TriggerAction" in classes["LeadRoutingHandler"].payload["body"]
    trig = next(
        r
        for r in recs
        if r.category is CategoryName.APEX_TRIGGER and r.api_name == "LeadDispatcher"
    )
    assert trig.payload["trigger_body"].startswith("trigger LeadDispatcher on Lead")
    # One record per class even though both .cls and .cls-meta.xml match globs.
    assert len([r for r in recs if r.api_name == "LeadRoutingHandler"]) == 1


def test_flow_variants_classified_once_each() -> None:
    tree = SourceTree(FIX)
    recs = tree.records(source="fixture", source_version="0", api_version="66.0")
    flows = {r.api_name: r.category for r in recs if r.payload.get("path", "").startswith("flows/")}
    assert flows["CaseTriageOrchestration"] is CategoryName.FLOW_ORCHESTRATION
    assert flows["LegacyOpportunityProcess"] is CategoryName.PROCESS_BUILDER
    assert flows["LeadRouting"] is CategoryName.RECORD_TRIGGERED_FLOW
    assert flows["NightlyHousekeeping"] is CategoryName.SCHEDULE_TRIGGERED_FLOW
    assert flows["OnCaseEscalation"] is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW
    assert flows["CaptureLeadDetails"] is CategoryName.SCREEN_FLOW
    assert flows["SendWelcomeEmail"] is CategoryName.AUTOLAUNCHED_FLOW


def test_locate_root_handles_sfdx_layout(tmp_path: Path) -> None:
    (tmp_path / "force-app" / "main" / "default" / "classes").mkdir(parents=True)
    assert locate_source_root(tmp_path) == tmp_path / "force-app" / "main" / "default"
    assert (
        derive_api_name(Path("Opportunity.HighValueDiscount.approvalProcess-meta.xml"))
        == "Opportunity.HighValueDiscount"
    )
    assert derive_api_name(Path("LeadHandler.cls")) == "LeadHandler"


def test_schema_from_source_tree() -> None:
    snap = from_source_tree(SourceTree(FIX), org_alias="t")
    by = snap.by_api_name()
    assert by["Territory__c"].kind is SchemaNodeKind.OBJECT and by["Territory__c"].custom
    assert by["Lead"].kind is SchemaNodeKind.OBJECT and not by["Lead"].custom
    terr = by["Lead.Territory__c"]
    assert (
        terr.field_type == "Lookup"
        and terr.reference_to == ["Territory__c"]
        and terr.relationship_name == "Leads"
    )
    assert by["Lead.Legacy_Segment__c"].picklist_values == ["SMB", "Enterprise"]
    assert by["Opportunity.Enterprise"].kind is SchemaNodeKind.RECORD_TYPE
    assert by["Account.AnnualRevenueK__c"].formula == "AnnualRevenue / 1000"


def test_schema_from_describe() -> None:
    g = {"sobjects": [{"name": "Lead", "label": "Lead", "custom": False, "queryable": True}]}
    d = {
        "Lead": {
            "fields": [
                {
                    "name": "OwnerId",
                    "label": "Owner",
                    "type": "reference",
                    "referenceTo": ["User", "Group"],
                    "relationshipName": "Owner",
                    "nillable": False,
                },
                {
                    "name": "Rating__c",
                    "label": "Rating",
                    "type": "picklist",
                    "custom": True,
                    "picklistValues": [{"value": "Hot"}, {"value": "Cold"}],
                    "nillable": True,
                },
            ],
            "recordTypeInfos": [
                {"developerName": "Master", "name": "Master"},
                {"developerName": "Partner", "name": "Partner", "active": True},
            ],
        }
    }
    snap = from_describe(g, d, org_alias="t")
    by = snap.by_api_name()
    assert (
        by["Lead.OwnerId"].reference_to == ["User", "Group"]
        and by["Lead.OwnerId"].field_type == "Lookup"
    )
    assert by["Lead.Rating__c"].picklist_values == ["Hot", "Cold"] and by["Lead.Rating__c"].custom
    assert by["Lead.Partner"].kind is SchemaNodeKind.RECORD_TYPE
