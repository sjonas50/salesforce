"""Tier 1 translator: Component → generated Python rule module.

Currently handles:
* :class:`CategoryName.VALIDATION_RULE` — formula; error-condition guard
* :class:`CategoryName.FORMULA_FIELD` — formula; populates a field
* :class:`CategoryName.WORKFLOW_RULE` — criteria + field-update actions
  (one Python function per ``<rules>`` block inside the .workflow XML)
* :class:`CategoryName.ASSIGNMENT_RULE` — Lead/Case OwnerId routing
  (first-match-wins across ``<ruleEntries>`` within a rule group)
* :class:`CategoryName.AUTOLAUNCHED_FLOW` — simple after-save flows whose
  DML is captured as field-update assignments (no callouts, no subflows)
* :class:`CategoryName.PROCESS_BUILDER` — stored as a Flow with
  processType=Workflow; same shape as autolaunched_flow

Everything else is surfaced as a clean ``NotImplementedError`` / ``ValueError``
that the orchestrator records as a "skipped" finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from offramp.core.models import CategoryName, Component
from offramp.extract.ooe_audit.audit import OoEStep
from offramp.generate.formula.emitter import emit_rule_body
from offramp.generate.formula.parser import UnsupportedFormulaError


@dataclass(frozen=True)
class GeneratedRule:
    """One rule's emitted code + metadata for the engine registration."""

    rule_id: str
    sobject: str
    ooe_step: int
    kind: str  # 'validation' | 'computation'
    function_name: str
    code: str
    error_message_template: str | None = None
    error_display_field: str | None = None
    fixes_field: str | None = None


def _safe_id(s: str) -> str:
    """Sanitize an arbitrary SF developer name into a Python identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"r_{cleaned}"
    return cleaned


# Shared preamble: generated modules that may use formula helpers import them
# eagerly. Emit-once; cheap. Makes every generated module self-contained.
_FORMULA_RUNTIME_IMPORT = (
    "from offramp.runtime.rules.formula_runtime import (\n"
    "    _addmonths, _begins, _blankvalue, _ceil, _contains, _date,\n"
    "    _field, _find, _floor, _ispickval, _isblank, _left, _lower,\n"
    "    _mid, _mod, _now, _right, _round, _substitute, _text, _today,\n"
    "    _trim, _upper, _value,\n"
    ")"
)


def translate(component: Component) -> GeneratedRule:
    """Dispatch by category and emit a GeneratedRule.

    For categories whose SF file contains MULTIPLE rules (workflow files,
    assignment files), this function emits ONE :class:`GeneratedRule`
    combining them. The generated function iterates the rule list
    internally — matches Salesforce's "walk the ruleset" semantics.
    """
    if component.category is CategoryName.VALIDATION_RULE:
        return _translate_validation_rule(component)
    if component.category is CategoryName.FORMULA_FIELD:
        return _translate_formula_field(component)
    if component.category is CategoryName.WORKFLOW_RULE:
        return _translate_workflow_rule(component)
    if component.category is CategoryName.ASSIGNMENT_RULE:
        return _translate_assignment_rule(component)
    if component.category in {
        CategoryName.AUTOLAUNCHED_FLOW,
        CategoryName.PROCESS_BUILDER,
        CategoryName.RECORD_TRIGGERED_FLOW,
    }:
        return _translate_simple_flow(component)
    raise NotImplementedError(f"Tier 1 translator does not yet handle {component.category.value}")


def _translate_validation_rule(component: Component) -> GeneratedRule:
    raw: dict[str, Any] = component.raw if isinstance(component.raw, dict) else {}
    formula = raw.get("error_condition_formula", "")
    if not formula:
        raise ValueError(f"validation rule {component.name} has no error_condition_formula")
    sobject = str(raw.get("object", "")) or "Unknown"
    function_name = f"vr_{_safe_id(component.api_name or component.name)}"
    code = emit_rule_body(formula, function_name=function_name)
    return GeneratedRule(
        rule_id=f"{sobject}.{component.name}",
        sobject=sobject,
        ooe_step=6,  # CUSTOM_VALIDATION
        kind="validation",
        function_name=function_name,
        code=code,
        error_message_template=str(raw.get("error_message", "Validation failed")),
        error_display_field=raw.get("error_display_field"),
    )


def _translate_formula_field(component: Component) -> GeneratedRule:
    raw: dict[str, Any] = component.raw if isinstance(component.raw, dict) else {}
    formula = raw.get("formula", "")
    if not formula:
        raise ValueError(f"formula field {component.name} has no formula")
    sobject = str(raw.get("object", "")) or "Unknown"
    field_name = str(raw.get("field_name", component.name))
    function_name = f"ff_{_safe_id(component.api_name or component.name)}"
    code = emit_rule_body(formula, function_name=function_name)
    return GeneratedRule(
        rule_id=f"{sobject}.{field_name}",
        sobject=sobject,
        ooe_step=6,  # field formulas don't have a save-path step; co-locate
        # with validation so the runtime computes them before validation reads
        # the value. Real formula fields evaluate lazily on read; we reify for
        # the externalized runtime so all downstream references see a value.
        kind="computation",
        function_name=function_name,
        code=code,
        fixes_field=field_name,
    )


def _translate_workflow_rule(component: Component) -> GeneratedRule:
    """Workflow Rule file -> one computation function running all rules.

    The generated function iterates the active rules, evaluates each
    criteria formula against the record, and returns the merged field
    mutations. Fires at OoE step 12 (WORKFLOW_RULES).
    """
    raw: dict[str, Any] = component.raw if isinstance(component.raw, dict) else {}
    sobject = str(raw.get("object", "")) or "Unknown"
    rules = [r for r in raw.get("rules", []) if r.get("active")]
    # field_updates is a name-keyed map shared across all rules.
    fu_by_name = {fu["name"]: fu for fu in raw.get("field_updates", [])}
    function_name = f"wf_{_safe_id(component.api_name or component.name)}"

    # Build the Python body: for each active rule, evaluate criteria;
    # if matched, apply the rule's immediate field-update actions.
    lines: list[str] = [
        '"""Auto-generated workflow rule batch."""',
        "from __future__ import annotations",
        "",
        _FORMULA_RUNTIME_IMPORT,
        "",
        f"def {function_name}(record, context):",
        f'    """Generated from {component.name} ({len(rules)} active rules)."""',
        "    mutations = {}",
    ]
    if not rules:
        lines.append("    return None")
    else:
        for rule in rules:
            rule_name = _safe_id(rule.get("name", ""))
            criteria_items = rule.get("criteria_items", [])
            formula = rule.get("formula")
            # Build the guard expression.
            if formula:
                # Formulas are validated at generate time; fail loudly if unsupported.
                try:
                    from offramp.generate.formula.emitter import emit as _emit
                    from offramp.generate.formula.parser import parse

                    guard_expr = _emit(parse(formula))
                except UnsupportedFormulaError as exc:
                    raise ValueError(
                        f"workflow rule {rule_name} has unsupported formula: {exc}"
                    ) from exc
            else:
                guard_expr = _criteria_to_py(criteria_items) or "True"
            # Apply immediate actions (field updates).
            action_lines: list[str] = []
            for act in rule.get("immediate_actions", []):
                if act.get("type") != "FieldUpdate":
                    # Email/Task/OutboundMessage aren't Tier 1; record as pending.
                    action_lines.append(
                        f"        # TODO: {act['type']} action '{act['name']}' "
                        f"is not Tier 1 — route to Tier 2 translator"
                    )
                    continue
                fu = fu_by_name.get(act["name"])
                if fu is None:
                    action_lines.append(
                        f"        # ERROR: field update '{act['name']}' not defined"
                    )
                    continue
                action_lines.append(_field_update_assignment_py(fu))
            if not action_lines:
                action_lines.append("        pass")
            lines.append(f"    # Rule: {rule_name}")
            lines.append(f"    if {guard_expr}:")
            lines.extend(action_lines)
        lines.append("    return mutations if mutations else None")

    lines.append("")
    code = "\n".join(lines)
    return GeneratedRule(
        rule_id=f"{sobject}.workflow",
        sobject=sobject,
        ooe_step=int(OoEStep.WORKFLOW_RULES),
        kind="computation",
        function_name=function_name,
        code=code,
        # The runtime merges returned dict into the record via field_mutations
        # path — fixes_field stays None since this rule can touch many fields.
    )


def _translate_assignment_rule(component: Component) -> GeneratedRule:
    """Assignment Rule group -> OwnerId-setting computation rule.

    Fires at OoE step 10 (ASSIGNMENT_RULES). Walks ``<ruleEntries>`` in
    order and returns the first matching OwnerId — matches SF's "first
    entry wins" semantics.
    """
    raw: dict[str, Any] = component.raw if isinstance(component.raw, dict) else {}
    sobject = str(raw.get("object", "")) or "Unknown"
    groups = raw.get("rule_groups", [])
    active_entries: list[dict[str, Any]] = []
    for g in groups:
        if not g.get("active"):
            continue
        for e in g.get("entries", []):
            active_entries.append(e)

    function_name = f"ar_{_safe_id(component.api_name or component.name)}"
    lines = [
        '"""Auto-generated assignment rule batch."""',
        "from __future__ import annotations",
        "",
        _FORMULA_RUNTIME_IMPORT,
        "",
        f"def {function_name}(record, context):",
        f'    """Generated from {component.name} ({len(active_entries)} active entries)."""',
    ]
    if not active_entries:
        lines.append("    return None")
    else:
        for i, entry in enumerate(active_entries):
            criteria_items = entry.get("criteria_items", [])
            formula = entry.get("formula")
            if formula:
                try:
                    from offramp.generate.formula.emitter import emit as _emit
                    from offramp.generate.formula.parser import parse

                    guard_expr = _emit(parse(formula))
                except UnsupportedFormulaError as exc:
                    raise ValueError(
                        f"assignment entry {i} has unsupported formula: {exc}"
                    ) from exc
            else:
                guard_expr = _criteria_to_py(criteria_items) or "True"
            assigned_to = entry.get("assigned_to", "")
            # Real resolution: resolve assigned_to (user alias / queue devname)
            # to a record ID at runtime via the MCP gateway. For generation
            # we emit the alias as a string; the runtime resolver does lookup.
            lines.append(f"    # Entry {i}: assigned_to={assigned_to!r}")
            lines.append(f"    if {guard_expr}:")
            lines.append(f"        return {{'OwnerId': {assigned_to!r}}}")
        lines.append("    return None")
    lines.append("")

    return GeneratedRule(
        rule_id=f"{sobject}.assignment",
        sobject=sobject,
        ooe_step=int(OoEStep.ASSIGNMENT_RULES),
        kind="computation",
        function_name=function_name,
        code="\n".join(lines),
    )


def _translate_simple_flow(component: Component) -> GeneratedRule:
    """Simple autolaunched / process-builder / record-triggered flow.

    'Simple' = no callouts, no subflows, no screens. The emitter walks the
    flow's ``record_updates`` and emits a rule that applies the literal
    assignments when the triggering condition matches. Complex flows fall
    through to the Tier 2 translator (not handled here).
    """
    raw: dict[str, Any] = component.raw if isinstance(component.raw, dict) else {}

    # Reject complex flows — caller should route to Tier 2.
    if raw.get("action_calls"):
        raise ValueError(f"flow {component.name} has action_calls (callouts) — route to Tier 2")
    if raw.get("subflows"):
        raise ValueError(f"flow {component.name} has subflows — route to Tier 2")
    if raw.get("screens"):
        raise ValueError(f"flow {component.name} has screens — route to Tier 3")

    sobject = str(raw.get("object", "")) or "Unknown"
    if not sobject or sobject == "Unknown":
        # For autolaunched flows sObject isn't in the XML; fall back to a
        # generic tag so the OoE runtime can still dispatch.
        sobject = "Flow"
    trigger_type = raw.get("trigger_type", "")

    record_updates = raw.get("record_updates", [])
    record_creates = raw.get("record_creates", [])
    decisions = raw.get("decisions", [])

    function_name = f"fl_{_safe_id(component.api_name or component.name)}"
    lines = [
        '"""Auto-generated simple flow rule."""',
        "from __future__ import annotations",
        "",
        _FORMULA_RUNTIME_IMPORT,
        "",
        f"def {function_name}(record, context):",
        f'    """Generated from {component.name} (trigger={trigger_type!r})."""',
        "    mutations = {}",
    ]
    if not record_updates and not record_creates:
        # Flow has only decisions (no DML) — emit a no-op with diagnostic.
        lines.append(f"    # Flow contains {len(decisions)} decisions but no DML.")
        lines.append("    return None")
    else:
        # Record_updates: translate each block's input_assignments into the
        # merged mutations dict.
        for ru in record_updates:
            for assign in ru.get("input_assignments", []):
                lines.append(_flow_assignment_py(assign))
        # Record_creates: also surface as a diagnostic; real create-flows
        # need Tier 2 (they imply a side effect, not a mutation).
        for rc in record_creates:
            lines.append(
                f"    # TODO: record create for {rc.get('object', '?')} — "
                "route via MCP gateway at deployment (Tier 2 candidate)"
            )
        lines.append("    return mutations if mutations else None")
    lines.append("")

    # Choose OoE step based on trigger type.
    ooe_step = OoEStep.PROCESSES_AND_FLOWS  # step 13
    if trigger_type in {"RecordBeforeSave"}:
        ooe_step = OoEStep.BEFORE_TRIGGERS  # step 5 — before-save flow
    return GeneratedRule(
        rule_id=f"{sobject}.{component.name}",
        sobject=sobject,
        ooe_step=int(ooe_step),
        kind="computation",
        function_name=function_name,
        code="\n".join(lines),
    )


# -- Shared helpers ----------------------------------------------------------


_OP_TO_PY = {
    "equals": "==",
    "notEqual": "!=",
    "lessThan": "<",
    "greaterThan": ">",
    "lessOrEqual": "<=",
    "greaterOrEqual": ">=",
    "contains": "in",  # flipped operands
    "startsWith": "startswith",
    "includes": "in",
}


def _criteria_to_py(items: list[dict[str, Any]]) -> str:
    """Turn a ``<criteriaItems>`` list into a conjunctive Python boolean expr.

    Each item is ANDed — matches SF's default filter logic. For OR /
    complex logic, SF uses the ``<formula>`` field instead which we parse
    via the real formula parser.
    """
    if not items:
        return ""
    parts: list[str] = []
    for item in items:
        field = item.get("field", "")
        if not field:
            continue
        # Strip sObject prefix if present (Account.AnnualRevenue -> AnnualRevenue
        # since our runtime receives the record directly by sObject).
        if "." in field:
            field = field.split(".", 1)[1]
        op = item.get("operation", "equals")
        value = item.get("value", "")
        py_op = _OP_TO_PY.get(op, "==")
        if py_op == "startswith":
            parts.append(f"str(record.get({field!r}, '')).startswith({value!r})")
        elif op in ("contains", "includes"):
            parts.append(f"{value!r} in (record.get({field!r}) or '')")
        else:
            # Try numeric interpretation, else fall back to string literal.
            if _looks_numeric(value):
                parts.append(f"record.get({field!r}) {py_op} {value}")
            else:
                parts.append(f"record.get({field!r}) {py_op} {value!r}")
    return " and ".join(parts)


def _field_update_assignment_py(fu: dict[str, Any]) -> str:
    """Emit a ``mutations['Field'] = ...`` line for a workflow field update."""
    target_field = fu.get("field", "")
    # Strip SF sObject prefix.
    if "." in target_field:
        target_field = target_field.split(".", 1)[1]
    if fu.get("formula"):
        try:
            from offramp.generate.formula.emitter import emit as _emit
            from offramp.generate.formula.parser import parse

            expr = _emit(parse(fu["formula"]))
        except UnsupportedFormulaError:
            return f"        # TODO: unsupported formula in field update '{fu.get('name', '?')}'"
        return f"        mutations[{target_field!r}] = {expr}"
    literal = fu.get("literal_value", "")
    if _looks_numeric(literal):
        return f"        mutations[{target_field!r}] = {literal}"
    return f"        mutations[{target_field!r}] = {literal!r}"


def _flow_assignment_py(assign: dict[str, Any]) -> str:
    """Emit a ``mutations[...] = ...`` line for a Flow input_assignment."""
    target_field = assign.get("field", "")
    if "." in target_field:
        target_field = target_field.split(".", 1)[1]
    value = assign.get("value")
    if value is None:
        return f"    mutations[{target_field!r}] = None"
    if isinstance(value, (int, float, bool)):
        return f"    mutations[{target_field!r}] = {value!r}"
    return f"    mutations[{target_field!r}] = {str(value)!r}"


def _looks_numeric(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str) and v:
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False


def is_supported(component: Component) -> bool:
    """Cheap predicate so the orchestrator can skip unsupported components."""
    if component.category not in {CategoryName.VALIDATION_RULE, CategoryName.FORMULA_FIELD}:
        return False
    raw = component.raw if isinstance(component.raw, dict) else {}
    formula = (
        raw.get("error_condition_formula")
        if component.category is CategoryName.VALIDATION_RULE
        else raw.get("formula")
    )
    if not formula:
        return False
    try:
        from offramp.generate.formula.parser import parse

        parse(str(formula))
    except UnsupportedFormulaError:
        return False
    return True
