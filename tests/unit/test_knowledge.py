"""Process model, per-category builders, the knowledge store, renderers, and the kg CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from offramp.cli.__main__ import main
from offramp.core.process import Fidelity, ProcessDefinition, StepKind, TriggerKind
from offramp.engram.client import InMemoryEngramClient
from offramp.extract.orchestrator import ExtractOrchestrator, ExtractRunResult
from offramp.extract.pull.fixture import FixturePullClient
from offramp.knowledge.render import to_markdown, to_mermaid
from offramp.knowledge.store import KnowledgeStore
from offramp.understand.process_ir import build_processes

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
def processes(run: ExtractRunResult) -> dict[str, ProcessDefinition]:
    return {
        p.name: p for p in build_processes(run.components, org_alias="sample_org", scan_id="s1")
    }


# ---- builders ---------------------------------------------------------------------


def test_flow_becomes_full_fidelity_process(processes: dict[str, ProcessDefinition]) -> None:
    p = processes["LeadRouting"]
    assert p.kind == "record_triggered_flow" and p.fidelity is Fidelity.FULL
    assert p.trigger.kind is TriggerKind.RECORD_SAVE and p.trigger.object == "Lead"
    assert p.trigger.events == ["create", "update"] and p.trigger.timing == "after"
    assert p.trigger.when.conditions[0].left == "Lead.Status" and p.trigger.requires_change
    kinds = {s.id: s.kind for s in p.steps}
    assert kinds == {
        "RouteByCountry": StepKind.DECISION,
        "FindTerritory": StepKind.LOOKUP,
        "AssignOwner": StepKind.UPDATE,
        "ScoreLead": StepKind.CALL_CODE,
        "NotifyOwner": StepKind.CALL_PROCESS,
    }
    decision = p.step("RouteByCountry")
    assert decision is not None and decision.branches[0].next == "AssignOwner"
    assert decision.branches[0].when.conditions[0].left == "$Record.Country__c"
    assert decision.default_next == "ScoreLead"
    assert p.entry == "RouteByCountry"
    assert p.fields_written == ["Lead.OwnerId", "Lead.Routed__c"]
    assert "Lead.Country__c" in p.fields_read and "Territory__c.Owner__c" in p.fields_read
    assert p.calls == ["LeadScoringService", "SendWelcomeEmail"]
    assert any(v.type == "formula" and v.expression for v in p.variables)


def test_rules_become_condition_action_processes(processes: dict[str, ProcessDefinition]) -> None:
    wf = processes["Account.NotifyOwnerOfHighValue"]
    assert wf.kind == "workflow_rule" and wf.trigger.events == ["create", "update"]
    assert wf.trigger.when.conditions[0].left == "Account.AnnualRevenue"
    assert [s.kind for s in wf.steps] == [StepKind.NOTIFY, StepKind.UPDATE]
    assert wf.fields_written == ["Account.IsStrategic__c"]

    vr = processes["Lead.Email_Required"]
    assert vr.trigger.timing == "before" and vr.steps[0].kind is StepKind.RAISE_ERROR
    assert (
        vr.trigger.when.conditions[0].expression
        and "ISPICKVAL" in vr.trigger.when.conditions[0].expression
    )

    ar = processes["Lead.RouteToInsideSales"]
    decision = ar.steps[0]
    assert decision.kind is StepKind.DECISION and len(decision.branches) == 2
    assert ar.fields_written == ["Lead.OwnerId"]
    assert any(s.kind is StepKind.NOTIFY for s in ar.steps)

    esc = processes["Case.SLAExpiry"]
    assert [s.kind for s in esc.steps] == [StepKind.DECISION, StepKind.WAIT, StepKind.ESCALATE]
    assert esc.steps[1].extras["minutes"] == "120"

    ap = processes["Opportunity.HighValueDiscount"]
    assert ap.trigger.kind is TriggerKind.APPROVAL_SUBMIT
    steps = [s for s in ap.steps if s.kind is StepKind.APPROVAL_STEP]
    assert [s.id for s in steps] == ["Manager_Review", "VP_Review"] and steps[0].next == "VP_Review"
    assert steps[1].when is not None and steps[1].when.conditions[0].expression


def test_apex_becomes_references_only_process(processes: dict[str, ProcessDefinition]) -> None:
    p = processes["LeadRoutingHandler"]
    assert p.fidelity is Fidelity.REFERENCES_ONLY and p.trigger.kind is TriggerKind.INVOCATION
    assert {s.kind for s in p.steps} >= {StepKind.LOOKUP, StepKind.UPDATE, StepKind.CALL_CODE}
    assert p.code and "implements TriggerAction" in p.code
    trig = processes["LeadDispatcher"]
    assert trig.trigger.object == "Lead" and trig.trigger.timing == "both"
    assert "LeadScoringServiceTest" not in processes  # tests are not processes
    assert "dynamic_access" in processes["DynamicFieldReader"].tags


def test_identity_is_content_addressed(processes: dict[str, ProcessDefinition]) -> None:
    p = processes["LeadRouting"]
    twin = p.model_copy(deep=True)
    twin.sources = []
    twin.label = "Something else"
    assert twin.finalize().id == p.id  # provenance and labels do not change identity
    twin.fields_written.append("Lead.Extra__c")
    assert twin.finalize().id != p.id
    # All three validation rules share one shape → one family fingerprint.
    fps = {
        processes[n].fingerprint
        for n in ("Lead.Email_Required", "Account.Industry_Required", "Opportunity.Discount_Cap")
    }
    assert len(fps) == 1


# ---- renderers --------------------------------------------------------------------


def test_render_mermaid_and_markdown(processes: dict[str, ProcessDefinition]) -> None:
    p = processes["LeadRouting"]
    mm = to_mermaid(p)
    assert mm.startswith("flowchart TD") and "T --> RouteByCountry" in mm
    assert 'RouteByCountry -->|"$Record.Country__c EqualTo US' in mm
    assert '-->|"otherwise"| ScoreLead' in mm
    md = to_markdown(p)
    assert "## Trigger" in md and "## Steps" in md and "```mermaid" in md
    assert "writes: Lead.OwnerId, Lead.Routed__c" in md


# ---- store --------------------------------------------------------------------------


def test_store_ingest_dedupes_across_scans_and_orgs(
    processes: dict[str, ProcessDefinition], tmp_path: Path
) -> None:
    store = KnowledgeStore(tmp_path / "lib")
    procs = list(processes.values())
    first = store.ingest(procs, org_alias="sample_org", scan_id="acme-1")
    assert len(first.new_process_ids) == len(procs) and len(store.index()) == len(procs)
    # Same logic again from another org: nothing new, but a second source on every entry.
    again = [p.model_copy(deep=True) for p in procs]
    for p in again:
        for s in p.sources:
            s.org_alias = "globex"
            s.scan_id = None
    second = store.ingest(again, org_alias="globex", scan_id="globex-1")
    assert second.new_process_ids == [] and len(store.index()) == len(procs)
    row = store.index()[processes["LeadRouting"].id]
    assert row.orgs == ["globex", "sample_org"]
    stored = store.get(processes["LeadRouting"].id)
    assert stored is not None and {s.org_alias for s in stored.sources} == {"globex", "sample_org"}
    # Code bodies live beside the definitions, keyed by content.
    assert list((tmp_path / "lib" / "code").glob("*.apex"))
    assert store.code(processes["LeadRoutingHandler"]) is not None

    assert [s.scan_id for s in store.scans()] == ["acme-1", "globex-1"]
    d = store.diff("acme-1", "globex-1")
    assert d["added"] == [] and d["removed"] == [] and len(d["unchanged"]) == len(procs)


def test_store_search_resolve_families(
    processes: dict[str, ProcessDefinition], tmp_path: Path
) -> None:
    store = KnowledgeStore(tmp_path / "lib")
    store.ingest(list(processes.values()), org_alias="acme", scan_id="acme-1")
    hits = store.search("lead routing")
    assert hits and hits[0][1].name == "LeadRouting"
    assert store.resolve("LeadRouting") is not None
    assert store.resolve(processes["LeadRouting"].id[:10]) is not None
    assert store.resolve("Email_Required") is not None  # suffix match on Lead.Email_Required
    assert store.resolve("nope") is None
    fams = store.families()
    assert any(len(f.members) == 3 and f.members[0].kind == "validation_rule" for f in fams)
    assert len(store.list_processes(kind="apex_class")) >= 10
    assert {r.name for r in store.list_processes(obj="Case")} >= {"Case.SLAExpiry"}
    export = store.export()
    assert len(export["processes"]) == len(processes) and export["scans"]


# ---- CLI --------------------------------------------------------------------------------


def test_kg_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "fx"
    lib = tmp_path / "lib"
    assert main(["extract", "--fixture", str(FIX), "--out", str(out), "--library", str(lib)]) == 0
    assert (out / "processes.json").is_file()
    assert (lib / "index.json").is_file()
    # A second ingest of the same scan output adds nothing new.
    assert (
        main(["kg", "--library", str(lib), "ingest", "--from", str(out), "--scan-id", "second"])
        == 0
    )
    assert "0 new" in capsys.readouterr().out
    assert main(["kg", "--library", str(lib), "search", "territory"]) == 0
    assert "LeadRouting" in capsys.readouterr().out
    assert main(["kg", "--library", str(lib), "show", "LeadRouting", "--format", "mermaid"]) == 0
    assert "flowchart TD" in capsys.readouterr().out
    assert (
        main(["kg", "--library", str(lib), "show", "LeadRoutingHandler", "--format", "code"]) == 0
    )
    assert "class LeadRoutingHandler" in capsys.readouterr().out
    assert main(["kg", "--library", str(lib), "families"]) == 0
    assert main(["kg", "--library", str(lib), "list", "--kind", "validation_rule"]) == 0
    assert "3 processes" in capsys.readouterr().out
    scans = [s.scan_id for s in KnowledgeStore(lib).scans()]
    assert main(["kg", "--library", str(lib), "diff", scans[0], "second"]) == 0
    assert "unchanged:" in capsys.readouterr().out
    assert main(["kg", "--library", str(lib), "export", "--out", str(tmp_path / "lib.json")]) == 0
    assert json.loads((tmp_path / "lib.json").read_text())["processes"]
    assert main(["kg", "--library", str(lib), "show", "does-not-exist"]) == 2
