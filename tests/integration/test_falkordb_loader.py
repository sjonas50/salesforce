"""FalkorDB loader against a live FalkorDB (integration; skipped when unreachable)."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient
from offramp.understand.clustering import (
    build_networkx_graph,
    detect_processes,
    write_processes_to_graph,
)
from offramp.understand.graph_loader import load_dependency_graph, open_graph

FIXTURE = Path(__file__).parent / "fixtures" / "sample_org"
URL = os.environ.get("FALKORDB_URL", "redis://localhost:6379")


def _has_falkordb() -> bool:
    try:
        open_graph(url=URL, name="_probe").client.list_graphs()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_falkordb(), reason="FalkorDB not reachable"),
]


def test_load_dependency_graph_round_trip() -> None:
    result = asyncio.run(
        ExtractOrchestrator(
            org_alias="sample_org",
            client=FixturePullClient(FIXTURE),
            engram=InMemoryEngramClient(),
            fixture_root=FIXTURE,
        ).run()
    )
    graph = result.build_graph()
    handle = open_graph(url=URL, name=f"test_dep_{uuid.uuid4().hex[:8]}")
    try:
        nodes, edges = load_dependency_graph(handle, graph)
        assert nodes == len(graph.nodes) and edges == len(graph.edges)
        comps = handle.graph.query("MATCH (c:Component) RETURN count(c)").result_set[0][0]
        assert comps == len(result.components)
        calls = handle.graph.query(
            "MATCH (:Component)-[r:CALLS]->(:Component) RETURN count(r)"
        ).result_set[0][0]
        assert calls >= 5
        fields = handle.graph.query(
            "MATCH (f:Field) WHERE f.custom = true RETURN count(f)"
        ).result_set[0][0]
        assert fields >= 10
        # Evidence + confidence ride on every relationship.
        row = handle.graph.query(
            "MATCH ()-[r:REFERENCES]->() RETURN r.evidence, r.confidence LIMIT 1"
        ).result_set[0]
        assert row[0] and 0 < row[1] <= 1
        processes = detect_processes(build_networkx_graph(graph))
        assert write_processes_to_graph(handle, processes) == len(processes)
        bp = handle.graph.query(
            "MATCH (:Component)-[:PARTICIPATES_IN]->(bp:BusinessProcess) RETURN count(DISTINCT bp)"
        ).result_set[0][0]
        assert bp == len(processes)
    finally:
        handle.graph.delete()
