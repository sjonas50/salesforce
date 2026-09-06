"""Field / global reference extraction from Salesforce formulas.

Used by every extractor that carries a formula (validation rules, formula
fields, workflow criteria, field updates, Flow formulas) to derive
dependency edges. Parses with the deterministic parser when it can and falls
back to a tolerant token scan when the formula uses syntax the parser does
not support yet, so a parse gap never costs an edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from offramp.generate.formula.parser import (
    BinaryOp,
    FieldRef,
    FuncCall,
    Ident,
    Node,
    UnaryOp,
    UnsupportedFormulaError,
    parse,
)

_IDENT_RE = re.compile(r"\$?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_FUNC_RE = re.compile(r"\b([A-Z_]+)\s*\(")

_LITERALS = frozenset({"TRUE", "FALSE", "NULL"})


@dataclass
class FormulaReferences:
    """What a formula reads."""

    fields: list[str] = field(default_factory=list)  # 'Industry', 'Owner.Email', 'Account.Name'
    globals: list[str] = field(
        default_factory=list
    )  # '$User.Id', '$Profile.Name', '$Setup.X__c.Y__c'
    functions: list[str] = field(default_factory=list)  # 'ISBLANK', 'PRIORVALUE'
    parsed: bool = True
    error: str | None = None

    def qualified_fields(self, object_name: str) -> list[str]:
        """Prefix bare field refs with the host object: 'Industry' → 'Account.Industry'.

        Relationship paths are left as-is with the host object prefixed
        ('Account.Owner.Email') because resolution to the target object needs
        the schema; the dependency builder does that.
        """
        return [f"{object_name}.{f}" if object_name else f for f in self.fields]


def extract_references(
    formula: str, *, known_functions: frozenset[str] | None = None
) -> FormulaReferences:
    """Return field, global, and function references for one formula."""
    refs = FormulaReferences()
    if not formula or not formula.strip():
        return refs
    try:
        tree = parse(formula)
    except (UnsupportedFormulaError, ValueError, RecursionError) as exc:
        refs.parsed = False
        refs.error = str(exc)
        _scan_tolerant(formula, refs)
        return refs
    _walk(tree, refs)
    _dedupe(refs)
    return refs


def _walk(node: Node, refs: FormulaReferences) -> None:
    match node:
        case Ident(name):
            _add_ident(name, refs)
        case FieldRef(parts):
            _add_ident(".".join(parts), refs)
        case UnaryOp(_, operand):
            _walk(operand, refs)
        case BinaryOp(_, left, right):
            _walk(left, refs)
            _walk(right, refs)
        case FuncCall(name, args):
            refs.functions.append(name)
            for a in args:
                _walk(a, refs)
        case _:
            return


def _add_ident(name: str, refs: FormulaReferences) -> None:
    if name.upper() in _LITERALS:
        return
    if name.startswith("$"):
        refs.globals.append(name)
    else:
        refs.fields.append(name)


def _scan_tolerant(formula: str, refs: FormulaReferences) -> None:
    """Regex fallback: strip strings, take dotted identifiers, drop function names."""
    stripped = _STRING_RE.sub(" ", formula)
    funcs = set(_FUNC_RE.findall(stripped))
    refs.functions.extend(sorted(funcs))
    # Remove function-call heads so "ISBLANK(" doesn't become a field.
    no_funcs = _FUNC_RE.sub("(", stripped)
    for m in _IDENT_RE.finditer(no_funcs):
        tok = m.group()
        if tok.upper() in _LITERALS or tok.upper() in funcs:
            continue
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            continue
        _add_ident(tok, refs)
    _dedupe(refs)


def _dedupe(refs: FormulaReferences) -> None:
    refs.fields = sorted(set(refs.fields))
    refs.globals = sorted(set(refs.globals))
    refs.functions = sorted(set(refs.functions))
