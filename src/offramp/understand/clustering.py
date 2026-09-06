"""Community detection on the dependency graph → BusinessProcess clusters.

Louvain (networkx) by default; Leiden when ``leidenalg`` + ``igraph`` are
installed and ``algorithm="leiden"`` is requested. Both take a resolution
parameter. Schema nodes participate so a process cluster naturally gathers
the objects and fields its automation shares; external nodes (templates,
labels) are excluded so they don't glue unrelated processes together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
from networkx.algorithms.community import louvain_communities

from offramp.core.logging import get_logger
from offramp.understand.dependencies import DependencyGraph

log = get_logger(__name__)

_EDGE_WEIGHT = {"calls": 3.0, "dispatches": 3.0, "triggers": 2.0, "owns": 2.0, "references": 1.0}


@dataclass
class BusinessProcess:
    """One detected cluster of related components + the data they share."""

    process_id: str
    label: str
    component_ids: list[str]
    object_names: list[str] = field(default_factory=list)
    field_ids: list[str] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.component_ids)


def build_networkx_graph(dep: DependencyGraph, *, include_schema: bool = True) -> nx.Graph:
    g = nx.Graph()
    for n in dep.nodes.values():
        if n.kind == "external":
            continue
        if not include_schema and n.kind != "component":
            continue
        g.add_node(
            n.id,
            kind=n.kind,
            category=n.category,
            name=n.name,
            api_name=n.api_name,
            object_name=n.object_name or "",
        )
    for e in dep.edges:
        s, t = str(e.source_id), str(e.target_id)
        if s in g and t in g:
            w = _EDGE_WEIGHT.get(e.kind.value, 1.0) * e.confidence
            if g.has_edge(s, t):
                g[s][t]["weight"] += w
            else:
                g.add_edge(s, t, weight=w, kind=e.kind.value)
    return g


def detect_processes(
    g: nx.Graph, *, resolution: float = 1.0, algorithm: str = "louvain", seed: int = 42
) -> list[BusinessProcess]:
    if g.number_of_nodes() == 0:
        return []
    if algorithm == "leiden":
        communities = _leiden(g, resolution, seed)
    else:
        communities = [
            set(c)
            for c in louvain_communities(g, weight="weight", resolution=resolution, seed=seed)
        ]
    processes: list[BusinessProcess] = []
    for community in communities:
        comps = [n for n in community if g.nodes[n]["kind"] == "component"]
        if not comps:
            continue
        objects = sorted(
            {g.nodes[n]["api_name"] for n in community if g.nodes[n]["kind"] == "object"}
            | {g.nodes[n]["object_name"] for n in comps if g.nodes[n]["object_name"]}
        )
        fields = [n for n in community if g.nodes[n]["kind"] == "field"]
        cats: dict[str, int] = {}
        for n in comps:
            cats[g.nodes[n]["category"]] = cats.get(g.nodes[n]["category"], 0) + 1
        processes.append(
            BusinessProcess(
                process_id="",
                label="",
                component_ids=sorted(comps, key=lambda n: g.nodes[n]["api_name"].lower()),
                object_names=objects,
                field_ids=fields,
                categories=dict(sorted(cats.items())),
            )
        )
    processes.sort(key=lambda p: (-p.size, p.object_names))
    for i, p in enumerate(processes):
        p.process_id = f"bp_{i:03d}"
        p.label = _label(p, g)
    log.info(
        "understand.clustering.detected",
        count=len(processes),
        resolution=resolution,
        algorithm=algorithm,
    )
    return processes


def _label(p: BusinessProcess, g: nx.Graph) -> str:
    objs = [o for o in p.object_names if not o.endswith(("__mdt", "__e"))][:2]
    head = " / ".join(objs) if objs else "Cross-object"
    top = (
        max(p.categories.items(), key=lambda kv: kv[1])[0].replace("_", " ")
        if p.categories
        else "components"
    )
    return f"{head} — {p.size} components, mostly {top}"


def _leiden(g: nx.Graph, resolution: float, seed: int) -> list[set[str]]:
    try:
        import igraph as ig  # type: ignore[import-not-found]
        import leidenalg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — optional extra
        raise RuntimeError(
            "Leiden requires the 'leiden' extra (python-igraph + leidenalg)"
        ) from exc
    nodes = list(g.nodes)
    index = {n: i for i, n in enumerate(nodes)}
    ig_g = ig.Graph(n=len(nodes), edges=[(index[u], index[v]) for u, v in g.edges], directed=False)
    ig_g.es["weight"] = [g[u][v].get("weight", 1.0) for u, v in g.edges]
    part = leidenalg.find_partition(
        ig_g,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        seed=seed,
    )
    return [{nodes[i] for i in community} for community in part]


def write_processes_to_graph(handle: Any, processes: list[BusinessProcess]) -> int:
    """Persist BusinessProcess nodes + PARTICIPATES_IN edges to FalkorDB."""
    if not processes:
        return 0
    handle.graph.query(
        "UNWIND $rows AS row CREATE (bp:BusinessProcess {id: row.id, label: row.label, size: row.size})",
        params={
            "rows": [{"id": p.process_id, "label": p.label, "size": p.size} for p in processes]
        },
    )
    edge_rows = [
        {"nid": nid, "pid": p.process_id}
        for p in processes
        for nid in [*p.component_ids, *p.field_ids]
    ]
    handle.graph.query(
        """
        UNWIND $rows AS row
        MATCH (n {id: row.nid}), (bp:BusinessProcess {id: row.pid})
        MERGE (n)-[:PARTICIPATES_IN]->(bp)
        """,
        params={"rows": edge_rows},
    )
    return len(processes)
