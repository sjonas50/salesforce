"""Build :class:`ProcessDefinition` instances from extracted Components.

One builder per category family. Flows and Process Builders carry full
control flow; rules (workflow, assignment, escalation, auto-response,
validation) are condition → action lists; approval processes are ordered
approval steps; Apex is captured as trigger + effects (lookups, DML, calls)
at ``references_only`` fidelity with the source body attached so a later
translator can do better.
"""

from __future__ import annotations

from typing import Any

from offramp.core.models import CategoryName, Component
from offramp.core.process import (
    Branch,
    Condition,
    ConditionGroup,
    Fidelity,
    ProcessDefinition,
    ProcessSource,
    Step,
    StepKind,
    Trigger,
    TriggerKind,
    Variable,
)

_FLOW_CATEGORIES = {
    CategoryName.RECORD_TRIGGERED_FLOW,
    CategoryName.SCREEN_FLOW,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.AUTOLAUNCHED_FLOW,
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.PROCESS_BUILDER,
}
_OPERATOR_MAP = {
    "equals": "EqualTo",
    "notequal": "NotEqualTo",
    "lessthan": "LessThan",
    "greaterthan": "GreaterThan",
    "lessorequal": "LessThanOrEqualTo",
    "greaterorequal": "GreaterThanOrEqualTo",
    "contains": "Contains",
    "notcontain": "DoesNotContain",
    "startswith": "StartsWith",
    "includes": "Includes",
    "excludes": "Excludes",
}


def build_processes(
    components: list[Component], *, org_alias: str, scan_id: str | None = None
) -> list[ProcessDefinition]:
    """Every component that is a process (surfaces and data definitions are skipped)."""
    out: list[ProcessDefinition] = []
    for c in components:
        for p in build_process(c, org_alias=org_alias, scan_id=scan_id):
            out.append(p)
    return out


def build_process(
    c: Component, *, org_alias: str, scan_id: str | None = None
) -> list[ProcessDefinition]:
    raw = c.raw if isinstance(c.raw, dict) else {}
    builders = {
        CategoryName.WORKFLOW_RULE: _from_workflow,
        CategoryName.VALIDATION_RULE: _from_validation,
        CategoryName.ASSIGNMENT_RULE: _from_assignment,
        CategoryName.ESCALATION_RULE: _from_escalation,
        CategoryName.AUTO_RESPONSE_RULE: _from_auto_response,
        CategoryName.APPROVAL_PROCESS: _from_approval,
        CategoryName.APEX_TRIGGER: _from_apex,
        CategoryName.APEX_CLASS: _from_apex,
    }
    if c.category in _FLOW_CATEGORIES:
        defs = [_from_flow(c, raw)]
    elif c.category in builders:
        defs = builders[c.category](c, raw)
    else:
        return []
    src = ProcessSource(
        org_alias=org_alias,
        category=c.category.value,
        api_name=c.api_name or c.name,
        component_id=str(c.id),
        content_hash=c.content_hash,
        scan_id=scan_id,
    )
    for d in defs:
        d.sources = [src]
        d.finalize()
    return defs


# ---- helpers ----------------------------------------------------------------------


def _qualify(obj: str | None, f: str) -> str:
    return f if "." in f or not obj else f"{obj}.{f}"


def _criteria_group(
    items: list[dict[str, Any]],
    formula: str | None,
    boolean_filter: str = "",
    obj: str | None = None,
) -> ConditionGroup:
    if formula:
        return ConditionGroup(logic="and", conditions=[Condition(expression=formula)])
    conds = [
        Condition(
            left=_qualify(obj, i.get("field", "")),
            operator=_OPERATOR_MAP.get(
                str(i.get("operation", "")).lower(), str(i.get("operation", ""))
            ),
            right={"ref": i["value_field"]} if i.get("value_field") else i.get("value"),
        )
        for i in items
    ]
    return ConditionGroup(logic="custom" if boolean_filter else "and", conditions=conds)


def _flow_value(v: Any) -> Any:
    return v


def _ref_fields(value: Any, host: str | None) -> list[str]:
    """Fields a flow value expression reads ($Record.X only; variables are not schema)."""
    ref = (
        value.get("ref") if isinstance(value, dict) else (value if isinstance(value, str) else None)
    )
    if not ref or not host:
        return []
    for prefix in ("$Record__Prior.", "$Record."):
        if ref.startswith(prefix):
            return [f"{host}.{ref[len(prefix) :]}"]
    return []


# ---- Flow / Process Builder ------------------------------------------------------------


def _from_flow(c: Component, raw: dict[str, Any]) -> ProcessDefinition:
    start = raw.get("start") or {}
    host = raw.get("object") or None
    tt = str(start.get("trigger_type", ""))
    rtt = str(start.get("record_trigger_type", ""))
    if tt == "PlatformEvent":
        tk = TriggerKind.PLATFORM_EVENT
    elif tt == "Scheduled" or raw.get("process_type") == "ScheduleTriggered":
        tk = TriggerKind.SCHEDULED
    elif tt.startswith("Record"):
        tk = TriggerKind.RECORD_DELETE if rtt == "Delete" else TriggerKind.RECORD_SAVE
    elif raw.get("screens"):
        tk = TriggerKind.SCREEN
    else:
        tk = TriggerKind.INVOCATION
    events = {
        "Create": ["create"],
        "Update": ["update"],
        "CreateAndUpdate": ["create", "update"],
        "Delete": ["delete"],
    }.get(rtt, [])
    trigger = Trigger(
        kind=tk,
        object=host,
        events=events,
        timing="before"
        if tt == "RecordBeforeSave"
        else ("after" if tt in {"RecordAfterSave", "PlatformEvent"} else None),
        when=_flow_filters(
            start.get("filters", []),
            start.get("filter_logic", ""),
            start.get("entry_formula", ""),
            host,
        ),
        requires_change=bool(start.get("requires_record_changed")),
        schedule=dict(start.get("schedule") or {}),
    )
    var_types = {
        v["name"]: v.get("object_type") for v in raw.get("resources", {}).get("variables", [])
    }
    reads: set[str] = set()
    writes: set[str] = set()
    objects: set[str] = set([host] if host else [])
    calls: set[str] = set()
    steps: list[Step] = []
    fidelity = Fidelity.FULL
    for el in raw.get("elements", []):
        s = _flow_step(el, host, var_types, reads, writes, objects, calls)
        if s is None:
            continue
        if s.kind in {StepKind.SCREEN} or (
            s.kind == StepKind.ASSIGN and any(isinstance(v, dict) for v in s.inputs.values())
        ):
            fidelity = Fidelity.PARTIAL if fidelity is Fidelity.FULL else fidelity
        steps.append(s)
    for c_ in trigger.when.conditions:
        reads.update(f for f in [c_.left] if f and "." in f and not f.startswith("$"))
    variables = [
        Variable(
            name=v["name"],
            type=v.get("data_type", ""),
            object=v.get("object_type") or None,
            is_input=bool(v.get("is_input")),
            is_output=bool(v.get("is_output")),
        )
        for v in raw.get("resources", {}).get("variables", [])
    ] + [
        Variable(name=f["name"], type="formula", expression=f.get("expression"))
        for f in raw.get("resources", {}).get("formulas", [])
    ]
    return ProcessDefinition(
        name=c.api_name or c.name,
        label=str(raw.get("label") or c.name),
        kind=c.category.value,
        trigger=trigger,
        steps=steps,
        entry=start.get("next") or (steps[0].id if steps else None),
        variables=variables,
        objects=sorted(objects),
        fields_read=sorted(reads - writes),
        fields_written=sorted(writes),
        calls=sorted(calls),
        fidelity=fidelity,
        active=str(raw.get("status", "Active")) == "Active",
    )


def _flow_filters(
    filters: list[dict[str, Any]], logic: str, formula: str, host: str | None
) -> ConditionGroup:
    if formula:
        return ConditionGroup(logic="and", conditions=[Condition(expression=formula)])
    return ConditionGroup(
        logic=(logic or "and").lower() if (logic or "and").lower() in {"and", "or"} else "custom",
        conditions=[
            Condition(
                left=_qualify(host, f.get("field", "")),
                operator=str(f.get("operator", "")),
                right=f.get("value"),
            )
            for f in filters
        ],
    )


def _flow_step(
    el: dict[str, Any],
    host: str | None,
    var_types: dict[str, str | None],
    reads: set[str],
    writes: set[str],
    objects: set[str],
    calls: set[str],
) -> Step | None:
    kind = el.get("kind", "")
    name = el.get("name", "")
    base = {
        "id": name,
        "label": el.get("label", ""),
        "next": el.get("next"),
        "on_fault": el.get("fault"),
    }
    target_obj = el.get("object") or el.get("resolved_object") or None
    if kind == "decisions":
        branches = []
        for r in el.get("rules", []):
            conds = []
            for cnd in r.get("conditions", []):
                left = cnd.get("left", "")
                conds.append(
                    Condition(left=left, operator=cnd.get("operator", ""), right=cnd.get("right"))
                )
                reads.update(_ref_fields(left, host))
                reads.update(_ref_fields(cnd.get("right"), host))
            branches.append(
                Branch(
                    name=r.get("name", ""),
                    label=r.get("label", ""),
                    when=ConditionGroup(logic=str(r.get("logic", "and")), conditions=conds),
                    next=r.get("next"),
                )
            )
        return Step(
            kind=StepKind.DECISION, branches=branches, default_next=el.get("default_next"), **base
        )
    if kind == "assignments":
        inputs: dict[str, Any] = {
            it.get("assign_to", ""): it.get("value") for it in el.get("items", [])
        }
        for it in el.get("items", []):
            writes.update(_ref_fields(it.get("assign_to", ""), host))
            reads.update(_ref_fields(it.get("value"), host))
        return Step(kind=StepKind.ASSIGN, inputs=inputs, **base)
    if kind in {"recordLookups", "recordCreates", "recordUpdates", "recordDeletes"}:
        sk = {
            "recordLookups": StepKind.LOOKUP,
            "recordCreates": StepKind.CREATE,
            "recordUpdates": StepKind.UPDATE,
            "recordDeletes": StepKind.DELETE,
        }[kind]
        if target_obj:
            objects.add(target_obj)
        fields: list[str] = []
        when = None
        if el.get("filters"):
            when = ConditionGroup(
                logic=str(el.get("filter_logic") or "and"),
                conditions=[
                    Condition(
                        left=_qualify(target_obj, f.get("field", "")),
                        operator=str(f.get("operator", "")),
                        right=f.get("value"),
                    )
                    for f in el["filters"]
                ],
            )
            for f in el["filters"]:
                fields.append(_qualify(target_obj, f.get("field", "")))
                reads.update(_ref_fields(f.get("value"), host))
        inputs = {}
        for a in el.get("input_assignments", []):
            q = _qualify(target_obj, a.get("field", ""))
            inputs[q] = a.get("value")
            fields.append(q)
            if sk in {StepKind.CREATE, StepKind.UPDATE}:
                writes.add(q)
            reads.update(_ref_fields(a.get("value"), host))
        for qf in el.get("queried_fields", []):
            q = _qualify(target_obj, qf)
            fields.append(q)
            reads.add(q)
        if sk is StepKind.LOOKUP:
            reads.update(f for f in fields if f)
        return Step(
            kind=sk,
            object=target_obj,
            fields=sorted(set(fields)),
            inputs=inputs,
            when=when,
            extras={"first_only": bool(el.get("get_first_record_only"))}
            if sk is StepKind.LOOKUP
            else {},
            **base,
        )
    if kind == "actionCalls":
        at, an = el.get("action_type", ""), el.get("action_name", "")
        inputs = {i.get("field", ""): i.get("value") for i in el.get("inputs", [])}
        for i in el.get("inputs", []):
            reads.update(_ref_fields(i.get("value"), host))
        if at == "apex":
            calls.add(an)
            return Step(kind=StepKind.CALL_CODE, target=an, inputs=inputs, **base)
        if at == "flow":
            calls.add(an)
            return Step(kind=StepKind.CALL_PROCESS, target=an, inputs=inputs, **base)
        if at in {"emailAlert", "emailSimple", "chatterPost", "customNotificationAction"}:
            return Step(
                kind=StepKind.NOTIFY, target=an, inputs=inputs, extras={"channel": at}, **base
            )
        calls.add(f"{at}:{an}")
        return Step(
            kind=StepKind.CALL_ACTION, target=an, inputs=inputs, extras={"action_type": at}, **base
        )
    if kind == "apexPluginCalls":
        calls.add(el.get("apex_class", ""))
        return Step(kind=StepKind.CALL_CODE, target=el.get("apex_class", ""), **base)
    if kind == "subflows":
        calls.add(el.get("flow_name", ""))
        inputs = {i.get("field", ""): i.get("value") for i in el.get("inputs", [])}
        for i in el.get("inputs", []):
            reads.update(_ref_fields(i.get("value"), host))
        return Step(
            kind=StepKind.CALL_PROCESS, target=el.get("flow_name", ""), inputs=inputs, **base
        )
    if kind == "screens":
        return Step(
            kind=StepKind.SCREEN,
            extras={"fields": [f.get("name") for f in el.get("fields", [])]},
            **base,
        )
    if kind == "loops":
        return Step(
            kind=StepKind.LOOP,
            inputs={"collection": el.get("collection", "")},
            extras={"after": el.get("after")},
            **base,
        )
    if kind == "waits":
        return Step(
            kind=StepKind.WAIT,
            branches=[
                Branch(
                    name=e.get("name", ""),
                    when=ConditionGroup(conditions=[Condition(expression=e.get("type", ""))]),
                    next=e.get("next"),
                )
                for e in el.get("events", [])
            ],
            default_next=el.get("default_next"),
            **base,
        )
    if kind == "customErrors":
        return Step(kind=StepKind.RAISE_ERROR, extras={"messages": el.get("messages", [])}, **base)
    if kind == "recordRollbacks":
        return Step(kind=StepKind.ROLLBACK, **base)
    if kind == "collectionProcessors":
        return Step(
            kind=StepKind.ASSIGN,
            inputs={"collection": el.get("collection", ""), "formula": el.get("formula", "")},
            extras={"processor": el.get("processor_type", "")},
            **base,
        )
    if kind == "orchestratedStages":
        return Step(kind=StepKind.CALL_PROCESS, extras={"stage_steps": el.get("steps", [])}, **base)
    if kind == "transforms":
        return Step(kind=StepKind.ASSIGN, object=el.get("object") or None, **base)
    return None


# ---- Rules ----------------------------------------------------------------------------


def _from_workflow(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    obj = raw.get("object") or None
    fus = {fu["name"]: fu for fu in raw.get("field_updates", [])}
    alerts = {a["name"]: a for a in raw.get("email_alerts", [])}
    tasks = {t["name"]: t for t in raw.get("tasks", [])}
    oms = {o["name"]: o for o in raw.get("outbound_messages", [])}
    out = []
    for rule in raw.get("rules", []):
        steps: list[Step] = []
        reads: set[str] = set()
        writes: set[str] = set()
        calls: set[str] = set()

        def action_steps(
            actions: list[dict[str, Any]],
            prefix: str,
            *,
            writes: set[str] = writes,
            calls: set[str] = calls,
        ) -> list[Step]:
            res: list[Step] = []
            for i, a in enumerate(actions):
                sid = f"{prefix}{i + 1}_{a.get('name', '')}"
                t = a.get("type", "")
                n = a.get("name", "")
                if t == "FieldUpdate" and n in fus:
                    fu = fus[n]
                    q = _qualify(fu.get("target_object") or obj, fu.get("field", ""))
                    writes.add(q)
                    val: Any = fu.get("literal_value")
                    if fu.get("formula"):
                        val = {"expression": fu["formula"]}
                    res.append(
                        Step(
                            id=sid,
                            kind=StepKind.UPDATE,
                            label=n,
                            object=fu.get("target_object") or obj,
                            fields=[q],
                            inputs={q: val},
                            extras={
                                "operation": fu.get("operation", ""),
                                "reevaluate": bool(fu.get("reevaluate_on_change")),
                            },
                        )
                    )
                elif t == "Alert":
                    a_ = alerts.get(n, {})
                    res.append(
                        Step(
                            id=sid,
                            kind=StepKind.NOTIFY,
                            label=n,
                            target=a_.get("template") or n,
                            extras={"channel": "email", "recipients": a_.get("recipients", [])},
                        )
                    )
                elif t == "Task":
                    t_ = tasks.get(n, {})
                    res.append(
                        Step(
                            id=sid,
                            kind=StepKind.TASK,
                            label=n,
                            object="Task",
                            inputs={"Task.Subject": t_.get("subject", "")},
                            target=t_.get("assigned_to") or None,
                        )
                    )
                elif t == "OutboundMessage":
                    om = oms.get(n, {})
                    calls.add(f"outbound:{n}")
                    res.append(
                        Step(
                            id=sid,
                            kind=StepKind.CALL_ACTION,
                            label=n,
                            target=om.get("endpoint_url") or n,
                            fields=[_qualify(obj, f) for f in om.get("fields", [])],
                            extras={"action_type": "outbound_message"},
                        )
                    )
                elif t == "FlowAction":
                    calls.add(n)
                    res.append(Step(id=sid, kind=StepKind.CALL_PROCESS, label=n, target=n))
                else:
                    res.append(
                        Step(
                            id=sid,
                            kind=StepKind.CALL_ACTION,
                            label=n,
                            target=n,
                            extras={"action_type": t},
                        )
                    )
            return res

        immediate = action_steps(rule.get("immediate_actions", []), "a")
        steps.extend(immediate)
        for k, tt in enumerate(rule.get("time_triggers", [])):
            wait = Step(
                id=f"wait{k + 1}",
                kind=StepKind.WAIT,
                extras={"offset": tt.get("offset"), "unit": tt.get("unit")},
            )
            timed = action_steps(tt.get("actions", []), f"t{k + 1}_")
            steps.append(wait)
            steps.extend(timed)
        for i in range(len(immediate) - 1):
            immediate[i].next = immediate[i + 1].id
        when = _criteria_group(
            rule.get("criteria_items", []), rule.get("formula"), rule.get("boolean_filter", ""), obj
        )
        for cnd in when.conditions:
            if cnd.left:
                reads.add(cnd.left)
        events = {
            "onCreateOnly": ["create"],
            "onCreateOrTriggeringUpdate": ["create", "update"],
            "onAllChanges": ["create", "update"],
        }.get(str(rule.get("trigger_type", "")), ["create", "update"])
        out.append(
            ProcessDefinition(
                name=f"{obj}.{rule.get('name', '')}",
                label=str(rule.get("name", "")),
                kind=c.category.value,
                trigger=Trigger(
                    kind=TriggerKind.RECORD_SAVE,
                    object=obj,
                    events=events,
                    timing="after",
                    when=when,
                    requires_change=str(rule.get("trigger_type", ""))
                    == "onCreateOrTriggeringUpdate",
                ),
                steps=steps,
                entry=steps[0].id if steps else None,
                objects=sorted({o for o in [obj] if o}),
                fields_read=sorted(reads - writes),
                fields_written=sorted(writes),
                calls=sorted(calls),
                fidelity=Fidelity.FULL,
                active=bool(rule.get("active", True)),
            )
        )
    return out


def _from_validation(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    obj = raw.get("object") or None
    refs = raw.get("references", {})
    formula = raw.get("error_condition_formula", "")
    step = Step(
        id="reject",
        kind=StepKind.RAISE_ERROR,
        label=c.name,
        object=obj,
        fields=[_qualify(obj, raw["error_display_field"])]
        if raw.get("error_display_field")
        else [],
        extras={"message": raw.get("error_message", "")},
    )
    return [
        ProcessDefinition(
            name=f"{obj}.{c.name}",
            label=c.name,
            kind=c.category.value,
            description=str(raw.get("description", "")),
            trigger=Trigger(
                kind=TriggerKind.RECORD_SAVE,
                object=obj,
                events=["create", "update"],
                timing="before",
                when=ConditionGroup(conditions=[Condition(expression=formula)]),
            ),
            steps=[step],
            entry="reject",
            objects=[obj] if obj else [],
            fields_read=sorted(refs.get("fields", [])),
            fidelity=Fidelity.FULL if refs.get("formula_parsed", True) else Fidelity.PARTIAL,
            active=bool(raw.get("active", True)),
        )
    ]


def _entry_rules(
    c: Component, raw: dict[str, Any], *, kind: StepKind, target_key: str, action_extras: Any
) -> list[ProcessDefinition]:
    """Shared shape for assignment / escalation / auto-response: first matching entry wins."""
    obj = raw.get("object") or None
    out = []
    for g in raw.get("rule_groups", []):
        branches: list[Branch] = []
        steps: list[Step] = []
        reads: set[str] = set()
        for i, e in enumerate(g.get("entries", [])):
            when = _criteria_group(
                e.get("criteria_items", []), e.get("formula"), e.get("boolean_filter", ""), obj
            )
            for cnd in when.conditions:
                if cnd.left:
                    reads.add(cnd.left)
            sid = f"entry{i + 1}"
            branches.append(Branch(name=sid, when=when, next=sid))
            steps.extend(action_extras(sid, e, obj))
        decision = Step(
            id="match",
            kind=StepKind.DECISION,
            label="first matching entry",
            branches=branches,
            default_next=None,
        )
        writes = {
            f for s in steps for f in s.fields if s.kind in {StepKind.ASSIGN, StepKind.UPDATE}
        }
        out.append(
            ProcessDefinition(
                name=f"{obj}.{g.get('name', '')}",
                label=str(g.get("name", "")),
                kind=c.category.value,
                trigger=Trigger(
                    kind=TriggerKind.RECORD_SAVE,
                    object=obj,
                    events=["create", "update"]
                    if c.category is CategoryName.ESCALATION_RULE
                    else ["create"],
                    timing="after",
                ),
                steps=[decision, *steps],
                entry="match",
                objects=[obj] if obj else [],
                fields_read=sorted(reads - writes),
                fields_written=sorted(writes),
                calls=[],
                fidelity=Fidelity.FULL,
                active=bool(g.get("active", True)),
            )
        )
    return out


def _from_assignment(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    def actions(sid: str, e: dict[str, Any], obj: str | None) -> list[Step]:
        steps = [
            Step(
                id=sid,
                kind=StepKind.UPDATE,
                label="assign owner",
                object=obj,
                fields=[_qualify(obj, "OwnerId")],
                inputs={
                    _qualify(obj, "OwnerId"): {
                        "ref": f"{e.get('assigned_to_type', 'User')}:{e.get('assigned_to', '')}"
                    }
                },
                target=e.get("assigned_to") or None,
            )
        ]
        if e.get("template"):
            steps[0].next = f"{sid}_notify"
            steps.append(
                Step(
                    id=f"{sid}_notify",
                    kind=StepKind.NOTIFY,
                    target=e["template"],
                    extras={"channel": "email"},
                )
            )
        return steps

    return _entry_rules(
        c, raw, kind=StepKind.UPDATE, target_key="assigned_to", action_extras=actions
    )


def _from_escalation(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    def actions(sid: str, e: dict[str, Any], obj: str | None) -> list[Step]:
        steps: list[Step] = []
        prev: Step | None = None
        for j, a in enumerate(e.get("actions", [])):
            wait = Step(
                id=f"{sid}_wait{j + 1}",
                kind=StepKind.WAIT,
                extras={
                    "minutes": a.get("minutes_to_escalation"),
                    "business_hours": e.get("business_hours", ""),
                },
            )
            esc = Step(
                id=f"{sid}_escalate{j + 1}",
                kind=StepKind.ESCALATE,
                object=obj,
                fields=[_qualify(obj, "OwnerId")] if a.get("assigned_to") else [],
                target=a.get("assigned_to") or None,
                extras={"notify": a.get("notify_to", ""), "template": a.get("template", "")},
            )
            wait.next = esc.id
            if prev is not None:
                prev.next = wait.id
            steps.extend([wait, esc])
            prev = esc
        return steps

    return _entry_rules(
        c, raw, kind=StepKind.ESCALATE, target_key="assigned_to", action_extras=actions
    )


def _from_auto_response(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    def actions(sid: str, e: dict[str, Any], obj: str | None) -> list[Step]:
        return [
            Step(
                id=sid,
                kind=StepKind.NOTIFY,
                target=e.get("template") or None,
                extras={"channel": "email", "sender": e.get("sender_email", "")},
            )
        ]

    return _entry_rules(c, raw, kind=StepKind.NOTIFY, target_key="template", action_extras=actions)


def _from_approval(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    obj = raw.get("object") or None
    entry = raw.get("entry_criteria") or {}
    when = _criteria_group(
        entry.get("criteria_items", []), entry.get("formula"), entry.get("boolean_filter", ""), obj
    )
    steps: list[Step] = []
    reads = {cnd.left for cnd in when.conditions if cnd.left}
    prev: Step | None = None
    for i, s in enumerate(raw.get("steps", [])):
        ec = s.get("entry_criteria") or {}
        swhen = _criteria_group(
            ec.get("criteria_items", []), ec.get("formula"), ec.get("boolean_filter", ""), obj
        )
        reads.update(cnd.left for cnd in swhen.conditions if cnd.left)
        st = Step(
            id=s.get("name") or f"step{i + 1}",
            kind=StepKind.APPROVAL_STEP,
            label=s.get("label", ""),
            object=obj,
            when=swhen if not swhen.empty else None,
            target=", ".join(f"{a.get('type')}:{a.get('name')}" for a in s.get("approvers", [])),
            extras={
                "when_multiple": s.get("when_multiple", ""),
                "reject": s.get("reject_behavior", ""),
                "if_criteria_not_met": s.get("if_criteria_not_met", ""),
                "approval_actions": s.get("approval_actions", []),
                "rejection_actions": s.get("rejection_actions", []),
            },
        )
        if prev is not None:
            prev.next = st.id
        steps.append(st)
        prev = st
    for key, sid in (
        ("final_approval_actions", "approved"),
        ("final_rejection_actions", "rejected"),
        ("recall_actions", "recalled"),
        ("initial_submission_actions", "submitted"),
    ):
        acts = raw.get(key, [])
        if acts:
            steps.append(
                Step(
                    id=sid,
                    kind=StepKind.CALL_ACTION,
                    label=key.replace("_", " "),
                    extras={"actions": acts},
                )
            )
    return [
        ProcessDefinition(
            name=c.api_name or c.name,
            label=str(raw.get("label") or c.name),
            kind=c.category.value,
            description=str(raw.get("description", "")),
            trigger=Trigger(kind=TriggerKind.APPROVAL_SUBMIT, object=obj, when=when),
            steps=steps,
            entry=steps[0].id if steps else None,
            objects=[obj] if obj else [],
            fields_read=sorted(reads),
            fidelity=Fidelity.PARTIAL if raw.get("partial") else Fidelity.FULL,
            active=bool(raw.get("active", True)),
        )
    ]


# ---- Apex ------------------------------------------------------------------------------


def _from_apex(c: Component, raw: dict[str, Any]) -> list[ProcessDefinition]:
    a = raw.get("analysis") or {}
    refs = raw.get("references") or {}
    if raw.get("is_test") or a.get("is_test"):
        return []
    steps: list[Step] = []
    calls: set[str] = set()
    objects: set[str] = set(refs.get("objects", []))
    for i, q in enumerate(a.get("soql", [])):
        if q.get("sobject"):
            steps.append(
                Step(
                    id=f"q{i + 1}",
                    kind=StepKind.LOOKUP,
                    object=q["sobject"],
                    fields=[
                        _qualify(q["sobject"], f)
                        for f in q.get("fields", []) + q.get("where_fields", [])
                    ],
                    extras={"dynamic": bool(q.get("dynamic"))},
                )
            )
    for i, d in enumerate(a.get("dml", [])):
        kind = {
            "insert": StepKind.CREATE,
            "update": StepKind.UPDATE,
            "upsert": StepKind.UPDATE,
            "delete": StepKind.DELETE,
            "undelete": StepKind.UPDATE,
            "merge": StepKind.UPDATE,
        }.get(str(d.get("op")), StepKind.UPDATE)
        steps.append(
            Step(
                id=f"dml{i + 1}",
                kind=kind,
                object=d.get("sobject"),
                extras={"op": d.get("op"), "target": d.get("target")},
            )
        )
    for cls in a.get("class_references", []):
        calls.add(cls)
        steps.append(Step(id=f"call_{cls}", kind=StepKind.CALL_CODE, target=cls))
    for x in a.get("async_calls", []):
        if x.get("target_class"):
            calls.add(x["target_class"])
            steps.append(
                Step(
                    id=f"async_{x['target_class']}",
                    kind=StepKind.CALL_CODE,
                    target=x["target_class"],
                    extras={"mechanism": x.get("mechanism")},
                )
            )
    for nc in a.get("named_credentials", []):
        calls.add(f"callout:{nc}")
        steps.append(
            Step(
                id=f"callout_{nc}",
                kind=StepKind.CALL_ACTION,
                target=nc,
                extras={"action_type": "callout"},
            )
        )
    if c.category is CategoryName.APEX_TRIGGER:
        events = sorted({e.split(" ")[1] for e in a.get("trigger_events", []) if " " in e})
        timings = sorted({e.split(" ")[0] for e in a.get("trigger_events", []) if " " in e})
        trigger = Trigger(
            kind=TriggerKind.RECORD_SAVE,
            object=a.get("trigger_object") or raw.get("sobject") or None,
            events=events,
            timing="both" if len(timings) == 2 else (timings[0] if timings else None),
        )
    else:
        trigger = Trigger(kind=TriggerKind.INVOCATION, entry_points=list(a.get("entry_points", [])))
    return [
        ProcessDefinition(
            name=c.api_name or c.name,
            label=c.name,
            kind=c.category.value,
            trigger=trigger,
            steps=steps,
            entry=steps[0].id if steps else None,
            objects=sorted(objects),
            fields_read=sorted(set(refs.get("fields", [])) - set(refs.get("fields_written", []))),
            fields_written=sorted(refs.get("fields_written", [])),
            calls=sorted(calls),
            fidelity=Fidelity.REFERENCES_ONLY,
            active=str(raw.get("status", "Active")) == "Active",
            tags=["dynamic_access"] if raw.get("dynamic_access") else [],
            code=raw.get("body") or None,
        )
    ]
