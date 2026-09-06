"""Impact analysis (C23), clustering, report rendering, and in-process CLI runs on the fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from offramp.cli.__main__ import main
from offramp.engram.client import InMemoryEngramClient
from offramp.extract.orchestrator import ExtractOrchestrator, ExtractRunResult
from offramp.extract.pull.fixture import FixturePullClient
from offramp.understand import impact
from offramp.understand.clustering import build_networkx_graph, detect_processes
from offramp.understand.complexity import score_all
from offramp.understand.dependencies import DependencyGraph
from offramp.understand.orphan.resolver import ResolutionInputs, resolve_orphans
from offramp.understand.xray.render import XRayInputs, build_context, render_html, render_json

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


@pytest.fixture(scope="module")
def run() -> ExtractRunResult:
    return asyncio.run(
        ExtractOrchestrator(
            org_alias="sample_org",
            client=FixturePullClient(FIX),
            engram=InMemoryEngramClient(),
            fixture_root=FIX,
        ).run()
    )


@pytest.fixture(scope="module")
def graph(run: ExtractRunResult) -> DependencyGraph:
    return run.build_graph()


def _node(graph: DependencyGraph, name: str, kind: str | None = None) -> str:
    n = graph.find(name, kind=kind)
    assert n is not None, name
    return n.id


# ---- where-used ---------------------------------------------------------------


def test_where_used_field_groups_by_category_with_evidence(graph: DependencyGraph) -> None:
    wu = impact.where_used(graph, _node(graph, "Lead.Country__c", "field"))
    assert wu.total == 7 and wu.active_total == 7 and wu.automation_total == 5
    assert set(wu.by_category) == {
        "apex_class",
        "assignment_rule",
        "record_triggered_flow",
        "page_layout",
        "permission_set",
    }
    flow_ref = wu.by_category["record_triggered_flow"][0]
    assert flow_ref.evidence == "flow_xml" and flow_ref.corroborated_by_api
    writer = next(
        r for r in wu.by_category["apex_class"] if r.node.api_name == "LeadValidationHandler"
    )
    assert writer.notes == "write"
    js = wu.to_jsonable()
    assert js["target"]["api_name"] == "Lead.Country__c" and js["total"] == 7
    assert js["automation_total"] == 5


def test_where_used_object_includes_field_level_references(graph: DependencyGraph) -> None:
    wu = impact.where_used(graph, _node(graph, "Territory__c", "object"))
    names = {r.node.api_name for refs in wu.by_category.values() for r in refs}
    assert {"LeadRouting", "LeadRoutingHandler", "LeadValidationHandler"} <= names
    via = [
        r.notes
        for refs in wu.by_category.values()
        for r in refs
        if r.notes and r.notes.startswith("via ")
    ]
    assert any("Territory__c.Owner__c" in v for v in via)


def test_where_used_unknown_node_raises(graph: DependencyGraph) -> None:
    with pytest.raises(KeyError):
        impact.where_used(graph, "no-such-id")


# ---- closure / save impact ------------------------------------------------------


def test_impact_closure_follows_writes_and_respects_depth(graph: DependencyGraph) -> None:
    rows = impact.impact_closure(graph, _node(graph, "LeadScoringService"), max_depth=2)
    by_name = {r.node.api_name: r for r in rows}
    assert by_name["LeadRouting"].distance == 1
    assert by_name["leadCard"].distance == 1
    # LeadRouting writes Lead.OwnerId → the field is impacted through the write propagation.
    assert "Lead.OwnerId" in by_name and by_name["Lead.OwnerId"].path[-2] == "LeadRouting"
    assert max(r.distance for r in rows) <= 3  # depth 2 + one write hop
    assert all(r.min_confidence >= 0.5 for r in rows)


def test_save_impact_is_ordered_by_ooe_step(graph: DependencyGraph) -> None:
    rows = impact.save_impact(graph, "Lead")
    steps = [r.step for r in rows]
    assert steps == sorted(steps)
    by_name = {r.component.api_name: r for r in rows}
    assert by_name["LeadDispatcher"].step == 5 and by_name["LeadDispatcher"].relation == "fires"
    assert by_name["Email_Required"].step == 6
    assert by_name["LeadRouting"].step == 13 and by_name["LeadRouting"].relation == "fires"
    # The Lead workflow file has only an alert and no active rules → inactive.
    assert by_name["Lead"].relation == "inactive" or any(r.relation == "inactive" for r in rows)
    assert impact.save_impact(graph, "NoSuchObject") == []


def test_save_impact_after_trigger_lands_at_step_9(graph: DependencyGraph) -> None:
    rows = {r.component.api_name: r for r in impact.save_impact(graph, "Opportunity")}
    assert rows["OpportunityDiscount"].step == 9


# ---- cleanup candidates ------------------------------------------------------------


def test_unused_fields_classify_by_what_still_references_them(graph: DependencyGraph) -> None:
    unused = {u.node.api_name: u for u in impact.unused_fields(graph)}
    assert unused["Lead.Old_Segment__c"].reason == "no_references"
    assert (
        unused["Lead.Old_Segment__c"].empty and unused["Lead.Old_Segment__c"].record_count == 12400
    )
    assert unused["Lead.Test_Flag__c"].reason == "test_only"
    assert unused["Lead.Test_Flag__c"].referenced_by == ["LeadScoringServiceTest"]
    assert unused["Lead.Archived_Reason__c"].reason == "security_only"
    assert unused["Opportunity.Legacy_Notes__c"].reason == "ui_only"
    assert unused["Opportunity.Legacy_Notes__c"].referenced_by == [
        "Admin",
        "Opportunity-Opportunity Layout",
    ]
    # A formula field nothing reads is unused too (its own definition does not count).
    assert unused["Account.AnnualRevenueK__c"].reason == "no_references"
    assert unused["Account.AnnualRevenueK__c"].fill_rate is not None
    # Referenced by an assignment rule → not unused, even though a report also reads it.
    assert "Lead.Legacy_Segment__c" not in unused
    # Referenced only by a layout AND live Apex → used.
    assert "Lead.Score__c" not in unused


def test_where_used_flags_test_only_and_closure_skips_tests(graph: DependencyGraph) -> None:
    wu = impact.where_used(graph, _node(graph, "Lead.Test_Flag__c", "field"))
    assert wu.total == 1 and wu.active_total == 0 and wu.test_only
    closure = {
        i.node.api_name for i in impact.impact_closure(graph, _node(graph, "LeadScoringService"))
    }
    assert "LeadScoringServiceTest" not in closure
    assert "Sales_User" in closure  # the permission set that grants class access is affected


def test_legacy_automation_blast_radius(graph: DependencyGraph, run: ExtractRunResult) -> None:
    legacy = {la.component.api_name: la for la in impact.legacy_automation(graph, run.components)}
    assert legacy["LegacyOpportunityProcess"].category == "process_builder"
    assert (
        legacy["LegacyOpportunityProcess"].active
        and legacy["LegacyOpportunityProcess"].downstream >= 1
    )
    assert "Opportunity.Discount__c" in legacy["LegacyOpportunityProcess"].fields_touched
    assert legacy["Lead"].active is False  # alert-only workflow file


def test_summary_numbers(graph: DependencyGraph, run: ExtractRunResult) -> None:
    s = impact.summarize(graph, run.schema, run.components)
    assert s["unused_custom_fields"] == 3 and s["surface_only_custom_fields"] == 2
    assert s["test_only_custom_fields"] == 1 and s["empty_custom_fields"] == 3
    assert s["profiled_fields"] == 17 and s["test_classes"] == 1
    assert s["dynamic_apex_classes"] == 2
    assert s["legacy_automation"] == 4 and s["legacy_active"] == 2
    assert s["api_matched"] >= 8 and s["api_only"] >= 1
    assert len(s["edges_by_evidence"]) >= 13


# ---- clustering / orphans ---------------------------------------------------------------


def test_clustering_groups_lead_routing_process(graph: DependencyGraph) -> None:
    nx_graph = build_networkx_graph(graph)
    processes = detect_processes(nx_graph, resolution=1.0)
    assert processes and processes[0].process_id == "bp_000"
    assert sum(p.size for p in processes) == sum(
        1 for n in graph.nodes.values() if n.kind == "component"
    )
    trigger = graph.find("LeadDispatcher", kind="component")
    assert trigger is not None
    cluster = next(p for p in processes if trigger.id in p.component_ids)
    members = {n.api_name for i in cluster.component_ids if (n := graph.node(i)) is not None}
    # The CMT dispatcher and its handlers cluster with the trigger that invokes them.
    assert {"MetadataTriggerHandler", "LeadValidationHandler", "LeadRoutingHandler"} <= members
    assert "Lead" in cluster.object_names and "Lead" in cluster.label


def test_orphan_resolver_uses_graph(graph: DependencyGraph, run: ExtractRunResult) -> None:
    rep = resolve_orphans(ResolutionInputs(components=run.components, graph=graph))
    channels = {r.apex_class_name: r.channel for r in rep.resolved}
    assert channels == {
        "LeadController": "lwc_import",
        "LeadScoringServiceTest": "test",
        "NightlyLeadCleanup": "cron_trigger",
    }
    assert rep.unresolved == ["DynamicFieldReader", "UnusedLegacyUtil"]
    assert rep.referenced >= 6  # handlers, services, interface, dispatcher…


# ---- report ---------------------------------------------------------------------


def _inputs(run: ExtractRunResult, graph: DependencyGraph) -> XRayInputs:
    assert run.coverage is not None and run.ooe is not None
    processes = detect_processes(build_networkx_graph(graph))
    return XRayInputs(
        org_alias="sample_org",
        components=run.components,
        coverage=run.coverage,
        ooe=run.ooe,
        graph=graph,
        processes=processes,
        orphans=resolve_orphans(ResolutionInputs(components=run.components, graph=graph)),
        complexity=score_all(run.components),
        schema=run.schema,
        save_impact_objects=["Lead"],
        partial_categories=["assignment_rule"],
    )


def test_render_context_and_html(run: ExtractRunResult, graph: DependencyGraph) -> None:
    ctx = build_context(_inputs(run, graph))
    assert ctx["save_impacts"][0]["object"] == "Lead"
    assert any(
        w["name"] == "Lead.Country__c" and w["total"] == 7 and w["automation_total"] == 5
        for w in ctx["where_used"]
    )
    assert [u["field"] for u in ctx["unused_fields"]][:3] == [
        "Account.AnnualRevenueK__c",
        "Lead.Old_Segment__c",
        "Opportunity.DiscountedAmount__c",
    ]
    assert ctx["has_data_profile"] is True
    assert any(r["legacy"] for r in ctx["component_rows"])
    html = render_html(_inputs(run, graph))
    for needle in (
        "Where is this used",
        "Save impact",
        "Legacy automation",
        "Partial coverage",
        "Lead.Old_Segment__c",
        "UnusedLegacyUtil",
    ):
        assert needle in html
    js = render_json(_inputs(run, graph))
    assert js["schema_version"] == "2.0"
    assert js["graph"]["stats"]["edges"] == len(graph.edges)
    assert js["business_processes"][0]["component_ids"]
    json.dumps(js, default=str)  # serializable


# ---- CLI in-process --------------------------------------------------------------


def test_cli_extract_and_impact_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fx"
    assert main(["extract", "--fixture", str(FIX), "--out", str(out)]) == 0
    assert (out / "graph.json").is_file() and (out / "schema.json").is_file()

    assert main(["impact", "--from", str(out), "--where-used", "Lead.Country__c"]) == 0
    text = capsys.readouterr().out
    assert "7 references" in text and "LeadRouting" in text

    assert main(["impact", "--from", str(out), "--save", "Opportunity", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["step"] <= payload["rows"][-1]["step"]

    assert (
        main(["impact", "--from", str(out), "--change", "LeadScoringService", "--depth", "2"]) == 0
    )
    assert "leadCard" in capsys.readouterr().out
    assert main(["impact", "--from", str(out), "--unused"]) == 0
    assert "Lead.Old_Segment__c" in capsys.readouterr().out
    assert main(["impact", "--from", str(out), "--legacy"]) == 0
    assert "LegacyOpportunityProcess" in capsys.readouterr().out

    assert main(["impact", "--from", str(out), "--where-used", "Nope.Nothing"]) == 2
    assert main(["impact", "--from", str(tmp_path / "missing"), "--unused"]) == 1


def test_cli_xray_in_memory(tmp_path: Path) -> None:
    out = tmp_path / "xray"
    rc = main(
        [
            "xray",
            "--fixture",
            str(FIX),
            "--out",
            str(out),
            "--no-graph-db",
            "--skip-annotations",
            "--save-impact",
            "Case",
        ]
    )
    assert rc == 0
    payload = json.loads((out / "xray.json").read_text())
    assert payload["save_impacts"][0]["object"] == "Case"
    assert (out / "extract" / "impact_summary.json").is_file()


def test_cli_rejects_missing_source(tmp_path: Path) -> None:
    assert main(["extract", "--fixture", str(tmp_path / "nope"), "--out", str(tmp_path / "o")]) == 1
    assert (
        main(
            [
                "xray",
                "--fixture",
                str(tmp_path / "nope"),
                "--out",
                str(tmp_path / "o"),
                "--no-graph-db",
                "--skip-annotations",
            ]
        )
        == 1
    )
