"""Salesforce formula parser + emitter — round-trip via Python eval."""

from __future__ import annotations

from typing import Any

import pytest

from offramp.generate.formula.emitter import emit
from offramp.generate.formula.parser import UnsupportedFormulaError, parse
from offramp.runtime.rules import formula_runtime


def _eval(formula: str, record: dict[str, Any] | None = None) -> Any:
    expr = emit(parse(formula))
    ns: dict[str, Any] = {
        name: getattr(formula_runtime, name)
        for name in dir(formula_runtime)
        if name.startswith("_") and not name.startswith("__")
    }
    ns["record"] = record or {}
    ns["context"] = {}
    return eval(expr, ns)


def test_simple_arithmetic() -> None:
    assert _eval("2 + 3 * 4") == 14


def test_parens_change_precedence() -> None:
    assert _eval("(2 + 3) * 4") == 20


def test_field_reference_returns_value() -> None:
    assert _eval("Industry", {"Industry": "Tech"}) == "Tech"


def test_dotted_field_walks_dict() -> None:
    record = {"Account": {"Owner": {"Email": "ceo@acme.com"}}}
    assert _eval("Account.Owner.Email", record) == "ceo@acme.com"


def test_isblank_true_for_missing_field() -> None:
    assert _eval("ISBLANK(Industry)", {}) is True


def test_isblank_false_for_present_value() -> None:
    assert _eval("ISBLANK(Industry)", {"Industry": "Tech"}) is False


def test_if_function() -> None:
    assert _eval("IF(Amount > 100, 'big', 'small')", {"Amount": 200}) == "big"
    assert _eval("IF(Amount > 100, 'big', 'small')", {"Amount": 50}) == "small"


def test_and_or_chains() -> None:
    record = {"A": True, "B": False, "C": True}
    assert _eval("AND(A, OR(B, C))", record) is True


def test_not_function_and_unary() -> None:
    assert _eval("NOT(TRUE)") is False
    assert _eval("!FALSE") is True


def test_string_functions() -> None:
    assert _eval("LEFT(Name, 3)", {"Name": "Salesforce"}) == "Sal"
    assert _eval("RIGHT(Name, 5)", {"Name": "Salesforce"}) == "force"
    # MID(text, start, length): 1-indexed; "Salesforce"[6..9] == "forc"
    assert _eval("MID(Name, 6, 4)", {"Name": "Salesforce"}) == "forc"
    assert _eval("CONTAINS(Name, 'force')", {"Name": "Salesforce"}) is True


def test_comparison_operators() -> None:
    assert _eval("Amount = 100", {"Amount": 100}) is True
    assert _eval("Amount <> 100", {"Amount": 50}) is True
    assert _eval("Amount <= 100", {"Amount": 100}) is True
    assert _eval("Amount > 0", {"Amount": 1}) is True


def test_unsupported_function_raises() -> None:
    with pytest.raises(UnsupportedFormulaError):
        parse("REGEX(Name, 'foo')")


def test_trailing_input_raises() -> None:
    with pytest.raises(UnsupportedFormulaError):
        parse("1 + 2 garbage")


def test_unknown_token_raises() -> None:
    with pytest.raises(UnsupportedFormulaError):
        parse("@@invalid")


def test_case_function() -> None:
    formula = "CASE(Stage, 'New', 1, 'Working', 2, 0)"
    assert _eval(formula, {"Stage": "Working"}) == 2
    assert _eval(formula, {"Stage": "Closed"}) == 0


def test_blankvalue_falls_back_when_blank() -> None:
    assert _eval("BLANKVALUE(Industry, 'Unknown')", {}) == "Unknown"
    assert _eval("BLANKVALUE(Industry, 'Unknown')", {"Industry": "Tech"}) == "Tech"
