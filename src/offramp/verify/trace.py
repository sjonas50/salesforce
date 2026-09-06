"""Parse Salesforce Apex debug logs into flow execution traces.

With the ``Workflow`` (Flow) debug category at FINER, a log carries one block per
flow interview::

    12:00:01.123 (1230000)|FLOW_START_INTERVIEWS_BEGIN|1
    12:00:01.124 (1240000)|FLOW_CREATE_INTERVIEW_BEGIN|00D...|300...|301...|Lead Routing
    12:00:01.125 (1250000)|FLOW_START_INTERVIEW_BEGIN|3f2...|Lead Routing
    12:00:01.126 (1260000)|FLOW_ELEMENT_BEGIN|3f2...|FlowDecision|RouteByCountry
    12:00:01.127 (1270000)|FLOW_ELEMENT_END|3f2...|FlowDecision|RouteByCountry
    12:00:01.128 (1280000)|FLOW_ELEMENT_BEGIN|3f2...|FlowRecordUpdate|AssignOwner
    12:00:01.129 (1290000)|FLOW_BULK_ELEMENT_BEGIN|FlowRecordUpdate|AssignOwner
    12:00:01.130 (1300000)|DML_BEGIN|[1]|Op:Update|Type:Lead|Rows:1
    12:00:01.140 (1400000)|FLOW_START_INTERVIEW_END|3f2...|Lead Routing

Salesforce names the flow by *label* in these lines, so callers match traces to
definitions by label or API name. Everything else in the log is ignored; the
parser is tolerant of unknown events and of lines without an interview id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d+ \(\d+\)\|([A-Z_]+)\|?(.*)$")
_DML_RE = re.compile(r"Op:(\w+)\|Type:([A-Za-z0-9_]+)\|Rows:(\d+)")

# Debug-log element types → the model's StepKind values.
ELEMENT_KINDS: dict[str, str] = {
    "FlowDecision": "decision",
    "FlowAssignment": "assign",
    "FlowRecordLookup": "lookup",
    "FlowRecordCreate": "create",
    "FlowRecordUpdate": "update",
    "FlowRecordDelete": "delete",
    "FlowActionCall": "call_code",  # apex actions, email alerts, invocable actions
    "FlowApexPluginCall": "call_code",
    "FlowSubflow": "call_process",
    "FlowScreen": "screen",
    "FlowLoop": "loop",
    "FlowWait": "wait",
    "FlowCustomError": "raise_error",
    "FlowRecordRollback": "rollback",
    "FlowCollectionProcessor": "assign",
    "FlowTransform": "assign",
}


@dataclass
class TraceElement:
    name: str
    element_type: str  # FlowDecision, FlowRecordUpdate, ...
    order: int
    error: str | None = None


@dataclass
class DmlOp:
    op: str  # Insert | Update | Delete | Upsert
    sobject: str
    rows: int
    after_element: str | None = None


@dataclass
class ExecutionTrace:
    flow_label: str
    interview_id: str = ""
    elements: list[TraceElement] = field(default_factory=list)
    dml: list[DmlOp] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def path(self) -> list[str]:
        return [e.name for e in self.elements]


def parse_debug_log(text: str) -> list[ExecutionTrace]:
    """Every flow interview in one debug log, in start order."""
    traces: list[ExecutionTrace] = []
    by_interview: dict[str, ExecutionTrace] = {}
    current: ExecutionTrace | None = None
    order = 0
    for line in text.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        event, rest = m.group(1), m.group(2)
        parts = rest.split("|") if rest else []
        if event in {"FLOW_START_INTERVIEW_BEGIN", "FLOW_CREATE_INTERVIEW_BEGIN"}:
            iid = parts[0] if event == "FLOW_START_INTERVIEW_BEGIN" and parts else ""
            label = parts[-1] if parts else ""
            if event == "FLOW_CREATE_INTERVIEW_BEGIN":
                # Precedes START_INTERVIEW_BEGIN for the same flow; keep the label only.
                current = ExecutionTrace(flow_label=label)
                traces.append(current)
                continue
            if current is not None and current.flow_label == label and not current.interview_id:
                current.interview_id = iid
            else:
                current = ExecutionTrace(flow_label=label, interview_id=iid)
                traces.append(current)
            if iid:
                by_interview[iid] = current
        elif event == "FLOW_ELEMENT_BEGIN" and len(parts) >= 3:
            t = by_interview.get(parts[0]) or current
            if t is not None:
                order += 1
                t.elements.append(TraceElement(name=parts[2], element_type=parts[1], order=order))
        elif event == "FLOW_ELEMENT_ERROR":
            t = by_interview.get(parts[0]) if parts else current
            if t is not None:
                msg = parts[-1] if parts else "error"
                t.errors.append(msg)
                if t.elements:
                    t.elements[-1].error = msg
        elif event == "FLOW_VALUE_ASSIGNMENT" and len(parts) >= 3:
            t = by_interview.get(parts[0]) or current
            if t is not None:
                t.assignments[parts[1]] = parts[2]
        elif event == "DML_BEGIN":
            dm = _DML_RE.search(rest)
            if dm and current is not None:
                current.dml.append(
                    DmlOp(
                        op=dm.group(1),
                        sobject=dm.group(2),
                        rows=int(dm.group(3)),
                        after_element=current.elements[-1].name if current.elements else None,
                    )
                )
        elif event == "FLOW_START_INTERVIEW_END" and parts:
            done = by_interview.get(parts[0])
            if done is current:
                current = None
    return traces
