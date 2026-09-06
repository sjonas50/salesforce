"""Render a ProcessDefinition for humans: Mermaid flowchart and Markdown."""

from __future__ import annotations

import re
from typing import Any

from offramp.core.process import ProcessDefinition, Step, StepKind

_SHAPE = {
    StepKind.DECISION: ("{", "}"),
    StepKind.WAIT: ("[/", "/]"),
    StepKind.SCREEN: ("[[", "]]"),
    StepKind.RAISE_ERROR: ("[(", ")]"),
    StepKind.END: ("((", "))"),
}


def _nid(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s) or "n"


def _short(v: Any) -> str:
    if isinstance(v, dict):
        if "ref" in v:
            return f"{{!{v['ref']}}}"
        if "expression" in v:
            return f"= {v['expression']}"
    return str(v)


def _esc(s: str) -> str:
    return s.replace('"', "'").replace("|", "/")


def _step_label(s: Step) -> str:
    head = s.label or s.id
    body = ""
    if s.kind in {
        StepKind.LOOKUP,
        StepKind.CREATE,
        StepKind.UPDATE,
        StepKind.DELETE,
        StepKind.ESCALATE,
    }:
        body = s.object or ""
        if s.inputs:
            body += "<br/>" + "<br/>".join(
                f"{k.split('.')[-1]} = {_short(v)}" for k, v in list(s.inputs.items())[:4]
            )
    elif s.kind in {
        StepKind.CALL_CODE,
        StepKind.CALL_PROCESS,
        StepKind.CALL_ACTION,
        StepKind.NOTIFY,
    }:
        body = s.target or ""
    elif s.kind is StepKind.RAISE_ERROR:
        body = str(s.extras.get("message", ""))[:60]
    elif s.kind is StepKind.WAIT:
        body = " ".join(str(v) for v in s.extras.values() if v)
    elif s.kind is StepKind.APPROVAL_STEP:
        body = s.target or ""
    return _esc(f"{s.kind.value}: {head}" + (f"<br/>{body}" if body else ""))


def to_mermaid(p: ProcessDefinition) -> str:
    """Flowchart with the trigger as the entry node and decision branches labelled."""
    lines = ["flowchart TD"]
    trig = p.trigger
    tl = f"{trig.kind.value}"
    if trig.object:
        tl += f" {trig.object}"
    if trig.events:
        tl += f" ({', '.join(trig.events)})"
    if trig.timing:
        tl += f" {trig.timing}"
    if not trig.when.empty:
        conds = [
            c.expression or f"{c.left} {c.operator} {_short(c.right)}"
            for c in trig.when.conditions[:3]
        ]
        tl += "<br/>when " + f" {trig.when.logic} ".join(conds)
    lines.append(f'  T(["{_esc(tl)}"])')
    for s in p.steps:
        lo, hi = _SHAPE.get(s.kind, ("[", "]"))
        lines.append(f'  {_nid(s.id)}{lo}"{_step_label(s)}"{hi}')
    if p.entry:
        lines.append(f"  T --> {_nid(p.entry)}")
    elif p.steps:
        lines.append(f"  T --> {_nid(p.steps[0].id)}")
    for s in p.steps:
        for b in s.branches:
            if b.next:
                cond = (
                    " and ".join(
                        c.expression or f"{c.left} {c.operator} {_short(c.right)}"
                        for c in b.when.conditions[:2]
                    )
                    or b.name
                )
                lines.append(f'  {_nid(s.id)} -->|"{_esc(cond)[:60]}"| {_nid(b.next)}')
        if s.default_next:
            lines.append(f'  {_nid(s.id)} -->|"otherwise"| {_nid(s.default_next)}')
        if s.next:
            lines.append(f"  {_nid(s.id)} --> {_nid(s.next)}")
        if s.on_fault:
            lines.append(f'  {_nid(s.id)} -.->|"fault"| {_nid(s.on_fault)}')
    return "\n".join(lines)


def to_markdown(p: ProcessDefinition) -> str:
    trig = p.trigger
    out = [
        f"# {p.label or p.name}",
        "",
        f"**Kind:** {p.kind}  ",
        f"**Fidelity:** {p.fidelity.value}  ",
        f"**Active:** {'yes' if p.active else 'no'}  ",
        f"**Id:** `{p.id[:16]}`  ",
    ]
    if p.summary:
        out += ["", p.summary]
    if p.description:
        out += ["", p.description]
    out += [
        "",
        "## Trigger",
        "",
        f"- {trig.kind.value}"
        + (f" on **{trig.object}**" if trig.object else "")
        + (f" ({', '.join(trig.events)})" if trig.events else "")
        + (f", {trig.timing}" if trig.timing else ""),
    ]
    if trig.entry_points:
        out.append(f"- entry points: {', '.join(trig.entry_points)}")
    if not trig.when.empty:
        out.append(f"- when ({trig.when.logic}):")
        out += [
            f"  - {c.expression or f'{c.left} {c.operator} {_short(c.right)}'}"
            for c in trig.when.conditions
        ]
    if p.steps:
        out += ["", "## Steps", ""]
        for s in p.steps:
            line = f"- **{s.id}** `{s.kind.value}`"
            if s.object:
                line += f" {s.object}"
            if s.target:
                line += f" → {s.target}"
            if s.inputs:
                line += ": " + ", ".join(
                    f"{k} = {_short(v)}" for k, v in list(s.inputs.items())[:6]
                )
            out.append(line)
            for b in s.branches:
                cond = (
                    f" {b.when.logic} ".join(
                        c.expression or f"{c.left} {c.operator} {_short(c.right)}"
                        for c in b.when.conditions
                    )
                    or "(always)"
                )
                out.append(f"  - if {cond} → {b.next or 'end'}")
            if s.default_next:
                out.append(f"  - otherwise → {s.default_next}")
    if p.fields_read or p.fields_written:
        out += ["", "## Data", ""]
        if p.fields_read:
            out.append(f"- reads: {', '.join(p.fields_read)}")
        if p.fields_written:
            out.append(f"- writes: {', '.join(p.fields_written)}")
    if p.calls:
        out += ["", "## Calls", "", *[f"- {c}" for c in p.calls]]
    if p.sources:
        out += [
            "",
            "## Seen in",
            "",
            *[
                f"- {s.org_alias}: {s.category} `{s.api_name}`"
                + (f" (scan {s.scan_id})" if s.scan_id else "")
                for s in p.sources
            ],
        ]
    out += ["", "```mermaid", to_mermaid(p), "```"]
    return "\n".join(out) + "\n"
