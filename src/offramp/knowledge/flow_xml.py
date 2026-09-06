"""Render a flow-derived :class:`ProcessDefinition` back to Flow metadata XML.

This is the round-trip half of validation: if the XML rendered from the model
re-extracts to the same model *and* deploys and runs like the original, the
model captured the flow. The renderer covers the flow element kinds the
normaliser reads (``src/extract/categories/flow.py``); anything the model keeps
opaque (screens' fields, custom-error messages) is rendered structurally so the
definition survives the trip, not so the screen is pixel-identical.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from offramp.core.process import (
    ConditionGroup,
    ProcessDefinition,
    Step,
    StepKind,
    TriggerKind,
)

_NS = "http://soap.sforce.com/2006/04/metadata"

_PROCESS_TYPE = {
    "screen_flow": "Flow",
    "record_triggered_flow": "AutoLaunchedFlow",
    "autolaunched_flow": "AutoLaunchedFlow",
    "schedule_triggered_flow": "AutoLaunchedFlow",
    "platform_event_triggered_flow": "AutoLaunchedFlow",
}


class RenderError(ValueError):
    """The definition holds something this renderer cannot express."""


def _el(tag: str, text: Any = None, *children: str, indent: int = 1) -> str:
    pad = "    " * indent
    if children:
        inner = "\n".join(children)
        return f"{pad}<{tag}>\n{inner}\n{pad}</{tag}>"
    if text is None:
        return f"{pad}<{tag}/>"
    return f"{pad}<{tag}>{escape(str(text))}</{tag}>"


def _value(v: Any, indent: int) -> str:
    """One Flow value wrapper; the inverse of the normaliser's ``_value``."""
    if isinstance(v, dict) and "ref" in v:
        return _el("elementReference", v["ref"], indent=indent)
    if isinstance(v, bool):
        return _el("booleanValue", "true" if v else "false", indent=indent)
    if isinstance(v, int | float):
        return _el("numberValue", v, indent=indent)
    if v is None:
        return _el("stringValue", "", indent=indent)
    return _el("stringValue", v, indent=indent)


def _conditions(group: ConditionGroup, indent: int, *, tag: str = "conditions") -> list[str]:
    out = []
    for c in group.conditions:
        if c.expression and not c.left:
            raise RenderError(
                f"condition with a formula expression cannot be rendered: {c.expression}"
            )
        out.append(
            _el(
                tag,
                None,
                _el("leftValueReference", c.left, indent=indent + 1),
                _el("operator", c.operator or "EqualTo", indent=indent + 1),
                _el("rightValue", None, _value(c.right, indent + 2), indent=indent + 1),
                indent=indent,
            )
        )
    return out


def _field_of(qualified: str) -> str:
    return qualified.split(".", 1)[1] if "." in qualified else qualified


def _node_head(s: Step, indent: int, y: int) -> list[str]:
    return [
        _el("name", s.id, indent=indent),
        _el("label", s.label or s.id, indent=indent),
        _el("locationX", 300, indent=indent),
        _el("locationY", y, indent=indent),
    ]


def _connector(tag: str, target: str | None, indent: int) -> list[str]:
    if not target:
        return []
    return [_el(tag, None, _el("targetReference", target, indent=indent + 1), indent=indent)]


def _render_step(s: Step, y: int) -> str:
    i = 2
    head = _node_head(s, i, y)
    fault = _connector("faultConnector", s.on_fault, i)
    nxt = _connector("connector", s.next, i)
    k = s.kind
    if k is StepKind.DECISION:
        rules = []
        for b in s.branches:
            rules.append(
                _el(
                    "rules",
                    None,
                    _el("name", b.name, indent=i + 1),
                    _el("conditionLogic", b.when.logic or "and", indent=i + 1),
                    *_conditions(b.when, i + 1),
                    *_connector("connector", b.next, i + 1),
                    _el("label", b.label or b.name, indent=i + 1),
                    indent=i,
                )
            )
        body = (
            head
            + _connector("defaultConnector", s.default_next, i)
            + [_el("defaultConnectorLabel", "Default Outcome", indent=i)]
            + rules
        )
        return _el("decisions", None, *body, indent=1)
    if k is StepKind.ASSIGN and "collection" in s.inputs and "formula" in s.inputs:
        raise RenderError(f"collection processor '{s.id}' is not rendered")
    if k is StepKind.ASSIGN:
        items = [
            _el(
                "assignmentItems",
                None,
                _el("assignToReference", target, indent=i + 1),
                _el("operator", "Assign", indent=i + 1),
                _el("value", None, _value(v, i + 2), indent=i + 1),
                indent=i,
            )
            for target, v in s.inputs.items()
        ]
        return _el("assignments", None, *head, *items, *nxt, indent=1)
    if k in {StepKind.LOOKUP, StepKind.CREATE, StepKind.UPDATE, StepKind.DELETE}:
        tag = {
            StepKind.LOOKUP: "recordLookups",
            StepKind.CREATE: "recordCreates",
            StepKind.UPDATE: "recordUpdates",
            StepKind.DELETE: "recordDeletes",
        }[k]
        parts = list(head) + nxt + fault
        if s.when:
            parts.append(_el("filterLogic", s.when.logic or "and", indent=i))
            for c in s.when.conditions:
                parts.append(
                    _el(
                        "filters",
                        None,
                        _el("field", _field_of(c.left), indent=i + 1),
                        _el("operator", c.operator or "EqualTo", indent=i + 1),
                        _el("value", None, _value(c.right, i + 2), indent=i + 1),
                        indent=i,
                    )
                )
        if k in {StepKind.CREATE, StepKind.UPDATE}:
            for q, v in s.inputs.items():
                parts.append(
                    _el(
                        "inputAssignments",
                        None,
                        _el("field", _field_of(q), indent=i + 1),
                        _el("value", None, _value(v, i + 2), indent=i + 1),
                        indent=i,
                    )
                )
        parts.append(_el("object", s.object or "", indent=i))
        if k is StepKind.LOOKUP:
            parts.append(
                _el(
                    "getFirstRecordOnly",
                    "true" if s.extras.get("first_only") else "false",
                    indent=i,
                )
            )
            filter_fields = {c.left for c in (s.when.conditions if s.when else [])}
            for f in s.fields:
                if f not in filter_fields:
                    parts.append(_el("queriedFields", _field_of(f), indent=i))
            parts.append(_el("storeOutputAutomatically", "true", indent=i))
        return _el(tag, None, *parts, indent=1)
    if k in {StepKind.CALL_CODE, StepKind.CALL_ACTION, StepKind.NOTIFY}:
        action_type = {
            StepKind.CALL_CODE: "apex",
            StepKind.NOTIFY: str(s.extras.get("channel") or "emailAlert"),
            StepKind.CALL_ACTION: str(s.extras.get("action_type") or "component"),
        }[k]
        inputs = [
            _el(
                "inputParameters",
                None,
                _el("name", n, indent=i + 1),
                _el("value", None, _value(v, i + 2), indent=i + 1),
                indent=i,
            )
            for n, v in s.inputs.items()
        ]
        return _el(
            "actionCalls",
            None,
            *head,
            _el("actionName", s.target or "", indent=i),
            _el("actionType", action_type, indent=i),
            *nxt,
            *fault,
            *inputs,
            indent=1,
        )
    if k is StepKind.CALL_PROCESS:
        if "stage_steps" in s.extras:
            raise RenderError(f"orchestration stage '{s.id}' is not rendered")
        inputs = [
            _el(
                "inputAssignments",
                None,
                _el("name", n, indent=i + 1),
                _el("value", None, _value(v, i + 2), indent=i + 1),
                indent=i,
            )
            for n, v in s.inputs.items()
        ]
        return _el(
            "subflows",
            None,
            *head,
            *nxt,
            _el("flowName", s.target or "", indent=i),
            *inputs,
            indent=1,
        )
    if k is StepKind.SCREEN:
        fields = [
            _el(
                "fields",
                None,
                _el("name", f, indent=i + 1),
                _el("fieldType", "DisplayText", indent=i + 1),
                indent=i,
            )
            for f in s.extras.get("fields", [])
            if f
        ]
        return _el("screens", None, *head, *nxt, *fields, indent=1)
    if k is StepKind.LOOP:
        return _el(
            "loops",
            None,
            *head,
            _el("collectionReference", s.inputs.get("collection", ""), indent=i),
            _el("iterationOrder", "Asc", indent=i),
            *_connector("nextValueConnector", s.next, i),
            *_connector("noMoreValuesConnector", s.extras.get("after"), i),
            indent=1,
        )
    if k is StepKind.RAISE_ERROR:
        msgs = [
            _el(
                "customErrorMessages",
                None,
                _el("errorMessage", m, indent=i + 1),
                _el("isFieldError", "false", indent=i + 1),
                indent=i,
            )
            for m in s.extras.get("messages", [])
        ]
        return _el("customErrors", None, *head, *nxt, *msgs, indent=1)
    if k is StepKind.ROLLBACK:
        return _el("recordRollbacks", None, *head, *nxt, indent=1)
    if k is StepKind.WAIT:
        events = [
            _el(
                "waitEvents",
                None,
                _el("name", b.name, indent=i + 1),
                *_connector("connector", b.next, i + 1),
                _el(
                    "eventType",
                    b.when.conditions[0].expression if b.when.conditions else "",
                    indent=i + 1,
                ),
                _el("label", b.label or b.name, indent=i + 1),
                indent=i,
            )
            for b in s.branches
        ]
        return _el(
            "waits",
            None,
            *head,
            *_connector("defaultConnector", s.default_next, i),
            *events,
            indent=1,
        )
    raise RenderError(f"step kind {k.value} ('{s.id}') is not rendered")


def _start(p: ProcessDefinition, y: int) -> str:
    t = p.trigger
    i = 2
    parts = [_el("locationX", 50, indent=i), _el("locationY", y, indent=i)]
    parts += _connector("connector", p.entry, i)
    # Entry conditions apply to record-triggered and scheduled flows alike.
    if t.kind in {TriggerKind.RECORD_SAVE, TriggerKind.RECORD_DELETE, TriggerKind.SCHEDULED}:
        if t.when.conditions and not any(c.expression for c in t.when.conditions):
            parts.append(_el("filterLogic", t.when.logic or "and", indent=i))
            for c in t.when.conditions:
                parts.append(
                    _el(
                        "filters",
                        None,
                        _el("field", _field_of(c.left), indent=i + 1),
                        _el("operator", c.operator or "EqualTo", indent=i + 1),
                        _el("value", None, _value(c.right, i + 2), indent=i + 1),
                        indent=i,
                    )
                )
        elif t.when.conditions:
            parts.append(_el("filterFormula", t.when.conditions[0].expression or "", indent=i))
    if t.kind in {TriggerKind.RECORD_SAVE, TriggerKind.RECORD_DELETE}:
        parts.append(_el("object", t.object or "", indent=i))
        rtt = {
            ("create",): "Create",
            ("update",): "Update",
            ("create", "update"): "CreateAndUpdate",
            ("delete",): "Delete",
        }.get(tuple(t.events), "CreateAndUpdate")
        parts.append(_el("recordTriggerType", rtt, indent=i))
        if t.requires_change:
            parts.append(_el("doesRequireRecordChangedToMeetCriteria", "true", indent=i))
        parts.append(
            _el(
                "triggerType",
                "RecordBeforeSave" if t.timing == "before" else "RecordAfterSave",
                indent=i,
            )
        )
    elif t.kind is TriggerKind.PLATFORM_EVENT:
        parts.append(_el("object", t.object or "", indent=i))
        parts.append(_el("triggerType", "PlatformEvent", indent=i))
    elif t.kind is TriggerKind.SCHEDULED:
        sched = t.schedule or {}
        parts.append(
            _el(
                "schedule",
                None,
                _el("frequency", sched.get("frequency") or "Once", indent=i + 1),
                _el("startDate", sched.get("start_date") or "2026-01-01", indent=i + 1),
                _el("startTime", sched.get("start_time") or "00:00:00.000Z", indent=i + 1),
                indent=i,
            )
        )
        if t.object:
            parts.append(_el("object", t.object, indent=i))
        parts.append(_el("triggerType", "Scheduled", indent=i))
    return _el("start", None, *parts, indent=1)


def to_flow_xml(
    p: ProcessDefinition,
    *,
    api_name: str | None = None,
    active: bool = True,
    label: str | None = None,
) -> str:
    """Flow metadata XML for ``p``; raises :class:`RenderError` for unsupported steps."""
    if p.kind not in _PROCESS_TYPE:
        raise RenderError(f"{p.kind} is not a flow kind")
    parts: list[str] = [_el("apiVersion", "66.0")]
    y = 100
    for s in p.steps:
        y += 100
        parts.append(_render_step(s, y))
    for v in p.variables:
        if v.type == "formula":
            parts.append(
                _el(
                    "formulas",
                    None,
                    _el("name", v.name, indent=2),
                    _el("dataType", "Boolean", indent=2),
                    _el("expression", v.expression or "", indent=2),
                    indent=1,
                )
            )
    shown = label or p.label or p.name
    parts.append(_el("interviewLabel", f"{shown} {{!$Flow.CurrentDateTime}}"))
    parts.append(_el("label", shown))
    parts.append(_el("processType", _PROCESS_TYPE[p.kind]))
    parts.append(_start(p, 100))
    parts.append(_el("status", "Active" if active else "Draft"))
    for v in p.variables:
        if v.type == "formula":
            continue
        vparts = [
            _el("name", v.name, indent=2),
            _el("dataType", v.type or "String", indent=2),
            _el("isCollection", "false", indent=2),
            _el("isInput", "true" if v.is_input else "false", indent=2),
            _el("isOutput", "true" if v.is_output else "false", indent=2),
        ]
        if v.object:
            vparts.append(_el("objectType", v.object, indent=2))
        if (v.type or "").lower() == "number":
            vparts.append(_el("scale", 0, indent=2))
        parts.append(_el("variables", None, *vparts, indent=1))
    body = "\n".join(parts)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow xmlns="{_NS}">\n{body}\n</Flow>\n'
