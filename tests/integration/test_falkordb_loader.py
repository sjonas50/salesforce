"""FalkorDB graph loader integration test.

Requires a running FalkorDB on localhost:6379 (see CLAUDE.md / Phase 2 setup).
Uses an isolated graph name per test to avoid cross-pollution.
"""

from __future__ import annotations

import os
import uuid

import pytest

from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.understand.graph_loader import (
    load_components,
    load_dispatch_edges,
    load_lwc_apex_edges,
    open_graph,
)


def _has_falkordb() -> bool:
    """Quick reachability check so the test skips cleanly when no FalkorDB."""
    try:
        from falkordb import FalkorDB

        url = os.environ.get("FALKORDB_URL", "redis://localhost:6379")
        host = url.replace("redis://", "").split(":")[0]
        port = int(url.replace("redis://", "").split(":")[1]) if ":" in url else 6379
        client = FalkorDB(host=host, port=port)
        client.list_graphs()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_falkordb(), reason="FalkorDB not reachable"),
]


def _provenance() -> Provenance:
    return Provenance(source_tool="t", source_version="0", api_version="66.0")


def _component(
    category: CategoryName, name: str, raw: dict[str, object] | None = None
) -> Component:
    return Component(
        org_alias="t",
        category=category,
        name=name,
        api_name=name,
        raw=raw or {},
        content_hash="0" * 64,
        provenance=_provenance(),
    )


def test_load_components_creates_nodes() -> None:
    graph_name = f"test_load_{uuid.uuid4().hex[:8]}"
    handle = open_graph(url="redis://localhost:6379", name=graph_name)
    try:
        components = [
            _component(CategoryName.VALIDATION_RULE, "v1"),
            _component(CategoryName.APEX_CLASS, "LeadHandler"),
        ]
        n = load_components(handle, components)
        assert n == 2
        result = handle.graph.query("MATCH (c:Component) RETURN count(c) AS n")
        assert result.result_set[0][0] == 2
    finally:
        handle.graph.delete()


def test_load_dispatch_edges_links_to_existing_components() -> None:
    graph_name = f"test_dispatch_{uuid.uuid4().hex[:8]}"
    handle = open_graph(url="redis://localhost:6379", name=graph_name)
    try:
        target = _component(CategoryName.APEX_CLASS, "LeadHandler")
        load_components(handle, [target])
        edges = [
            DispatchEdge(
                dispatcher_cmt="Lead_Insert_001",
                handler_class="LeadHandler",
                field_name="Apex_Class__c",
                confidence=1.0,
            )
        ]
        n = load_dispatch_edges(handle, edges, components_by_name={"LeadHandler": str(target.id)})
        assert n == 1
        rel_count = handle.graph.query(
            "MATCH (:DispatchSource)-[r:DISPATCHES]->(:Component) RETURN count(r) AS n"
        )
        assert rel_count.result_set[0][0] == 1
    finally:
        handle.graph.delete()


def test_lwc_apex_edge_links() -> None:
    graph_name = f"test_lwc_{uuid.uuid4().hex[:8]}"
    handle = open_graph(url="redis://localhost:6379", name=graph_name)
    try:
        apex = _component(CategoryName.APEX_CLASS, "LeadController")
        lwc = _component(
            CategoryName.LWC_BUNDLE,
            "leadCard",
            {"apex_imports": ["LeadController.getLead", "LeadController.updateLead"]},
        )
        load_components(handle, [apex, lwc])
        n = load_lwc_apex_edges(
            handle, [apex, lwc], components_by_name={"LeadController": str(apex.id)}
        )
        # Two imports, both to the same Apex class — MERGE means one edge per
        # unique (lwc, target, import) triple, so we expect 2.
        assert n == 2
        rel_count = handle.graph.query(
            "MATCH (:Component)-[r:CALLS]->(:Component) RETURN count(r) AS n"
        )
        assert rel_count.result_set[0][0] == 2
    finally:
        handle.graph.delete()
