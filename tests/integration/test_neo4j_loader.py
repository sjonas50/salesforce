"""Neo4j knowledge-graph integration test (default backend).

Verifies the Component + comprehensive Flow execution graph load against a real
Neo4j over Bolt. Skips cleanly when Neo4j (or the driver) is unavailable, like
the FalkorDB suite. Configure via NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD.

The point of this test is to prove the loader's Cypher is portable — the same
UNWIND/MERGE queries the FakeBackend unit tests exercise run unchanged here
against a live Neo4j.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.flow.parser import parse_flow
from offramp.understand.flow_graph import load_flows
from offramp.understand.graph_backend import Neo4jBackend
from offramp.understand.graph_loader import GraphHandle, load_components

FIXTURE = (
    Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "comprehensive_flow.flow-meta.xml"
)


def _env() -> tuple[str, str, str]:
    return (
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "neo4j"),
    )


def _has_neo4j() -> bool:
    try:
        from neo4j import GraphDatabase

        uri, user, password = _env()
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
        driver.close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_neo4j(), reason="Neo4j not reachable"),
]


def _provenance() -> Provenance:
    return Provenance(source_tool="t", source_version="0", api_version="66.0")


def _open() -> GraphHandle:
    uri, user, password = _env()
    backend = Neo4jBackend(
        uri=uri, user=user, password=password, database="neo4j", name="offramp_test"
    )
    return GraphHandle(graph=backend, name="offramp_test")


def _flow_component() -> Component:
    ir = parse_flow(FIXTURE.read_text(encoding="utf-8"), api_name=f"Flow_{uuid.uuid4().hex[:8]}")
    return Component(
        org_alias="t",
        category=CategoryName.RECORD_TRIGGERED_FLOW,
        name=ir.api_name,
        api_name=ir.api_name,
        content_hash="0" * 64,
        provenance=_provenance(),
        raw={"flow_ir": ir.model_dump(mode="json")},
    )


def _apex(name: str) -> Component:
    return Component(
        org_alias="t",
        category=CategoryName.APEX_CLASS,
        name=name,
        api_name=name,
        content_hash="0" * 64,
        provenance=_provenance(),
    )


def test_flow_execution_graph_round_trips_through_neo4j() -> None:
    handle = _open()
    try:
        handle.reset()
        flow = _flow_component()
        scoring = _apex("OpportunityScoringService")
        components = [flow, scoring]
        by_name = {c.name: str(c.id) for c in components}

        load_components(handle, components)
        n = load_flows(handle, components, components_by_name=by_name)
        assert n == 1

        # Every element became a node, including the synthetic start.
        elements = handle.graph.query(
            "MATCH (:Component {id: $id})-[:HAS_ELEMENT]->(e:FlowElement) RETURN count(e) AS n",
            params={"id": str(flow.id)},
        )
        assert elements.result_set[0][0] >= 11

        # Typed control-flow edges survive the round trip.
        faults = handle.graph.query(
            "MATCH (:FlowElement)-[r:CONTROL_FLOW {kind:'fault'}]->(:FlowElement) RETURN count(r)"
        )
        assert faults.result_set[0][0] >= 1

        # CALLS edge resolves the apex action to the real Component node.
        calls = handle.graph.query(
            "MATCH (:FlowElement)-[:CALLS]->(c:Component {id:$id}) RETURN count(*)",
            params={"id": str(scoring.id)},
        )
        assert calls.result_set[0][0] >= 1

        # Data dependency: a WRITE to the Task SObject.
        writes = handle.graph.query(
            "MATCH (:FlowElement)-[:DATA_ACCESS {mode:'write'}]->(o:SObject {name:'Task'}) "
            "RETURN count(*)"
        )
        assert writes.result_set[0][0] >= 1
    finally:
        handle.reset()
        handle.close()
