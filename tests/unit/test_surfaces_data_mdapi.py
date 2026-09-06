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
from offramp.extract.pull.mdapi import PARTIAL_TYPES, CompositePullClient, MetadataApiPullClient
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
    assert fp["references"] == {
        "objects": ["Lead"],
        "fields": ["Lead.Routed__c"],
        "flows": ["CaptureLeadDetails"],
        "lwc_bundles": ["leadCard"],
        "visualforce_pages": [],
    }


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
    assert (
        "Lead.Legacy_Segment__c" in rep["references"]["fields"]
        and "LEAD.CREATED_DATE" in rep["references"]["fields"]
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
