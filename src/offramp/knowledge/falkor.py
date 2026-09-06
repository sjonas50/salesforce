"""Persistent FalkorDB mirror of the knowledge store.

Unlike the per-scan graph loader, nothing here resets the graph: processes,
steps, objects, and fields are ``MERGE``d by stable ids, and every scan adds
a ``(:Scan)-[:OBSERVED]->(:Process)`` edge. Query examples::

    MATCH (p:Process)-[:WRITES]->(f:Field {api_name: 'Lead.OwnerId'}) RETURN p.name
    MATCH (s:Scan {org_alias: 'acme'})-[:OBSERVED]->(p:Process) RETURN count(p)
    MATCH (p:Process) WHERE p.fingerprint = $fp RETURN p.name, p.kind
"""

from __future__ import annotations

from typing import Any

from offramp.core.logging import get_logger
from offramp.core.process import ProcessDefinition
from offramp.knowledge.store import ScanRecord
from offramp.understand.graph_loader import GraphHandle

log = get_logger(__name__)


def persist(handle: GraphHandle, processes: list[ProcessDefinition], scan: ScanRecord) -> int:
    """Merge processes + scan into the persistent graph. Returns processes written."""
    g = handle.graph
    g.query(
        "MERGE (s:Scan {id: $id}) SET s.org_alias = $org, s.scanned_at = $at, s.processes = $n",
        params={
            "id": scan.scan_id,
            "org": scan.org_alias,
            "at": scan.scanned_at.isoformat(),
            "n": len(scan.process_ids),
        },
    )
    for p in processes:
        g.query(
            """
            MERGE (p:Process {id: $id})
            SET p.name = $name, p.label = $label, p.kind = $kind, p.fingerprint = $fp,
                p.fidelity = $fid, p.active = $active, p.trigger = $trigger, p.trigger_object = $tobj
            """,
            params={
                "id": p.id,
                "name": p.name,
                "label": p.label,
                "kind": p.kind,
                "fp": p.fingerprint,
                "fid": p.fidelity.value,
                "active": p.active,
                "trigger": p.trigger.kind.value,
                "tobj": p.trigger.object or "",
            },
        )
        g.query(
            "MATCH (s:Scan {id: $sid}), (p:Process {id: $pid}) MERGE (s)-[:OBSERVED]->(p)",
            params={"sid": scan.scan_id, "pid": p.id},
        )
        for src in p.sources:
            g.query(
                "MATCH (p:Process {id: $pid}) MERGE (o:Org {alias: $org}) MERGE (p)-[r:FOUND_IN {api_name: $api}]->(o) SET r.category = $cat",
                params={
                    "pid": p.id,
                    "org": src.org_alias,
                    "api": src.api_name,
                    "cat": src.category,
                },
            )
        for obj in p.objects:
            g.query(
                "MATCH (p:Process {id: $pid}) MERGE (o:Object {api_name: $obj}) MERGE (p)-[:TOUCHES]->(o)",
                params={"pid": p.id, "obj": obj},
            )
        for f in p.fields_read:
            g.query(
                "MATCH (p:Process {id: $pid}) MERGE (f:Field {api_name: $f}) MERGE (p)-[:READS]->(f)",
                params={"pid": p.id, "f": f},
            )
        for f in p.fields_written:
            g.query(
                "MATCH (p:Process {id: $pid}) MERGE (f:Field {api_name: $f}) MERGE (p)-[:WRITES]->(f)",
                params={"pid": p.id, "f": f},
            )
        for c in p.calls:
            g.query(
                "MATCH (p:Process {id: $pid}) MERGE (t:Callable {name: $c}) MERGE (p)-[:CALLS]->(t)",
                params={"pid": p.id, "c": c},
            )
        rows: list[dict[str, Any]] = [
            {
                "sid": f"{p.id}:{s.id}",
                "step": s.id,
                "kind": s.kind.value,
                "label": s.label,
                "object": s.object or "",
                "target": s.target or "",
                "order": i,
            }
            for i, s in enumerate(p.steps)
        ]
        if rows:
            g.query(
                """
                MATCH (p:Process {id: $pid})
                UNWIND $rows AS row
                MERGE (st:Step {id: row.sid})
                SET st.step = row.step, st.kind = row.kind, st.label = row.label, st.object = row.object, st.target = row.target, st.order = row.order
                MERGE (p)-[:HAS_STEP]->(st)
                """,
                params={"pid": p.id, "rows": rows},
            )
            edges = [
                {"a": f"{p.id}:{s.id}", "b": f"{p.id}:{s.next}"} for s in p.steps if s.next
            ] + [
                {"a": f"{p.id}:{s.id}", "b": f"{p.id}:{b.next}"}
                for s in p.steps
                for b in s.branches
                if b.next
            ]
            if edges:
                g.query(
                    "UNWIND $edges AS e MATCH (a:Step {id: e.a}), (b:Step {id: e.b}) MERGE (a)-[:NEXT]->(b)",
                    params={"edges": edges},
                )
    log.info(
        "knowledge.falkor.persisted", graph=handle.name, scan=scan.scan_id, processes=len(processes)
    )
    return len(processes)
