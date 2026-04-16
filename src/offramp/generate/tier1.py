"""Tier 1 translator: Component → generated Python rule module.

Currently handles:
* :class:`CategoryName.VALIDATION_RULE` — formula-based; emits a rule whose
  return value is the error condition (True = failed validation)
* :class:`CategoryName.FORMULA_FIELD` — emits a computation rule that
  populates the named field

Other categories (Workflow Rule, Assignment Rule, before-save Flow, ...)
follow the same pattern; they're staged as TODOs that the unit tests will
catch when the matrix routes them here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from offramp.core.models import CategoryName, Component
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


def translate(component: Component) -> GeneratedRule:
    """Dispatch by category and emit a GeneratedRule."""
    if component.category is CategoryName.VALIDATION_RULE:
        return _translate_validation_rule(component)
    if component.category is CategoryName.FORMULA_FIELD:
        return _translate_formula_field(component)
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
