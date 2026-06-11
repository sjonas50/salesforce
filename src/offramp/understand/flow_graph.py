"""Load the comprehensive Flow execution graph into the knowledge graph (C5).

Reads the :class:`~offramp.extract.flow.ir.FlowIR` carried on each Flow
``Component`` (``raw["flow_ir"]``) and materializes it as a queryable graph:

* ``(:Component)-[:HAS_ELEMENT]->(:FlowElement)`` — one node per Flow element,
  plus a synthetic ``__start__`` element so the trigger is a first-class node.
* ``(:FlowElement)-[:CONTROL_FLOW {kind, is_go_to}]->(:FlowElement)`` — every
  connector, typed (next / fault / rule / default / loop_next / loop_end / …).
  This is the execution graph that makes real reverse-engineering possible.
* ``(:FlowElement)-[:READS|WRITES]->(:SObject)`` and ``->(:SObjectField)`` —
  the data dependencies.
* ``(:FlowElement)-[:CALLS]->(:Component)`` — Apex invoked by the Flow.
* ``(:FlowElement)-[:INVOKES]->(:Component)`` — subflows.
* ``(:FlowElement)-[:REFERENCES]->(:FlowResource)`` — variable/formula usage.

All queries are portable Cypher (UNWIND + MERGE), so they run unchanged on both
Neo4j and FalkorDB.
"""

from __future__ import annotations

from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component
from offramp.extract.flow.ir import FlowElement, FlowElementType, FlowIR
from offramp.understand.graph_loader import GraphHandle

log = get_logger(__name__)

FLOW_CATEGORIES = frozenset(
    {
        CategoryName.RECORD_TRIGGERED_FLOW,
        CategoryName.SCREEN_FLOW,
        CategoryName.SCHEDULE_TRIGGERED_FLOW,
        CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
        CategoryName.AUTOLAUNCHED_FLOW,
        CategoryName.FLOW_ORCHESTRATION,
        CategoryName.PROCESS_BUILDER,
    }
)

_START = "__start__"
_MUTATING = {
    FlowElementType.RECORD_CREATE,
    FlowElementType.RECORD_UPDATE,
    FlowElementType.RECORD_DELETE,
}
_READING = {
    FlowElementType.RECORD_LOOKUP,
    FlowElementType.RECORD_UPDATE,
    FlowElementType.RECORD_DELETE,
}


def load_flows(
    handle: GraphHandle,
    components: list[Component],
    *,
    components_by_name: dict[str, str],
) -> int:
    """Materialize the execution graph for every Flow component.

    ``components_by_name`` maps a component's developer name → id, used to
    resolve CALLS (Apex) and INVOKES (subflow) edges to real Component nodes.
    Returns the number of Flow components loaded.
    """
    flows = [(c, _load_ir(c)) for c in components if c.category in FLOW_CATEGORIES and _has_ir(c)]
    if not flows:
        return 0

    element_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    obj_edge_rows: list[dict[str, Any]] = []
    field_edge_rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    invoke_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    reference_rows: list[dict[str, Any]] = []

    for component, ir in flows:
        cid = str(component.id)
        resource_names = {r.name for r in ir.resources}

        # Synthetic start node + the real elements.
        element_rows.append(_element_row(cid, _START, "start", ir.label or component.name))
        for el in ir.elements:
            element_rows.append(
                _element_row(cid, el.name, el.element_type.value, el.label or el.name)
            )

        # Control-flow edges (incl. start's immediate + scheduled paths).
        for src, conn in ir.control_flow_edges():
            control_rows.append(
                {
                    "src": _eid(cid, src),
                    "dst": _eid(cid, conn.target),
                    "kind": conn.kind.value,
                    "is_go_to": conn.is_go_to,
                    "label": conn.label or "",
                }
            )

        # Resources.
        for r in ir.resources:
            resource_rows.append(
                {
                    "key": _rid(cid, r.name),
                    "name": r.name,
                    "kind": r.kind.value,
                    "data_type": r.data_type or "",
                    "object_type": r.object_type or "",
                }
            )

        for el in ir.elements:
            eid = _eid(cid, el.name)
            _emit_data_edges(eid, el, ir, obj_edge_rows, field_edge_rows)
            _emit_calls(eid, el, components_by_name, call_rows)
            _emit_invokes(eid, el, components_by_name, invoke_rows)
            for ref in el.references:
                base = ref.split(".", 1)[0]  # strip $Record.Field → element/var name
                if base in resource_names:
                    reference_rows.append({"src": eid, "dst": _rid(cid, base)})

    # Component → element membership + element node creation.
    handle.graph.query(
        """
        UNWIND $rows AS row
        MERGE (e:FlowElement {id: row.id})
        SET e.name = row.name, e.element_type = row.element_type, e.label = row.label,
            e.flow_id = row.flow_id
        WITH e, row
        MATCH (c:Component {id: row.flow_id})
        MERGE (c)-[:HAS_ELEMENT]->(e)
        """,
        params={"rows": element_rows},
    )
    _merge_edges(
        handle,
        control_rows,
        """
        UNWIND $rows AS row
        MATCH (a:FlowElement {id: row.src})
        MATCH (b:FlowElement {id: row.dst})
        MERGE (a)-[r:CONTROL_FLOW {kind: row.kind, label: row.label}]->(b)
        SET r.is_go_to = row.is_go_to
        """,
    )
    if resource_rows:
        handle.graph.query(
            """
            UNWIND $rows AS row
            MERGE (r:FlowResource {id: row.key})
            SET r.name = row.name, r.kind = row.kind,
                r.data_type = row.data_type, r.object_type = row.object_type
            """,
            params={"rows": resource_rows},
        )
    _merge_edges(
        handle,
        obj_edge_rows,
        """
        UNWIND $rows AS row
        MATCH (e:FlowElement {id: row.src})
        MERGE (o:SObject {name: row.object})
        MERGE (e)-[rel:DATA_ACCESS {mode: row.mode}]->(o)
        """,
    )
    _merge_edges(
        handle,
        field_edge_rows,
        """
        UNWIND $rows AS row
        MATCH (e:FlowElement {id: row.src})
        MERGE (f:SObjectField {key: row.key})
        SET f.object = row.object, f.field = row.field
        MERGE (e)-[rel:FIELD_ACCESS {mode: row.mode}]->(f)
        """,
    )
    _merge_edges(
        handle,
        call_rows,
        """
        UNWIND $rows AS row
        MATCH (e:FlowElement {id: row.src})
        MATCH (c:Component {id: row.target_id})
        MERGE (e)-[:CALLS]->(c)
        """,
    )
    _merge_edges(
        handle,
        invoke_rows,
        """
        UNWIND $rows AS row
        MATCH (e:FlowElement {id: row.src})
        MATCH (c:Component {id: row.target_id})
        MERGE (e)-[:INVOKES]->(c)
        """,
    )
    _merge_edges(
        handle,
        reference_rows,
        """
        UNWIND $rows AS row
        MATCH (e:FlowElement {id: row.src})
        MATCH (r:FlowResource {id: row.dst})
        MERGE (e)-[:REFERENCES]->(r)
        """,
    )

    log.info(
        "understand.graph.flows_loaded",
        flows=len(flows),
        elements=len(element_rows),
        control_edges=len(control_rows),
        data_edges=len(obj_edge_rows) + len(field_edge_rows),
        calls=len(call_rows),
        invokes=len(invoke_rows),
    )
    return len(flows)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _emit_data_edges(
    eid: str,
    el: FlowElement,
    ir: FlowIR,
    obj_rows: list[dict[str, Any]],
    field_rows: list[dict[str, Any]],
) -> None:
    """READS / WRITES edges to SObject + SObjectField for one element."""
    write_obj = _write_object(el, ir)
    read_obj = el.object if el.element_type in _READING else None

    if write_obj:
        obj_rows.append({"src": eid, "object": write_obj, "mode": "write"})
        for w in el.field_writes:
            if w.field:
                field_rows.append(
                    {
                        "src": eid,
                        "object": write_obj,
                        "field": w.field,
                        "key": f"{write_obj}.{w.field}",
                        "mode": "write",
                    }
                )
    if read_obj:
        obj_rows.append({"src": eid, "object": read_obj, "mode": "read"})
        read_fields = [f.field for f in el.field_reads if f.field] + el.queried_fields
        for field in read_fields:
            field_rows.append(
                {
                    "src": eid,
                    "object": read_obj,
                    "field": field,
                    "key": f"{read_obj}.{field}",
                    "mode": "read",
                }
            )


def _write_object(el: FlowElement, ir: FlowIR) -> str | None:
    if el.element_type not in _MUTATING:
        return None
    if el.object:
        return el.object
    # record-update against $Record resolves to the trigger object.
    if el.element_type is FlowElementType.RECORD_UPDATE and ir.start and ir.start.object:
        return ir.start.object
    return None


def _emit_calls(
    eid: str,
    el: FlowElement,
    components_by_name: dict[str, str],
    rows: list[dict[str, Any]],
) -> None:
    apex = el.calls_apex()
    if not apex:
        return
    target_id = components_by_name.get(apex)
    if target_id:
        rows.append({"src": eid, "target_id": target_id})


def _emit_invokes(
    eid: str,
    el: FlowElement,
    components_by_name: dict[str, str],
    rows: list[dict[str, Any]],
) -> None:
    if el.element_type is not FlowElementType.SUBFLOW or not el.flow_name:
        return
    target_id = components_by_name.get(el.flow_name)
    if target_id:
        rows.append({"src": eid, "target_id": target_id})


def _merge_edges(handle: GraphHandle, rows: list[dict[str, Any]], cypher: str) -> None:
    if rows:
        handle.graph.query(cypher, params={"rows": rows})


def _has_ir(c: Component) -> bool:
    return isinstance(c.raw, dict) and isinstance(c.raw.get("flow_ir"), dict)


def _load_ir(c: Component) -> FlowIR:
    return FlowIR.model_validate(c.raw["flow_ir"])


def _eid(component_id: str, element_name: str) -> str:
    return f"{component_id}:{element_name}"


def _rid(component_id: str, resource_name: str) -> str:
    return f"{component_id}::res::{resource_name}"


def _element_row(cid: str, name: str, element_type: str, label: str) -> dict[str, Any]:
    return {
        "id": _eid(cid, name),
        "flow_id": cid,
        "name": name,
        "element_type": element_type,
        "label": label,
    }
