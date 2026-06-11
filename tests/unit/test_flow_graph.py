"""Flow execution graph loader — verifies the emitted nodes/edges.

Uses a fake backend that records every (cypher, params) call, so we assert on
the rows the loader builds without needing a live Neo4j/FalkorDB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.flow.parser import parse_flow
from offramp.understand.flow_graph import load_flows
from offramp.understand.graph_backend import QueryResult
from offramp.understand.graph_loader import GraphHandle

FIXTURE = Path(__file__).parent / "fixtures" / "comprehensive_flow.flow-meta.xml"


class FakeBackend:
    """Records queries; returns empty results."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> QueryResult:
        self.calls.append((cypher, params or {}))
        return QueryResult([])

    def reset(self) -> None: ...
    def delete(self) -> None: ...
    def close(self) -> None: ...

    def rows_for(self, needle: str) -> list[dict[str, Any]]:
        """All UNWIND rows from the (single) query containing `needle`."""
        out: list[dict[str, Any]] = []
        for cypher, params in self.calls:
            if needle in cypher:
                out.extend(params.get("rows", []))
        return out


def _provenance() -> Provenance:
    return Provenance(source_tool="fixture", source_version="0", api_version="66.0")


def _flow_component() -> Component:
    ir = parse_flow(FIXTURE.read_text(encoding="utf-8"), api_name="Opportunity_Risk_Router")
    return Component(
        org_alias="test",
        category=CategoryName.RECORD_TRIGGERED_FLOW,
        name="Opportunity_Risk_Router",
        api_name="Opportunity_Risk_Router",
        content_hash="h",
        provenance=_provenance(),
        raw={"flow_ir": ir.model_dump(mode="json")},
    )


def _apex(name: str) -> Component:
    return Component(
        org_alias="test",
        category=CategoryName.APEX_CLASS,
        name=name,
        api_name=name,
        content_hash="h",
        provenance=_provenance(),
    )


def _subflow(name: str) -> Component:
    # Bare flow component (no IR loaded here): the INVOKES edge resolves by name
    # via components_by_name regardless, and this keeps the loaded-flow count to
    # the one flow we actually assert internals on.
    return Component(
        org_alias="test",
        category=CategoryName.AUTOLAUNCHED_FLOW,
        name=name,
        api_name=name,
        content_hash="h",
        provenance=_provenance(),
    )


@pytest.fixture
def loaded():
    flow = _flow_component()
    scoring = _apex("OpportunityScoringService")
    logger = _apex("ErrorLogger")
    erp = _subflow("ERP_Sync_Subflow")
    components = [flow, scoring, logger, erp]
    by_name = {c.name: str(c.id) for c in components}
    backend = FakeBackend()
    handle = GraphHandle(graph=backend, name="test")
    n = load_flows(handle, components, components_by_name=by_name)
    return backend, n, by_name, str(flow.id)


def test_loads_one_flow(loaded) -> None:
    _, n, _, _ = loaded
    assert n == 1


def test_creates_start_and_element_nodes(loaded) -> None:
    backend, _, _, _ = loaded
    elements = backend.rows_for("HAS_ELEMENT")
    names = {r["name"] for r in elements}
    # Synthetic start + a representative spread of real elements.
    assert "__start__" in names
    assert {"Get_Account", "HighValue_Decision", "Loop_Contacts", "Create_Task"} <= names
    types = {r["element_type"] for r in elements}
    assert {"start", "record_lookup", "decision", "loop", "record_create"} <= types


def test_control_flow_edges_are_typed(loaded) -> None:
    backend, _, _, _ = loaded
    edges = backend.rows_for("CONTROL_FLOW")
    kinds = {r["kind"] for r in edges}
    # The whole point: fault/rule/default/loop edges are preserved, not flattened.
    assert {"next", "fault", "rule", "default", "loop_next", "loop_end", "scheduled_path"} <= kinds
    # Start fans out to the immediate path and the scheduled path.
    start_targets = {r["dst"].split(":", 5)[-1] for r in edges if r["src"].endswith(":__start__")}
    assert {"Get_Account", "Notify_Owner"} <= start_targets


def test_data_access_edges(loaded) -> None:
    backend, _, _, _ = loaded
    obj = backend.rows_for("DATA_ACCESS")
    writes = {(r["object"], r["mode"]) for r in obj}
    assert ("Task", "write") in writes
    assert ("Opportunity", "write") in writes  # record-update via $Record
    assert ("Account", "read") in writes  # lookup reads Account

    fields = backend.rows_for("FIELD_ACCESS")
    field_keys = {(r["key"], r["mode"]) for r in fields}
    assert ("Task.Subject", "write") in field_keys
    assert ("Account.AnnualRevenue", "read") in field_keys  # queried field


def test_calls_and_invokes(loaded) -> None:
    backend, _, by_name, _ = loaded
    calls = backend.rows_for("MERGE (e)-[:CALLS]->(c)")
    called_ids = {r["target_id"] for r in calls}
    assert by_name["OpportunityScoringService"] in called_ids
    assert by_name["ErrorLogger"] in called_ids

    invokes = backend.rows_for("MERGE (e)-[:INVOKES]->(c)")
    invoked_ids = {r["target_id"] for r in invokes}
    assert by_name["ERP_Sync_Subflow"] in invoked_ids


def test_references_to_resources(loaded) -> None:
    backend, _, _, flow_id = loaded
    refs = backend.rows_for("MERGE (e)-[:REFERENCES]->(r)")
    # Decision condition references the accountRevenue variable.
    ref_targets = {r["dst"] for r in refs}
    assert f"{flow_id}::res::accountRevenue" in ref_targets


def test_skips_flows_without_ir() -> None:
    # A flow component lacking flow_ir is silently skipped (no crash).
    bare = Component(
        org_alias="test",
        category=CategoryName.SCREEN_FLOW,
        name="Bare",
        content_hash="h",
        provenance=_provenance(),
    )
    backend = FakeBackend()
    handle = GraphHandle(graph=backend, name="test")
    assert load_flows(handle, [bare], components_by_name={}) == 0
