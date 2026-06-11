"""Parse a ``.flow-meta.xml`` body into the comprehensive :class:`FlowIR`.

The parser walks every element collection Salesforce can emit and normalizes
each into a :class:`FlowElement`, capturing connectors (typed by
:class:`ConnectorKind`), the data each element reads/writes, decision logic, and
external invocations. Resources (variables / formulas / choices) are parsed too.

Robustness: Salesforce omits empty collections and emits a *single* child as a
dict rather than a one-element list, so every collection access goes through
``_as_list``. Unknown element kinds are still captured via the ``raw`` escape
hatch on the closest typed field, so coverage degrades gracefully rather than
dropping data silently.
"""

from __future__ import annotations

import re
from typing import Any

from offramp.extract.flow.ir import (
    ConnectorKind,
    FlowCondition,
    FlowConnector,
    FlowDecisionRule,
    FlowElement,
    FlowElementType,
    FlowFieldRead,
    FlowFieldWrite,
    FlowIR,
    FlowResource,
    FlowResourceKind,
    FlowScheduledPath,
    FlowStart,
)

_MERGE_FIELD_RE = re.compile(r"\{!([^}]+)\}")
_VALUE_KINDS = (
    "stringValue",
    "numberValue",
    "booleanValue",
    "dateValue",
    "dateTimeValue",
    "elementReference",
    "apexValue",
    "sobjectValue",
)


def parse_flow(raw_xml: str, *, api_name: str) -> FlowIR:
    """Parse Flow XML text into a :class:`FlowIR`."""
    # Imported lazily: categories.xml_utils lives in the `categories` package
    # whose __init__ eagerly imports this module via the Flow extractor. A
    # top-level import here would form a bundle-style cycle.
    from offramp.extract.categories.xml_utils import parse_xml

    parsed = parse_xml(raw_xml)
    body = parsed.get("Flow")
    if not isinstance(body, dict):
        raise ValueError(f"Flow {api_name}: XML root is not a <Flow> element")
    return _parse_body(body, api_name=api_name)


def parse_flow_dict(body: dict[str, Any], *, api_name: str) -> FlowIR:
    """Parse an already-de-namespaced Flow dict (e.g. from a reconciled record)."""
    return _parse_body(body, api_name=api_name)


def _parse_body(body: dict[str, Any], *, api_name: str) -> FlowIR:
    elements: list[FlowElement] = []
    elements += [_parse_assignment(d) for d in _as_list(body.get("assignments"))]
    elements += [_parse_decision(d) for d in _as_list(body.get("decisions"))]
    elements += [_parse_loop(d) for d in _as_list(body.get("loops"))]
    elements += [_parse_record_create(d) for d in _as_list(body.get("recordCreates"))]
    elements += [_parse_record_update(d) for d in _as_list(body.get("recordUpdates"))]
    elements += [_parse_record_delete(d) for d in _as_list(body.get("recordDeletes"))]
    elements += [_parse_record_lookup(d) for d in _as_list(body.get("recordLookups"))]
    elements += [
        _simple_element(d, FlowElementType.RECORD_ROLLBACK)
        for d in _as_list(body.get("recordRollbacks"))
    ]
    elements += [_parse_action_call(d) for d in _as_list(body.get("actionCalls"))]
    elements += [_parse_apex_plugin_call(d) for d in _as_list(body.get("apexPluginCalls"))]
    elements += [_parse_subflow(d) for d in _as_list(body.get("subflows"))]
    elements += [_parse_screen(d) for d in _as_list(body.get("screens"))]
    elements += [_parse_wait(d) for d in _as_list(body.get("waits"))]
    elements += [
        _simple_element(d, FlowElementType.COLLECTION_PROCESSOR)
        for d in _as_list(body.get("collectionProcessors"))
    ]
    elements += [
        _simple_element(d, FlowElementType.TRANSFORM) for d in _as_list(body.get("transforms"))
    ]
    elements += [_simple_element(d, FlowElementType.STEP) for d in _as_list(body.get("steps"))]
    elements += [_parse_orchestrated_stage(d) for d in _as_list(body.get("orchestratedStages"))]
    elements += [
        _simple_element(d, FlowElementType.CUSTOM_ERROR) for d in _as_list(body.get("customErrors"))
    ]

    resources = _parse_resources(body)
    start = _parse_start(body.get("start") if isinstance(body.get("start"), dict) else None)

    return FlowIR(
        api_name=api_name,
        label=_text(body.get("label")),
        api_version=_text(body.get("apiVersion")) or "66.0",
        process_type=_text(body.get("processType")) or "",
        status=_text(body.get("status")) or "Active",
        run_in_mode=_text(body.get("runInMode")),
        interview_label=_text(body.get("interviewLabel")),
        start=start,
        elements=elements,
        resources=resources,
    )


# --------------------------------------------------------------------------- #
# Element parsers
# --------------------------------------------------------------------------- #


def _common(d: dict[str, Any]) -> dict[str, Any]:
    """Shared element header fields."""
    return {
        "name": _text(d.get("name")),
        "label": _text(d.get("label")),
        "description": _text(d.get("description")),
        "location_x": _maybe_float(d.get("locationX")),
        "location_y": _maybe_float(d.get("locationY")),
        "raw": d,
    }


def _simple_element(d: dict[str, Any], etype: FlowElementType) -> FlowElement:
    """An element we capture structurally (connectors) but don't deeply model yet."""
    return FlowElement(
        element_type=etype,
        connectors=_connectors_of(d),
        **_common(d),
    )


def _parse_assignment(d: dict[str, Any]) -> FlowElement:
    refs: list[str] = []
    writes: list[FlowFieldWrite] = []
    for item in _as_list(d.get("assignmentItems")):
        if not isinstance(item, dict):
            continue
        ref = _text(item.get("assignToReference"))
        value, kind = _parse_value(item.get("value"))
        writes.append(FlowFieldWrite(field=ref, value=value, value_kind=kind))
        if ref:
            refs.append(ref)
        if kind == "elementReference" and value:
            refs.append(value)
    return FlowElement(
        element_type=FlowElementType.ASSIGNMENT,
        connectors=_connectors_of(d),
        field_writes=writes,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_decision(d: dict[str, Any]) -> FlowElement:
    rules: list[FlowDecisionRule] = []
    refs: list[str] = []
    connectors: list[FlowConnector] = []
    for rule in _as_list(d.get("rules")):
        if not isinstance(rule, dict):
            continue
        conditions = _parse_conditions(rule.get("conditions"))
        for c in conditions:
            if c.left:
                refs.append(c.left)
        conn = _connector(rule.get("connector"), ConnectorKind.RULE, label=_text(rule.get("name")))
        if conn is not None:
            connectors.append(conn)
        rules.append(
            FlowDecisionRule(
                name=_text(rule.get("name")),
                label=_text(rule.get("label")),
                condition_logic=_text(rule.get("conditionLogic")) or "and",
                conditions=conditions,
                connector=conn,
            )
        )
    default_conn = _connector(
        d.get("defaultConnector"),
        ConnectorKind.DEFAULT,
        label=_text(d.get("defaultConnectorLabel")) or "default",
    )
    if default_conn is not None:
        connectors.append(default_conn)
    return FlowElement(
        element_type=FlowElementType.DECISION,
        connectors=connectors,
        rules=rules,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_loop(d: dict[str, Any]) -> FlowElement:
    connectors: list[FlowConnector] = []
    nxt = _connector(d.get("nextValueConnector"), ConnectorKind.LOOP_NEXT)
    if nxt is not None:
        connectors.append(nxt)
    end = _connector(d.get("noMoreValuesConnector"), ConnectorKind.LOOP_END)
    if end is not None:
        connectors.append(end)
    coll = _text(d.get("collectionReference"))
    return FlowElement(
        element_type=FlowElementType.LOOP,
        connectors=connectors,
        collection_reference=coll or None,
        references=_dedupe([coll]) if coll else [],
        **_common(d),
    )


def _parse_record_create(d: dict[str, Any]) -> FlowElement:
    writes, refs = _input_assignments(d.get("inputAssignments"))
    return FlowElement(
        element_type=FlowElementType.RECORD_CREATE,
        connectors=_connectors_of(d, fault=True),
        object=_text(d.get("object")) or None,
        field_writes=writes,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_record_update(d: dict[str, Any]) -> FlowElement:
    writes, refs = _input_assignments(d.get("inputAssignments"))
    reads = _parse_filters(d.get("filters"))
    input_ref = _text(d.get("inputReference"))
    if input_ref:
        refs.append(input_ref)
    return FlowElement(
        element_type=FlowElementType.RECORD_UPDATE,
        connectors=_connectors_of(d, fault=True),
        object=_text(d.get("object")) or None,
        input_reference=input_ref or None,
        field_writes=writes,
        field_reads=reads,
        filter_logic=_text(d.get("filterLogic")) or None,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_record_delete(d: dict[str, Any]) -> FlowElement:
    reads = _parse_filters(d.get("filters"))
    input_ref = _text(d.get("inputReference"))
    return FlowElement(
        element_type=FlowElementType.RECORD_DELETE,
        connectors=_connectors_of(d, fault=True),
        object=_text(d.get("object")) or None,
        input_reference=input_ref or None,
        field_reads=reads,
        filter_logic=_text(d.get("filterLogic")) or None,
        references=_dedupe([input_ref]) if input_ref else [],
        **_common(d),
    )


def _parse_record_lookup(d: dict[str, Any]) -> FlowElement:
    reads = _parse_filters(d.get("filters"))
    queried = [_text(f) for f in _as_list(d.get("queriedFields")) if _text(f)]
    writes: list[FlowFieldWrite] = []
    refs: list[str] = []
    for oa in _as_list(d.get("outputAssignments")):
        if not isinstance(oa, dict):
            continue
        assign_to = _text(oa.get("assignToReference"))
        field = _text(oa.get("field"))
        writes.append(FlowFieldWrite(field=field, value=assign_to or None, value_kind="reference"))
        if assign_to:
            refs.append(assign_to)
    return FlowElement(
        element_type=FlowElementType.RECORD_LOOKUP,
        connectors=_connectors_of(d, fault=True),
        object=_text(d.get("object")) or None,
        field_reads=reads,
        field_writes=writes,
        queried_fields=queried,
        filter_logic=_text(d.get("filterLogic")) or None,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_action_call(d: dict[str, Any]) -> FlowElement:
    refs = _input_parameter_refs(d.get("inputParameters"))
    return FlowElement(
        element_type=FlowElementType.ACTION_CALL,
        connectors=_connectors_of(d, fault=True),
        action_name=_text(d.get("actionName")) or None,
        action_type=_text(d.get("actionType")) or None,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_apex_plugin_call(d: dict[str, Any]) -> FlowElement:
    return FlowElement(
        element_type=FlowElementType.APEX_PLUGIN_CALL,
        connectors=_connectors_of(d, fault=True),
        apex_class=_text(d.get("apexClass")) or None,
        **_common(d),
    )


def _parse_subflow(d: dict[str, Any]) -> FlowElement:
    refs = _input_assignment_refs(d.get("inputAssignments"))
    return FlowElement(
        element_type=FlowElementType.SUBFLOW,
        connectors=_connectors_of(d, fault=True),
        flow_name=_text(d.get("flowName")) or None,
        references=_dedupe(refs),
        **_common(d),
    )


def _parse_screen(d: dict[str, Any]) -> FlowElement:
    return FlowElement(
        element_type=FlowElementType.SCREEN,
        connectors=_connectors_of(d),
        **_common(d),
    )


def _parse_wait(d: dict[str, Any]) -> FlowElement:
    connectors: list[FlowConnector] = []
    for ev in _as_list(d.get("waitEvents")):
        if not isinstance(ev, dict):
            continue
        conn = _connector(
            ev.get("connector"), ConnectorKind.WAIT_EVENT, label=_text(ev.get("name"))
        )
        if conn is not None:
            connectors.append(conn)
    default_conn = _connector(d.get("defaultConnector"), ConnectorKind.DEFAULT)
    if default_conn is not None:
        connectors.append(default_conn)
    fault = _connector(d.get("faultConnector"), ConnectorKind.FAULT)
    if fault is not None:
        connectors.append(fault)
    return FlowElement(
        element_type=FlowElementType.WAIT,
        connectors=connectors,
        **_common(d),
    )


def _parse_orchestrated_stage(d: dict[str, Any]) -> FlowElement:
    return FlowElement(
        element_type=FlowElementType.ORCHESTRATED_STAGE,
        connectors=_connectors_of(d, fault=True),
        **_common(d),
    )


# --------------------------------------------------------------------------- #
# Start + resources
# --------------------------------------------------------------------------- #


def _parse_start(d: dict[str, Any] | None) -> FlowStart | None:
    if not d:
        return None
    scheduled: list[FlowScheduledPath] = []
    for sp in _as_list(d.get("scheduledPaths")):
        if not isinstance(sp, dict):
            continue
        scheduled.append(
            FlowScheduledPath(
                name=_text(sp.get("name")),
                label=_text(sp.get("label")),
                offset_number=_maybe_int(sp.get("offsetNumber")),
                offset_unit=_text(sp.get("offsetUnit")),
                time_source=_text(sp.get("timeSource")),
                connector=_connector(
                    sp.get("connector"), ConnectorKind.SCHEDULED_PATH, label=_text(sp.get("name"))
                ),
            )
        )
    return FlowStart(
        trigger_type=_text(d.get("triggerType")) or None,
        record_trigger_type=_text(d.get("recordTriggerType")) or None,
        object=_text(d.get("object")) or None,
        filter_logic=_text(d.get("filterLogic")) or None,
        filters=_parse_filters(d.get("filters")),
        schedule_frequency=_text((d.get("schedule") or {}).get("frequency"))
        if isinstance(d.get("schedule"), dict)
        else None,
        run_in_mode=_text(d.get("runInMode")) or None,
        connector=_connector(d.get("connector"), ConnectorKind.IMMEDIATE),
        scheduled_paths=scheduled,
        location_x=_maybe_float(d.get("locationX")),
        location_y=_maybe_float(d.get("locationY")),
    )


def _parse_resources(body: dict[str, Any]) -> list[FlowResource]:
    out: list[FlowResource] = []
    for v in _as_list(body.get("variables")):
        if not isinstance(v, dict):
            continue
        out.append(
            FlowResource(
                name=_text(v.get("name")),
                kind=FlowResourceKind.VARIABLE,
                data_type=_text(v.get("dataType")) or None,
                object_type=_text(v.get("objectType")) or None,
                is_collection=_bool(v.get("isCollection")),
                is_input=_bool(v.get("isInput")),
                is_output=_bool(v.get("isOutput")),
            )
        )
    for c in _as_list(body.get("constants")):
        if isinstance(c, dict):
            out.append(
                FlowResource(
                    name=_text(c.get("name")),
                    kind=FlowResourceKind.CONSTANT,
                    data_type=_text(c.get("dataType")) or None,
                )
            )
    for f in _as_list(body.get("formulas")):
        if isinstance(f, dict):
            expr = _text(f.get("expression"))
            out.append(
                FlowResource(
                    name=_text(f.get("name")),
                    kind=FlowResourceKind.FORMULA,
                    data_type=_text(f.get("dataType")) or None,
                    expression=expr or None,
                    references=_merge_fields(expr),
                )
            )
    for t in _as_list(body.get("textTemplates")):
        if isinstance(t, dict):
            text = _text(t.get("text"))
            out.append(
                FlowResource(
                    name=_text(t.get("name")),
                    kind=FlowResourceKind.TEXT_TEMPLATE,
                    expression=text or None,
                    references=_merge_fields(text),
                )
            )
    for ch in _as_list(body.get("choices")):
        if isinstance(ch, dict):
            out.append(FlowResource(name=_text(ch.get("name")), kind=FlowResourceKind.CHOICE))
    for dc in _as_list(body.get("dynamicChoiceSets")):
        if isinstance(dc, dict):
            out.append(
                FlowResource(
                    name=_text(dc.get("name")),
                    kind=FlowResourceKind.DYNAMIC_CHOICE_SET,
                    object_type=_text(dc.get("object")) or None,
                )
            )
    for st in _as_list(body.get("stages")):
        if isinstance(st, dict):
            out.append(FlowResource(name=_text(st.get("name")), kind=FlowResourceKind.STAGE))
    return out


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #


def _connectors_of(d: dict[str, Any], *, fault: bool = False) -> list[FlowConnector]:
    """Plain <connector> (+ optional <faultConnector>) for simple elements."""
    out: list[FlowConnector] = []
    conn = _connector(d.get("connector"), ConnectorKind.NEXT)
    if conn is not None:
        out.append(conn)
    if fault:
        f = _connector(d.get("faultConnector"), ConnectorKind.FAULT)
        if f is not None:
            out.append(f)
    return out


def _connector(raw: Any, kind: ConnectorKind, *, label: str | None = None) -> FlowConnector | None:
    if not isinstance(raw, dict):
        return None
    target = _text(raw.get("targetReference"))
    if not target:
        return None
    return FlowConnector(
        target=target,
        kind=kind,
        is_go_to=_bool(raw.get("isGoTo")),
        label=label,
    )


def _parse_conditions(raw: Any) -> list[FlowCondition]:
    out: list[FlowCondition] = []
    for c in _as_list(raw):
        if not isinstance(c, dict):
            continue
        value, vkind = _parse_value(c.get("rightValue"))
        out.append(
            FlowCondition(
                left=_text(c.get("leftValueReference")),
                operator=_text(c.get("operator")),
                right=value,
                right_kind=vkind,
            )
        )
    return out


def _parse_filters(raw: Any) -> list[FlowFieldRead]:
    out: list[FlowFieldRead] = []
    for f in _as_list(raw):
        if not isinstance(f, dict):
            continue
        value, vkind = _parse_value(f.get("value"))
        out.append(
            FlowFieldRead(
                field=_text(f.get("field")),
                operator=_text(f.get("operator")) or None,
                value=value,
                value_kind=vkind,
            )
        )
    return out


def _input_assignments(raw: Any) -> tuple[list[FlowFieldWrite], list[str]]:
    writes: list[FlowFieldWrite] = []
    refs: list[str] = []
    for a in _as_list(raw):
        if not isinstance(a, dict):
            continue
        value, kind = _parse_value(a.get("value"))
        writes.append(FlowFieldWrite(field=_text(a.get("field")), value=value, value_kind=kind))
        if kind == "elementReference" and value:
            refs.append(value)
    return writes, refs


def _input_assignment_refs(raw: Any) -> list[str]:
    refs: list[str] = []
    for a in _as_list(raw):
        if not isinstance(a, dict):
            continue
        value, kind = _parse_value(a.get("value"))
        if kind == "elementReference" and value:
            refs.append(value)
    return refs


def _input_parameter_refs(raw: Any) -> list[str]:
    refs: list[str] = []
    for p in _as_list(raw):
        if not isinstance(p, dict):
            continue
        value, kind = _parse_value(p.get("value"))
        if kind == "elementReference" and value:
            refs.append(value)
    return refs


def _parse_value(raw: Any) -> tuple[str | None, str | None]:
    """Flatten a Salesforce <value> wrapper into (literal_or_ref, kind)."""
    if raw is None:
        return None, None
    if isinstance(raw, str):
        return (raw or None), ("stringValue" if raw else None)
    if not isinstance(raw, dict):
        return str(raw), "unknown"
    for kind in _VALUE_KINDS:
        if kind in raw:
            inner = raw[kind]
            return (str(inner) if inner != "" else None), kind
    return None, None


def _merge_fields(text: str | None) -> list[str]:
    """Extract {!Ref} merge fields from a formula expression / template body."""
    if not text:
        return []
    return _dedupe(m.strip() for m in _MERGE_FIELD_RE.findall(text))


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return ""
    return str(v)


def _bool(v: Any) -> bool:
    return _text(v).lower() == "true"


def _maybe_float(v: Any) -> float | None:
    t = _text(v)
    try:
        return float(t) if t else None
    except ValueError:
        return None


def _maybe_int(v: Any) -> int | None:
    t = _text(v)
    try:
        return int(t) if t else None
    except ValueError:
        return None


def _dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = it if isinstance(it, str) else str(it)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
