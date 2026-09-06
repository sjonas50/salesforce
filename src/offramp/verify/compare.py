"""Compare an execution trace with a process definition."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from offramp.core.process import ProcessDefinition, Step, StepKind
from offramp.verify.trace import ELEMENT_KINDS, ExecutionTrace

_DML_KIND = {"Insert": StepKind.CREATE, "Update": StepKind.UPDATE, "Delete": StepKind.DELETE}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerificationResult:
    process: str
    status: str  # pass | mismatch | not_verifiable
    checks: list[Check] = field(default_factory=list)
    path: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "process": self.process,
            "status": self.status,
            "path": self.path,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def _successors(s: Step) -> set[str]:
    out = {x for x in (s.next, s.default_next, s.on_fault) if x}
    out |= {b.next for b in s.branches if b.next}
    if s.kind is StepKind.LOOP and s.extras.get("after"):
        out.add(str(s.extras["after"]))
    return out


def find_trace(p: ProcessDefinition, traces: list[ExecutionTrace]) -> ExecutionTrace | None:
    wanted = {p.name.lower(), (p.label or "").lower()}
    for t in traces:
        if t.flow_label.lower() in wanted:
            return t
    return None


def verify_process(p: ProcessDefinition, trace: ExecutionTrace | None) -> VerificationResult:
    """Did the org run the steps the model predicts, in an order the model allows,
    with the DML the model attributes to those steps?"""
    if trace is None:
        return VerificationResult(
            p.name, "not_verifiable", [Check("trace_found", False, "no interview in the log")]
        )
    steps = {s.id: s for s in p.steps}
    checks: list[Check] = [Check("trace_found", True, f"interview {trace.interview_id or '?'}")]
    visited = trace.path
    unknown = [n for n in visited if n not in steps]
    checks.append(
        Check(
            "elements_in_model",
            not unknown,
            f"{len(visited)} elements visited"
            + (f"; unknown to the model: {unknown}" if unknown else ""),
        )
    )
    # Element type vs step kind.
    kind_mismatch = []
    for e in trace.elements:
        s = steps.get(e.name)
        expected = ELEMENT_KINDS.get(e.element_type)
        if s is None or expected is None:
            continue
        got = s.kind.value
        if expected == "call_code" and got in {
            "call_code",
            "call_process",
            "call_action",
            "notify",
        }:
            continue  # FlowActionCall covers every action flavour
        if expected != got:
            kind_mismatch.append(f"{e.name}: log says {e.element_type}, model says {got}")
    checks.append(Check("step_kinds_match", not kind_mismatch, "; ".join(kind_mismatch)))
    # Entry and transitions.
    bad_edges = []
    if visited:
        first = visited[0]
        if p.entry and first != p.entry:
            bad_edges.append(f"entered at {first}, model entry is {p.entry}")
        for a, b in pairwise(visited):
            sa = steps.get(a)
            if sa is None:
                continue
            allowed = _successors(sa)
            if sa.kind is StepKind.LOOP:
                allowed |= {b}  # loop bodies return to the loop element
            if allowed and b not in allowed and b != a:
                bad_edges.append(f"{a} -> {b} (model allows {sorted(allowed)})")
    checks.append(Check("path_follows_model", not bad_edges, "; ".join(bad_edges)))
    # Branch outcomes: a decision followed by X means one branch (or the default) leads to X.
    # DML: every DML op after a visited step must be a step of that kind on that object.
    dml_bad = []
    for op in trace.dml:
        want = _DML_KIND.get(op.op)
        s = steps.get(op.after_element or "")
        if want is None:
            continue
        if s is None or s.kind is not want or (s.object or "").lower() != op.sobject.lower():
            dml_bad.append(
                f"{op.op} {op.sobject} x{op.rows} after {op.after_element}: "
                f"model has {s.kind.value + ' ' + str(s.object) if s else 'no such step'}"
            )
    checks.append(Check("dml_matches_steps", not dml_bad, "; ".join(dml_bad)))
    if trace.errors:
        checks.append(Check("no_runtime_errors", False, "; ".join(trace.errors)[:300]))
    status = "pass" if all(c.ok for c in checks) else "mismatch"
    return VerificationResult(p.name, status, checks, visited)


def compare_roundtrip(original: ExecutionTrace, copy: ExecutionTrace | None) -> Check:
    """The rendered copy must take the same path and perform the same DML as the original."""
    if copy is None:
        return Check("roundtrip_copy_ran", False, "no interview for the rendered copy")
    if original.path != copy.path:
        return Check(
            "roundtrip_path_matches", False, f"original {original.path} vs copy {copy.path}"
        )
    a = [(d.op, d.sobject, d.rows) for d in original.dml]
    b = [(d.op, d.sobject, d.rows) for d in copy.dml]
    if a != b:
        return Check("roundtrip_dml_matches", False, f"original {a} vs copy {b}")
    return Check(
        "roundtrip_matches", True, f"{len(copy.path)} elements, {len(b)} DML ops identical"
    )
