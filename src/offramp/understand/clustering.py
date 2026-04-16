"""Leiden clustering on the Component dependency graph.

The architecture (v2.1 §8.2) uses Leiden clustering with a tunable resolution
parameter. networkx ships ``louvain_communities``; the Leiden variant lives in
``networkx-graph-tool`` extensions which add a heavy native dep. For Phase 2
we use Louvain — the algorithmic family is the same and the resolution
parameter behaves the same way. The cluster IDs are stored back as
:class:`BusinessProcess` nodes in FalkorDB.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx
from networkx.algorithms.community import louvain_communities

from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.understand.graph_loader import GraphHandle

log = get_logger(__name__)


@dataclass
class BusinessProcess:
    """One detected cluster of related components."""

    process_id: str  # 'bp_<index>'
    label: str  # auto-derived from member categories
    component_ids: list[str]


def build_networkx_graph(
    components: list[Component],
    dispatch_edges: list[DispatchEdge],
    *,
    components_by_name: dict[str, str],
) -> nx.Graph:
    """Project the FalkorDB graph into a networkx graph for clustering.

    Edges:

    * Apex Trigger ↔ Apex Class (regex from dispatch + LWC import linkage)
    * LWC Bundle ↔ Apex Class (from Phase 1 LWC analyzer)
    * CMT-resolved DISPATCHES edges (treat the dispatcher CMT as a virtual
      hub node so cluster membership flows through it)
    """
    g = nx.Graph()
    by_id = {str(c.id): c for c in components}
    for c in components:
        g.add_node(
            str(c.id),
            category=c.category.value,
            name=c.name,
            api_name=c.api_name or c.name,
        )

    # LWC -> Apex edges
    for c in components:
        if c.category.value != "lwc_bundle":
            continue
        imports = c.raw.get("apex_imports", []) if isinstance(c.raw, dict) else []
        for imp in imports:
            if not isinstance(imp, str):
                continue
            class_name = imp.split(".", 1)[0]
            target_id = components_by_name.get(class_name)
            if target_id and target_id in by_id:
                g.add_edge(str(c.id), target_id, kind="calls")

    # Dispatch edges via virtual CMT hub nodes
    for e in dispatch_edges:
        target_id = components_by_name.get(e.handler_class)
        if not target_id:
            continue
        hub = f"cmt:{e.dispatcher_cmt}"
        g.add_node(hub, category="dispatch_hub", name=e.dispatcher_cmt, api_name=e.dispatcher_cmt)
        g.add_edge(hub, target_id, kind="dispatches", weight=e.confidence)

    return g


def detect_processes(g: nx.Graph, *, resolution: float = 1.0) -> list[BusinessProcess]:
    """Run Louvain (Leiden-family) community detection.

    ``resolution`` higher → more, smaller clusters; lower → fewer, bigger.
    Default 1.0 matches networkx's Louvain default.
    """
    if g.number_of_nodes() == 0:
        return []
    # Singleton nodes (isolated) become their own one-element clusters.
    communities = louvain_communities(g, resolution=resolution, seed=42)
    processes: list[BusinessProcess] = []
    for i, community in enumerate(communities):
        # Filter to real Component nodes (drop the virtual CMT hubs).
        members = [n for n in community if g.nodes[n].get("category") not in {"dispatch_hub", None}]
        if not members:
            continue
        # Cluster label = most common category in the cluster, qualified by size.
        cats = [g.nodes[n]["category"] for n in members]
        top_cat = max(set(cats), key=cats.count)
        label = f"{top_cat} cluster ({len(members)} components)"
        processes.append(
            BusinessProcess(
                process_id=f"bp_{i:03d}",
                label=label,
                component_ids=members,
            )
        )
    log.info("understand.clustering.detected", count=len(processes), resolution=resolution)
    return processes


def write_processes_to_graph(handle: GraphHandle, processes: list[BusinessProcess]) -> int:
    """Persist BusinessProcess nodes + PARTICIPATES_IN edges."""
    if not processes:
        return 0
    proc_rows = [
        {"id": p.process_id, "label": p.label, "size": len(p.component_ids)} for p in processes
    ]
    handle.graph.query(
        """
        UNWIND $rows AS row
        CREATE (bp:BusinessProcess {id: row.id, label: row.label, size: row.size})
        """,
        params={"rows": proc_rows},
    )
    edge_rows = [
        {"comp_id": cid, "process_id": p.process_id} for p in processes for cid in p.component_ids
    ]
    handle.graph.query(
        """
        UNWIND $rows AS row
        MATCH (c:Component {id: row.comp_id})
        MATCH (bp:BusinessProcess {id: row.process_id})
        MERGE (c)-[:PARTICIPATES_IN]->(bp)
        """,
        params={"rows": edge_rows},
    )
    return len(processes)
