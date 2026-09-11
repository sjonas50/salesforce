"""Execution-trace verification: log parser, comparer, org runner, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offramp.core.process import ProcessDefinition
from offramp.engram.client import InMemoryEngramClient
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway
from offramp.verify.compare import find_trace, verify_process
from offramp.verify.runner import Recipe, run_with_trace
from offramp.verify.trace import parse_debug_log

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"

LOG = """\
12:00:01.100 (1100000)|EXECUTION_STARTED
12:00:01.101 (1101000)|CODE_UNIT_STARTED|[EXTERNAL]|Flow:Lead
12:00:01.102 (1102000)|FLOW_START_INTERVIEWS_BEGIN|1
12:00:01.103 (1103000)|FLOW_CREATE_INTERVIEW_BEGIN|00D000000000001|300000000000001|301000000000001|Lead Routing
12:00:01.104 (1104000)|FLOW_START_INTERVIEW_BEGIN|3f2a0000-0000-0000-0000-000000000001|Lead Routing
12:00:01.105 (1105000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowDecision|RouteByCountry
12:00:01.106 (1106000)|FLOW_ELEMENT_END|3f2a0000-0000-0000-0000-000000000001|FlowDecision|RouteByCountry
12:00:01.109 (1109000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowRecordUpdate|AssignOwner
12:00:01.110 (1110000)|FLOW_BULK_ELEMENT_BEGIN|FlowRecordUpdate|AssignOwner
12:00:01.111 (1111000)|DML_BEGIN|[1]|Op:Update|Type:Lead|Rows:1
12:00:01.112 (1112000)|DML_END|[1]
12:00:01.113 (1113000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowActionCall|ScoreLead
12:00:01.114 (1114000)|FLOW_VALUE_ASSIGNMENT|3f2a0000-0000-0000-0000-000000000001|scoreOut|42
12:00:01.115 (1115000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowSubflow|NotifyOwner
12:00:01.116 (1116000)|FLOW_START_INTERVIEW_END|3f2a0000-0000-0000-0000-000000000001|Lead Routing
12:00:01.117 (1117000)|FLOW_START_INTERVIEWS_END|1
"""


def _processes() -> dict[str, ProcessDefinition]:
    from offramp.cli.verify import load_processes

    out = FIX.parents[2] / "unit"  # placeholder to keep mypy quiet about unused Path
    assert out.is_dir()
    import subprocess
    import sys
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    subprocess.run(
        [sys.executable, "-m", "offramp.cli", "extract", "--fixture", str(FIX), "--out", str(tmp)],
        check=True,
        capture_output=True,
    )
    d = tmp if (tmp / "processes.json").is_file() else tmp / "extract"
    return {p.name: p for p in load_processes(d, [])}


def test_parse_debug_log_yields_one_trace_per_interview() -> None:
    traces = parse_debug_log(LOG)
    assert len(traces) == 1
    t = traces[0]
    assert t.flow_label == "Lead Routing" and t.interview_id.startswith("3f2a")
    assert t.path == ["RouteByCountry", "AssignOwner", "ScoreLead", "NotifyOwner"]
    assert [(d.op, d.sobject, d.rows, d.after_element) for d in t.dml] == [
        ("Update", "Lead", 1, "AssignOwner")
    ]
    assert t.assignments["scoreOut"] == "42"
    assert not t.errors


def test_matching_trace_passes_and_divergent_trace_fails() -> None:
    procs = _processes()
    p = procs["LeadRouting"]
    traces = parse_debug_log(LOG)
    r = verify_process(p, find_trace(p, traces))
    assert r.status == "pass", [c for c in r.checks if not c.ok]
    # A trace that visits a step the model does not know, out of order, with the wrong DML.
    bad = LOG.replace("FlowRecordUpdate|AssignOwner", "FlowRecordCreate|MakeTask").replace(
        "Op:Update|Type:Lead", "Op:Insert|Type:Task"
    )
    r2 = verify_process(p, find_trace(p, parse_debug_log(bad)))
    assert r2.status == "mismatch"
    failed = {c.name for c in r2.checks if not c.ok}
    assert {"elements_in_model", "path_follows_model", "dml_matches_steps"} <= failed
    assert verify_process(p, None).status == "not_verifiable"


@pytest.mark.asyncio
async def test_runner_creates_trace_flag_fires_record_and_cleans_up() -> None:
    procs = _processes()
    p = procs["LeadRouting"]
    backend = InMemorySalesforceBackend()
    backend.tooling["DebugLevel"] = []
    backend.records["User"] = {"005000000000001": {"Id": "005000000000001", "Username": "u@x"}}
    backend.records["ApexLog"] = {"07L000000000001": {"Id": "07L000000000001"}}
    backend.responses[("GET", "sobjects/ApexLog/07L000000000001/Body")] = LOG  # served as text
    gateway = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    outcome = await run_with_trace(
        gateway,
        p,
        Recipe(object="Lead", create={"LastName": "T", "Company": "C", "Country__c": "US"}),
        user_id="005000000000001",
        settle_seconds=0,
    )
    assert "FLOW_START_INTERVIEW_BEGIN" in outcome.log_text and outcome.record_id
    posts = [(m, path) for m, path, _ in backend.requests]
    assert ("POST", "tooling/sobjects/DebugLevel") in posts
    assert ("POST", "tooling/sobjects/TraceFlag") in posts
    assert any(
        m == "DELETE" and path.startswith("tooling/sobjects/TraceFlag/") for m, path in posts
    )
    assert backend.records.get("Lead", {}) == {}  # the test record was deleted again


def test_cli_offline_log_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offramp.cli.__main__ import main

    out = tmp_path / "x"
    assert main(["extract", "--fixture", str(FIX), "--out", str(out)]) == 0
    d = out if (out / "processes.json").is_file() else out / "extract"
    logf = tmp_path / "trace.log"
    logf.write_text(LOG)
    capsys.readouterr()
    rc = main(
        [
            "verify",
            "--from",
            str(d),
            "--flow",
            "LeadRouting",
            "--flow",
            "SendWelcomeEmail",
            "--log",
            str(logf),
            "--json",
        ]
    )
    text = capsys.readouterr().out
    results = {r["process"]: r["status"] for r in json.loads(text[text.index("[\n") :])}
    assert rc == 0 and results == {"LeadRouting": "pass", "SendWelcomeEmail": "not_verifiable"}


def test_roundtrip_comparison_of_two_traces() -> None:
    from offramp.verify.compare import compare_roundtrip

    original = parse_debug_log(LOG)[0]
    copy_log = LOG.replace("|Lead Routing", "|Lead Routing (rt)")
    copy = parse_debug_log(copy_log)[0]
    assert copy.flow_label == "Lead Routing (rt)"
    assert compare_roundtrip(original, copy).ok
    diverged = parse_debug_log(copy_log.replace("FlowActionCall|ScoreLead\n", ""))[0]
    assert not compare_roundtrip(original, diverged).ok
    assert not compare_roundtrip(original, None).ok


@pytest.mark.asyncio
async def test_remove_flow_copy_deactivates_then_deletes_versions() -> None:
    from offramp.verify.runner import remove_flow_copy

    backend = InMemorySalesforceBackend()
    backend.tooling["FlowDefinition"] = [{"Id": "300X", "DeveloperName": "LeadRouting_rt"}]
    backend.tooling["Flow"] = [
        {"Id": "301A", "DefinitionId": "300X"},
        {"Id": "301B", "DefinitionId": "300X"},
    ]
    gateway = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    assert await remove_flow_copy(gateway, "LeadRouting_rt") == 2
    ops = [(m, path) for m, path, _ in backend.requests]
    assert ("PATCH", "tooling/sobjects/FlowDefinition/300X") in ops
    assert ("DELETE", "tooling/sobjects/Flow/301A") in ops and (
        "DELETE",
        "tooling/sobjects/Flow/301B",
    ) in ops


def test_runtime_error_is_its_own_status_and_no_fire_is_explained() -> None:
    from offramp.verify.compare import explain_no_fire

    procs = _processes()
    p = procs["LeadRouting"]
    errored = LOG.replace(
        "FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowSubflow|NotifyOwner",
        "FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowSubflow|NotifyOwner\n"
        "12:00:01.116 (1116000)|FLOW_ELEMENT_ERROR|Default Workflow User Email has not been verified.",
    )
    r = verify_process(p, find_trace(p, parse_debug_log(errored)))
    assert r.status == "runtime_error" and any(c.name == "no_runtime_errors" for c in r.checks)
    why = explain_no_fire(p, {"Status": "Working - Contacted", "Country__c": "US"})
    assert "Status EqualTo" in why and "Working - Contacted" in why


def test_subflow_elements_are_nested_not_unknown() -> None:
    procs = _processes()
    p = procs["LeadRouting"]
    nested = LOG.replace(
        "FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowSubflow|NotifyOwner",
        "FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowSubflow|NotifyOwner\n"
        "12:00:01.116 (1116000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowRecordLookup|GetLead\n"
        "12:00:01.117 (1117000)|FLOW_ELEMENT_BEGIN|3f2a0000-0000-0000-0000-000000000001|FlowActionCall|SendEmail",
    )
    r = verify_process(p, find_trace(p, parse_debug_log(nested)))
    assert r.status == "pass", [c for c in r.checks if not c.ok]
    assert any(c.name == "nested_elements" and "GetLead" in c.detail for c in r.checks)
    assert r.path == ["RouteByCountry", "AssignOwner", "ScoreLead", "NotifyOwner"]


REAL_SHAPE = """\
17:29:49.479 (479473812)|FLOW_CREATE_INTERVIEW_BEGIN|00DdM00000qwHVZ|300dM00003xQXrG|301dM000049LE8q
17:29:49.479 (479619083)|FLOW_CREATE_INTERVIEW_END|49867e9c-58e3|Lead Routing
17:29:49.480 (480167514)|FLOW_START_INTERVIEWS_BEGIN|1
17:29:49.480 (480199054)|FLOW_START_INTERVIEW_BEGIN|49867e9c-58e3|Lead Routing
17:29:49.480 (481315196)|FLOW_ELEMENT_BEGIN|49867e9c-58e3|FlowDecision|RouteByCountry
17:29:49.480 (481804347)|FLOW_ELEMENT_DEFERRED|FlowDecision|RouteByCountry
17:29:49.480 (481964457)|FLOW_START_INTERVIEW_END|49867e9c-58e3|Lead Routing
17:29:49.480 (482694049)|FLOW_RULE_DETAIL|49867e9c-58e3|IsUS|false|false
17:29:49.480 (482909849)|FLOW_ELEMENT_BEGIN|49867e9c-58e3|FlowActionCall|ScoreLead
17:29:49.480 (825819127)|FLOW_ACTIONCALL_DETAIL|49867e9c-58e3|ScoreLead|Apex|LeadScoringService|true|
17:29:49.480 (826256308)|FLOW_ELEMENT_BEGIN|49867e9c-58e3|FlowSubflow|NotifyOwner
17:29:49.480 (891107523)|FLOW_SUBFLOW_DETAIL|49867e9c-58e3|Send Welcome Email|300dM00003xQJN6|301dM000049L2aj
17:29:49.480 (891446994)|FLOW_ELEMENT_BEGIN|49867e9c-58e3|FlowRecordLookup|GetLead
17:29:49.480 (897531627)|FLOW_ELEMENT_BEGIN|49867e9c-58e3|FlowActionCall|SendEmail
17:29:49.480 (988613927)|FLOW_ACTIONCALL_DETAIL|49867e9c-58e3|SendEmail|Email Alerts|Lead.Welcome_Lead_Alert|false|Default Workflow User
17:29:49.480 (1252140329)|FLOW_ELEMENT_ERROR|Default Workflow User Email has not been verified.
17:29:49.480 (1252470359)|FLOW_START_INTERVIEWS_END|1
"""


def test_real_log_shape_deferred_elements_rules_actions_and_late_error() -> None:
    traces = parse_debug_log(REAL_SHAPE)
    assert [t.flow_label for t in traces] == ["Lead Routing"]  # no phantom from CREATE_BEGIN
    t = traces[0]
    assert t.path == ["RouteByCountry", "ScoreLead", "NotifyOwner", "GetLead", "SendEmail"]
    assert t.rule_results == {"IsUS": False}
    assert t.action_targets["ScoreLead"] == ("Apex", "LeadScoringService")
    assert t.subflow_calls == [("NotifyOwner", "Send Welcome Email")]
    assert t.errors and t.errors[0].startswith("Default Workflow User Email")
    p = _processes()["LeadRouting"]
    r = verify_process(p, find_trace(p, traces))
    assert r.status == "runtime_error", [c for c in r.checks if not c.ok]
    names = {c.name: c for c in r.checks}
    assert names["branch_outcomes_match"].ok and names["action_targets_match"].ok
    assert names["nested_elements"].ok and "GetLead" in names["nested_elements"].detail
    # A wrong branch in the model would be caught: pretend the default led elsewhere.
    p2 = p.model_copy(deep=True)
    next(s for s in p2.steps if s.id == "RouteByCountry").default_next = "AssignOwner"
    r2 = verify_process(p2, find_trace(p2, traces))
    assert not {c.name: c for c in r2.checks}["branch_outcomes_match"].ok
