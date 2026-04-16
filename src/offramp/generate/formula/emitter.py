"""SF formula AST → Python expression emitter.

The Tier 1 rule body is "the Python expression equivalent to this formula"
plus the surrounding ``def evaluate(record, context)`` shell. Each formula
operates on a flat ``record`` dict — dotted refs are resolved by walking
the dict.

The runtime helpers (``_isblank``, ``_ispickval`` etc.) live in
:mod:`offramp.runtime.rules.formula_runtime` so generated code stays
small and the helpers can be unit-tested in isolation.
"""

from __future__ import annotations

from offramp.generate.formula.parser import (
    BinaryOp,
    BoolLit,
    FieldRef,
    FuncCall,
    Ident,
    Node,
    NullLit,
    NumberLit,
    StringLit,
    UnaryOp,
    UnsupportedFormulaError,
)

# Functions that map directly to a Python built-in / helper.
_DIRECT_FN = {
    "ABS": "abs",
    "MAX": "max",
    "MIN": "min",
    "ROUND": "_round",
    "FLOOR": "_floor",
    "CEILING": "_ceil",
    "MOD": "_mod",
    "LEN": "len",
    "UPPER": "_upper",
    "LOWER": "_lower",
    "TRIM": "_trim",
    "LEFT": "_left",
    "RIGHT": "_right",
    "MID": "_mid",
    "SUBSTITUTE": "_substitute",
    "FIND": "_find",
    "BEGINS": "_begins",
    "CONTAINS": "_contains",
    "TEXT": "_text",
    "VALUE": "_value",
    "TODAY": "_today",
    "NOW": "_now",
    "DATE": "_date",
    "ADDMONTHS": "_addmonths",
    "BLANKVALUE": "_blankvalue",
    "NULLVALUE": "_blankvalue",
}

_BIN_OPS = {
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
    "=": "==",
    "<>": "!=",
    "!=": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "AND": "and",
    "OR": "or",
}


def emit(node: Node) -> str:
    """Render an AST node as a Python expression string."""
    if isinstance(node, NumberLit):
        # Preserve int vs float — SF Number can be either; emit the narrowest.
        return str(int(node.value)) if node.value.is_integer() else repr(node.value)
    if isinstance(node, StringLit):
        return repr(node.value)
    if isinstance(node, BoolLit):
        return "True" if node.value else "False"
    if isinstance(node, NullLit):
        return "None"
    if isinstance(node, Ident):
        return f"_field(record, {node.name!r})"
    if isinstance(node, FieldRef):
        path = ".".join(node.parts)
        return f"_field(record, {path!r})"
    if isinstance(node, UnaryOp):
        operand = emit(node.operand)
        if node.op == "-":
            return f"(-({operand}))"
        if node.op == "NOT":
            return f"(not ({operand}))"
        raise UnsupportedFormulaError(f"unsupported unary op {node.op}")
    if isinstance(node, BinaryOp):
        py_op = _BIN_OPS.get(node.op)
        if py_op is None:
            raise UnsupportedFormulaError(f"unsupported binary op {node.op}")
        return f"({emit(node.left)} {py_op} {emit(node.right)})"
    if isinstance(node, FuncCall):
        return _emit_call(node)
    raise UnsupportedFormulaError(f"unhandled AST node: {type(node).__name__}")


def _emit_call(node: FuncCall) -> str:
    name = node.name
    args = [emit(a) for a in node.args]

    # Special-cased functions whose Python form is non-trivial.
    if name == "ISBLANK" or name == "ISNULL":
        if len(args) != 1:
            raise UnsupportedFormulaError(f"{name}() takes 1 arg, got {len(args)}")
        return f"_isblank({args[0]})"
    if name == "NOT":
        if len(args) != 1:
            raise UnsupportedFormulaError(f"NOT() takes 1 arg, got {len(args)}")
        return f"(not ({args[0]}))"
    if name == "AND":
        if not args:
            raise UnsupportedFormulaError("AND() requires at least 1 arg")
        return "(" + " and ".join(args) + ")"
    if name == "OR":
        if not args:
            raise UnsupportedFormulaError("OR() requires at least 1 arg")
        return "(" + " or ".join(args) + ")"
    if name == "IF":
        if len(args) != 3:
            raise UnsupportedFormulaError(f"IF() takes 3 args, got {len(args)}")
        return f"(({args[1]}) if ({args[0]}) else ({args[2]}))"
    if name == "ISPICKVAL":
        if len(args) != 2:
            raise UnsupportedFormulaError(f"ISPICKVAL() takes 2 args, got {len(args)}")
        return f"_ispickval({args[0]}, {args[1]})"
    if name == "CASE":
        # CASE(expression, val1, result1, val2, result2, ..., else_result)
        if len(args) < 4 or (len(args) - 2) % 2 != 0:
            raise UnsupportedFormulaError("CASE() arity must be expression + N pairs + else")
        expr = args[0]
        else_ = args[-1]
        pairs = [(args[i], args[i + 1]) for i in range(1, len(args) - 1, 2)]
        py = else_
        for val, result in reversed(pairs):
            py = f"(({result}) if ({expr}) == ({val}) else ({py}))"
        return py

    direct = _DIRECT_FN.get(name)
    if direct:
        return f"{direct}({', '.join(args)})"
    raise UnsupportedFormulaError(f"unsupported function {name}()")


def emit_rule_body(formula: str, *, function_name: str) -> str:
    """Render a complete rule MODULE (imports + function) for a formula string.

    Generated modules are self-contained — they import the formula-runtime
    helpers at the top so they can be loaded individually without namespace
    injection from the rules engine.
    """
    from offramp.generate.formula.parser import parse

    tree = parse(formula)
    expr = emit(tree)
    return (
        '"""Auto-generated formula rule. Do not edit by hand."""\n'
        "from __future__ import annotations\n\n"
        "from offramp.runtime.rules.formula_runtime import (\n"
        "    _addmonths, _begins, _blankvalue, _ceil, _contains, _date,\n"
        "    _field, _find, _floor, _ispickval, _isblank, _left, _lower,\n"
        "    _mid, _mod, _now, _right, _round, _substitute, _text, _today,\n"
        "    _trim, _upper, _value,\n"
        ")\n\n"
        f"def {function_name}(record, context):\n"
        f'    """Auto-generated from a Salesforce formula."""\n'
        f"    return {expr}\n"
    )
