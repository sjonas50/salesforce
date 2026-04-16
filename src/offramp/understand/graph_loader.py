"""FalkorDB graph loader (C5).

Materializes the Phase 1 ``ExtractRunResult`` into a typed FalkorDB graph
(Cypher-compatible). Subsequent passes (clustering, annotation, X-Ray
rendering) read from the graph rather than juggling raw lists.

Schema::

    (:Component {id, category, name, api_name, namespace, content_hash})
    (:BusinessProcess {id, label, size})
    (:DispatchEdge {dispatcher_cmt, handler_class, confidence})  # auxiliary

Edges::

    (:Component)-[:DEPENDS_ON]->(:Component)         # generic deps
    (:Component)-[:DISPATCHES]->(:Component)         # CMT-resolved
    (:Component)-[:CALLS]->(:Component)              # LWC -> Apex
    (:Component)-[:PARTICIPATES_IN]->(:BusinessProcess)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from falkordb import FalkorDB
from falkordb.graph import Graph as FalkorGraph

from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.extract.dispatch.class_resolver import DispatchEdge

log = get_logger(__name__)


@dataclass
class GraphHandle:
    """Owned FalkorDB connection + the named graph it works against."""

    client: FalkorDB
    graph: FalkorGraph
    name: str

    def reset(self) -> None:
        """Drop and re-create the graph — used at the start of each load."""
        # Graph may not exist yet on first load; suppress the missing-graph case.
        with contextlib.suppress(Exception):
            self.graph.delete()
        self.graph = self.client.select_graph(self.name)


def open_graph(*, url: str, name: str) -> GraphHandle:
    """Connect to FalkorDB and select a per-org graph."""
    # FalkorDB python client expects host/port — parse from a redis:// URL.
    if "://" in url:
        _, _, hostport = url.partition("://")
    else:
        hostport = url
    host, _, port_str = hostport.partition(":")
    port = int(port_str) if port_str else 6379
    client = FalkorDB(host=host, port=port)
    graph = client.select_graph(name)
    return GraphHandle(client=client, graph=graph, name=name)


def load_components(handle: GraphHandle, components: list[Component]) -> int:
    """Bulk-insert Component nodes. Returns the number written."""
    if not components:
        return 0

    # Clear any prior version of this org's graph so re-loads are idempotent.
    handle.reset()

    # Cypher's UNWIND pattern is the fastest bulk insert for FalkorDB.
    rows: list[dict[str, Any]] = [
        {
            "id": str(c.id),
            "category": c.category.value,
            "name": c.name,
            "api_name": c.api_name or c.name,
            "namespace": c.namespace or "",
            "content_hash": c.content_hash,
        }
        for c in components
    ]
    handle.graph.query(
        """
        UNWIND $rows AS row
        CREATE (n:Component {
            id: row.id,
            category: row.category,
            name: row.name,
            api_name: row.api_name,
            namespace: row.namespace,
            content_hash: row.content_hash
        })
        """,
        params={"rows": rows},
    )
    log.info("understand.graph.loaded", count=len(rows), graph=handle.name)
    return len(rows)


def load_dispatch_edges(
    handle: GraphHandle,
    edges: list[DispatchEdge],
    *,
    components_by_name: dict[str, str],
) -> int:
    """Insert DISPATCHES edges between Apex classes.

    ``components_by_name`` maps Apex class name → component id. Edges where
    we cannot resolve both endpoints are skipped (logged but not fatal — the
    coverage report already accounts for unresolved references).
    """
    if not edges:
        return 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for e in edges:
        # The dispatcher is identified by its CMT row, not an Apex class name —
        # we don't carry that through to a Component yet. For Phase 2, model
        # the edge as: a synthetic source labelled by the CMT row, terminating
        # at the resolved handler class.
        target_id = components_by_name.get(e.handler_class)
        if target_id is None:
            skipped += 1
            continue
        rows.append(
            {
                "cmt": e.dispatcher_cmt,
                "target_id": target_id,
                "field_name": e.field_name,
                "confidence": float(e.confidence),
            }
        )
    if rows:
        handle.graph.query(
            """
            UNWIND $rows AS row
            MERGE (d:DispatchSource {cmt: row.cmt})
            WITH d, row
            MATCH (t:Component {id: row.target_id})
            MERGE (d)-[r:DISPATCHES {field_name: row.field_name}]->(t)
            ON CREATE SET r.confidence = row.confidence
            """,
            params={"rows": rows},
        )
    log.info(
        "understand.graph.dispatch_edges_loaded",
        written=len(rows),
        skipped=skipped,
    )
    return len(rows)


def load_lwc_apex_edges(
    handle: GraphHandle,
    components: list[Component],
    *,
    components_by_name: dict[str, str],
) -> int:
    """Insert CALLS edges from LWC bundles to the Apex classes they import."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    for c in components:
        if c.category.value != "lwc_bundle":
            continue
        imports = c.raw.get("apex_imports", []) if isinstance(c.raw, dict) else []
        for imp in imports:
            # imp is "ClassName.methodName"; map to ClassName component id
            class_name = imp.split(".", 1)[0] if isinstance(imp, str) else ""
            target_id = components_by_name.get(class_name)
            if target_id is None:
                skipped += 1
                continue
            rows.append(
                {
                    "lwc_id": str(c.id),
                    "target_id": target_id,
                    "import": imp,
                }
            )
    if rows:
        handle.graph.query(
            """
            UNWIND $rows AS row
            MATCH (lwc:Component {id: row.lwc_id})
            MATCH (apex:Component {id: row.target_id})
            MERGE (lwc)-[r:CALLS {import: row.import}]->(apex)
            """,
            params={"rows": rows},
        )
    log.info("understand.graph.lwc_apex_edges_loaded", written=len(rows), skipped=skipped)
    return len(rows)
