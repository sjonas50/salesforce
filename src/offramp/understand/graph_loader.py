"""FalkorDB graph loader (C5).

Materializes a :class:`DependencyGraph` into a typed FalkorDB graph
(Cypher-compatible). The X-Ray CLI can also run without FalkorDB
(``--no-graph-db``): clustering and impact analysis work on the in-memory
graph; FalkorDB is for interactive exploration and the hosted service.

Schema::

    (:Component {id, category, name, api_name, object_name, active})
    (:Object    {id, api_name, custom})
    (:Field     {id, api_name, object_name, field_type, custom})
    (:RecordType{id, api_name, object_name})
    (:External  {id, category, api_name})
    (:BusinessProcess {id, label, size})

Edges carry ``kind``, ``evidence``, ``confidence``, ``api`` (corroborated),
``notes``. Relationship type = upper-cased ``kind`` (REFERENCES, CALLS,
TRIGGERS, OWNS, DISPATCHES, ...), plus PARTICIPATES_IN to BusinessProcess.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from falkordb import FalkorDB
from falkordb.graph import Graph as FalkorGraph

from offramp.core.logging import get_logger
from offramp.understand.dependencies import DependencyGraph

log = get_logger(__name__)

_LABELS = {
    "component": "Component",
    "object": "Object",
    "field": "Field",
    "record_type": "RecordType",
    "external": "External",
    "cmt_record": "ConfigRecord",
}


@dataclass
class GraphHandle:
    """Owned FalkorDB connection + the named graph it works against."""

    client: FalkorDB
    graph: FalkorGraph
    name: str

    def reset(self) -> None:
        with contextlib.suppress(Exception):
            self.graph.delete()
        self.graph = self.client.select_graph(self.name)


def open_graph(*, url: str, name: str) -> GraphHandle:
    if "://" in url:
        _, _, hostport = url.partition("://")
    else:
        hostport = url
    host, _, port_str = hostport.partition(":")
    port = int(port_str) if port_str else 6379
    client = FalkorDB(host=host, port=port)
    graph = client.select_graph(name)
    return GraphHandle(client=client, graph=graph, name=name)


def load_dependency_graph(handle: GraphHandle, dep: DependencyGraph) -> tuple[int, int]:
    """Replace the org's graph with ``dep``. Returns (nodes, edges) written."""
    handle.reset()
    by_label: dict[str, list[dict[str, Any]]] = {}
    for n in dep.nodes.values():
        label = _LABELS.get(n.kind, "External")
        by_label.setdefault(label, []).append(
            {
                "id": n.id,
                "kind": n.kind,
                "category": n.category,
                "name": n.name,
                "api_name": n.api_name,
                "object_name": n.object_name or "",
                "active": bool(n.meta.get("active", True)),
                "custom": bool(n.meta.get("custom", False)),
                "field_type": str(n.meta.get("field_type") or ""),
            }
        )
    for label, rows in by_label.items():
        handle.graph.query(
            f"""
            UNWIND $rows AS row
            CREATE (n:{label} {{
                id: row.id, kind: row.kind, category: row.category, name: row.name,
                api_name: row.api_name, object_name: row.object_name, active: row.active,
                custom: row.custom, field_type: row.field_type
            }})
            """,
            params={"rows": rows},
        )
    for label in sorted(set(_LABELS.values())):
        with contextlib.suppress(Exception):  # index may already exist on a reused graph name
            handle.graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")

    by_kind: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for e in dep.edges:
        s_node = dep.node(str(e.source_id))
        t_node = dep.node(str(e.target_id))
        if s_node is None or t_node is None:
            continue
        key = (
            e.kind.value.upper(),
            _LABELS.get(s_node.kind, "External"),
            _LABELS.get(t_node.kind, "External"),
        )
        by_kind.setdefault(key, []).append(
            {
                "s": str(e.source_id),
                "t": str(e.target_id),
                "evidence": e.evidence.value,
                "confidence": float(e.confidence),
                "api": bool(e.corroborated_by_api),
                "notes": e.notes or "",
            }
        )
    written = 0
    for (rel, s_label, t_label), rows in by_kind.items():
        handle.graph.query(
            f"""
            UNWIND $rows AS row
            MATCH (s:{s_label} {{id: row.s}}), (t:{t_label} {{id: row.t}})
            CREATE (s)-[:{rel} {{evidence: row.evidence, confidence: row.confidence, api: row.api, notes: row.notes}}]->(t)
            """,
            params={"rows": rows},
        )
        written += len(rows)
    log.info("understand.graph.loaded", graph=handle.name, nodes=len(dep.nodes), edges=written)
    return len(dep.nodes), written
