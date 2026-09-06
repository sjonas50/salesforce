"""REST/Tooling pull path against the in-memory backend (AD-29)."""

from __future__ import annotations

import pytest

from offramp.core.models import CategoryName
from offramp.engram.client import InMemoryEngramClient
from offramp.extract.orchestrator import ExtractOrchestrator, ToolingSupplement
from offramp.extract.pull.tooling_api import ToolingApiPullClient, classify_flow
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway

APEX = "public with sharing class Svc { public static void go(Id i) { Lead l = [SELECT Id, Email FROM Lead WHERE Id = :i]; l.Email = 'x'; update l; } }"


def _backend() -> InMemorySalesforceBackend:
    b = InMemorySalesforceBackend()
    b.tooling = {
        "ApexClass": [
            {"Id": "01p1", "Name": "Svc", "ApiVersion": 66.0, "Status": "Active", "Body": APEX}
        ],
        "ApexTrigger": [
            {
                "Id": "01q1",
                "Name": "LeadTrg",
                "ApiVersion": 66.0,
                "Status": "Active",
                "TableEnumOrId": "Lead",
                "Body": "trigger LeadTrg on Lead (before insert) { Svc.go(null); }",
            }
        ],
        "FlowDefinition": [{"Id": "300a", "DeveloperName": "AcctFlag", "ActiveVersionId": "301a"}],
        "Flow": [
            {
                "Id": "301a",
                "FullName": "AcctFlag",
                "ProcessType": "AutoLaunchedFlow",
                "Status": "Active",
                "VersionNumber": 3,
                "Metadata": {
                    "start": {
                        "object": "Account",
                        "triggerType": "RecordBeforeSave",
                        "recordTriggerType": "Create",
                    },
                    "recordUpdates": [
                        {
                            "name": "SetFlag",
                            "inputReference": "$Record",
                            "inputAssignments": [
                                {"field": "IsStrategic__c", "value": {"booleanValue": True}}
                            ],
                        }
                    ],
                },
            }
        ],
        "ValidationRule": [
            {
                "Id": "03d1",
                "ValidationName": "Email_Required",
                "Active": True,
                "EntityDefinition": {"QualifiedApiName": "Lead"},
                "Metadata": {
                    "errorConditionFormula": "ISBLANK(Email)",
                    "errorMessage": "Email required",
                },
            }
        ],
        "WorkflowRule": [
            {
                "Id": "01Q1",
                "Name": "HighValue",
                "TableEnumOrId": "Account",
                "Metadata": {
                    "active": True,
                    "criteriaItems": [
                        {"field": "Account.AnnualRevenue", "operation": "greaterThan", "value": "1"}
                    ],
                    "actions": [{"name": "MarkStrategic", "type": "FieldUpdate"}],
                },
            }
        ],
        "WorkflowFieldUpdate": [
            {
                "Id": "04Y1",
                "Name": "MarkStrategic",
                "EntityDefinitionId": "Account",
                "Metadata": {
                    "field": "IsStrategic__c",
                    "literalValue": "1",
                    "operation": "Literal",
                },
            }
        ],
        "CustomField": [
            {
                "Id": "00N1",
                "DeveloperName": "AnnualRevenueK",
                "TableEnumOrId": "Account",
                "Metadata": {"formula": "AnnualRevenue / 1000", "type": "Number"},
            },
            {
                "Id": "00N2",
                "DeveloperName": "IsStrategic",
                "TableEnumOrId": "Account",
                "Metadata": {"type": "Checkbox"},
            },
        ],
        "EntityDefinition": [
            {"QualifiedApiName": "Trigger_Action__mdt", "Label": "Trigger Action"}
        ],
        "MetadataComponentDependency": [
            {
                "MetadataComponentType": "ApexClass",
                "MetadataComponentName": "Svc",
                "RefMetadataComponentType": "CustomObject",
                "RefMetadataComponentName": "Lead",
            },
        ],
        "CronTrigger": [],
        "AsyncApexJob": [],
        "LightningComponentBundle": [],
        "PlatformEventChannelMember": [],
        "ProcessDefinition": [],
        "AssignmentRule": [
            {"Id": "01Q9", "Name": "Route", "EntityDefinitionId": "Lead", "Active": True}
        ],
        "EscalationRule": [],
        "AutoResponseRule": [],
        "SharingRules": [],
    }
    b.records = {
        "Trigger_Action__mdt": {
            "m1": {
                "Id": "m1",
                "DeveloperName": "Lead_Insert",
                "Apex_Class__c": "Svc",
                "Object__c": "Lead",
            }
        }
    }
    b.describes = {
        "Lead": {
            "fields": [{"name": "Email", "label": "Email", "type": "email", "nillable": True}],
            "recordTypeInfos": [],
        },
        "Account": {
            "fields": [
                {"name": "IsStrategic__c", "label": "Strategic", "type": "boolean", "custom": True},
                {"name": "AnnualRevenue", "type": "currency"},
            ],
            "recordTypeInfos": [],
        },
    }
    return b


@pytest.mark.asyncio
async def test_tooling_pull_and_graph() -> None:
    backend = _backend()
    gateway = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    client = ToolingApiPullClient(gateway=gateway, org_alias="rest_org")
    recs = list(await client.pull())
    cats = {r.category for r in recs}
    assert {
        CategoryName.APEX_CLASS,
        CategoryName.APEX_TRIGGER,
        CategoryName.RECORD_TRIGGERED_FLOW,
        CategoryName.VALIDATION_RULE,
        CategoryName.WORKFLOW_RULE,
        CategoryName.FORMULA_FIELD,
        CategoryName.ASSIGNMENT_RULE,
    } <= cats
    assert (
        next(r for r in recs if r.category is CategoryName.ASSIGNMENT_RULE).payload["partial"]
        is True
    )

    supplement = ToolingSupplement(
        cmt_records=await client.cmt_records(),
        dependency_rows=await client.dependency_rows(types=["ApexClass"]),
        cron_rows=await client.cron_rows(),
        schema=await client.schema(),
    )
    assert supplement.cmt_records[0].fields["Apex_Class__c"] == "Svc"
    assert (
        supplement.schema is not None
        and "Account.IsStrategic__c" in supplement.schema.by_api_name()
    )

    orch = ExtractOrchestrator(
        org_alias="rest_org", client=client, engram=InMemoryEngramClient(), supplement=supplement
    )
    result = await orch.run()
    assert not result.failures, result.failures
    by_name = {c.name: c for c in result.components}
    assert by_name["Svc"].raw["references"]["fields_written"] == ["Lead.Email"]
    assert by_name["AcctFlag"].raw["references"]["fields_written"] == ["Account.IsStrategic__c"]
    assert by_name["Account"].raw["field_updates"][0]["field"] == "IsStrategic__c"

    g = result.build_graph()
    svc = g.find("Svc", kind="component")
    assert svc is not None
    targets = {
        n.api_name for e in g.outbound(svc.id) if (n := g.node(str(e.target_id))) is not None
    }
    assert {"Lead", "Lead.Email", "Lead.Id"} <= targets
    # API row (Svc → Lead) corroborates the parser edge.
    assert any(e.corroborated_by_api for e in g.outbound(svc.id))
    assert g.api_matched == 1


def test_classify_flow_variants() -> None:
    assert classify_flow({"processType": "Orchestrator"}) is CategoryName.FLOW_ORCHESTRATION
    assert classify_flow({"processType": "Workflow"}) is CategoryName.PROCESS_BUILDER
    assert (
        classify_flow({"processType": "AutoLaunchedFlow", "start": {"triggerType": "Scheduled"}})
        is CategoryName.SCHEDULE_TRIGGERED_FLOW
    )
    assert (
        classify_flow(
            {"processType": "AutoLaunchedFlow", "start": {"triggerType": "PlatformEvent"}}
        )
        is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW
    )
    assert classify_flow({"processType": "Flow", "screens": [{}]}) is CategoryName.SCREEN_FLOW
    assert classify_flow({"processType": "AutoLaunchedFlow"}) is CategoryName.AUTOLAUNCHED_FLOW
