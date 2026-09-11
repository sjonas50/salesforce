"""Static health checks over process definitions, code analysis and the schema.

Each check is a rule the live verification (``offramp verify``) or the org
itself would otherwise be the first to report. They are deterministic, need
no API call, and run on every scan (``health.json`` next to ``processes.json``,
a section in the X-Ray report). Codes:

* ``picklist_value_unknown`` — a literal compared against or assigned to a
  picklist field is not one of the field's values: the entry condition can
  never be true, the decision branch is dead, or the save will fail.
* ``callout_in_save_path`` — a record-triggered flow or trigger reaches Apex
  that makes a synchronous callout: every save fails with
  ``CalloutException: uncommitted work pending``.
* ``owner_alert_after_queue_assignment`` — an email alert addressed to the
  record owner on an object whose assignment rules hand records to a queue;
  a queue without an email address means zero recipients.
* ``same_object_update_after_save`` — an after-save flow updates its own
  record; a before-save assignment does the same work without a second save
  and a re-entry.
* ``multiple_triggers_per_object`` — more than one active Apex trigger on an
  object: execution order is undefined.
* ``no_fault_path`` — a flow performs DML or calls code without any fault
  connector, so the first exception rolls the whole save back with a raw error.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from offramp.core.models import CategoryName, Component, SchemaNode, SchemaNodeKind, SchemaSnapshot
from offramp.core.process import ProcessDefinition, Step, StepKind, TriggerKind

_FLOW_KINDS = {
    "record_triggered_flow",
    "autolaunched_flow",
    "schedule_triggered_flow",
    "platform_event_triggered_flow",
    "screen_flow",
}
_PICKLIST_TYPES = {"picklist", "multiselectpicklist", "multipicklist"}
_DML_OR_CODE = {
    StepKind.CREATE,
    StepKind.UPDATE,
    StepKind.DELETE,
    StepKind.CALL_CODE,
    StepKind.CALL_ACTION,
}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class HealthFinding:
    code: str
    severity: str  # error | warning | info
    component: str  # process / component api name
    message: str
    object: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ClassFacts:
    name: str
    callouts: list[str]
    async_targets: list[str]
    entry_points: list[str]
    class_refs: list[str]
    active: bool


def _class_facts(components: list[Component]) -> dict[str, _ClassFacts]:
    out: dict[str, _ClassFacts] = {}
    for c in components:
        if c.category is not CategoryName.APEX_CLASS:
            continue
        a = c.raw.get("analysis") or {}
        refs = c.raw.get("references") or {}
        out[c.name.lower()] = _ClassFacts(
            name=c.name,
            callouts=list(a.get("callouts") or []),
            async_targets=[
                str(x.get("target_class"))
                for x in (a.get("async_calls") or [])
                if x.get("target_class")
            ],
            entry_points=list(a.get("entry_points") or []),
            class_refs=list(refs.get("apex_classes") or []),
            active=str(c.raw.get("status", "Active")).lower() != "inactive",
        )
    return out


def _field_index(schema: SchemaSnapshot | None) -> dict[str, dict[str, SchemaNode]]:
    idx: dict[str, dict[str, SchemaNode]] = defaultdict(dict)
    for n in schema.nodes if schema else []:
        if n.kind is SchemaNodeKind.FIELD and "." in n.api_name:
            obj, fname = n.api_name.split(".", 1)
            idx[obj.lower()][fname.lower()] = n
    return idx


def _lookup_field(
    idx: dict[str, dict[str, SchemaNode]], obj: str | None, qualified: str
) -> SchemaNode | None:
    if "." in qualified:
        o, f = qualified.split(".", 1)
    else:
        o, f = obj or "", qualified
    if not o or "." in f:
        return None
    return idx.get(o.lower(), {}).get(f.lower())


def run_health_checks(
    components: list[Component],
    definitions: list[ProcessDefinition],
    schema: SchemaSnapshot | None,
) -> list[HealthFinding]:
    """All checks, errors first, then by component name."""
    findings: list[HealthFinding] = []
    fields = _field_index(schema)
    classes = _class_facts(components)
    active = [d for d in definitions if d.active]

    for d in active:
        findings.extend(_check_picklists(d, fields))
        findings.extend(_check_callouts(d, classes))
        findings.extend(_check_same_object_update(d))
        findings.extend(_check_fault_paths(d))
    findings.extend(_check_owner_alerts(active))
    findings.extend(_check_multiple_triggers(components))

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.component, f.code))
    return findings


# ---- picklists --------------------------------------------------------------


def _literal_values(value: Any) -> list[str]:
    if isinstance(value, dict) or value is None or isinstance(value, bool):
        return []
    if isinstance(value, list):
        return [str(v) for v in value if not isinstance(v, dict | bool) and v is not None]
    return [str(value)]


def _check_picklists(
    d: ProcessDefinition, fields: dict[str, dict[str, SchemaNode]]
) -> list[HealthFinding]:
    out: list[HealthFinding] = []
    obj = d.trigger.object

    def check(qualified: str, value: Any, where: str) -> None:
        node = _lookup_field(fields, obj, qualified)
        if node is None or (node.field_type or "").lower() not in _PICKLIST_TYPES:
            return
        if not node.picklist_values:
            return
        allowed = {v.lower() for v in node.picklist_values}
        for lit in _literal_values(value):
            if lit.lower() in allowed:
                continue
            # Rule criteria list alternatives as "Gold, Platinum"; multi-select as "A;B".
            parts = (
                [p.strip() for p in re.split(r"[;,]", lit)] if re.search(r"[;,]", lit) else [lit]
            )
            for part in parts:
                if part and part.lower() not in allowed:
                    out.append(
                        HealthFinding(
                            code="picklist_value_unknown",
                            severity="error",
                            component=d.name,
                            object=obj,
                            message=(
                                f"{where}: {node.api_name} has no value {part!r} "
                                f"(values: {', '.join(node.picklist_values[:6])}"
                                f"{', …' if len(node.picklist_values) > 6 else ''})"
                            ),
                            details={"field": node.api_name, "value": part, "where": where},
                        )
                    )

    for c in d.trigger.when.conditions:
        if c.left and not c.expression and (c.operator or "").lower() in {"equalto", "notequalto"}:
            check(c.left, c.right, "entry condition")
    for s in d.steps:
        for b in s.branches:
            for c in b.when.conditions:
                if (
                    c.left
                    and not c.expression
                    and (c.operator or "").lower()
                    in {
                        "equalto",
                        "notequalto",
                    }
                ):
                    check(c.left, c.right, f"decision {s.id} outcome {b.name}")
        if s.when is not None:
            for c in s.when.conditions:
                if c.left and not c.expression and (c.operator or "").lower() == "equalto":
                    check(c.left, c.right, f"step {s.id} filter")
        if s.kind in {StepKind.ASSIGN, StepKind.UPDATE, StepKind.CREATE}:
            step_obj = s.object or obj
            for k, v in s.inputs.items():
                if "." in k or step_obj:
                    node = _lookup_field(fields, step_obj, k)
                    if node is not None:
                        check(node.api_name, v, f"step {s.id} assigns")
    return out


# ---- callouts ---------------------------------------------------------------


def _check_callouts(d: ProcessDefinition, classes: dict[str, _ClassFacts]) -> list[HealthFinding]:
    if d.trigger.kind not in {TriggerKind.RECORD_SAVE, TriggerKind.RECORD_DELETE}:
        return []
    out: list[HealthFinding] = []
    targets: list[tuple[str, str]] = []  # (class, via)
    if d.kind == "apex_trigger":
        for cls in d.calls:
            targets.append((cls, "trigger"))
    else:
        for s in d.steps:
            if s.kind is StepKind.CALL_CODE and s.target:
                targets.append((s.target, f"step {s.id}"))
    seen: set[str] = set()
    for cls, via in targets:
        facts = classes.get(cls.lower())
        if facts is None or cls.lower() in seen:
            continue
        seen.add(cls.lower())
        if not facts.callouts:
            # one hop further: a handler that delegates to a service with the callout
            for ref in facts.class_refs:
                inner = classes.get(ref.lower())
                if inner is not None and inner.callouts and not inner.async_targets:
                    out.append(
                        HealthFinding(
                            code="callout_in_save_path",
                            severity="error",
                            component=d.name,
                            object=d.trigger.object,
                            message=(
                                f"{via} runs {facts.name}, which calls {inner.name} "
                                f"({', '.join(inner.callouts)}) synchronously; a callout "
                                "in a save transaction fails with 'uncommitted work pending' "
                                "and the whole save is rejected. Move it to a Queueable with "
                                "Database.AllowsCallouts or a @future(callout=true) method."
                            ),
                            details={"class": inner.name, "via": facts.name},
                        )
                    )
            continue
        if facts.async_targets:
            out.append(
                HealthFinding(
                    code="callout_deferred",
                    severity="info",
                    component=d.name,
                    object=d.trigger.object,
                    message=(
                        f"{via} runs {facts.name}, which references {', '.join(facts.callouts)} "
                        f"and enqueues {', '.join(facts.async_targets)}; confirm the callout "
                        "happens only in the async job."
                    ),
                    details={"class": facts.name, "async": facts.async_targets},
                )
            )
        else:
            out.append(
                HealthFinding(
                    code="callout_in_save_path",
                    severity="error",
                    component=d.name,
                    object=d.trigger.object,
                    message=(
                        f"{via} runs {facts.name}, which makes a synchronous callout "
                        f"({', '.join(facts.callouts)}); a callout in a save transaction fails "
                        "with 'uncommitted work pending' and the whole save is rejected "
                        "(CANNOT_EXECUTE_FLOW_TRIGGER). Move it to a Queueable with "
                        "Database.AllowsCallouts or a @future(callout=true) method."
                    ),
                    details={"class": facts.name, "callouts": facts.callouts},
                )
            )
    return out


# ---- same-record update -----------------------------------------------------


def _touches_record(s: Step) -> bool:
    if str(s.extras.get("input", "")).startswith("$Record"):
        return True
    return any(
        isinstance(v, dict) and str(v.get("ref", "")).startswith("$Record")
        for v in s.inputs.values()
    ) or any(k.startswith("$Record") for k in s.inputs)


def _check_same_object_update(d: ProcessDefinition) -> list[HealthFinding]:
    if d.kind not in _FLOW_KINDS or d.trigger.kind is not TriggerKind.RECORD_SAVE:
        return []
    if (d.trigger.timing or "").lower() != "after":
        return []
    obj = d.trigger.object
    hits = [
        s
        for s in d.steps
        if s.kind is StepKind.UPDATE
        and s.object
        and obj
        and s.object.lower() == obj.lower()
        and (_touches_record(s) or not s.when)
    ]
    if not hits:
        return []
    guarded = bool(d.trigger.when.conditions) or d.trigger.requires_change
    return [
        HealthFinding(
            code="same_object_update_after_save",
            severity="info" if guarded else "warning",
            component=d.name,
            object=obj,
            message=(
                f"after-save flow updates {obj} again in {', '.join(s.id for s in hits)} "
                "(a second save, and the flow re-enters on update"
                + (
                    "; entry conditions limit the re-entry)"
                    if guarded
                    else " with no entry condition to stop it). Prefer a before-save flow "
                    "for same-record field changes."
                )
            ),
            details={"steps": [s.id for s in hits], "guarded": guarded},
        )
    ]


# ---- fault paths ------------------------------------------------------------


def _check_fault_paths(d: ProcessDefinition) -> list[HealthFinding]:
    if d.kind not in _FLOW_KINDS:
        return []
    risky = [s for s in d.steps if s.kind in _DML_OR_CODE]
    if not risky or any(s.on_fault for s in risky):
        return []
    return [
        HealthFinding(
            code="no_fault_path",
            severity="info",
            component=d.name,
            object=d.trigger.object,
            message=(
                f"{len(risky)} step(s) perform DML or call code without a fault connector "
                f"({', '.join(s.id for s in risky[:5])}); the first exception surfaces as an "
                "unhandled flow error and rolls the transaction back."
            ),
            details={"steps": [s.id for s in risky]},
        )
    ]


# ---- owner alerts vs queue assignment ---------------------------------------


def _check_owner_alerts(definitions: list[ProcessDefinition]) -> list[HealthFinding]:
    queues_by_object: dict[str, set[str]] = defaultdict(set)
    for d in definitions:
        if d.kind != "assignment_rule" or not d.trigger.object:
            continue
        for s in d.steps:
            for v in s.inputs.values():
                ref = str(v.get("ref", "")) if isinstance(v, dict) else ""
                if ref.startswith("Queue:"):
                    queues_by_object[d.trigger.object.lower()].add(ref.split(":", 1)[1])
    out: list[HealthFinding] = []
    for d in definitions:
        obj = (d.trigger.object or "").lower()
        if not obj or obj not in queues_by_object:
            continue
        for s in d.steps:
            if s.kind is not StepKind.NOTIFY:
                continue
            recipients = [str(r).lower() for r in s.extras.get("recipients", [])]
            if "owner" not in recipients:
                continue
            queues = sorted(queues_by_object[obj])
            out.append(
                HealthFinding(
                    code="owner_alert_after_queue_assignment",
                    severity="warning",
                    component=d.name,
                    object=d.trigger.object,
                    message=(
                        f"email alert {s.target or s.id} goes to the record owner, and assignment "
                        f"rules on {d.trigger.object} route records to queue(s) "
                        f"{', '.join(queues)}; a queue without an email address yields "
                        "'0 recipients' and the alert is silently dropped."
                    ),
                    details={"alert": s.target or s.id, "queues": queues},
                )
            )
    return out


# ---- triggers ---------------------------------------------------------------


def _check_multiple_triggers(components: list[Component]) -> list[HealthFinding]:
    by_obj: dict[str, list[str]] = defaultdict(list)
    for c in components:
        if c.category is not CategoryName.APEX_TRIGGER:
            continue
        if str(c.raw.get("status", "Active")).lower() == "inactive":
            continue
        obj = str(c.raw.get("sobject") or c.raw.get("object") or "")
        if obj:
            by_obj[obj].append(c.name)
    out: list[HealthFinding] = []
    for obj, names in sorted(by_obj.items()):
        if len(names) > 1:
            out.append(
                HealthFinding(
                    code="multiple_triggers_per_object",
                    severity="warning",
                    component=", ".join(sorted(names)),
                    object=obj,
                    message=(
                        f"{len(names)} active triggers on {obj}; Salesforce does not define "
                        "their order. Consolidate into one trigger with a handler."
                    ),
                    details={"triggers": sorted(names)},
                )
            )
    return out


def summarize(findings: list[HealthFinding]) -> dict[str, int]:
    return {
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warning"),
        "infos": sum(1 for f in findings if f.severity == "info"),
    }
