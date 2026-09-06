"""Surface categories, data profile, Metadata API path, dynamic-access flag, and review fixes."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from offramp.core.models import CategoryName
from offramp.engram.client import InMemoryEngramClient
from offramp.extract.apex import analyze
from offramp.extract.apex.tokenizer import strip_comments
from offramp.extract.categories.base import get_extractor
from offramp.extract.data_profile import profile_from_dump, profile_from_gateway
from offramp.extract.orchestrator import ExtractOrchestrator, ToolingSupplement
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.mdapi import (
    PARTIAL_TYPES,
    CompositePullClient,
    MetadataApiPullClient,
    _source_format_name,
)
from offramp.extract.pull.reconciler import ReconciledRecord, reconcile
from offramp.extract.pull.source_tree import SourceTree
from offramp.extract.schema import from_source_tree
from offramp.generate.formula.emitter import emit
from offramp.generate.formula.parser import UnsupportedFormulaError, parse
from offramp.generate.tier1 import _flow_assignment_py
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def _rec(cat: CategoryName, rel: str, api_name: str | None = None) -> ReconciledRecord:
    path = FIX / rel
    name = api_name or path.name.split(".")[0]
    return ReconciledRecord(
        category=cat,
        api_name=name,
        namespace=None,
        payload={"path": rel, "raw_xml": path.read_text()},
    )


# ---- surface extractors ---------------------------------------------------------


def test_layout_and_flexipage_references() -> None:
    lay = get_extractor(CategoryName.PAGE_LAYOUT).parse_payload(
        _rec(
            CategoryName.PAGE_LAYOUT,
            "layouts/Opportunity-Opportunity Layout.layout-meta.xml",
            "Opportunity-Opportunity Layout",
        )
    )
    assert lay["object"] == "Opportunity" and lay["surface"] == "ui"
    assert "Opportunity.Legacy_Notes__c" in lay["references"]["fields"]
    assert lay["quick_actions"] == ["Opportunity.Submit_Discount"]
    fp = get_extractor(CategoryName.FLEXIPAGE).parse_payload(
        _rec(CategoryName.FLEXIPAGE, "flexipages/Lead_Record_Page.flexipage-meta.xml")
    )
    # The fixture page is shaped like a retrieved record page: fields live in a
    # field-section facet and the LWC is referenced without the ``c:`` prefix.
    assert fp["references"] == {
        "objects": ["Lead"],
        "fields": ["Lead.Routed__c", "Lead.Score__c"],
        "flows": [],
        "lwc_bundles": ["leadCard"],
        "visualforce_pages": [],
    }


_FLEXIPAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexiPage xmlns="http://soap.sforce.com/2006/04/metadata">
    <flexiPageRegions>
        <itemInstances>
            <componentInstance>
                <componentInstanceProperties><name>flowName</name><value>{flow}</value></componentInstanceProperties>
                <componentName>{component}</componentName>
            </componentInstance>
        </itemInstances>
        <itemInstances>
            <componentInstance><componentName>{lwc}</componentName></componentInstance>
        </itemInstances>
        <itemInstances>
            <componentInstance><componentName>force:highlightsPanel</componentName></componentInstance>
        </itemInstances>
        <name>main</name>
        <type>Region</type>
    </flexiPageRegions>
    <masterLabel>Intake</masterLabel>
    <sobjectType>Lead</sobjectType>
    <template><name>flexipage:recordHomeTemplateDesktop</name></template>
    <type>RecordPage</type>
</FlexiPage>
"""


@pytest.mark.parametrize(
    ("component", "lwc"),
    [
        ("flowruntime:flowRuntimeForFlexipage", "leadCard"),  # what App Builder writes
        ("flowruntime:flowRuntime", "c:leadCard"),  # older / hand-authored form
    ],
)
def test_flexipage_flow_component_and_lwc_naming(component: str, lwc: str) -> None:
    xml = _FLEXIPAGE_XML.format(flow="CaptureLeadDetails", component=component, lwc=lwc)
    rec = ReconciledRecord(
        category=CategoryName.FLEXIPAGE,
        api_name="Intake",
        namespace=None,
        payload={"path": "flexipages/Intake.flexipage-meta.xml", "raw_xml": xml},
    )
    fp = get_extractor(CategoryName.FLEXIPAGE).parse_payload(rec)
    assert fp["references"]["flows"] == ["CaptureLeadDetails"]
    assert fp["references"]["lwc_bundles"] == ["leadCard"]  # standard components are not bundles


def test_permission_set_profile_and_report_references() -> None:
    ps = get_extractor(CategoryName.PERMISSION_SET).parse_payload(
        _rec(CategoryName.PERMISSION_SET, "permissionsets/Sales_User.permissionset-meta.xml")
    )
    assert ps["references"]["apex_classes"] == [
        "LeadScoringService"
    ]  # disabled UnusedLegacyUtil excluded
    assert ps["references"]["fields_editable"] == ["Lead.Archived_Reason__c", "Lead.Country__c"]
    prof = get_extractor(CategoryName.PROFILE).parse_payload(
        _rec(CategoryName.PROFILE, "profiles/Admin.profile-meta.xml")
    )
    assert prof["custom"] is False and "Opportunity.Legacy_Notes__c" in prof["references"]["fields"]
    rep = get_extractor(CategoryName.REPORT).parse_payload(
        _rec(
            CategoryName.REPORT,
            "reports/Pipeline/Pipeline_by_Segment.report-meta.xml",
            "Pipeline/Pipeline_by_Segment",
        )
    )
    assert rep["object"] == "Lead" and rep["folder"] == "Pipeline"
    # standard columns are UPPER_SNAKE report aliases, mapped to field API names
    assert {"Lead.Legacy_Segment__c", "Lead.CreatedDate", "Lead.LastName", "Lead.Status"} <= set(
        rep["references"]["fields"]
    )


def test_source_tree_classifies_surfaces() -> None:
    recs = SourceTree(FIX).records(source="fixture", source_version="0", api_version="66.0")
    cats = {r.api_name: r.category for r in recs}
    assert cats["Lead-Lead Layout"] is CategoryName.PAGE_LAYOUT
    assert cats["Lead_Record_Page"] is CategoryName.FLEXIPAGE
    assert cats["Sales_User"] is CategoryName.PERMISSION_SET
    assert cats["Admin"] is CategoryName.PROFILE
    assert cats["Pipeline/Pipeline_by_Segment"] is CategoryName.REPORT


# ---- data profile -------------------------------------------------------------


def test_profile_from_dump() -> None:
    prof = profile_from_dump(
        {"Lead": {"record_count": 100, "fields": {"A__c": 25, "B__c": 0}}}, org_alias="t"
    )
    assert prof.objects["Lead"].record_count == 100
    assert prof.field("Lead.A__c") is not None and prof.field("Lead.A__c").fill_rate == 0.25  # type: ignore[union-attr]
    assert prof.field("Lead.B__c").fill_rate == 0.0  # type: ignore[union-attr]
    assert prof.field("Lead.Nope__c") is None


@pytest.mark.asyncio
async def test_profile_from_gateway_uses_aggregates_and_record_counts() -> None:
    backend = InMemorySalesforceBackend()
    backend.records = {"Lead": {f"L{i}": {"Id": f"L{i}"} for i in range(7)}}
    # Aggregate query answers are canned per object.
    backend.rest["query"] = None

    class _Gateway(MCPGateway):
        async def sf_query(self, soql: str) -> dict[str, Any]:
            assert soql.startswith(
                "SELECT COUNT(Id) total, MAX(LastModifiedDate) lm, COUNT(Country__c) c0"
            )
            return {"records": [{"total": 7, "lm": "2026-09-01T00:00:00.000+0000", "c0": 5}]}

    gw = _Gateway(backend=backend, engram=InMemoryEngramClient())
    schema = from_source_tree(SourceTree(FIX), org_alias="t")
    only_lead = schema.model_copy(
        update={"nodes": [n for n in schema.nodes if n.api_name in {"Lead", "Lead.Country__c"}]}
    )
    prof = await profile_from_gateway(gw, only_lead, org_alias="t")
    lead = prof.objects["Lead"]
    assert lead.record_count == 7  # from limits/recordCount
    assert (
        lead.fields["Lead.Country__c"].non_null == 5
        and abs(lead.fields["Lead.Country__c"].fill_rate - 5 / 7) < 1e-9
    )
    assert lead.last_modified is not None


# ---- Metadata API path ----------------------------------------------------------


def _zip_of_fixture(*subdirs: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("unpackaged/package.xml", "<Package/>")
        for sub in subdirs:
            for p in (FIX / sub).rglob("*"):
                if p.is_file():
                    zf.writestr(f"unpackaged/{p.relative_to(FIX)}", p.read_bytes())
    return buf.getvalue()


@pytest.mark.asyncio
async def test_mdapi_client_retrieves_and_reads_partial_types(tmp_path: Path) -> None:
    backend = InMemorySalesforceBackend()
    backend.mdapi_zip = _zip_of_fixture(
        "approvalProcesses", "assignmentRules", "reports", "layouts"
    )
    backend.mdapi_folders = {
        "ReportFolder:": ["Pipeline"],
        "Report:Pipeline": ["Pipeline/Pipeline_by_Segment"],
    }
    gw = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    client = MetadataApiPullClient(
        gateway=gw, org_alias="t", workdir=tmp_path / "md", types=PARTIAL_TYPES
    )
    recs = list(await client.pull())
    by = {(r.category, r.api_name): r for r in recs}
    assert (CategoryName.APPROVAL_PROCESS, "Opportunity.HighValueDiscount") in by
    assert (CategoryName.REPORT, "Pipeline/Pipeline_by_Segment") in by
    assert (CategoryName.PAGE_LAYOUT, "Lead-Lead Layout") in by
    assert not client.failures


@pytest.mark.asyncio
async def test_reconciler_prefers_full_body_over_tooling_stub() -> None:
    stub = RawMetadataRecord(
        source="tooling_api",
        source_version="0",
        api_version="66.0",
        category=CategoryName.APPROVAL_PROCESS,
        api_name="Opportunity.HighValueDiscount",
        payload={
            "path": "approvalProcesses/x.xml",
            "parsed": {"ApprovalProcess": {"label": "HV", "active": True}},
            "partial": True,
        },
    )
    full = RawMetadataRecord(
        source="metadata_api",
        source_version="0",
        api_version="66.0",
        category=CategoryName.APPROVAL_PROCESS,
        api_name="Opportunity.HighValueDiscount",
        payload={
            "path": "approvalProcesses/x.xml",
            "raw_xml": (
                FIX / "approvalProcesses/Opportunity.HighValueDiscount.approvalProcess-meta.xml"
            ).read_text(),
        },
    )
    merged = reconcile([stub, full]).records[0]
    assert (
        "raw_xml" in merged.payload
        and "parsed" not in merged.payload
        and "partial" not in merged.payload
    )
    out = get_extractor(CategoryName.APPROVAL_PROCESS).parse_payload(merged)
    assert len(out["steps"]) == 2

    composite = CompositePullClient()
    assert await composite.list_categories() == set()


@pytest.mark.asyncio
async def test_composite_client_merges_and_orchestrator_accepts_it(tmp_path: Path) -> None:
    backend = InMemorySalesforceBackend()
    backend.mdapi_zip = _zip_of_fixture("classes", "objects")
    gw = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    md = MetadataApiPullClient(gateway=gw, org_alias="t", workdir=tmp_path / "md")
    client = CompositePullClient(md)
    result = await ExtractOrchestrator(
        org_alias="t", client=client, engram=InMemoryEngramClient(), supplement=ToolingSupplement()
    ).run()
    assert any(c.category is CategoryName.APEX_CLASS for c in result.components)
    assert any(c.category is CategoryName.VALIDATION_RULE for c in result.components)


# ---- analyzer: dynamic access + review fixes --------------------------------------


def test_dynamic_access_flag_and_literal_get() -> None:
    a = analyze((FIX / "classes/DynamicFieldReader.cls").read_text())
    assert a.dynamic_access == ["dynamic_soql", "global_describe"]
    assert (
        "Lead.Rating" in a.field_references
    )  # l.get('Rating') resolves; so.get(fieldName) does not
    b = analyze(
        "public class D { void f(String n) { Type t = Type.forName(n); Lead l = new Lead(); l.put(n, 1); } }"
    )
    assert b.dynamic_access == ["dynamic_field", "dynamic_type"]


def test_comment_markers_inside_strings_are_not_comments() -> None:
    src = "public class C { void f(Account acc) { String u = 'https://api.x.com/v1/' + acc.Website; insert acc; String s = 'a /* b'; Lead l = new Lead(); insert l; } }"
    assert "insert l" in strip_comments(src)
    a = analyze(src)
    assert [(d.op, d.sobject) for d in a.dml] == [("insert", "Account"), ("insert", "Lead")]
    assert "Account.Website" in a.field_references


def test_case_and_group_declarations_are_typed() -> None:
    a = analyze(
        "public class S { void f(Id cid) { Case c = [SELECT Id, Status FROM Case WHERE Id = :cid]; c.Status = 'Closed'; update c; } }"
    )
    assert "Case.Status" in a.field_writes
    assert [(d.op, d.sobject) for d in a.dml] == [("update", "Case")]


# ---- Tier 1 hardening ---------------------------------------------------------------


def test_tier1_rejects_globals_and_concats_safely() -> None:
    with pytest.raises(UnsupportedFormulaError):
        emit(parse("$User.ProfileId = '00e'"))
    code = emit(parse('"Total: " & Amount'))
    assert code.startswith("_concat(")
    from offramp.runtime.rules.formula_runtime import _concat

    assert _concat("Total: ", 100.0) == "Total: 100.0" and _concat(None, "x") == "x"


def test_tier1_flow_assignment_handles_element_references() -> None:
    assert _flow_assignment_py({"field": "OwnerId", "value": {"ref": "$Record.Owner__c"}}) == (
        "    mutations['OwnerId'] = _field(record, 'Owner__c')"
    )
    with pytest.raises(UnsupportedFormulaError):
        _flow_assignment_py({"field": "OwnerId", "value": {"ref": "FindTerritory.Owner__c"}})


def test_metadata_api_files_are_renamed_to_source_format() -> None:
    """A ``retrieve`` ZIP names files by type suffix; the source tree reader needs ``-meta.xml``."""
    assert _source_format_name("assignmentRules/Lead.assignmentRules") == (
        "assignmentRules/Lead.assignmentRules-meta.xml"
    )
    assert _source_format_name("permissionsets/Sales_User.permissionset") == (
        "permissionsets/Sales_User.permissionset-meta.xml"
    )
    assert _source_format_name("reports/Pipeline/Pipeline_by_Segment.report") == (
        "reports/Pipeline/Pipeline_by_Segment.report-meta.xml"
    )
    # already source format, or content files whose layout the two formats share
    assert _source_format_name("layouts/A-B.layout-meta.xml") == "layouts/A-B.layout-meta.xml"
    assert _source_format_name("classes/Foo.cls") == "classes/Foo.cls"
    assert _source_format_name("classes/Foo.cls-meta.xml") == "classes/Foo.cls-meta.xml"
    assert _source_format_name("lwc/leadCard/leadCard.js") == "lwc/leadCard/leadCard.js"


def test_metadata_api_zip_member_names_are_percent_decoded(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("unpackaged/layouts/Account-Account %28Marketing%29 Layout.layout", "<Layout/>")
    client = MetadataApiPullClient(gateway=None, org_alias="o", workdir=tmp_path)
    client._unzip(buf.getvalue())
    assert (tmp_path / "layouts" / "Account-Account (Marketing) Layout.layout-meta.xml").exists()


def test_report_columns_map_to_field_api_names() -> None:
    from offramp.extract.categories.surfaces import _report_field

    assert _report_field("LAST_NAME", "Lead") == "Lead.LastName"
    assert _report_field("CREATED_DATE", "Lead") == "Lead.CreatedDate"
    assert _report_field("FULL_NAME", "Lead") == "Lead.Name"
    assert _report_field("LEAD.STATUS", "Lead") == "Lead.Status"
    assert _report_field("Lead.Score__c", "Lead") == "Lead.Score__c"
    assert _report_field("Account.Industry", "Lead") == "Account.Industry"


def test_field_definitions_supplement_fields_hidden_from_describe() -> None:
    from offramp.core.models import SchemaSnapshot
    from offramp.extract.schema import supplement_from_field_definitions

    snap = SchemaSnapshot(org_alias="o", source="describe")
    rows = {
        "Lead": [
            {
                "QualifiedApiName": "Routed__c",
                "Label": "Routed",
                "DataType": "Checkbox",
                "IsCustom": True,
            },
            {
                "QualifiedApiName": "Territory__c",
                "Label": "Territory",
                "DataType": "Lookup(Territory)",
                "IsCustom": True,
            },
            {
                "QualifiedApiName": "Score__c",
                "Label": "Score",
                "DataType": "Formula (Number)",
                "IsCustom": True,
            },
        ]
    }
    assert supplement_from_field_definitions(snap, rows) == 3
    by = {n.api_name: n for n in snap.nodes}
    assert by["Lead.Routed__c"].field_type == "Checkbox"
    assert by["Lead.Territory__c"].field_type == "Lookup" and by[
        "Lead.Territory__c"
    ].reference_to == ["Territory"]
    assert (
        by["Lead.Score__c"].field_type == "Formula"
        and by["Lead.Score__c"].raw["hidden_from_describe"]
    )
    assert supplement_from_field_definitions(snap, rows) == 0  # idempotent


def test_source_tree_handles_multi_package_projects_and_skips_git(tmp_path: Path) -> None:
    """sfdx-project.json with several packageDirectories; ``.git/objects`` is not metadata."""
    (tmp_path / ".git" / "objects" / "ab").mkdir(parents=True)
    (tmp_path / "sfdx-project.json").write_text(
        '{"packageDirectories": [{"path": "./es-base-objects"}, {"path": "./es-base-code"}]}'
    )
    a = tmp_path / "es-base-objects" / "main" / "default"
    (a / "objects" / "Reservation__c" / "fields").mkdir(parents=True)
    (a / "objects" / "Reservation__c" / "Reservation__c.object-meta.xml").write_text(
        '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata"><label>R</label></CustomObject>'
    )
    (a / "objects" / "Reservation__c" / "fields" / "Status__c.field-meta.xml").write_text(
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata"><fullName>Status__c</fullName><type>Text</type><length>10</length></CustomField>'
    )
    b = tmp_path / "es-base-code" / "main" / "default"
    (b / "objects" / "Reservation__c" / "fields").mkdir(parents=True)
    (b / "objects" / "Reservation__c" / "fields" / "Notes__c.field-meta.xml").write_text(
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata"><fullName>Notes__c</fullName><type>Text</type><length>10</length></CustomField>'
    )
    (b / "classes").mkdir()
    (b / "classes" / "MarketServices.cls").write_text("public with sharing class MarketServices {}")
    (b / "classes" / "MarketServices.cls-meta.xml").write_text(
        '<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata"><apiVersion>66.0</apiVersion></ApexClass>'
    )
    tree = SourceTree(tmp_path)
    assert [r.relative_to(tmp_path).as_posix() for r in tree.roots] == [
        "es-base-objects/main/default",
        "es-base-code/main/default",
    ]
    objs = {o.name: o for o in tree.object_files()}
    assert set(objs["Reservation__c"].fields) == {"Status__c", "Notes__c"}  # merged across packages
    assert CategoryName.APEX_CLASS in tree.present_categories()


def test_cmt_records_are_read_from_source_format(tmp_path: Path) -> None:
    from offramp.extract.dispatch.cmt_reader import read_cmt_records_from_source

    d = tmp_path / "customMetadata"
    d.mkdir()
    (d / "Customer_Fields.Contact_Customer_Fields.md-meta.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CustomMetadata xmlns="http://soap.sforce.com/2006/04/metadata" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
    <label>Contact Customer Fields</label>
    <protected>false</protected>
    <values><field>Customer_City__c</field><value xsi:type="xsd:string">MailingCity</value></values>
    <values><field>Sobject_Type__c</field><value xsi:type="xsd:string">Contact</value></values>
</CustomMetadata>"""
    )
    rows = read_cmt_records_from_source([tmp_path])
    assert len(rows) == 1 and rows[0].cmt_type == "Customer_Fields__mdt"
    assert rows[0].developer_name == "Contact_Customer_Fields"
    assert rows[0].fields == {"Customer_City__c": "MailingCity", "Sobject_Type__c": "Contact"}


def test_tab_application_and_path_assistant_references() -> None:
    tab = get_extractor(CategoryName.CUSTOM_TAB).parse_payload(
        _rec(CategoryName.CUSTOM_TAB, "tabs/Territory__c.tab-meta.xml", "Territory__c")
    )
    assert tab["tab_kind"] == "object" and tab["references"]["objects"] == ["Territory__c"]
    tab2 = get_extractor(CategoryName.CUSTOM_TAB).parse_payload(
        _rec(CategoryName.CUSTOM_TAB, "tabs/Lead_Intake.tab-meta.xml", "Lead_Intake")
    )
    assert tab2["tab_kind"] == "component" and tab2["references"]["lwc_bundles"] == ["leadCard"]
    app = get_extractor(CategoryName.CUSTOM_APPLICATION).parse_payload(
        _rec(
            CategoryName.CUSTOM_APPLICATION,
            "applications/Sales_Offramp.app-meta.xml",
            "Sales_Offramp",
        )
    )
    assert app["references"] == {
        "objects": ["Lead", "Opportunity"],
        "tabs": ["Territory__c", "Lead_Intake"],
        "flexipages": ["Lead_Record_Page"],
    }
    path = get_extractor(CategoryName.PATH_ASSISTANT).parse_payload(
        _rec(
            CategoryName.PATH_ASSISTANT,
            "pathAssistants/Lead_Status_Path.pathAssistant-meta.xml",
            "Lead_Status_Path",
        )
    )
    assert path["object"] == "Lead" and path["picklist_field"] == "Lead.Status"
    assert path["references"]["fields"] == [
        "Lead.Country__c",
        "Lead.Routed__c",
        "Lead.Score__c",
        "Lead.Status",
    ]
    assert path["record_type"] == "" and path["active"] is True
